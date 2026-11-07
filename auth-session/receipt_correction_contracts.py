"""Real ephemeral RSA signatures, existing auth/session/CAS, synthetic receipts.

No browser identity/email from Sheets, real OAuth, network, authority or writer.
Loaded ONLY by run_receipt_registration_checks.py into a pinned offline fixture.
"""
from copy import deepcopy
from hashlib import sha256
import json
import time
from types import SimpleNamespace

from cryptography.fernet import Fernet
from google.auth import jwt
import pytest

from app.drive_run_state import StateError
from app.human_general_auth_transport import (
    AuthRequestStore, GoogleIdentityVerifier, empty_auth_state, validate_auth_state, TTL,
)
from receipt_registration_preview.models import ReceiptItem, ReceiptResult
from app.page_receipt_model import digest
from receipt_registration_preview.reconciliation import parse_import_rows
from receipt_registration_preview.receipt_review_snapshot import candidate_rows
from receipt_registration_preview.receipt_item_correction_review import review, registration_plan
from receipt_registration_preview.category_rules import parse_rules
from services.human_general.backend import FirestoreConditionalState, canonical, timestamp
from services.human_general.shared_login import SharedLogins
from services.human_general.receipt_correction_registration import (
    AuthenticatedCorrectionRegistration, validate_registration_state, ACTION, SCOPE,
)
from test_human_general_auth_transport import keys, CLIENT, OWNER, REDIRECT
from test_human_general_http import Database

RID = '33333333-3333-4333-8333-333333333333'
OTHER = '44444444-4444-4444-8444-444444444444'
SOURCE = 'f' * 33
CATEGORIES = [('食費', '食品'), ('日用品', '洗剤'), ('その他', '未分類')]


class Rig:
    def __init__(self, keys, *, total=1368):
        self.signer, self.public = keys
        self.now, self.db = int(time.time()), Database()
        self.claims, self.exchanges, self.reads = {}, 0, 0
        self.corrupt_signature = False
        self.raw = b'synthetic-original-not-a-medical-image'
        self.source = dict(source_id=SOURCE, sha256=sha256(self.raw).hexdigest(), version='7',
                           mime_type='image/png', classification='normal', transaction_kind='purchase')
        amounts = [500, 730 if total == 1368 else 439]
        result = ReceiptResult(merchant='合成商店', date='2026-11-06', total=total,
            items=[ReceiptItem(name='商品'+str(i), amount=a, major_category='食費', minor_category='食品')
                   for i, a in enumerate(amounts, 1)])
        tx = parse_import_rows([['receipt:'+SOURCE, '2026-11-06T10:00:00+09:00', 'receipt', SOURCE,
            result.date, result.merchant, total, '', '要確認', '', 'a'*64,
            f'明細合計{sum(amounts)}≠レシート合計{total}']])[0]
        rows = list(enumerate(candidate_rows(tx.import_id, 'a'*64, result), 2))
        self.packet = dict(tx=tx, indexed_rows=rows, source=self.source, source_bytes=self.raw,
            operations=[dict(action='edit', item_id=rows[1][1][0], changes={'amount':total-500},
                             evidence='原本の商品金額を確認')], categories=CATEGORIES,
            total_confirmation=dict(source=deepcopy(self.source), receipt_total=total,
                                    actor_id=digest(['https://accounts.google.com', OWNER])),
            approved_product_rules=[])
        self.identity = GoogleIdentityVerifier(CLIENT, OWNER, request=self.certificates, clock=lambda:self.now)
        self.logins = SharedLogins(self.db, Fernet.generate_key(), self.identity, clock=lambda:self.now)
        self.gateway = self.make_gateway(RID)

    def read_case(self, case_id):
        self.reads += 1
        assert case_id == self.packet['tx'].import_id
        return deepcopy(self.packet)  # Independently loaded, not the browser form.

    def make_gateway(self, rid, *, initialize=True):
        doc = self.db.collection('registration_requests').document(rid)
        if initialize:
            doc.create(dict(payload=canonical(empty_auth_state(SCOPE)), expires_at=timestamp(self.now+TTL)),
                       retry=None, timeout=10)
        store = AuthRequestStore(FirestoreConditionalState(doc, clock=lambda:self.now), SCOPE,
                                 preflight=lambda:None, validator=validate_registration_state)
        return AuthenticatedCorrectionRegistration(store, self.identity, read_case=self.read_case,
                    build_review=review, validate_snapshot=registration_plan,
                    redirect_uri=REDIRECT, exchange_code=self.exchange, clock=lambda:self.now)

    def certificates(self, url, **kwargs):
        assert url == 'https://www.googleapis.com/oauth2/v1/certs'
        return SimpleNamespace(status=200, data=json.dumps({'synthetic-key':self.public}).encode())

    def exchange(self, code, verifier):
        assert code == 'synthetic-code' and verifier == self.ticket.pkce_verifier
        self.exchanges += 1
        claims = dict(iss='https://accounts.google.com', aud=CLIENT, sub=OWNER, iat=self.now,
                      exp=self.now+300, nonce=self.ticket.nonce, email='registrant@example.test', email_verified=True)
        claims.update(self.claims)
        token = jwt.encode(self.signer, claims).decode()
        if self.corrupt_signature:
            parts = token.split('.')
            parts[2] = ('A' if parts[2][0] != 'A' else 'B') + parts[2][1:]
            token = '.'.join(parts)
        return token

    def authenticate(self):
        self.gateway.prepare(self.packet['tx'].import_id, request_id=RID)
        self.ticket = self.gateway.begin(RID)
        return self.gateway.callback(self.ticket, cookie=self.ticket.cookie, state=self.ticket.state,
                                     code='synthetic-code')

    def confirm(self, **changes):
        args = dict(cookie=self.ticket.cookie, csrf=self.ticket.csrf,
                    origin='https://confirmation.example.test', method='POST')
        args.update(changes)
        return self.gateway.confirm(self.ticket, **args)

    def state(self):
        return self.gateway.store.load()['requests'][RID]


@pytest.fixture
def rig(keys):
    return Rig(keys)


@pytest.mark.parametrize('total', [1368, 967])
def test_signed_registrant_submits_exact_snapshot_to_PR196_no_authority(keys, total):
    r = Rig(keys, total=total)
    actor = r.authenticate()
    before = deepcopy(r.db.data)
    shown = r.gateway.details(r.ticket, cookie=r.ticket.cookie)
    assert r.db.data == before and r.state()['status'] == 'authenticated'
    assert shown['preview']['item_total'] == total
    result = r.confirm()
    record = r.state()
    assert record['status'] == 'complete' and result['actor_id'] == actor.actor_id
    assert record['binding']['snapshot'] == shown['snapshot']
    assert result['snapshot_digest'] == shown['snapshot']['digest']
    assert result['case_id'] == shown['case_id'] and result['source'] == r.source
    assert record['binding']['expires_at'] == record['created_at'] + TTL
    assert actor.request_digest == digest(record['binding'])
    assert result['validation']['state'] == '検証済み・未記帳'
    assert sum(i['amount'] for i in result['validation']['items']) == total
    assert result['posting_authority'] is False and result['financial_write_count'] == 0
    assert all(k.startswith('registration_requests/') for k in r.db.data)
    assert 'authority_digest' not in record
    with pytest.raises(StateError):validate_auth_state(r.gateway.store.load(), SCOPE)
    before = deepcopy(r.db.data)
    assert r.gateway.result(r.ticket, cookie=r.ticket.cookie)['registration_digest'] == result['registration_digest']
    assert r.db.data == before
    with pytest.raises(StateError):r.confirm()
    with pytest.raises(StateError):r.gateway.prepare(r.packet['tx'].import_id, request_id=RID)
    assert r.db.data == before
    for secret in (r.ticket.cookie, r.ticket.csrf, r.ticket.pkce_verifier, r.ticket.nonce):
        assert secret not in json.dumps(record)


def test_editor_claims_are_not_registrant_and_reused_session_keeps_exact_binding(rig):
    # Arbitrary draft editor metadata is ignored. Registration comes from OIDC.
    rig.packet['editor_email'] = 'another-editor@example.invalid'
    actor = rig.authenticate()
    login = rig.logins.create(actor)
    rig.confirm()
    other = rig.make_gateway(OTHER)
    other.prepare(rig.packet['tx'].import_id, request_id=OTHER)
    other.verify_login = lambda record:rig.logins.verify_bound(record, login)
    record = other.store.load()['requests'][OTHER]
    ticket = other.resume_login(OTHER, rig.logins.actor(login, record), rig.logins.evidence(login))
    result = other.confirm(ticket, cookie=ticket.cookie, csrf=ticket.csrf,
                           origin=other.origin, method='POST')
    assert result['actor_id'] == actor.actor_id and result['request_id'] == OTHER
    assert rig.exchanges == 1
    assert all(not k.startswith('authorities/') for k in rig.db.data)


@pytest.mark.parametrize('claim,value', [('sub','999999'), ('aud','other.apps.googleusercontent.com'),
    ('iss','https://wrong.example.test'), ('email_verified',False), ('nonce','wrong'), ('exp',1)])
def test_invalid_signed_identity_never_reaches_validation(rig, claim, value):
    rig.claims[claim] = value
    with pytest.raises(StateError):rig.authenticate()
    assert rig.state()['status'] != 'complete' and 'validation' not in rig.state()


@pytest.mark.parametrize('field,value', [('method','GET'), ('csrf','wrong'), ('cookie','wrong'),
    ('origin',None), ('origin','null'), ('origin','https://other.example.test')])
def test_missing_explicit_post_or_browser_protection_is_rejected(rig, field, value):
    rig.authenticate()
    before = deepcopy(rig.db.data)
    with pytest.raises(StateError):rig.confirm(**{field:value})
    assert rig.db.data == before


@pytest.mark.parametrize('change', ['bytes','source_id','hash','version','case','amount','quantity','candidate','category','rule','total_confirmation'])
def test_snapshot_changes_between_login_and_post_are_rejected(rig, change):
    rig.authenticate()
    if change == 'bytes':rig.packet['source_bytes'] = b'changed-original'
    elif change in {'source_id','version'}:rig.packet['source'][change] = 'changed'
    elif change == 'hash':rig.packet['source']['sha256'] = 'e'*64
    elif change == 'case':rig.packet['tx'].row[3] = 'another-case'
    elif change == 'candidate':rig.packet['indexed_rows'][0][1][4] = 'changed candidate'
    elif change == 'category':rig.packet['categories'] = CATEGORIES[:1]
    elif change == 'total_confirmation':rig.packet['total_confirmation'] = None
    elif change == 'rule':
        rig.packet['approved_product_rules'] = parse_rules([
            ['new-rule','product','receipt','','','合成商店','','別の商品','',
             '食費','食品','approved-expense','2026-01-01T00:00:00+09:00',1,'TRUE']])
    else:rig.packet['operations'][0]['changes'][change] = 2
    before = deepcopy(rig.db.data)
    with pytest.raises(StateError):rig.confirm()
    assert rig.db.data == before


def test_expired_request_and_revoked_reused_login_fail_closed(rig):
    actor = rig.authenticate()
    login = rig.logins.create(actor)
    other = rig.make_gateway(OTHER)
    other.prepare(rig.packet['tx'].import_id, request_id=OTHER)
    other.verify_login = lambda record:rig.logins.verify_bound(record, login)
    record = other.store.load()['requests'][OTHER]
    ticket = other.resume_login(OTHER, rig.logins.actor(login, record), rig.logins.evidence(login))
    rig.logins.revoke(login)
    with pytest.raises(StateError):
        other.confirm(ticket, cookie=ticket.cookie, csrf=ticket.csrf, origin=other.origin, method='POST')
    rig.now += TTL
    with pytest.raises(StateError):rig.confirm()


@pytest.mark.parametrize('field', ['snapshot','actor','purpose'])
def test_untrusted_resealed_evidence_is_not_a_valid_registration(rig, field):
    rig.authenticate()
    key = 'registration_requests/'+RID
    data = json.loads(rig.db.data[key]['payload'])
    rec = data['requests'][RID]
    if field == 'snapshot':rec['binding']['snapshot']['operations'] = []
    elif field == 'actor':rec['actor']['subject'] = '999999'
    else:rec['binding']['requested_action'] = 'general_receipt_and_gemini_permission'
    rig.db.data[key]['payload'] = canonical(data)
    with pytest.raises(StateError):rig.confirm()


def test_CAS_conflict_and_second_worker_cannot_repeat_registration(rig):
    rig.authenticate()
    rig.db.race = True
    with pytest.raises(StateError):rig.confirm()
    assert rig.state()['status'] == 'authenticated'
    rig.db.race = False
    rig.confirm()
    second = rig.make_gateway(RID, initialize=False)
    before = deepcopy(rig.db.data)
    with pytest.raises(StateError):
        second.confirm(rig.ticket, cookie=rig.ticket.cookie, csrf=rig.ticket.csrf,
                       origin=second.origin, method='POST')
    assert rig.db.data == before


@pytest.mark.parametrize('stage', ['claim','complete'])
def test_unknown_commit_is_reconcile_only_without_auto_retry(rig, stage):
    rig.authenticate()
    document = rig.gateway.store.transport.document
    original = document.update
    def lost_response(data, **kwargs):
        original(data, **kwargs)
        record = json.loads(data['payload'])['requests'][RID]
        if record['status'] == ('claimed' if stage == 'claim' else 'complete'):
            raise TimeoutError('synthetic lost response')
    document.update = lost_response
    with pytest.raises(StateError):rig.confirm()
    before = deepcopy(rig.db.data)
    with pytest.raises(StateError):rig.confirm()
    assert rig.db.data == before
    assert rig.state()['status'] == ('claimed' if stage == 'claim' else 'complete')
    if stage == 'complete':assert rig.gateway.result(rig.ticket, cookie=rig.ticket.cookie)['validation']['financial_write_count'] == 0


def test_unresolved_items_return_needs_correction_not_a_posting_grant(rig):
    rig.packet['operations'] = []
    rig.authenticate()
    assert rig.confirm()['validation']['state'] == '修正が必要'


def test_modified_RSA_signature_is_rejected_without_registration(rig):
    rig.corrupt_signature = True
    with pytest.raises(StateError):rig.authenticate()
    assert rig.state()['status'] == 'verifying' and 'validation' not in rig.state()


def test_other_request_browser_cannot_register_this_snapshot(rig):
    rig.authenticate()
    first = rig.ticket
    second = rig.make_gateway(OTHER)
    second.prepare(rig.packet['tx'].import_id, request_id=OTHER)
    rig.ticket = second.begin(OTHER)
    second.callback(rig.ticket, cookie=rig.ticket.cookie, state=rig.ticket.state, code='synthetic-code')
    before = deepcopy(rig.db.data)
    with pytest.raises(StateError):
        rig.gateway.confirm(first, cookie=rig.ticket.cookie, csrf=rig.ticket.csrf,
                            origin=rig.gateway.origin, method='POST')
    assert rig.db.data == before


def test_draft_changes_after_claim_are_held_without_replay(rig):
    rig.authenticate()
    document = rig.gateway.store.transport.document
    original = document.update
    def concurrent_edit(data, **kwargs):
        original(data, **kwargs)
        if json.loads(data['payload'])['requests'][RID]['status'] == 'claimed':
            rig.packet['operations'][0]['changes']['amount'] += 1
    document.update = concurrent_edit
    with pytest.raises(StateError):rig.confirm()
    assert rig.state()['status'] == 'claimed' and 'validation' not in rig.state()
    before = deepcopy(rig.db.data)
    with pytest.raises(StateError):rig.confirm()
    assert rig.db.data == before


@pytest.mark.parametrize('stage', ['prepare', 'post'])
def test_independent_reader_outage_never_uses_cached_or_browser_snapshot(rig, stage):
    if stage == 'post':rig.authenticate()
    before = deepcopy(rig.db.data)
    rig.gateway.read_case = lambda _: (_ for _ in ()).throw(TimeoutError('synthetic read unavailable'))
    with pytest.raises(StateError):
        if stage == 'prepare':rig.gateway.prepare(rig.packet['tx'].import_id, request_id=RID)
        else:rig.confirm()
    assert rig.db.data == before
