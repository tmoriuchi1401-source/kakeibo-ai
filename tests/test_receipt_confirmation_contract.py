"""Native v2 isolation: never dispatch a writer or reach a Google client."""
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
from app.iphone_accounting_gate import AccountingProbeRequest, SCHEMA as PROBE_SCHEMA
from app.receipt_confirmation_contract import (SCHEMA, PROVIDER, IsolatedConfirmationContract,
    IsolatedReimportStore, pending_item, planned_after, token, validate_request)
from app.receipt_reimport_production import ReimportStore
from app.unified_receipt_review import digest
from app.unified_receipt_write import ConditionalRequests
from accounting_gate_fixtures import reader_for
from test_legacy_contract_poc import (harness as old_harness, mem, NOW, forbid_clients_and_writers)
from test_unified_receipt_write import Memory


class Crash(BaseException):
    pass


def harness(kind):
    h = old_harness(kind)
    h.c = deepcopy(h.c)
    c = h.c
    own = reader_for('legacy_confirmation', c['source'], b'fully-synthetic-legacy-confirmation-source', [],
        dict(namespace='receipt_confirmation', scope='whole', unit_id=None, receipt_index=None,
             input_snapshot=digest(c['inputs']), authority_revision=c['authority']['revision'],
             ledger_targets=[r[0] for _, r in c['plan']], case_id=c['case_id']))
    c['ownership'] = own.reader.observe('legacy_confirmation', 'u1').value()
    c['fingerprint'] = digest({k: v for k, v in c.items() if k != 'fingerprint'})
    validate_request(c)
    h.shared = ConditionalRequests(mem(dict(schema='unified-receipt-requests-v1', revision=0, operations={})),
                                  'synthetic-shared', synthetic_accounting=True)
    req = AccountingProbeRequest.parse(dict(schema=PROBE_SCHEMA, synthetic_only=True, owner=c['owner'],
        request_id=c['shared_request_id'], expected_state_revision=0, operation_identity=c['operation'],
        ownership=c['ownership'], native_preflight_digest=c['fingerprint']))
    v, raw, tag = h.shared.read()
    after = deepcopy(v); after['revision'] += 1
    after['operations'][c['operation']] = dict(request=asdict(req), fingerprint=req.fingerprint(),
        state='accepted', events=[dict(state='accepted', at=NOW)])
    h.shared.replace(v, raw, tag, after)
    legacy = h.journal.read()[0]['legacy']
    h.manifest = h.journal.manifest_digest; h.sid = h.journal.spreadsheet_id
    h.native = mem(dict(schema=SCHEMA, revision=0, legacy=legacy, operations={}))
    h.current = dict(source=c['source'], inputs=c['inputs'], authority=c['authority'])
    h.readers = SimpleNamespace(synthetic_memory_only=True, current=lambda: deepcopy(h.current),
        ownership=lambda: own.reader.observe('legacy_confirmation', 'u1'), native=lambda: (h.service, h.port, h.case),
        ledger=h.ledger, before_observation=token(*h.ledger.read_versioned()))
    h.own = own; h.writer = Mock(side_effect=AssertionError('writer forbidden'))
    rebuild(h)
    return h


def rebuild(h):
    h.store2 = ReimportStore.isolated_v2(h.native, h.manifest, h.sid, enabled=True, isolated_synthetic=True)
    h.contract = IsolatedConfirmationContract(h.store2, h.shared, h.auth, h.readers,
        clock=lambda: NOW, enabled=True, isolated_synthetic=True)
    return h


def run(h, phase, revision=None):
    rev = h.store2.read()[0].get('revision', 0) if revision is None else revision
    if phase == 'accept': h.contract.accept_fixture(h.c, None, expected_revision=rev)
    elif phase == 'consume': h.contract.consume(h.c, None, expected_revision=rev)
    elif phase == 'start': h.contract.start(h.c, None, expected_revision=rev)
    else:
        r = h.store2.read()[0]['operations'][h.c['operation']]
        raw, tag = h.ledger.read_versioned()
        certificate = dict(mode=PROVIDER, fingerprint=h.c['fingerprint'], start_digest=digest(r['start']),
            pending_digest=digest(r['pending']), snapshot_digest=digest(planned_after(h.c['before'], h.c['plan'])),
            observation=token(raw, tag), at=NOW)
        h.contract.complete_fixture(h.c, certificate, expected_revision=rev)


def observe_full(h, prefix=None):
    value = planned_after(h.c['before'], h.c['plan'] if prefix is None else h.c['plan'][:prefix])
    raw, tag = h.ledger.read_versioned()
    h.ledger.replace_versioned(raw, tag, json.dumps(dict(complete=True, snapshot=value)).encode())


def inspect(h):
    before = [io.read_versioned() for io in (h.native, h.shared.io, h.ledger)]
    result = h.contract.inspect(h.c)
    assert result['writer_dispatches'] == 0 and result['real_native_complete'] is result['accounting_permission'] is False
    assert [io.read_versioned() for io in (h.native, h.shared.io, h.ledger)] == before
    h.writer.assert_not_called(); h.service._write_accounting_plan.assert_not_called()
    return result['classification']


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_native_v2_positive_contract_without_writer(kind):
    h = harness(kind); legacy = deepcopy(h.store2.value)
    assert inspect(h) == 'UNKNOWN'
    run(h, 'accept'); assert inspect(h) == 'UNKNOWN'
    run(h, 'consume')
    r = h.store2.read()[0]['operations'][h.c['operation']]
    assert r['phase'] == 'pending' and r['pending'] == pending_item(h.c)
    assert r['consume']['pending_digest'] == digest(r['pending']) and r['start'] is None
    assert h.native.writes == 2 and h.store2.value == legacy
    assert inspect(h) == 'NOT_EXECUTED'
    run(h, 'start'); assert inspect(h) == 'UNKNOWN'
    with pytest.raises(StateError, match='writer_disabled'): h.contract.dispatch(h.writer)
    observe_full(h); assert inspect(h) == 'UNKNOWN'
    run(h, 'complete'); assert inspect(h) == 'COMPLETE'
    assert h.native.writes == 4 and h.store2.value == legacy and h.db.writes == 0
    assert h.service.items[h.key]['status'] == 'waiting'


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('field', ['actor', 'owner', 'purpose', 'request_id', 'input_fingerprint', 'revision',
    'issued_at', 'expires_at', 'revoked', 'authenticated_action_digest', 'scope_digest', 'mode'])
def test_missing_authority_never_saves(kind, field):
    h = harness(kind); del h.c['authority'][field]
    with pytest.raises(StateError): run(h, 'accept')
    assert h.native.writes == 0 and inspect(h) == 'UNKNOWN'


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('field,value', [('actor', '0'*64), ('owner', '0'*64), ('purpose', 'METADATA_REVIEW'),
    ('expires_at', NOW), ('issued_at', NOW+1), ('revoked', True), ('mode', 'synthetic-owner-acknowledgements-v1')])
def test_wrong_or_expired_authority_never_saves(kind, field, value):
    h = harness(kind); h.c['authority'][field] = value
    h.c['fingerprint'] = digest({k: v for k, v in h.c.items() if k != 'fingerprint'})
    with pytest.raises(StateError): run(h, 'accept')
    assert h.native.writes == 0


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('phase', ['accept', 'consume', 'start'])
@pytest.mark.parametrize('change', ['source', 'inputs', 'version', 'authority', 'session', 'ledger'])
def test_revalidation_at_each_boundary(kind, phase, change):
    h = harness(kind)
    for before in ('accept', 'consume')[:('accept', 'consume', 'start').index(phase)]: run(h, before)
    writes = h.native.writes
    h.current = deepcopy(h.current)
    if change == 'source': h.current['source']['sha256'] = '0'*64
    elif change == 'inputs': h.current['inputs']['confirmed'][-1] = 'changed'
    elif change == 'version': h.current['source']['drive_revision_id'] = 'new-revision'
    elif change == 'authority': h.current['authority']['revision'] += 1
    elif change == 'session':
        verify = h.auth.verify_post
        h.auth.verify_post = lambda *args: type(verify(*args))(**dict(asdict(verify(*args)), session_active=False))
    else: h.ledger.revision += 1
    with pytest.raises(StateError): run(h, phase)
    assert h.native.writes == writes


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('point', ['before_qualification', 'after_qualification', 'before_consume', 'after_consume',
    'before_start', 'after_start', 'before_completion', 'after_completion'])
def test_crash_at_native_boundaries_is_read_only_after_restart(kind, point):
    h = harness(kind)
    def crash(p):
        if p == point: raise Crash()
    h.contract.checkpoint = crash
    with pytest.raises(Crash):
        for phase in ('accept', 'consume', 'start', 'complete'):
            if phase == 'complete': observe_full(h)
            run(h, phase)
    state = h.store2.read()[0]
    record = state['operations'].get(h.c['operation'])
    if record and record['phase'] != 'qualified':
        assert record['consume'] and record['pending']  # Never one without the other.
    writes = h.native.writes; rebuild(h)
    assert inspect(h) == ('COMPLETE' if point == 'after_completion' else 'UNKNOWN')
    with pytest.raises(StateError): run(h, 'start')
    assert h.native.writes == writes and len(h.shared.read()[0]['operations']) == 1


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('phase', ['accept', 'consume', 'start', 'complete'])
@pytest.mark.parametrize('fault', ['lost', '412', 'readback', 'unchanged_tag'])
def test_cas_ambiguity_stops_without_followup_write(kind, phase, fault):
    h = harness(kind)
    for before in ('accept', 'consume', 'start')[:('accept', 'consume', 'start', 'complete').index(phase)]:
        run(h, before)
    if phase == 'complete': observe_full(h)
    writes = h.native.writes
    original = h.native.replace_versioned
    def injected(*args):
        if fault == '412': raise StateError('HTTP_412')
        old_revision = h.native.revision
        original(*args)
        if fault == 'lost': raise TimeoutError('synthetic response lost')
        if fault == 'unchanged_tag': h.native.revision = old_revision
        if fault == 'readback':
            value = json.loads(h.native.raw); value['revision'] += 1
            h.native.raw = json.dumps(value).encode()
    h.native.replace_versioned = injected
    with pytest.raises(StateError, match='CAS_UNKNOWN'): run(h, phase)
    assert h.store2.uncertain is True and inspect(h) == 'UNKNOWN'
    assert h.native.writes == writes + (fault != '412')
    after = h.native.writes
    with pytest.raises(StateError, match='UNKNOWN_read_only'): run(h, phase)
    assert h.native.writes == after


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('count', [2, 16])
def test_independent_instances_one_conditional_update_no_lost_update(kind, count):
    h = harness(kind); barrier = Barrier(count)
    original = h.native.replace_versioned
    def race(*args):
        barrier.wait(timeout=15)
        return original(*args)
    h.native.replace_versioned = race
    contracts = []
    for _ in range(count):
        store = ReimportStore.isolated_v2(h.native, h.manifest, h.sid, enabled=True, isolated_synthetic=True)
        contracts.append(IsolatedConfirmationContract(store, h.shared, h.auth, h.readers,
            clock=lambda: NOW, enabled=True, isolated_synthetic=True))
    def call(contract):
        try: contract.accept_fixture(h.c, None, expected_revision=0); return contract
        except StateError: return None
    with ThreadPoolExecutor(max_workers=count) as pool: outcomes = list(pool.map(call, contracts))
    winners = [c for c in outcomes if c is not None]
    assert len(winners) == 1 and h.native.writes == 1
    assert h.store2.read()[0]['revision'] == 1
    assert h.store2.read()[0]['operations'][h.c['operation']]['request'] == h.c
    assert sum(c.store.uncertain for c in contracts) == count-1


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('phase', ['pending', 'writer_started', 'complete_fixture'])
def test_evidence_cannot_be_rolled_back_or_fence_removed(kind, phase):
    h = harness(kind)
    for name in ('accept', 'consume', 'start', 'complete')[:{'pending': 2, 'writer_started': 3, 'complete_fixture': 4}[phase]]:
        if name == 'complete': observe_full(h)
        run(h, name)
    v, raw, tag = h.store2.read(); after = deepcopy(v); after['revision'] += 1
    r = after['operations'][h.c['operation']]
    r.update(phase='qualified', claim=None, consume=None, pending=None, unstarted=None, start=None, completion=None)
    with pytest.raises(StateError): h.store2._commit(v, raw, tag, after, v['revision'])
    del after['operations'][h.c['operation']]
    with pytest.raises(StateError, match='fence_removal'): h.store2._commit(v, raw, tag, after, v['revision'])


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('status', ['pending', 'applied'])
def test_old_v1_and_legacy_pending_never_migrated(kind, status):
    h = harness(kind); value = h.store2.value
    value['confirmation_items'][h.key]['status'] = status
    h.native.raw = json.dumps(value).encode()
    assert h.store2.value == value and inspect(h) == 'UNKNOWN'
    with pytest.raises(StateError, match='v1_read_only'): run(h, 'accept')
    h.native.raw = json.dumps(dict(schema=SCHEMA, revision=0, legacy=value, operations={})).encode()
    with pytest.raises(StateError, match='legacy_pending_or_applied'): run(h, 'accept')
    assert h.native.writes == 0


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('missing', ['start', 'consume', 'pending', 'completion', 'qualification'])
def test_all_rows_or_old_applied_do_not_complete_without_full_evidence(kind, missing):
    h = harness(kind)
    for phase in ('accept', 'consume', 'start'): run(h, phase)
    observe_full(h); assert inspect(h) == 'UNKNOWN'
    run(h, 'complete'); assert inspect(h) == 'COMPLETE'
    v = h.store2.read()[0]; v['operations'][h.c['operation']][missing] = None
    h.native.raw = json.dumps(v).encode()
    assert inspect(h) == 'UNKNOWN'


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_partial_has_no_redispatch_or_unlock(kind):
    h = harness(kind)
    for phase in ('accept', 'consume', 'start'): run(h, phase)
    observe_full(h, prefix=1)
    assert inspect(h) == 'PARTIAL'
    writes = h.native.writes
    with pytest.raises(StateError): run(h, 'start')
    with pytest.raises(StateError): run(h, 'complete')
    assert h.native.writes == writes and len(h.shared.read()[0]['operations']) == 1


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('held', ['G103', 'H301', 'SYN-G103', 'SYN-H301', 'old_p1'])
def test_held_case_not_eligible(kind, held):
    from app.unified_receipt_write import OLD_P1_SOURCE
    h = harness(kind)
    if held == 'old_p1': h.c['source']['source_id'] = OLD_P1_SOURCE
    else: h.case.legacy_id = held
    with pytest.raises(StateError): run(h, 'accept')
    assert h.native.writes == 0


@pytest.mark.parametrize('field', [0, 1, 2, 3, 'automatic'])
def test_medical_partial_or_automatic_input_refused(field):
    h = harness('medical')
    if field == 'automatic': h.service.items[h.key]['decision_origin'] = 'automatic'
    else:
        h.service.items[h.key]['inputs'][field] = ''
        h.case.fields[field][2] = ''
    with pytest.raises((StateError, ValueError)): run(h, 'accept')
    assert h.native.writes == 0


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_request_replay_and_different_content_cannot_overwrite(kind):
    h = harness(kind); run(h, 'accept')
    with pytest.raises(StateError): run(h, 'accept')
    changed = deepcopy(h.c); changed['inputs']['confirmed'][-1] = 'different'
    with pytest.raises(StateError): h.contract.consume(changed, None, expected_revision=1)
    assert h.native.writes == 1


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('phase', ['qualified', 'pending', 'writer_started', 'complete_fixture'])
def test_other_process_reconstructs_saved_native_state_read_only(kind, phase):
    h = harness(kind)
    for name in ('accept', 'consume', 'start', 'complete')[:{'qualified': 1, 'pending': 2, 'writer_started': 3, 'complete_fixture': 4}[phase]]:
        if name == 'complete': observe_full(h)
        run(h, name)
    value = dict(c=h.c, current=h.current, manifest=h.manifest, sid=h.sid, stores=[
        dict(raw=io.raw.decode(), revision=io.revision) for io in (h.native, h.shared.io, h.ledger)])
    script = '''
import socket, sys, json
def denied(*a, **k): raise AssertionError('network forbidden')
socket.socket.connect = socket.socket.connect_ex = denied
sys.path.insert(0,'tests')
from types import SimpleNamespace
from test_unified_receipt_write import Memory
from app.receipt_reimport_production import ReimportStore
from app.receipt_confirmation_contract import IsolatedConfirmationContract
from app.unified_receipt_purposes import PurposeBoundary
from app.unified_receipt_write import ConditionalRequests
v=json.load(sys.stdin); stores=[]
for part in v['stores']:
    io=Memory(); io.synthetic_memory_only=True; io.raw=part['raw'].encode(); io.revision=part['revision']; stores.append(io)
native=ReimportStore.isolated_v2(stores[0],v['manifest'],v['sid'],enabled=True,isolated_synthetic=True)
shared=ConditionalRequests(stores[1],'synthetic-shared',synthetic_accounting=True)
readers=SimpleNamespace(synthetic_memory_only=True,current=lambda:v['current'],
    ownership=lambda:SimpleNamespace(value=lambda:v['c']['ownership']),ledger=stores[2])
auth=PurposeBoundary(v['c']['owner'],denied,denied,lambda:[],clock=lambda:1792400000)
c=IsolatedConfirmationContract(native,shared,auth,readers,clock=lambda:1792400000,enabled=True,isolated_synthetic=True)
print(json.dumps(dict(result=c.inspect(v['c']),writes=[io.writes for io in stores])))
'''
    p = subprocess.run([sys.executable, '-c', script], input=json.dumps(value), text=True, capture_output=True,
        check=True, timeout=30, cwd=Path(__file__).resolve().parents[1])
    result = json.loads(p.stdout)
    assert result['result']['classification'] == ('COMPLETE' if phase == 'complete_fixture' else 'UNKNOWN')
    assert result['writes'] == [0, 0, 0]


def test_default_v1_and_explicit_isolation_are_separate():
    h = harness('normal')
    for flags in ({}, dict(enabled=True), dict(isolated_synthetic=True)):
        with pytest.raises(StateError): ReimportStore.isolated_v2(h.native, h.manifest, h.sid, **flags)
    with pytest.raises(StateError): h.store2.save(h.store2.value)
    with pytest.raises(StateError): h.store2.replace(None)


@pytest.mark.parametrize('change', ['schema', 'missing', 'revision', 'extra', 'weak', 'duplicate_json', 'nan'])
def test_strict_native_envelope(change):
    h = harness('normal'); v = h.store2.read()[0]
    if change == 'schema': v['schema'] = 'unknown'
    elif change == 'missing': del v['operations']
    elif change == 'revision': v['revision'] = True
    elif change == 'extra': v['unexpected'] = 1
    if change == 'weak': h.native.read_versioned = lambda: (h.native.raw, 'W/"0"')
    elif change == 'duplicate_json': h.native.raw = b'{"schema":"receipt-reimport-v2","schema":"other"}'
    elif change == 'nan': h.native.raw = b'{"schema":NaN}'
    else: h.native.raw = json.dumps(v).encode()
    with pytest.raises(StateError): h.store2.read()
    assert h.native.writes == 0 and inspect(h) == 'UNKNOWN'


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('phase', ['accept', 'consume', 'start', 'complete'])
def test_source_change_inside_checkpoint_is_seen_before_cas(kind, phase):
    h = harness(kind)
    for name in ('accept', 'consume', 'start')[:('accept', 'consume', 'start', 'complete').index(phase)]: run(h, name)
    if phase == 'complete': observe_full(h)
    writes = h.native.writes
    point = dict(accept='before_qualification', consume='before_consume', start='before_start', complete='before_completion')[phase]
    def change(p):
        if p == point:
            h.current = deepcopy(h.current); h.current['source']['native_source_version'] = 'changed'
    h.contract.checkpoint = change
    with pytest.raises(StateError): run(h, phase)
    assert h.native.writes == writes


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('change', ['formula', 'existing_value', 'target_id', 'duplicate', 'incomplete'])
def test_full_readback_checks_all_ids_formulas_and_existing_rows(kind, change):
    h = harness(kind)
    for phase in ('accept', 'consume', 'start'): run(h, phase)
    observe_full(h); run(h, 'complete'); assert inspect(h) == 'COMPLETE'
    value = json.loads(h.ledger.raw)
    rows = value['snapshot']
    if change == 'formula': rows[1]['支出明細'][0][4] = '=3'
    elif change == 'existing_value': rows[0]['支出明細'][0][4] = 4
    elif change == 'target_id': rows[0]['取込データ'][-1][0] = 'wrong-id'
    elif change == 'duplicate': rows[0]['取込データ'].append(deepcopy(rows[0]['取込データ'][-1]))
    else: value['complete'] = False
    h.ledger.raw = json.dumps(value).encode(); h.ledger.revision += 1
    assert inspect(h) in {'PARTIAL', 'UNKNOWN'}


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_same_source_new_native_request_is_not_new_authority(kind):
    h = harness(kind); run(h, 'accept')
    c = deepcopy(h.c)
    c['request_id'] = c['authority']['request_id'] = str(uuid4())
    p = h.auth.verify_post(None, 'ACCOUNTING_POST', c['request_id'], digest(c['inputs']), 0)
    c['authority']['authenticated_action_digest'] = digest(asdict(p))
    c['fingerprint'] = digest({k: v for k, v in c.items() if k != 'fingerprint'})
    h.current = dict(source=c['source'], inputs=c['inputs'], authority=c['authority'])
    with pytest.raises(StateError, match='request_or_source_owned'):
        h.contract.accept_fixture(c, None, expected_revision=1)
    assert h.native.writes == 1


@pytest.mark.parametrize('revision', [True, -1, '0', 1])
def test_expected_revision_is_exact(revision):
    h = harness('normal')
    with pytest.raises(StateError, match='expected_revision'): run(h, 'accept', revision=revision)
    assert h.native.writes == 0


def test_live_backend_is_refused_before_read():
    io = SimpleNamespace(read_versioned=Mock(side_effect=AssertionError('must not read')))
    with pytest.raises(StateError, match='memory_required'):
        ReimportStore.isolated_v2(io, '0'*64, 'unused', enabled=True, isolated_synthetic=True)
    io.read_versioned.assert_not_called()


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_correct_saved_completion_turns_unknown_if_source_changes_during_readback(kind):
    h = harness(kind)
    for phase in ('accept', 'consume', 'start'): run(h, phase)
    observe_full(h); run(h, 'complete')
    original = h.ledger.read_versioned
    def changed():
        h.current = deepcopy(h.current); h.current['source']['drive_numeric_version'] = '2'
        return original()
    h.ledger.read_versioned = changed
    assert inspect(h) == 'UNKNOWN'


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('lost', [False, True])
def test_actual_drive_versioned_io_injected_with_memory_http_only(kind, lost):
    from app.pdf_intake_registry import DriveRegistryIO
    from app.pdf_intake_authority import digest as drive_digest
    h = harness(kind); backend = h.native; calls = []
    config = dict(file_id='synthetic-existing-file', folder_id='synthetic-existing-folder',
        inbox_id='synthetic-existing-inbox', processed_id='synthetic-existing-processed',
        owner_digest=drive_digest('owner@example.invalid'))
    def response(value, code=200):
        data = value if isinstance(value, bytes) else json.dumps(value).encode()
        return SimpleNamespace(status_code=code, content=data, json=lambda: json.loads(data))
    def request(method, url, **kw):
        calls.append((method, url, deepcopy(kw)))
        raw, tag = backend.read_versioned()
        if method == 'PUT':
            try: backend.replace_versioned(raw, kw['headers']['If-Match'], kw['data'])
            except StateError: return response({}, 412)
            if lost: raise TimeoutError('synthetic Drive response lost')
            return response(dict(id=config['file_id'], etag=backend.read_versioned()[1]))
        if kw.get('params', {}).get('alt') == 'media': return response(raw)
        fid = url.rsplit('/', 1)[-1]
        return response(dict(id=fid, etag=tag, parents=[dict(id=config['folder_id'])], labels=dict(trashed=False),
            mimeType='application/json', owners=[dict(emailAddress='owner@example.invalid')],
            permissions=[dict(type='user', role='owner', emailAddress='owner@example.invalid'),
                         dict(type='user', role='writer', emailAddress='sa@example.invalid')]))
    io = DriveRegistryIO(config, dict(client_email='sa@example.invalid'),
        session=SimpleNamespace(request=request), writable=True)
    io.synthetic_memory_only = True  # Trusted fake HTTP, no Google/OAuth constructor.
    h.native = io; rebuild(h)
    if lost:
        with pytest.raises(StateError, match='CAS_UNKNOWN'): run(h, 'accept')
        assert inspect(h) == 'UNKNOWN'
        with pytest.raises(StateError, match='UNKNOWN_read_only'): run(h, 'consume')
        assert backend.writes == 1
    else:
        for phase in ('accept', 'consume', 'start'): run(h, phase)
        observe_full(h); run(h, 'complete')
        assert inspect(h) == 'COMPLETE' and backend.writes == 4
    puts = [c for c in calls if c[0] == 'PUT']
    assert len(puts) == backend.writes
    assert all(c[2]['headers']['If-Match'].startswith('"') for c in puts)


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('other', ['normal_auto', 'grouped_unit'])
def test_shared_auto_or_pdf_scope_cannot_be_consumed_by_legacy(kind, other):
    from app.page_receipt_model import SourceRef, stable_page
    h = harness(kind); run(h, 'accept')
    source = h.c['source']
    unit = dict(unit_id='synthetic-other-unit', receipt_index=1, members=[dict(page=1,
        identity=stable_page(SourceRef(source_file_id=source['source_id'], source_content_hash=source['sha256'],
                                      source_kind='image', page_count=1), 1), box=[0, 0, 1, 1])])
    pdf = other == 'grouped_unit'
    reader = reader_for(other, source, b'fully-synthetic-legacy-confirmation-source', [unit] if pdf else [],
        dict(namespace='pdf_unit_processing' if pdf else 'receipt_confirmation', scope='unit' if pdf else 'whole',
             unit_id=unit['unit_id'] if pdf else None, receipt_index=1 if pdf else None,
             input_snapshot=digest('other'), authority_revision=1, ledger_targets=['R-synthetic-other-unit']))
    own = reader.reader.observe(other, 'u1').value()
    req = AccountingProbeRequest.parse(dict(schema=PROBE_SCHEMA, synthetic_only=True, owner=h.c['owner'],
        request_id=str(uuid4()), expected_state_revision=1, operation_identity=own['operation_identity'],
        ownership=own, native_preflight_digest=digest('other-preflight')))
    before, raw, tag = h.shared.read(); after = deepcopy(before); after['revision'] += 1
    after['operations'][req.operation_identity] = dict(request=asdict(req), fingerprint=req.fingerprint(),
        state='accepted', events=[dict(state='accepted', at=NOW)])
    h.shared.replace(before, raw, tag, after)
    with pytest.raises(StateError): run(h, 'consume')
    assert h.native.writes == 1
    assert h.store2.read()[0]['operations'][h.c['operation']]['consume'] is None


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('change', ['unstarted', 'ledger_generation', 'reconstruction'])
def test_not_executed_requires_persisted_unstarted_and_exact_before(kind, change):
    h = harness(kind); run(h, 'accept'); run(h, 'consume')
    assert inspect(h) == 'NOT_EXECUTED'
    if change == 'unstarted':
        value = h.store2.read()[0]; value['operations'][h.c['operation']]['unstarted'] = None
        h.native.raw = json.dumps(value).encode()
    elif change == 'ledger_generation': h.ledger.revision += 1
    else: rebuild(h)
    assert inspect(h) == 'UNKNOWN'


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('phase', ['accept', 'consume', 'start'])
def test_elapsed_authority_deadline_prevents_new_evidence(kind, phase):
    h = harness(kind)
    for name in ('accept', 'consume')[:('accept', 'consume', 'start').index(phase)]: run(h, name)
    writes = h.native.writes; h.contract.clock = lambda: NOW+600
    with pytest.raises(StateError): run(h, phase)
    assert h.native.writes == writes


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_native_invalid_presentation_dry_run_does_not_mutate_old_payload(kind):
    from app.receipt_confirmation import TITLE
    h = harness(kind); before = deepcopy(h.service.store.value)
    h.db.rows[TITLE][0][5] = 'changed presentation'
    with pytest.raises(StateError): run(h, 'accept')
    assert h.service.store.value == before and h.native.writes == 0
