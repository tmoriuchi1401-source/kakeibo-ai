"""Disposable synthetic ports. No production adapter, credential or writer."""
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

from app.receipt_confirmation_contract import FIXTURE, encoded, token
from app.receipt_confirmation_draft_request import DraftReaders, prepare_inputs
from app.receipt_reimport_production import digest as native_digest
from app.unified_receipt_purposes import PurposeBoundary, VerifiedPost, VerifiedRead
from app.unified_receipt_review import digest
from app.unified_receipt_write import ConditionalRequests
from app.iphone_accounting_gate import AccountingProbeRequest, SCHEMA as SHARED_SCHEMA
from staging_shadow.receipt_draft_contract import IsolatedDraftStore, IsolatedDraftContract
from accounting_gate_fixtures import reader_for
from iphone_draft_request_design_examples import Example
from test_receipt_reimport_production import fixtures
from test_legacy_contract_poc import mem, NOW
from test_pdf_intake_authority import OWNER

RAW = b'fully-synthetic-legacy-confirmation-source'


class SourceBytes:
    synthetic_memory_only = True
    def __init__(self, sid, revision): self.sid, self.revision, self.raw = sid, revision, RAW
    def revision_bytes(self, sid, revision):
        assert (sid, revision) == (self.sid, self.revision)
        return self.raw


def set_value(port, value):
    raw, tag = port.read_versioned()
    port.replace_versioned(raw, tag, encoded(value))


def rebuild(h, *, fresh=False):
    h.store = IsolatedDraftStore(h.native, h.manifest, h.spid, parent_digest=h.pin,
        enabled=True, isolated_synthetic=True)
    h.contract = IsolatedDraftContract(h.store, h.shared, h.auth, h.readers, clock=lambda: NOW,
        fresh_process=fresh, enabled=True, isolated_synthetic=True)
    return h


def harness(kind, monkeypatch):
    f = Example(kind, monkeypatch)
    v1, _, _ = fixtures(); legacy = deepcopy(v1.value)
    if kind == 'normal':
        old = legacy['manifest']['sources'][0]; del legacy['records'][old['source_id']]
        legacy['manifest']['sources'][0] = dict(f.baseline['confirmation_items'][f.key]['source'], classification='normal', pages=1)
        legacy['records'][f.sid] = deepcopy(f.baseline['records'][f.sid])
    legacy['confirmation_items'] = deepcopy(f.baseline['confirmation_items'])
    h = SimpleNamespace(f=f, manifest=native_digest(legacy['manifest']), spid='synthetic-sheet', legacy=legacy)
    parent = dict(schema='receipt-reimport-v2', revision=0, legacy=legacy, operations={})
    h.pin = digest(parent); h.native = mem(parent)
    h.shared = ConditionalRequests(mem(dict(schema='unified-receipt-requests-v1', revision=0, operations={})),
        'synthetic-draft-shared', synthetic_accounting=True)
    h.store = IsolatedDraftStore(h.native, h.manifest, h.spid, parent_digest=h.pin,
        enabled=True, isolated_synthetic=True)
    h.store.inherit_memory_v2(expected_revision=0)
    draft = f.draft(); draft.pop('tag'); draft['owner'] = OWNER
    source = {k: v for k, v in f.source.items() if k in {
        'source_id', 'sha256', 'native_source_version', 'drive_revision_id', 'drive_numeric_version', 'source_kind', 'page_count'}}
    h.ports = dict(draft=mem(draft), source=mem(dict(source=source,
        privacy_state='general-eligible' if kind == 'normal' else 'manual-medical')),
        categories=mem(dict(pairs=[list(p) for p in f.categories], revision=f.category_revision)),
        ledger=mem(dict(complete=True, snapshot=f.before)), authority=mem({}),
        source_bytes=SourceBytes(f.sid, source['drive_revision_id']))
    h.readers = DraftReaders(**h.ports, ownership=lambda: h.own.reader.observe('legacy_confirmation', 'u1'))
    inputs, before, plan = prepare_inputs(legacy, kind, f.case_id, OWNER, token(*h.native.read_versioned()),
        '2026-10-19 00:00:00', h.readers)
    h.own = reader_for('legacy_confirmation', source, RAW, [], dict(namespace='receipt_confirmation', scope='whole',
        unit_id=None, receipt_index=None, input_snapshot=digest(inputs), authority_revision=7,
        ledger_targets=[r[0] for _, r in plan], case_id=f.case_id))
    o = h.own.reader.observe('legacy_confirmation', 'u1').value()
    rid = str(uuid4())
    def verify(context, purpose, request, binding, revision):
        p = VerifiedPost(OWNER, purpose, request, binding, revision, NOW, NOW+600, csrf_valid=True, session_active=True)
        return VerifiedPost(**dict(asdict(p), **(context or {})))
    h.auth = PurposeBoundary(OWNER, verify,
        lambda context: VerifiedRead(**dict(dict(actor_id=OWNER, expires_at=NOW+600, session_active=True), **(context or {}))),
        lambda: [], clock=lambda: NOW)
    p = verify(None, 'ACCOUNTING_POST', rid, digest(inputs), 0)
    authority = dict(mode=FIXTURE, actor=OWNER, owner=OWNER, purpose='ACCOUNTING_POST', request_id=rid,
        input_fingerprint=digest(inputs), revision=7, issued_at=NOW, expires_at=NOW+600, revoked=False,
        authenticated_action_digest=digest(asdict(p)), scope_digest=digest([o['operation_identity'], source, plan, before]))
    h.c = dict(mode=FIXTURE, kind=kind, case_id=f.case_id, review_id=f.key, operation=o['operation_identity'],
        request_id=rid, shared_request_id=str(uuid4()), owner=OWNER, source=source, inputs=inputs, plan=plan,
        before=before, authority=authority, ownership=o)
    rehash(h.c); set_value(h.ports['authority'], authority)
    return rebuild(h, fresh=True)


def rehash(c):
    c['fingerprint'] = digest({k: v for k, v in c.items() if k != 'fingerprint'})
    return c


def shared_claim(h):
    h.contract.claim_fixture(h.c, None, expected_shared_revision=h.shared.read()[0]['revision'])


def run(h, phase, context=None, revision=None):
    revision = h.store.read()[0].get('revision', 0) if revision is None else revision
    getattr(h.contract, dict(accept='accept_fixture', consume='consume', start='start')[phase])(
        h.c, context, expected_revision=revision)


def stage(h, phase):
    if phase in {'consume', 'start'}: run(h, 'accept'); shared_claim(h)
    if phase == 'start': run(h, 'consume')
