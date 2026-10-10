"""Registered new-PDF page routing in the existing OIDC/session service.

No renderer, Gemini, accounting writer, mover, credentials provisioning, or
session-reader capability for Actions. Exact page records come from the one
configured private registry; browser inputs cannot supply a page snapshot.
"""
from copy import deepcopy
from types import SimpleNamespace
from dataclasses import replace
from uuid import uuid4
import base64
import json

from itsdangerous import URLSafeTimedSerializer
from app.drive_run_state import StateError
from app.pdf_intake_authority import (
    DecisionSealer, binding, digest, eligible_for_hga, page_key, validate_page,
)
from app.pdf_intake_registry import DriveRegistryIO, RegistryStore, canonical
from app.page_receipt_model import PageUnit
from app.human_general_authority import empty_state, validate_state
from app.human_general_auth_transport import empty_auth_state, request_binding, AI_CONSENT_ACTION, TTL
from .real_runtime import RealPageRuntime
from .runtime import SyntheticRuntime, ExpectedTagAuthorityStore
from .backend import timestamp


def verify_machine_token(token, audience, subject, *, verifier=None, clock=None):
    """Google-issued machine identity, not the spreadsheet owner/session."""
    from time import time
    if clock is None: clock = time
    try:
        if not isinstance(token,str) or len(token)>16384 or token.count('.')!=2:
            raise ValueError()
        header = json.loads(base64.urlsafe_b64decode(token.split('.')[0]+'==='))
        if header.get('alg') != 'RS256' or any(k in header for k in ('jku','x5u','jwk')): raise ValueError()
        if verifier is None:
            from google.oauth2 import id_token
            from google.auth.transport.requests import Request
            http = Request()
            def certificates(url, **kwargs):
                if url != 'https://www.googleapis.com/oauth2/v1/certs': raise ValueError()
                return http(url, timeout=15, allow_redirects=False, **kwargs)
            verifier = lambda raw,aud:id_token.verify_oauth2_token(raw,certificates,aud)
        claims = verifier(token,audience); now = int(clock())
        if (claims.get('iss') not in {'https://accounts.google.com','accounts.google.com'}
                or claims.get('aud') != audience or claims.get('sub') != subject
                or claims.get('email_verified') is not True
                or type(claims.get('exp')) is not int or type(claims.get('iat')) is not int
                or not claims['iat'] <= now < claims['exp']
                or not 0 < claims['exp']-claims['iat'] <= 3600): raise ValueError()
    except Exception: raise StateError('pdf_intake_machine_identity_rejected') from None


def verify_registered_proof(router, bearer, query, *, verifier=None):
    """Read only. Machine callers never read Firestore or owner session state."""
    if not router.intake_config or not isinstance(bearer,str) or not bearer.startswith('Bearer '):
        raise StateError('pdf_intake_machine_identity_rejected')
    verify_machine_token(bearer[7:],router.origin,router.info['client_id'],verifier=verifier,clock=router.clock)
    if (not isinstance(query,dict) or set(query)!={'page_key','operation','unit_id','binding_digest','authority_digest'}
            or query['operation'] not in {'single_page_ai','receipt_posting'}):
        raise StateError('pdf_intake_authority_query_invalid')
    io = DriveRegistryIO(router.intake_config,router.info,writable=False)
    current = RegistryStore(io,io.anchor).load()
    record = current['pages'].get(query['page_key'])
    if record is None: raise StateError('pdf_intake_authority_missing')
    if query['operation']=='single_page_ai':
        if query['unit_id'] is not None: raise StateError('pdf_intake_authority_query_invalid')
        saved=record['authority'].get('single_page_ai')
        proof=None if saved is None else saved['proof']
    else:
        unit=record['units'].get(query['unit_id']); proof=None if unit is None else unit['posting_authority']
    if proof is None or digest(proof)!=query['authority_digest']:
        raise StateError('pdf_intake_authority_missing_or_revoked')
    expected=proof['decision']['binding']
    if (record['status']=='privacy_observation_changed' or expected['page']!=record['page'] or digest(expected)!=query['binding_digest']
            or expected['operation']!=query['operation'] or expected.get('unit',{}).get('receipt_unit_id')!=query['unit_id']): raise StateError('pdf_intake_authority_binding_stale')
    sealer=DecisionSealer(base64.urlsafe_b64decode(router.key),
                          digest(['https://accounts.google.com',router.identity.owner_subject]))
    return sealer.verify(proof,expected)


class RegisteredPageDrive:
    def __init__(self, config, target, info):
        self.io = DriveRegistryIO(config, info, writable=True)
        self.store = RegistryStore(self.io, self.io.anchor)
        self.target = target
        current = self.store.load()
        record = current['pages'].get(target)
        if record is None or record['status'] in {'imported','reconciled_existing','medical_manual_imported','duplicate_confirmed','intentionally_skipped'}:
            raise StateError('human_general_auth_replay_or_unknown_request')
        self.page = PageUnit.model_validate(validate_page(record['page']))
        self.config = {'binding': digest(['registered-page-hga-v1', self.io.anchor, target, self.page.model_dump()])}
        self.expected = self.page.model_dump()
        self._source_cache = None

    def begin_request(self): self.io.begin_request(); self._source_cache = None
    def end_request(self): self.io.end_request(); self._source_cache = None
    def acl(self): self.io.acl()

    def fresh(self):
        current = self.store.load()['pages'].get(self.target)
        if current is None or current['page'] != self.expected or current['status']=='privacy_observation_changed':
            raise StateError('registered_page_stale')
        self.source(self.page.source.source_file_id)
        return self.page

    def source(self, sid):
        from hashlib import sha256
        if sid != self.page.source.source_file_id:
            raise StateError('registered_page_source_forbidden')
        if self._source_cache is not None: return self._source_cache
        fields = 'id,etag,parents(id),labels(trashed),mimeType'
        before = self.io.metadata(sid, fields)
        if (before.get('id') != sid or before.get('labels',{}).get('trashed')
                or before.get('mimeType') != 'application/pdf'
                or before.get('parents') not in ([{'id':self.io.config['inbox_id']}],
                                                 [{'id':self.io.config['processed_id']}])):
            raise StateError('registered_page_source_changed')
        raw = self.io._request('GET','https://www.googleapis.com/drive/v2/files/'+sid,params={'alt':'media'}).content
        if (sha256(raw).hexdigest() != self.page.source.source_content_hash
                or self.io.metadata(sid, fields) != before):
            raise StateError('registered_page_source_changed')
        if self.io.deadline is not None: self._source_cache = raw
        return raw


class RegisteredAuthorityTransport:
    def __init__(self, runtime, actor=None):
        self.runtime, self.drive, self.actor = runtime, runtime.drive, actor
        self.stage = lambda *_args: None

    def read_versioned(self):
        current = self.drive.store.load()
        record = current['pages'][self.drive.target]
        if record['page'] != self.drive.expected:
            raise StateError('registered_page_stale')
        saved = record['authority'].get('single_page_ai')
        state = empty_state(self.drive.config['binding']) if saved is None else saved['state']
        validate_state(state, self.drive.config['binding'])
        if saved is not None:
            self.runtime.sealer.verify(saved['proof'], binding(record['page'], 'single_page_ai'))
        return canonical(state), self.drive.store.tag

    def replace_versioned(self, before, tag, after):
        if self.actor is None: raise StateError('registered_page_verified_actor_required')
        # Claim/read-back from the protected OIDC gateway precedes this method.
        self.drive._source_cache = None  # Actual fresh source read before PUT.
        self.drive.fresh()
        current, current_tag = self.read_versioned()
        if current != before or current_tag != tag: raise StateError('HTTP_412')
        record = self.drive.store.load()['pages'][self.drive.target]
        if self.drive.store.tag != tag: raise StateError('HTTP_412')
        if record['authority'].get('single_page_ai') is not None:
            raise StateError('human_general_auth_replay_or_unknown_request')
        state = validate_state(json.loads(after), self.drive.config['binding'])
        from app.human_general_authority import validate_grant
        validate_grant(state['grants'].get(page_key(self.drive.expected)),self.drive.page)
        actor = self.actor; rid = actor.request_id
        expected_legacy = request_binding(self.drive.page, rid, AI_CONSENT_ACTION)
        if (actor.actor_id != self.runtime.sealer.owner_actor_id
                or actor.request_digest != digest(expected_legacy)
                or len(state['grants']) != 1 or state['generation'] != 1
                or len(state['audit']) != 1 or state['audit'][0]['request_id'] != rid):
            raise StateError('registered_page_verified_actor_required')
        expected = binding(self.drive.expected, 'single_page_ai')
        verified = SimpleNamespace(**{k:getattr(actor,k) for k in ('request_id','actor_id','method','verified_at')},
                                   request_digest=digest({'request_id':rid, **expected}))
        proof = self.runtime.sealer.seal(expected, verified, rid, int(self.runtime.clock()))
        proposed = self.drive.store.load()
        if self.drive.store.tag != tag: raise StateError('HTTP_412')
        proposed['pages'][self.drive.target]['authority']['single_page_ai'] = {'state':state, 'proof':proof}
        proposed['generation'] += 1
        self.drive.store.save(proposed)  # no retry/fallback; exact whole-file read-back.


class RegisteredPageRuntime(RealPageRuntime):
    def __init__(self, client, settings, key, config, target, info, **kwargs):
        SyntheticRuntime.__init__(self, client, settings, key, **kwargs)
        self.drive = RegisteredPageDrive(config, target, info)
        self.config = self.drive.config
        self.mode = 'registered_page_authority_only'
        self.sealer = DecisionSealer(base64.urlsafe_b64decode(key),
                                    digest(['https://accounts.google.com', self.identity.owner_subject]))

    def state(self, rid, kind):
        if kind == 'authorities': return RegisteredAuthorityTransport(self)
        return super().state(rid, kind)

    def factory(self, rid, expected_tag=None):
        from app.human_general_authority import HumanGeneralConfirmation
        def build(actor):
            verified = None
            if actor is not None:
                verified = self.gateway(rid).verified_actor(rid)
                if actor(rid) != verified.legacy_actor(): raise StateError('registered_page_actor_replaced')
            return HumanGeneralConfirmation(
                ExpectedTagAuthorityStore(RegisteredAuthorityTransport(self, verified), self.config['binding'],
                                          preflight=lambda:None, expected_tag=expected_tag),
                lambda sid,n:self.current_page(rid,sid,n), actor, load_source=self.source)
        return build

    def reconcile(self, rid):
        try:
            saved = self.drive.store.load()['pages'][self.drive.target]['authority'].get('single_page_ai')
            if saved is None: return 'not_written'
            proof = self.sealer.verify(saved['proof'],binding(self.drive.expected,'single_page_ai'))
            if (proof['request_id'] == rid and len(saved['state']['audit']) == 1
                    and saved['state']['audit'][0]['request_id'] == rid): return 'written'
        except Exception: pass
        return 'unknown'

    def seed(self):
        current = self.drive.fresh()
        if not eligible_for_hga(current.model_dump()): raise StateError('registered_page_hga_ineligible')
        store = ExpectedTagAuthorityStore(self.state(str(uuid4()),'authorities'), self.config['binding'],preflight=lambda:None)
        if store.load()['grants']: raise StateError('human_general_auth_replay_or_unknown_request')
        rid, now = str(uuid4()), int(self.clock())
        self.client.collection('real_requests').document(rid).create(
            {'payload':canonical(empty_auth_state(self.config['binding'])),'expires_at':timestamp(now+TTL)},retry=None,timeout=10)
        self.gateway(rid).prepare(current,request_id=rid)
        self.client.collection('real_request_profiles').document(rid).create(
            {'profile':'registered_page','profile_digest':digest(self.drive.expected),
             'target':self.drive.target,'expires_at':timestamp(now+TTL)},retry=None,timeout=10)
        proof = URLSafeTimedSerializer(self.key,salt='hga-link-v1').dumps({'request':rid})
        return self.origin+'/start?request='+rid+'&proof='+proof
