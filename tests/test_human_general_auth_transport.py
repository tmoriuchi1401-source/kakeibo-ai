"""Real RSA signature verification with ephemeral synthetic keys; no network."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from google.auth import crypt, jwt

from app import human_general_auth_transport as auth
from app.human_general_authority import HumanGeneralConfirmation, HumanGeneralAuthorityStore
from app.drive_run_state import StateError
from test_page_receipts import MemoryTransport, BINDING, page

CLIENT = 'synthetic.apps.googleusercontent.com'
OWNER = '123456789012345678901'
REDIRECT = 'https://confirmation.example.test/oauth/callback'


@pytest.fixture(scope='module')
def keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    public = private.public_key().public_bytes(serialization.Encoding.PEM,
                                              serialization.PublicFormat.SubjectPublicKeyInfo)
    return crypt.RSASigner.from_string(pem, key_id='synthetic-key'), public.decode()


class AuthMemory(MemoryTransport):
    def __init__(self):
        super().__init__()
        self.payload = json.dumps(auth.empty_auth_state(BINDING)).encode()
        self.force_stale = False
        self.bad_readback = False
        self.reads = 0

    def read_versioned(self):
        self.reads += 1
        payload, tag = super().read_versioned()
        return (b'{}' if self.bad_readback else payload), tag

    def replace_versioned(self, before, tag, after):
        if self.force_stale:
            self.writes += 1
            raise StateError('HTTP_412')
        super().replace_versioned(before, tag, after)


class Rig:
    def __init__(self, keys):
        self.signer, self.public = keys
        self.now = int(time.time())
        self.p, self.raw = page('unknown')
        self.transport, self.grants = AuthMemory(), MemoryTransport()
        self.cert_calls = 0
        self.exchange_calls = 0
        self.claim_changes = {}
        self.unsigned = False
        self.source = self.raw
        self.policy = auth.GoogleIdentityVerifier(CLIENT, OWNER, request=self.certificates,
                                                  clock=lambda: self.now)
        self.store = auth.AuthRequestStore(self.transport, BINDING, preflight=lambda: None)
        self.gateway = auth.AuthenticatedGeneralConfirmation(self.store, self.policy,
            lambda *_: self.p, lambda _: self.source, redirect_uri=REDIRECT,
            exchange_code=self.exchange, clock=lambda: self.now)

    def certificates(self, url, **kwargs):
        assert url == 'https://www.googleapis.com/oauth2/v1/certs'
        self.cert_calls += 1
        return SimpleNamespace(status=200, data=json.dumps({'synthetic-key': self.public}).encode())

    def token(self, nonce):
        claims = dict(iss=auth.ISSUER, aud=CLIENT, sub=OWNER, iat=self.now,
                      exp=self.now + 300, nonce=nonce, email='owner@example.test', email_verified=True)
        claims.update(self.claim_changes)
        if self.unsigned:
            return 'eyJhbGciOiJub25lIn0.e30.'
        return jwt.encode(self.signer, claims).decode()

    def exchange(self, code, verifier):
        self.exchange_calls += 1
        assert code == 'synthetic-code'
        assert verifier == self.ticket.pkce_verifier
        return self.token(self.ticket.nonce)

    def begin(self):
        self.rid = self.gateway.prepare(self.p)
        self.ticket = self.gateway.begin(self.rid)
        return self.ticket

    def authenticate(self):
        self.begin()
        return self.callback()

    def callback(self, **changes):
        args = dict(cookie=self.ticket.cookie, state=self.ticket.state, code='synthetic-code')
        args.update(changes)
        return self.gateway.callback(self.ticket, **args)

    def factory(self, verified_actor):
        return HumanGeneralConfirmation(
            HumanGeneralAuthorityStore(self.grants, BINDING, preflight=lambda: None),
            lambda *_: self.p, verified_actor, load_source=lambda _: self.source,
            clock=lambda: datetime.fromtimestamp(self.now, timezone.utc).isoformat())

    def confirm(self, **changes):
        args = dict(cookie=self.ticket.cookie, csrf=self.ticket.csrf,
                    origin='https://confirmation.example.test', method='POST',
                    confirmation_factory=self.factory)
        args.update(changes)
        return self.gateway.confirm(self.ticket, **args)


@pytest.fixture
def rig(keys, monkeypatch):
    # All exchange/certificate calls must use the synthetic transport.
    import requests
    monkeypatch.setattr(requests.sessions.Session, 'request',
                        lambda *_a, **_k: pytest.fail('unexpected live network'))
    return Rig(keys)


def test_signed_owner_end_to_end_separate_actor_evidence_conditional_readback(rig):
    actor = rig.authenticate()
    assert actor.subject == OWNER and actor.method == 'google_oidc_code_pkce_v1'
    assert rig.cert_calls == 1 and rig.exchange_calls == 1
    assert rig.grants.writes == 0  # Google login alone is not confirmation
    result = rig.confirm()
    record = rig.store.load()['requests'][rig.rid]
    assert result['status'] == record['status'] == 'complete'
    assert record['actor']['actor_id'] == actor.actor_id
    assert record['authority_digest'] == result['authority_digest']
    assert rig.grants.writes == 1
    grant_state = json.loads(rig.grants.payload)
    grant = next(iter(grant_state['grants'].values()))
    assert not any(grant[k] for k in ('accounting_allowed', 'medical_handoff_allowed', 'archive_allowed'))
    assert grant['automatic_classification'] == 'sensitive_unknown'
    assert grant_state['audit'][0]['request_id'] == rig.rid
    assert record['digest'] == actor.request_digest
    assert rig.transport.reads >= rig.transport.writes
    # Persist only verified metadata/hashes, not bearer tokens or OAuth secrets.
    for secret in (rig.ticket.cookie, rig.ticket.state, rig.ticket.nonce,
                   rig.ticket.csrf, rig.ticket.pkce_verifier, rig.token(rig.ticket.nonce)):
        assert secret not in rig.transport.payload.decode()


@pytest.mark.parametrize('change', [
    {'sub': '999999999999999999999'}, {'aud': 'other.apps.googleusercontent.com'},
    {'iss': 'https://attacker.example.test'}, {'email_verified': False},
    {'email_verified': 'true'}, {'nonce': 'wrong-nonce'}, {'email': ''},
    {'azp': 'other.apps.googleusercontent.com'}, {'sub': None},
])
def test_actor_rejection_before_authority(rig, change):
    rig.claim_changes = change
    rig.begin()
    with pytest.raises(StateError, match='identity_rejected'):
        rig.callback()
    assert rig.grants.writes == 0
    with pytest.raises(StateError):
        rig.confirm()


@pytest.mark.parametrize('problem', ['unsigned', 'expired', 'future_iat', 'old_iat', 'signature'])
def test_token_signature_and_time_rejected(rig, problem):
    rig.begin()
    if problem == 'unsigned':
        rig.unsigned = True
    elif problem == 'expired':
        rig.claim_changes.update(iat=rig.now - 100, exp=rig.now - 1)
    elif problem == 'future_iat':
        rig.claim_changes['iat'] = rig.now + 100
    elif problem == 'old_iat':
        rig.claim_changes['iat'] = rig.now - 100
    else:
        original = rig.exchange
        def changed(code, verifier):
            token = original(code, verifier)
            head, body, signature = token.split('.')
            return '.'.join([head, body, ('A' if signature[0] != 'A' else 'B') + signature[1:]])
        rig.gateway.exchange_code = changed
    with pytest.raises(StateError, match='identity_rejected'):
        rig.callback()
    assert rig.grants.writes == 0


@pytest.mark.parametrize('phase', ['url', 'callback', 'confirmation'])
def test_one_use_url_callback_confirmation_replay_no_extra_write(rig, phase):
    rig.authenticate()
    rig.confirm()
    writes = rig.transport.writes, rig.grants.writes
    with pytest.raises(StateError, match='replay_or_unknown_request'):
        if phase == 'url':
            rig.gateway.begin(rig.rid)
        elif phase == 'callback':
            rig.callback()
        else:
            rig.confirm()
    assert (rig.transport.writes, rig.grants.writes) == writes


@pytest.mark.parametrize('change', [dict(state='wrong-state'), dict(cookie='wrong-cookie')])
def test_login_csrf_rejected_before_code_exchange(rig, change):
    rig.begin()
    with pytest.raises(StateError):
        rig.callback(**change)
    assert rig.exchange_calls == rig.grants.writes == 0


@pytest.mark.parametrize('change', [dict(csrf='wrong-csrf'), dict(cookie='wrong-cookie'),
    dict(origin='https://attacker.example.test'), dict(method='GET')])
def test_explicit_post_csrf_required(rig, change):
    rig.authenticate()
    with pytest.raises(StateError):
        rig.confirm(**change)
    assert rig.grants.writes == 0


@pytest.mark.parametrize('problem', ['source', 'page', 'review', 'revision', 'privacy', 'state'])
def test_freshness_changes_after_login_rejected(rig, problem):
    rig.authenticate()
    if problem == 'source':
        rig.source += b'changed'
    else:
        data = rig.p.model_dump()
        if problem == 'page':
            other, _ = page('unknown', number=2, kinds=['unknown', 'unknown'])
            rig.p = other
        else:
            if problem == 'review': data['review_identity'] = 'f' * 64
            if problem == 'revision': data['authority_revision'] += 1
            if problem == 'privacy': data['clearly_sensitive'] = True
            if problem == 'state': data['extraction_status'] = 'failed'
            rig.p = type(rig.p).model_validate(data)
    with pytest.raises(StateError):
        rig.confirm()
    assert rig.grants.writes == 0


@pytest.mark.parametrize('phase', ['begin', 'callback', 'confirm'])
def test_expired_request(rig, phase):
    if phase == 'begin':
        rid = rig.gateway.prepare(rig.p)
    elif phase == 'callback':
        rig.begin()
    else:
        rig.authenticate()
    rig.now += auth.TTL
    with pytest.raises(StateError, match='request_expired'):
        if phase == 'begin': rig.gateway.begin(rid)
        elif phase == 'callback': rig.callback()
        else: rig.confirm()
    assert rig.grants.writes == 0


@pytest.mark.parametrize('problem', ['medical', 'payroll', 'pii'])
def test_sensitive_rejection_even_after_valid_google_identity(rig, problem):
    rig.authenticate()
    if problem == 'pii':
        data = {**rig.p.model_dump(), 'clearly_sensitive': True}
        rig.p = type(rig.p).model_validate(data)
    else:
        rig.p, _ = page(problem)
    with pytest.raises(StateError, match='stale_or_sensitive_page'):
        rig.confirm()
    assert rig.grants.writes == 0


def test_uuid_and_other_request_evidence_cannot_be_reused(rig):
    rig.authenticate()
    other = rig.gateway.prepare(rig.p)
    other_ticket = rig.gateway.begin(other)
    with pytest.raises(StateError):
        rig.gateway.callback(replace(rig.ticket, request_id=other), cookie=rig.ticket.cookie,
                             state=rig.ticket.state, code='synthetic-code')
    with pytest.raises(StateError):
        rig.gateway.begin(rig.rid[:-1] + ('0' if rig.rid[-1] != '0' else '1'))
    assert rig.grants.writes == 0
    assert other_ticket.request_id != rig.ticket.request_id


def test_owner_policy_revision_change_requires_new_confirmation(rig):
    rig.authenticate()
    rig.policy.policy_revision += 1
    with pytest.raises(StateError, match='actor_policy_changed'):
        rig.confirm()
    assert rig.grants.writes == 0


def test_stale_etag_no_retry_or_unconditional_fallback(rig):
    rig.begin()
    rig.transport.force_stale = True
    writes = rig.transport.writes
    with pytest.raises(StateError, match='HTTP_412'):
        rig.callback()
    assert rig.transport.writes == writes + 1
    assert rig.exchange_calls == rig.grants.writes == 0


def test_readback_mismatch_does_not_report_success(rig):
    original = rig.transport.replace_versioned
    def bad(before, tag, after):
        original(before, tag, after)
        rig.transport.bad_readback = True
    rig.transport.replace_versioned = bad
    with pytest.raises(StateError, match='readback_mismatch'):
        rig.gateway.prepare(rig.p)
    assert rig.grants.writes == 0


def test_authority_save_failure_never_retries_consumed_request(rig):
    rig.authenticate()
    def broken(_):
        raise StateError('synthetic_save_unknown')
    with pytest.raises(StateError):
        rig.confirm(confirmation_factory=broken)
    writes = rig.transport.writes
    with pytest.raises(StateError):
        rig.confirm()
    assert rig.transport.writes == writes and rig.grants.writes == 0


def test_link_scope_pkce_nonce_and_no_token_in_browser_url(rig):
    rig.begin()
    params = parse_qs(urlsplit(rig.ticket.authorization_url).query)
    assert params['scope'] == ['openid email'] and params['response_type'] == ['code']
    assert params['code_challenge_method'] == ['S256']
    assert 'client_secret' not in params and 'id_token' not in params
    assert rig.ticket.pkce_verifier not in rig.ticket.authorization_url
    link = rig.gateway.review_link('https://confirmation.example.test/confirm', rig.rid)
    assert parse_qs(urlsplit(link).query) == {'request': [rig.rid]}
    assert rig.rid not in repr(rig.ticket)  # session secrets have no dataclass repr


def test_explicit_tamper_digest_and_actor_record_fail_closed(rig):
    rig.authenticate()
    value = json.loads(rig.transport.payload)
    value['requests'][rig.rid]['actor']['subject'] = '999999999999999999999'
    rig.transport.payload = json.dumps(value).encode()
    with pytest.raises(StateError, match='state_unavailable'):
        rig.confirm()
    assert rig.grants.writes == 0


def test_no_live_runtime_or_writer_dependencies():
    from pathlib import Path
    import ast
    source = Path(auth.__file__).read_text(encoding='utf-8')
    tree = ast.parse(source)
    imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not any(any(word in (name or '') for word in
        ('medical', 'gemini', 'pipeline', 'production', 'manual_entry')) for name in imports)
    assert not hasattr(auth, 'main')


def test_current_page_processing_state_change_rejected(rig):
    rig.authenticate()
    rig.p = type(rig.p).model_validate({**rig.p.model_dump(), 'processing_status': 'held'})
    with pytest.raises(StateError):
        rig.confirm()
    assert rig.grants.writes == 0


def test_two_concurrent_snapshot_updates_cannot_overwrite(rig):
    rig.begin()
    first, stale = rig.store.load(), rig.store.load()
    first['requests'][rig.rid]['status'] = 'verifying'
    rig.store.save(first)
    before = rig.transport.payload
    with pytest.raises(StateError, match='changed_since_read'):
        rig.store.save(stale)
    assert rig.transport.payload == before and rig.grants.writes == 0


def test_duplicate_confirmation_different_uuid_never_creates_second_grant(rig):
    rig.authenticate()
    rig.confirm()
    writes = rig.grants.writes
    rig.authenticate()
    with pytest.raises(StateError, match='existing_authority_requires_reconciliation'):
        rig.confirm()
    assert rig.grants.writes == writes


def test_projection_uses_existing_card_without_onedit_or_legacy_consent(rig):
    from app.human_general_auth_projection import authenticated_general_card
    card = authenticated_general_card(rig.p, rig.gateway)
    assert card['identity']['kind'] == 'human_general'
    assert card['confirmation_link']['url'].startswith('https://confirmation.example.test/confirm?request=')
    assert any(row[0] == 'human_general_kind' and row[2] == '未選択' for row in card['rows'])
    assert rig.grants.writes == 0


def test_complete_state_save_failure_after_grant_is_unknown_no_automatic_retry(rig):
    rig.authenticate()
    original = rig.transport.replace_versioned
    def unavailable(before, tag, after):
        if json.loads(after)['requests'][rig.rid]['status'] == 'complete':
            raise StateError('HTTP_412')
        original(before, tag, after)
    rig.transport.replace_versioned = unavailable
    with pytest.raises(StateError, match='HTTP_412'):
        rig.confirm()
    assert rig.grants.writes == 1
    assert rig.store.load()['requests'][rig.rid]['status'] == 'claimed'
    with pytest.raises(StateError):
        rig.confirm()
    assert rig.grants.writes == 1


@pytest.mark.parametrize('automatic', ['unknown', 'sensitive_unknown'])
def test_both_unknown_classes_keep_automatic_result(rig, automatic):
    from app.human_general_authority import review_identity
    data = {**rig.p.model_dump(), 'automatic_classification': automatic}
    candidate = type(rig.p).model_validate(data)
    data['review_identity'] = review_identity(candidate)
    rig.p = type(rig.p).model_validate(data)
    rig.authenticate()
    rig.confirm()
    grant = next(iter(json.loads(rig.grants.payload)['grants'].values()))
    assert grant['automatic_classification'] == automatic


def test_google_code_exchange_drops_other_tokens_and_has_timeout_no_retry():
    calls = []
    class Session:
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(status_code=200, json=lambda: {
                'id_token': 'synthetic-id-token', 'access_token': 'unused', 'refresh_token': 'unused'})
    exchange = auth.GoogleCodeExchange(CLIENT, 'synthetic-secret', REDIRECT, Session())
    assert exchange('synthetic-code', 'synthetic-verifier') == 'synthetic-id-token'
    assert len(calls) == 1 and calls[0][0] == 'https://oauth2.googleapis.com/token'
    assert calls[0][1]['timeout'] == 15
    assert calls[0][1]['allow_redirects'] is False
    assert calls[0][1]['data']['code_verifier'] == 'synthetic-verifier'


def test_google_code_exchange_failure_has_safe_error_only():
    class Session:
        def post(self, *_args, **_kwargs):
            raise ValueError('synthetic-sensitive-transport-message')
    exchange = auth.GoogleCodeExchange(CLIENT, 'synthetic-secret', REDIRECT, Session())
    with pytest.raises(StateError) as result:
        exchange('synthetic-code', 'synthetic-verifier')
    assert str(result.value) == 'human_general_auth_code_exchange_failed'


def test_mobile_preview_single_column_without_technical_inputs():
    from pathlib import Path
    preview = Path('docs/human-general-auth-mobile.html').read_text(encoding='utf-8')
    assert 'width=device-width' in preview and 'width:100%' in preview
    assert 'min-height:48px' in preview and '<table' not in preview and '<input' not in preview
    assert 'disabled' in preview and '認証と保存は実行しません' in preview
    # Static constraints only: not a claim of real iPhone OAuth/cookie testing.
