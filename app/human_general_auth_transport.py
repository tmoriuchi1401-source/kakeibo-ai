"""Offline-tested OIDC confirmation core. No HTTP server or live entrypoint.

The host must provide protected CAS state, private browser sessions, HTTPS,
secret configuration and fresh Drive readers. Never wire a Sheet email into
this adapter. No writer, Gemini, Medical, mover or Actions dispatch dependency.
"""
from dataclasses import dataclass, field
from hashlib import sha256
import base64
import hmac
import json
import re
import secrets
import time
from threading import RLock
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from google.auth.transport.requests import Request
from google.oauth2 import id_token

from .drive_run_state import StateError
from .human_general_authority import binding_fields, eligible, HumanGeneralAuthorityStore
from .page_receipt_model import digest

ISSUER = 'https://accounts.google.com'
SCHEMA = 'human-general-auth-requests-v1'
ACTION = 'general_receipt'
AI_CONSENT_ACTION = 'general_receipt_and_gemini_permission'
TTL = 600
UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')


def fail(code):
    raise StateError('human_general_auth_' + code)


def fingerprint(value):
    return sha256(value.encode()).hexdigest()


def same(a, b):
    return isinstance(a, str) and isinstance(b, str) and hmac.compare_digest(a.encode(), b.encode())


def request_binding(page, rid, action=ACTION):
    if action not in {ACTION,AI_CONSENT_ACTION}:fail('request_invalid')
    return {**binding_fields(page), 'request_id': rid, 'requested_action': action,
            'page_processing_status': page.processing_status}


def empty_auth_state(binding):
    return {'schema': SCHEMA, 'binding': binding, 'requests': {}}


def validate_auth_state(value, binding, *, allowed_actions=None):
    allowed_actions = {ACTION,AI_CONSENT_ACTION} if allowed_actions is None else allowed_actions
    try:
        if (set(value) != {'schema', 'binding', 'requests'} or value['schema'] != SCHEMA
                or value['binding'] != binding or not isinstance(value['requests'], dict)
                or len(value['requests']) > 1000):
            raise ValueError()
        for rid, record in value['requests'].items():
            if (not UUID.fullmatch(rid) or record['request_id'] != rid
                    or record['status'] not in {'prepared', 'authenticating', 'verifying',
                                               'authenticated', 'claimed', 'complete'}
                    or record['digest'] != digest(record['binding'])
                    or record['binding']['request_id'] != rid
                    or record['binding']['requested_action'] not in allowed_actions
                    or type(record['created_at']) is not int or type(record['expires_at']) is not int
                    or record['expires_at'] - record['created_at'] != TTL):
                raise ValueError()
            fields = {'request_id', 'binding', 'digest', 'created_at', 'expires_at', 'status'}
            if record['status'] != 'prepared':
                fields.add('session')
            if record['status'] in {'authenticated', 'claimed', 'complete'}:
                fields.update({'actor', 'evidence_digest'})
            if record['status'] == 'complete':
                fields.add('authority_digest')
            if set(record) != fields:
                raise ValueError()
            if record['status'] != 'prepared':
                if set(record['session']) != {'cookie', 'state', 'nonce', 'csrf'}:
                    raise ValueError()
                if any(not re.fullmatch('[a-f0-9]{64}', v) for v in record['session'].values()):
                    raise ValueError()
            if record['status'] in {'authenticated', 'claimed', 'complete'}:
                actor = record['actor']
                if (set(actor) != {'issuer', 'subject', 'email', 'method', 'verified_at', 'request_id',
                                  'request_digest', 'policy_revision', 'verification_revision', 'actor_id'}
                        or actor['issuer'] != ISSUER or actor['method'] != 'google_oidc_code_pkce_v1'
                        or not re.fullmatch(r'[0-9]{1,255}', actor['subject'])
                        or actor['actor_id'] != digest([ISSUER, actor['subject']])
                        or type(actor['verified_at']) is not int
                        or not record['created_at'] <= actor['verified_at'] < record['expires_at']
                        or type(actor['policy_revision']) is not int or actor['policy_revision'] < 1
                        or actor['verification_revision'] != 1
                        or actor['request_digest'] != record['digest']
                        or actor['request_id'] != rid
                        or record['evidence_digest'] != digest(actor)):
                    raise ValueError()
            if record['status'] == 'complete' and not re.fullmatch('[a-f0-9]{64}', record['authority_digest']):
                raise ValueError()
    except Exception:
        fail('state_invalid')
    return value


class AuthRequestStore(HumanGeneralAuthorityStore):
    """Reuse strong ETag / If-Match / exact read-back; dedicated binding/file."""
    def __init__(self, transport, binding, *, preflight, validator=validate_auth_state):
        super().__init__(transport, binding, preflight=preflight, validator=validator)
        self.lock = RLock()

    def load(self):
        with self.lock:
            value = super().load()
            result = _VersionedSnapshot(value)
            result.before, result.tag = self.payload, self.tag
            return result

    def save(self, value):
        # Bind CAS to this read, not mutable store-global last-read metadata.
        if not isinstance(value, _VersionedSnapshot):
            fail('versioned_snapshot_required')
        with self.lock:
            self.payload, self.tag = value.before, value.tag
            super().save(value)


class _VersionedSnapshot(dict):
    pass


@dataclass(frozen=True, repr=False)
class VerifiedActor:
    issuer: str
    subject: str
    email: str
    method: str
    verified_at: int
    request_id: str
    request_digest: str
    policy_revision: int
    verification_revision: int = 1

    @property
    def actor_id(self):
        return digest([self.issuer, self.subject])

    def record(self):
        return {**self.__dict__, 'actor_id': self.actor_id}

    def legacy_actor(self):
        # Compatibility only: email originates from verified Google claims.
        # Stable issuer/sub stays in the protected request record, linked by UUID.
        return {'provider': 'google', 'subject': self.email}


class GoogleIdentityVerifier:
    def __init__(self, client_id, owner_subject, *, policy_revision=1, request=None, clock=time.time):
        if not client_id.endswith('.apps.googleusercontent.com') or not re.fullmatch(r'[0-9]{1,255}', owner_subject):
            fail('policy_invalid')
        if type(policy_revision) is not int or policy_revision < 1:
            fail('policy_invalid')
        self.client_id, self.owner_subject, self.policy_revision = client_id, owner_subject, policy_revision
        self.request, self.clock = request or Request(), clock

    def verify(self, token, nonce, record):
        try:
            if not isinstance(token, str) or len(token) > 16384 or token.count('.') != 2:
                raise ValueError()
            header = json.loads(base64.urlsafe_b64decode(token.split('.')[0] + '==='))
            if header.get('alg') != 'RS256' or any(k in header for k in ('jku', 'x5u', 'jwk')):
                raise ValueError()
            # google-auth validates signature with Google's certificates, aud,
            # exp/iat and issuer. Never decode-and-trust or use tokeninfo fallback.
            def certificates(url, **kwargs):
                if url != 'https://www.googleapis.com/oauth2/v1/certs':
                    raise ValueError()
                return self.request(url, timeout=15, allow_redirects=False, **kwargs)
            claims = id_token.verify_oauth2_token(token, certificates, self.client_id)
            now = int(self.clock())
            if (claims.get('iss') not in {ISSUER, 'accounts.google.com'}
                    or claims.get('aud') != self.client_id
                    or claims.get('azp', self.client_id) != self.client_id
                    or type(claims.get('iat')) is not int or type(claims.get('exp')) is not int
                    or not record['created_at'] - 30 <= claims['iat'] <= now
                    or not now < claims['exp'] <= claims['iat'] + 3600
                    or claims.get('email_verified') is not True
                    or not re.fullmatch(r'[^\s@]{1,100}@[^\s@]{1,150}', claims.get('email', ''))
                    or claims.get('sub') != self.owner_subject
                    or not same(claims.get('nonce'), nonce)):
                raise ValueError()
        except Exception:
            fail('identity_rejected')
        return VerifiedActor(ISSUER, claims['sub'], claims['email'], 'google_oidc_code_pkce_v1',
                             now, record['request_id'], record['digest'], self.policy_revision)


class GoogleCodeExchange:
    """Pinned Google endpoint; secrets/tokens only in memory, bounded no retry."""
    def __init__(self, client_id, client_secret, redirect_uri, session):
        self.client_id, self.client_secret = client_id, client_secret
        self.redirect_uri, self.session = redirect_uri, session

    def __call__(self, code, verifier):
        try:
            response = self.session.post('https://oauth2.googleapis.com/token', data={
                'grant_type': 'authorization_code', 'code': code,
                'client_id': self.client_id, 'client_secret': self.client_secret,
                'redirect_uri': self.redirect_uri, 'code_verifier': verifier}, timeout=15,
                allow_redirects=False)
            if response.status_code != 200:
                raise ValueError()
            token = response.json()['id_token']
            if not isinstance(token, str):
                raise ValueError()
            return token  # access/refresh token discarded; never returned/logged
        except Exception:
            fail('code_exchange_failed')


@dataclass(frozen=True, repr=False)
class BrowserSession:
    """Host-private session; never accept this object from client JSON/cells.

    Multi-instance hosting requires an encrypted TTL session store. This
    offline core intentionally does not provision one or start an HTTP server.
    """
    request_id: str
    cookie: str = field(repr=False)
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    csrf: str = field(repr=False)
    pkce_verifier: str = field(repr=False)
    authorization_url: str = field(repr=False)


class AuthenticatedGeneralConfirmation:
    def __init__(self, store, identity, current_page, load_source, *, redirect_uri,
                 exchange_code, clock=time.time, requested_action=ACTION, stage=None):
        parsed = urlsplit(redirect_uri)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
            fail('redirect_invalid')
        self.origin = parsed.scheme + '://' + parsed.netloc
        self.store, self.identity, self.current_page, self.load_source = store, identity, current_page, load_source
        self.redirect_uri, self.exchange_code, self.clock = redirect_uri, exchange_code, clock
        if requested_action not in {ACTION,AI_CONSENT_ACTION}:fail('request_invalid')
        self.requested_action=requested_action
        self.stage=stage or (lambda *_:None)

    def fresh(self, record):
        binding = record['binding']
        try:
            page = self.current_page(binding['source_file_id'], binding['page_number'])
            raw = self.load_source(page.source.source_file_id)
        except Exception:
            fail('freshness_unavailable')
        if request_binding(page, record['request_id'],self.requested_action) != binding or not eligible(page):
            fail('stale_or_sensitive_page')
        if type(raw) is not bytes or sha256(raw).hexdigest() != page.source.source_content_hash:
            fail('source_changed')
        return page

    def record(self, rid, status):
        if not isinstance(rid, str) or not UUID.fullmatch(rid):
            fail('request_invalid')
        state = self.store.load()
        record = state['requests'].get(rid)
        if not record or record['status'] != status:
            fail('replay_or_unknown_request')
        if not record['created_at'] <= int(self.clock()) < record['expires_at']:
            fail('request_expired')
        return state, record

    def prepare(self, expected, *, request_id=None):
        # Caller is trusted projection/Drive reader, not browser/Sheet JSON.
        rid = request_id if request_id is not None else str(uuid4())
        if not isinstance(rid, str) or not UUID.fullmatch(rid):
            fail('request_invalid')
        now = int(self.clock())
        record = {'request_id': rid, 'binding': request_binding(expected, rid,self.requested_action),
                  'created_at': now, 'expires_at': now + TTL, 'status': 'prepared'}
        record['digest'] = digest(record['binding'])
        self.fresh(record)
        state = self.store.load()
        if rid in state['requests']:
            fail('request_replaced')
        state['requests'][rid] = record
        self.store.save(state)
        return rid

    def begin(self, rid):
        state, record = self.record(rid, 'prepared')
        self.fresh(record)
        cookie, oauth_state, nonce, csrf, verifier = [secrets.token_urlsafe(32) for _ in range(5)]
        record.update(status='authenticating', session={
            'cookie': fingerprint(cookie), 'state': fingerprint(oauth_state),
            'nonce': fingerprint(nonce), 'csrf': fingerprint(csrf)})
        self.store.save(state)
        challenge = base64.urlsafe_b64encode(sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({
            'client_id': self.identity.client_id, 'redirect_uri': self.redirect_uri,
            'response_type': 'code', 'scope': 'openid email', 'state': oauth_state, 'nonce': nonce,
            'code_challenge': challenge, 'code_challenge_method': 'S256', 'prompt': 'select_account'})
        return BrowserSession(rid, cookie, oauth_state, nonce, csrf, verifier, url)

    def browser(self, record, ticket, cookie):
        if (ticket.request_id != record['request_id'] or not same(cookie, ticket.cookie)
                or not same(fingerprint(cookie), record['session']['cookie'])):
            fail('browser_binding_invalid')

    def callback(self, ticket, *, cookie, state, code):
        stored, record = self.record(ticket.request_id, 'authenticating')
        self.browser(record, ticket, cookie)
        if (not same(state, ticket.state) or not same(fingerprint(state), record['session']['state'])
                or not same(fingerprint(ticket.nonce), record['session']['nonce'])
                or not isinstance(code, str) or not 1 <= len(code) <= 4096):
            fail('oauth_state_invalid')
        self.fresh(record)
        record['status'] = 'verifying'
        self.store.save(stored)  # claim callback before exchange; no concurrent replay
        token = self.exchange_code(code, ticket.pkce_verifier)
        actor = self.identity.verify(token, ticket.nonce, record)
        self.fresh(record)
        stored, record = self.record(ticket.request_id, 'verifying')
        record.update(status='authenticated', actor=actor.record(), evidence_digest=digest(actor.record()))
        self.store.save(stored)
        return actor

    def verified_actor(self, rid):
        _, record = self.record(rid, 'claimed')
        self.fresh(record)
        actor = record['actor']
        if (actor['subject'] != self.identity.owner_subject or actor['policy_revision'] != self.identity.policy_revision
                or actor['verification_revision'] != 1 or actor['actor_id'] != digest([ISSUER, actor['subject']])
                or not record['created_at'] <= actor['verified_at'] < record['expires_at']):
            fail('actor_policy_changed')
        return VerifiedActor(**{k: v for k, v in actor.items() if k != 'actor_id'})

    def confirm(self, ticket, *, cookie, csrf, origin, method, confirmation_factory):
        self.stage('confirm_received')
        stored, record = self.record(ticket.request_id, 'authenticated')
        self.browser(record, ticket, cookie)
        if (method != 'POST' or not same(origin, self.origin) or not same(csrf, ticket.csrf)
                or not same(fingerprint(csrf), record['session']['csrf'])):
            fail('csrf_invalid')
        self.stage('session_verified')
        page = self.fresh(record)
        self.stage('request_freshness_verified')
        record['status'] = 'claimed'
        self.store.save(stored)  # consume request before any authority mutation
        self.stage('request_claimed')
        actor = self.verified_actor(ticket.request_id)
        self.stage('actor_verified')
        service = confirmation_factory(lambda rid: self.verified_actor(rid).legacy_actor())
        grant = service.confirm(page, operation='confirm_general_receipt_ai', request_id=ticket.request_id)
        audit = service.store.load()['audit']
        if not any(e['request_id'] == ticket.request_id and e['result'] == 'confirmed'
                   and e['confirmation_digest'] == grant['confirmation_digest'] for e in audit):
            fail('existing_authority_requires_reconciliation')
        stored, record = self.record(ticket.request_id, 'claimed')
        record.update(status='complete', authority_digest=grant['confirmation_digest'])
        self.store.save(stored)
        self.stage('request_complete')
        return {'request_id': ticket.request_id, 'actor_id': actor.actor_id,
                'authority_digest': grant['confirmation_digest'], 'status': 'complete'}

    def review_link(self, base_url, rid):
        if base_url != self.origin + '/confirm' or not UUID.fullmatch(rid):
            fail('link_invalid')
        return base_url + '?' + urlencode({'request': rid})
