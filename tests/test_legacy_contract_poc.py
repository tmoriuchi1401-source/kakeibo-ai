"""Fully synthetic 2C-1 evidence model; all writers and clients forbidden."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.drive_run_state import StateError
from app.iphone_accounting_gate import AccountingProbeRequest, SCHEMA as SHARED_SCHEMA
from app.pdf_intake_registry import DriveRegistryIO
from app.receipt_reimport_production import digest as native_digest
from app.unified_receipt_native import ReceiptConfirmationPort
from app.unified_receipt_purposes import PurposeBoundary, VerifiedPost
from app.unified_receipt_review import digest
from app.unified_receipt_write import ConditionalRequests
from accounting_gate_fixtures import reader_for
from test_legacy_confirmation_connection_stop import native_fixture, case_for, StoppedBeforeWriter
from test_receipt_reimport_production import fixtures as reimport_fixture
from test_unified_receipt_write import Memory
from staging_shadow.legacy_contract_poc import (NativeContractPoC, SCHEMA, FIXTURE,
    encoded, freeze_candidate, planned_after, classify_fixture)

NOW = 1792400000
OWNER = digest(['https://accounts.google.com', 'synthetic-owner'])


def mem(value):
    io = Memory()
    io.synthetic_memory_only = True
    io.raw = encoded(value)
    return io


@pytest.fixture(autouse=True)
def forbid_clients_and_writers(monkeypatch):
    import app.receipt_confirmation as native
    from app.receipt_pipeline import ReceiptPipeline
    from app.sheets import SheetsDB
    from googleapiclient import discovery
    from google.cloud import firestore
    def forbidden(*args, **kwargs):
        pytest.fail('client or writer called in contract-only PoC')
    monkeypatch.setattr(native, 'now_jst_string', lambda: '2026-10-19 00:00:00')
    monkeypatch.setattr(ReceiptPipeline, '_materialize_result', forbidden)
    monkeypatch.setattr(SheetsDB, '__init__', forbidden)
    monkeypatch.setattr(discovery, 'build', forbidden)
    monkeypatch.setattr(firestore, 'Client', forbidden)


def harness(kind):
    service, store, db, metadata, key = native_fixture(kind)
    db.rows['支出明細'].append(['synthetic-existing', '2020-01-01', 'old', 'old', 3,
        '食費', '食品', '', 'manual', '', '', '', 'active'])
    # Existing native normal snapshot must include this unrelated ledger too.
    before = [deepcopy(db.rows), deepcopy(db.rows)]
    before[1]['支出明細'][-1][4] = '=1+2'
    item = service.items[key]
    case = case_for(service, key)
    parsed = service._parsed(item)
    plan = service._plan(item, parsed, '')
    inputs = dict(old=deepcopy(item), confirmed=deepcopy(item['inputs']), parsed=parsed.model_dump(),
                  categories=[list(p) for p in service.db.categories()])
    rid, sid = str(uuid4()), str(uuid4())
    source = dict(source_id=item['source']['source_id'], sha256=item['source']['sha256'],
        native_source_version='1', drive_revision_id='synthetic-drive-revision-8', drive_numeric_version='1',
        source_kind='image', page_count=1)
    raw = b'fully-synthetic-legacy-confirmation-source'
    own = reader_for('legacy_confirmation', source, raw, [], dict(namespace='receipt_confirmation',
        scope='whole', unit_id=None, receipt_index=None, input_snapshot=digest(inputs),
        authority_revision=7, ledger_targets=[r[0] for _, r in plan]))
    o = own.reader.observe('legacy_confirmation', 'u1').value()
    def verify(context, purpose, request, binding, revision):
        value = VerifiedPost(OWNER, purpose, request, binding, revision, NOW, NOW+600,
                             csrf_valid=True, session_active=True)
        return VerifiedPost(**dict(asdict(value), **(context or {})))
    auth = PurposeBoundary(OWNER, verify, lambda _: None, lambda: [], clock=lambda: NOW)
    proof = verify(None, 'ACCOUNTING_POST', rid, digest(inputs), 0)
    authority = dict(mode=FIXTURE, actor=OWNER, owner=OWNER, purpose='ACCOUNTING_POST', request_id=rid,
        input_fingerprint=digest(inputs), revision=7, issued_at=NOW, expires_at=NOW+600,
        revoked=False, authenticated_action_digest=digest(asdict(proof)),
        scope_digest=digest([o['operation_identity'], source, plan, before]))
    port = ReceiptConfirmationPort(service, key, lambda *_: None)
    c = freeze_candidate(service, port, case, source, authority, o, before, rid, sid)
    old, _, _ = reimport_fixture()
    legacy = deepcopy(old.value)
    legacy['confirmation_items'] = deepcopy(store.value['confirmation_items'])
    native_io = mem(dict(schema=SCHEMA, revision=0, legacy=legacy, operations={}))
    journal = NativeContractPoC(native_io, old.expected_digest, old.spreadsheet_id, enabled=True)
    shared_io = Memory()
    shared_io.synthetic_memory_only = True
    shared = ConditionalRequests(shared_io, 'synthetic-common-shared', synthetic_accounting=True)
    req = AccountingProbeRequest.parse(dict(schema=SHARED_SCHEMA, synthetic_only=True, owner=OWNER,
        request_id=sid, expected_state_revision=0, operation_identity=c['operation'], ownership=o,
        native_preflight_digest=c['fingerprint']))
    v, raw, tag = shared.read()
    after = deepcopy(v)
    after['revision'] = 1
    after['operations'][req.operation_identity] = dict(request=asdict(req), fingerprint=req.fingerprint(),
        state='accepted', events=[dict(state='accepted', at=NOW)])
    shared.replace(v, raw, tag, after)
    return SimpleNamespace(service=service, db=db, store=store, key=key, metadata=metadata,
        case=case, port=port, c=c, auth=auth, journal=journal, native=native_io,
        shared=shared, ledger=mem(dict(complete=True, snapshot=before)),
        current=dict(source=c['source'], inputs=c['inputs'], authority=c['authority']))


def claim(h, journal=None):
    (journal or h.journal).claim_fixture(h.c, h.shared, h.auth, None, h.ledger, now=NOW)


def advance(h, phase):
    h.journal.advance_fixture(h.c, h.shared, phase, h.current, h.auth, None, h.ledger, now=NOW)


def observe(h, value):
    # Observation supplied by a fixture, not a writer or accounting append.
    raw, tag = h.ledger.read_versioned()
    h.ledger.replace_versioned(raw, tag, encoded(dict(complete=True, snapshot=value)))


def classify(h):
    before = [io.read_versioned() for io in (h.native, h.shared.io, h.ledger)]
    value = classify_fixture(h.journal, h.shared, h.ledger, h.c, h.current, h.service)
    assert [io.read_versioned() for io in (h.native, h.shared.io, h.ledger)] == before
    assert value['real_native_complete'] is value['accounting_permission'] is False
    h.service._write_accounting_plan.assert_not_called()
    return value['classification']


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_full_contract_is_synthetic_only_and_preserves_native_v1(kind):
    h = harness(kind)
    legacy = deepcopy(h.journal.read()[0]['legacy'])
    assert classify(h) == 'UNKNOWN'
    claim(h)
    assert classify(h) == 'NOT_EXECUTED'
    advance(h, 'writer_started')
    assert classify(h) == 'UNKNOWN'
    observe(h, planned_after(h.c['before'], h.c['plan']))
    assert classify(h) == 'UNKNOWN'
    advance(h, 'complete_fixture')
    assert classify(h) == 'COMPLETE'
    assert h.journal.read()[0]['legacy'] == legacy
    assert h.store.value['confirmation_items'][h.key]['status'] == 'waiting'
    assert h.db.writes == 0
    with pytest.raises(StateError, match='readback_only'):
        advance(h, 'writer_started')


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('field', ['actor', 'owner', 'purpose', 'request_id', 'input_fingerprint',
    'revision', 'expires_at', 'revoked', 'authenticated_action_digest', 'scope_digest'])
def test_missing_authority_never_claims(kind, field):
    h = harness(kind)
    del h.c['authority'][field]
    with pytest.raises(StateError):
        claim(h)
    assert h.native.writes == 0 and classify(h) == 'UNKNOWN'


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('change', ['source', 'inputs', 'authority', 'formula', 'existing', 'incomplete', 'native', 'shared'])
def test_mismatch_never_complete(kind, change):
    h = harness(kind)
    claim(h)
    advance(h, 'writer_started')
    full = planned_after(h.c['before'], h.c['plan'])
    observe(h, full)
    advance(h, 'complete_fixture')
    h.current = deepcopy(h.current)
    if change == 'source': h.current['source']['drive_revision_id'] = 'changed'
    elif change == 'inputs': h.current['inputs']['confirmed'][-1] = 'changed'
    elif change == 'authority': h.current['authority']['revision'] += 1
    elif change in {'formula', 'existing'}:
        full[1 if change == 'formula' else 0]['支出明細'][0][4] = 'changed'
        observe(h, full)
    elif change == 'incomplete': h.ledger.raw = encoded(dict(complete=False, snapshot=full))
    elif change == 'native': h.native.raw = h.native.raw.replace(b'complete_fixture', b'writer_started')
    elif change == 'shared': h.shared.io.raw = encoded(dict(schema='unified-receipt-requests-v1', revision=0, operations={}))
    assert classify(h) in {'UNKNOWN', 'PARTIAL'}


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('phase', ['claim', 'start', 'completion'])
def test_lost_response_is_readback_only_no_retry(kind, phase):
    h = harness(kind)
    if phase != 'claim': claim(h)
    if phase == 'completion':
        advance(h, 'writer_started')
        observe(h, planned_after(h.c['before'], h.c['plan']))
    h.native.lost = True
    writes = h.native.writes
    with pytest.raises(TimeoutError):
        claim(h) if phase == 'claim' else advance(h, 'writer_started' if phase == 'start' else 'complete_fixture')
    assert h.native.writes == writes + 1
    h.native.lost = False
    assert classify(h) == {'claim': 'NOT_EXECUTED', 'start': 'UNKNOWN', 'completion': 'COMPLETE'}[phase]
    with pytest.raises(StateError):
        claim(h) if phase == 'claim' else advance(h, 'writer_started' if phase == 'start' else 'complete_fixture')
    assert h.native.writes == writes + 1


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('count', [2, 16])
def test_distinct_instances_one_atomic_claim(kind, count):
    h = harness(kind)
    barrier = Barrier(count)
    original = h.native.replace_versioned
    def raced(*args):
        barrier.wait(timeout=10)
        return original(*args)
    h.native.replace_versioned = raced
    journals = [NativeContractPoC(h.native, h.journal.manifest_digest, h.journal.spreadsheet_id,
                               enabled=True) for _ in range(count)]
    def run(j):
        try: claim(h, j); return True
        except StateError: return False
    with ThreadPoolExecutor(max_workers=count) as pool:
        results = list(pool.map(run, journals))
    assert results.count(True) == 1 and h.native.writes == 1
    assert classify(h) == 'NOT_EXECUTED'


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_old_applied_and_pending_are_unknown_not_promoted(kind):
    h = harness(kind)
    with pytest.raises(StoppedBeforeWriter): h.service.apply_confirmations(keys=[h.key])
    pending = deepcopy(h.service.items[h.key])
    for title, row in pending['plan']:
        existing = next((i for i, r in enumerate(h.db.rows[title]) if r[0] == row[0]), None)
        if existing is None: h.db.rows[title].append(deepcopy(row))
        else: h.db.rows[title][existing] = deepcopy(row)
    assert h.service.apply_confirmations(keys=[h.key]) == 0
    assert h.service.items[h.key]['status'] == 'applied'
    h.service._write_accounting_plan.reset_mock()
    assert classify(h) == 'UNKNOWN'  # Exact old ledger/applied is not new evidence.
    for status in ('pending', 'applied'):
        v = h.journal.read()[0]
        v['legacy']['confirmation_items'][h.key] = dict(pending, status=status)
        h.native.raw = encoded(v)
        with pytest.raises(StateError, match='legacy_pending_or_applied'): claim(h)
        assert h.native.writes == 0


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_partial_keeps_fence_and_cannot_reclaim(kind):
    h = harness(kind)
    claim(h)
    advance(h, 'writer_started')
    observe(h, planned_after(h.c['before'], h.c['plan'][:1]))
    assert classify(h) == 'PARTIAL'
    with pytest.raises(StateError): claim(h)
    assert len(h.shared.read()[0]['operations']) == 1 and h.native.writes == 2


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_stale_etag_and_exact_readback_failure(kind):
    h = harness(kind)
    v, raw, tag = h.journal.read()
    after = deepcopy(v); after['revision'] += 1
    h.native.revision += 1
    with pytest.raises(StateError, match='HTTP_412'):
        h.journal.replace(v, raw, tag, after)
    assert h.native.writes == 0
    original = h.native.replace_versioned
    def corrupt(*args):
        original(*args)
        value = h.journal.read()[0]
        value['revision'] += 1
        h.native.raw = encoded(value)
    h.native.replace_versioned = corrupt
    with pytest.raises(StateError, match='cas_readback_unknown'): claim(h)
    assert h.native.writes == 1


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('field,value', [('csrf_valid', False), ('session_active', False),
    ('actor_id', '0'*64), ('purpose', 'METADATA_REVIEW'), ('expires_at', NOW)])
def test_existing_post_gate_is_required(kind, field, value):
    h = harness(kind)
    with pytest.raises(StateError):
        h.journal.claim_fixture(h.c, h.shared, h.auth, {field: value}, h.ledger, now=NOW)
    assert h.native.writes == 0


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_no_ledger_only_not_executed_and_expired_start(kind):
    h = harness(kind)
    assert classify(h) == 'UNKNOWN'
    claim(h)
    h.ledger.revision += 1  # Same values, different observed generation.
    assert classify(h) == 'UNKNOWN'
    with pytest.raises(StateError, match='before_changed'): advance(h, 'writer_started')
    h.ledger.revision -= 1
    with pytest.raises(StateError, match='expired'):
        h.journal.advance_fixture(h.c, h.shared, 'writer_started', h.current, h.auth, None, h.ledger, now=NOW+600)


def test_plain_v1_is_not_automatically_wrapped_or_written():
    old, _, _ = reimport_fixture()
    io = mem(old.value)
    journal = NativeContractPoC(io, old.expected_digest, old.spreadsheet_id, enabled=True)
    with pytest.raises(StateError, match='state_UNKNOWN'): journal.read()
    assert io.writes == 0


def test_real_existing_drive_conditional_io_on_fake_session_only():
    # Constructor intentionally bypassed: no AuthorizedSession/OAuth/key/client.
    io = object.__new__(DriveRegistryIO)
    io.writable = True; io.deadline = None
    io.config = dict(file_id='synthetic-existing-file')
    io.read_versioned = lambda: (b'old', '"strong-1"')
    calls = []
    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
    io._request = request
    io.replace_versioned(b'old', '"strong-1"', b'new')
    assert len(calls) == 1 and calls[0][0] == 'PUT'
    assert calls[0][2]['headers']['If-Match'] == '"strong-1"'
    assert calls[0][2]['data'] == b'new'
    with pytest.raises(StateError, match='HTTP_412'):
        io.replace_versioned(b'old', '"stale"', b'new')
    assert len(calls) == 1


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_different_request_cannot_overwrite_existing_pending(kind):
    h = harness(kind)
    claim(h)
    before = h.native.read_versioned()
    c = deepcopy(h.c)
    c['request_id'] = c['authority']['request_id'] = str(uuid4())
    proof = h.auth.verify_post(None, 'ACCOUNTING_POST', c['request_id'], digest(c['inputs']), 0)
    c['authority']['authenticated_action_digest'] = digest(asdict(proof))
    c['fingerprint'] = digest({k: v for k, v in c.items() if k != 'fingerprint'})
    with pytest.raises(StateError):
        h.journal.claim_fixture(c, h.shared, h.auth, None, h.ledger, now=NOW)
    assert h.native.read_versioned() == before


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('field', ['mode', 'authenticated_action_digest'])
def test_acknowledgement_or_forged_fixture_proof_is_not_authority(kind, field):
    h = harness(kind)
    h.c['authority'][field] = 'synthetic-owner-acknowledgements-v1' if field == 'mode' else '0'*64
    h.c['fingerprint'] = digest({k: v for k, v in h.c.items() if k != 'fingerprint'})
    with pytest.raises(StateError): claim(h)
    assert h.native.writes == 0


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('held', ['G103', 'H301', 'SYN-G103', 'SYN-H301', 'old_p1'])
def test_held_cases_never_freeze_financial_candidate(kind, held):
    from app.unified_receipt_write import OLD_P1_SOURCE, OLD_P1_HASH
    h = harness(kind)
    if held == 'old_p1':
        h.case.identity.update(source_file_id=OLD_P1_SOURCE, source_content_hash=OLD_P1_HASH)
        h.case.identity['page_numbers'] = [1]
    else:
        h.case.legacy_id = held
    with pytest.raises(StateError):
        freeze_candidate(h.service, h.port, h.case, h.c['source'], h.c['authority'],
                         h.c['ownership'], h.c['before'], h.c['request_id'], h.c['shared_request_id'])
    assert h.native.writes == 0


@pytest.mark.parametrize('index', [0, 1, 2, 3])
def test_medical_partial_input_never_freezes(index):
    h = harness('medical')
    h.service.items[h.key]['inputs'][index] = ''
    h.case.fields[index][2] = ''
    with pytest.raises((StateError, ValueError)):
        freeze_candidate(h.service, h.port, h.case, h.c['source'], h.c['authority'],
                         h.c['ownership'], h.c['before'], h.c['request_id'], h.c['shared_request_id'])
    assert h.native.writes == 0


@pytest.mark.parametrize('status', [412, 503])
def test_existing_drive_http_failure_has_one_put_no_fallback(status):
    io = object.__new__(DriveRegistryIO)
    io.writable = True; io.deadline = None
    io.config = dict(file_id='synthetic-existing-file')
    io.read_versioned = lambda: (b'old', '"1"')
    request = Mock(return_value=SimpleNamespace(status_code=status, content=b''))
    io.http = SimpleNamespace(request=request)
    with pytest.raises(StateError): io.replace_versioned(b'old', '"1"', b'new')
    request.assert_called_once()
    assert request.call_args.args[0] == 'PUT'
    assert request.call_args.kwargs['headers']['If-Match'] == '"1"'
    assert request.call_args.kwargs['allow_redirects'] is False


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('phase', ['claimed', 'writer_started', 'complete_fixture'])
def test_separate_process_reconstructs_readonly_synthetic_evidence(kind, phase):
    h = harness(kind)
    claim(h)
    if phase != 'claimed': advance(h, 'writer_started')
    if phase == 'complete_fixture':
        observe(h, planned_after(h.c['before'], h.c['plan']))
        advance(h, 'complete_fixture')
    value = dict(c=h.c, current=h.current, manifest=h.journal.manifest_digest,
                 sid=h.journal.spreadsheet_id, stores=[dict(raw=io.raw.decode(), revision=io.revision)
                     for io in (h.native, h.shared.io, h.ledger)])
    script = '''
import socket, sys, json
def denied(*a, **k): raise AssertionError('child network forbidden')
socket.socket.connect = socket.socket.connect_ex = denied
sys.path.insert(0, 'tests')
from types import SimpleNamespace
from test_unified_receipt_write import Memory
from staging_shadow.legacy_contract_poc import NativeContractPoC, classify_fixture
from app.unified_receipt_write import ConditionalRequests
v=json.load(sys.stdin); stores=[]
for part in v['stores']:
    io=Memory(); io.synthetic_memory_only=True
    io.raw=part['raw'].encode(); io.revision=part['revision']; stores.append(io)
j=NativeContractPoC(stores[0],v['manifest'],v['sid'],enabled=True)
s=ConditionalRequests(stores[1],'synthetic-common-shared',synthetic_accounting=True)
result=classify_fixture(j,s,stores[2],v['c'],v['current'],SimpleNamespace())
print(json.dumps(dict(result=result,writes=[io.writes for io in stores])))
'''
    before = [io.read_versioned() for io in (h.native, h.shared.io, h.ledger)]
    completed = subprocess.run([sys.executable, '-c', script], input=json.dumps(value),
        text=True, capture_output=True, check=True, timeout=30, cwd=Path(__file__).resolve().parents[1])
    result = json.loads(completed.stdout)
    assert result['result']['classification'] == dict(claimed='NOT_EXECUTED', writer_started='UNKNOWN',
                                                     complete_fixture='COMPLETE')[phase]
    assert result['writes'] == [0, 0, 0]
    assert [io.read_versioned() for io in (h.native, h.shared.io, h.ledger)] == before


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('change', ['source', 'inputs', 'authority', 'session', 'shared_complete'])
def test_change_after_claim_prevents_start_evidence(kind, change):
    h = harness(kind)
    claim(h)
    before = h.native.read_versioned()
    h.current = deepcopy(h.current)
    context = None
    if change == 'source': h.current['source']['native_source_version'] = '2'
    elif change == 'inputs': h.current['inputs']['confirmed'][-1] = 'changed'
    elif change == 'authority': h.current['authority']['revoked'] = True
    elif change == 'session': context = dict(session_active=False)
    else:
        req = h.shared.parse_request(h.shared.read()[0]['operations'][h.c['operation']]['request'])
        for state in ('executing', 'awaiting_readback', 'complete'):
            h.shared.transition(req, state, NOW)
        assert classify(h) == 'UNKNOWN'
    with pytest.raises(StateError):
        h.journal.advance_fixture(h.c, h.shared, 'writer_started', h.current, h.auth, context, h.ledger, now=NOW)
    assert h.native.read_versioned() == before


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_independent_drive_observation_cannot_be_filled_from_native_version(kind):
    h = harness(kind)
    source = deepcopy(h.c['source'])
    source['drive_numeric_version'] = '2'
    with pytest.raises(StateError, match='drive_observation_binding'):
        freeze_candidate(h.service, h.port, h.case, source, h.c['authority'], h.c['ownership'],
                         h.c['before'], h.c['request_id'], h.c['shared_request_id'])
