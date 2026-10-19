"""New isolated runtime tests. No accounting writer/materializer is permitted."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
from threading import Barrier

import pytest

from app.drive_run_state import StateError
from app.receipt_confirmation_contract import encoded, load, planned_after, validate_request as old_validate, token
from app.receipt_confirmation_draft_request import (SCHEMA as INPUT_SCHEMA, decode_request, prepare_inputs,
    validate_request, validate_draft_request_v2)
from app.unified_receipt_review import digest
from staging_shadow.receipt_draft_contract import IsolatedDraftStore, IsolatedDraftContract, LINEAGE, SCHEMA
from draft_runtime_fixtures import harness, run, stage, shared_claim, rebuild, rehash, set_value, NOW
from test_receipt_confirmation_contract import harness as old_harness, run as old_run


@pytest.fixture(autouse=True)
def no_financial_or_network_calls(monkeypatch):
    import socket
    from app.receipt_confirmation import ReceiptConfirmation
    from app.receipt_pipeline import ReceiptPipeline
    from app.sheets import SheetsDB
    from googleapiclient import discovery
    from google.cloud import firestore
    def denied(*args, **kwargs): pytest.fail('new runtime dispatched writer/client')
    for cls, name in ((ReceiptConfirmation, '_write_accounting_plan'), (ReceiptPipeline, '_materialize_result'),
                      (SheetsDB, '__init__'), (discovery, 'build'), (firestore, 'Client'), (socket, 'create_connection')):
        monkeypatch.setattr(cls, name, denied)


def frozen(h): return [p.read_versioned() for p in (h.native, h.shared.io, h.ports['ledger'])]


def inspect(h, expected):
    before = frozen(h)
    result = h.contract.inspect(h.c)
    assert result == dict(classification=expected, synthetic_only=True, real_native_complete=False,
        accounting_permission=False, writer_dispatches=0, retry_permission=False)
    assert frozen(h) == before
    assert h.f.financial_calls == 0 and h.f.db.writes == 0


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_modified_snapshot_through_native_start_without_writer(kind, monkeypatch):
    h = harness(kind, monkeypatch); legacy = deepcopy(h.legacy); before = frozen(h)
    assert validate_request(encoded(h.c), legacy=h.legacy, readers=h.readers) == h.c
    assert h.c['inputs']['baseline']['item']['inputs'] != h.c['inputs']['proposed']['confirmed']
    with pytest.raises(StateError): old_validate(h.c)
    assert frozen(h) == before
    p = h.c['plan']; original = h.c['inputs']['proposed']['original_plan']
    assert p[0][1][1:5] == ['2026-01-03', 'Synthetic edited shop' if kind == 'normal' else 'Synthetic manual clinic',
        120 if kind == 'normal' else 135, 'Synthetic card']
    assert original != p
    assert p[1][1][0] == 'R-'+h.f.sid+'-01'
    if kind == 'normal':
        assert [r[1][4] for r in original[1:-1]] == [70, 50]
        assert [r[1][4] for r in p[1:-1]] == [80, 40]
        assert p[1][1][3:7] == ['Synthetic corrected 1', 80, '日用品', '消耗品']
        assert p[1][1][11] == 'preserve-label'
    else:
        assert original[1][1][4] == 120 and p[1][1][4] == 135
        assert p[1][1][3] == '医療費（本人確認）'
    for phase in ('accept', 'consume', 'start'):
        if phase == 'consume': shared_claim(h)
        run(h, phase)
        inspect(h, 'NOT_EXECUTED' if phase == 'consume' else 'UNKNOWN')
    root = h.store.read()[0]; r = root['operations'][h.c['operation']]
    assert r['pending']['inputs'] == h.c['inputs']['proposed']['confirmed']
    assert r['consume']['fingerprint'] == r['start']['fingerprint'] == h.c['fingerprint']
    assert root['legacy'] == legacy and h.f.store.value == h.f.baseline
    assert h.ports['ledger'].read_versioned() == before[2] and h.native.writes == 4
    with pytest.raises(StateError, match='writer_disabled'): h.contract.dispatch()
    with pytest.raises(StateError, match='completion_disabled'): h.contract.complete_fixture()


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_old_requests_still_use_old_validator(kind):
    h = old_harness(kind)
    assert validate_request(h.c) == old_validate(h.c)
    old_run(h, 'accept'); old_run(h, 'consume'); old_run(h, 'start')
    c = deepcopy(h.c); c['inputs']['confirmed'][0] = '2026-02-03'; rehash(c)
    with pytest.raises(StateError): validate_request(c)


@pytest.mark.parametrize('schema', [None, '', 2, True, 'unknown', 'receipt-confirmation-draft-inputs-v3'])
def test_schema_is_not_a_fallback(schema, monkeypatch):
    h = harness('normal', monkeypatch); h.c['inputs']['schema'] = schema; rehash(h.c)
    with pytest.raises(StateError): validate_request(h.c, legacy=h.legacy, readers=h.readers)


@pytest.mark.parametrize('raw', [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'[]', b'null', b'x', b' '*300001],
    ids=['duplicate', 'nan', 'inf', 'array', 'null', 'syntax', 'capacity'])
def test_invalid_json(raw):
    with pytest.raises((StateError, ValueError)): validate_request(raw)


def rebound(h):
    """Attacker recomputes every public digest. Trusted repositories stay fixed."""
    c = h.c
    c['authority']['input_fingerprint'] = digest(c['inputs'])
    c['authority']['scope_digest'] = digest([c['operation'], c['source'], c['plan'], c['before']])
    c['ownership']['input_snapshot'] = digest(c['inputs'])
    rehash(c)


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('path', [
    ('baseline', 'legacy_digest'), ('baseline', 'candidate_digest'), ('baseline', 'item', 'inputs', 0),
    ('draft', 'revision'), ('draft', 'generation'), ('draft', 'observation', 'tag'), ('draft', 'input_digest'),
    ('source_binding', 'source', 'drive_revision_id'), ('source_binding', 'source', 'drive_numeric_version'),
    ('source_binding', 'source', 'native_source_version'), ('source_binding', 'source', 'sha256'),
    ('source_binding', 'page_identity'), ('source_binding', 'scope'), ('source_binding', 'receipt_unit_id'),
    ('source_binding', 'receipt_index'), ('categories', 'revision'), ('categories', 'observation', 'tag'),
    ('proposed', 'confirmed', 0), ('proposed', 'parsed', 'total'), ('derivation', 'plan_digest'),
    ('derivation', 'before_digest'), ('derivation', 'line_digest')])
def test_rehashed_snapshot_tampering_is_rejected(kind, path, monkeypatch):
    h = harness(kind, monkeypatch); p = h.c['inputs']
    for key in path[:-1]: p = p[key]
    p[path[-1]] = 77 if type(p[path[-1]]) is int else 'changed'
    rebound(h)
    before = h.native.writes
    with pytest.raises(StateError): run(h, 'accept')
    assert h.native.writes == before


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('where', ['outer', 'inputs', 'baseline', 'draft', 'source_binding', 'categories', 'proposed', 'derivation', 'authority'])
def test_unknown_fields(kind, where, monkeypatch):
    h = harness(kind, monkeypatch)
    target = h.c if where == 'outer' else h.c['authority'] if where == 'authority' else h.c['inputs'] if where == 'inputs' else h.c['inputs'][where]
    target['unknown'] = 'x'; rebound(h)
    with pytest.raises(StateError): run(h, 'accept')


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('field,value', [('amount', True), ('amount', 120.0), ('amount', float('nan')),
    ('amount', float('inf')), ('amount', 0), ('amount', 1000000000000), ('merchant', '=SUM(1)'), ('memo', 'x'*4001),
    ('date', ''), ('merchant', ''), ('extra', 'x')])
def test_saved_raw_invalid_values(kind, field, value, monkeypatch):
    h = harness(kind, monkeypatch); d = load(h.ports['draft'].raw)
    d['value']['fields'][field] = value; d['input_digest'] = digest(d['value']); set_value(h.ports['draft'], d)
    with pytest.raises((StateError, ValueError)):
        prepare_inputs(h.legacy, kind, h.c['case_id'], h.c['owner'], token(*h.native.read_versioned()),
            '2026-10-19 00:00:00', h.readers)


@pytest.mark.parametrize('change', ['id', 'order', 'index_bool', 'amount', 'category', 'add', 'delete', 'unknown', 'name', 'note', 'duplicate_id'])
def test_fixed_general_catalog_rejects_invalid_saved_edits(change, monkeypatch):
    h = harness('normal', monkeypatch); d = load(h.ports['draft'].raw); items = d['value']['items']
    if change == 'id': items[0]['expense_id'] = 'invented'
    elif change == 'order': items.reverse()
    elif change == 'index_bool': items[0]['baseline_index'] = True
    elif change == 'amount': items[0]['amount'] += 1
    elif change == 'category': items[0]['category'] = 'unknown｜unknown'
    elif change == 'add': items.append(deepcopy(items[0]))
    elif change == 'delete': items.pop()
    elif change == 'unknown': items[0]['quantity'] = 3
    elif change == 'name': items[0]['name'] = ''
    elif change == 'note': items[0]['note'] = 'x'*8001
    else: items[1]['expense_id'] = items[0]['expense_id']
    d['input_digest'] = digest(d['value']); set_value(h.ports['draft'], d)
    with pytest.raises((StateError, ValueError)):
        prepare_inputs(h.legacy, 'normal', h.c['case_id'], h.c['owner'], token(*h.native.read_versioned()),
            '2026-10-19 00:00:00', h.readers)


@pytest.mark.parametrize('phase', ['accept', 'consume', 'start'])
@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('change', ['draft', 'source_version', 'source_bytes', 'source_drive', 'master', 'ledger_value', 'ledger_formula', 'authority', 'native'])
def test_independent_reader_drift_at_every_boundary(kind, phase, change, monkeypatch):
    h = harness(kind, monkeypatch); stage(h, phase); writes = h.native.writes
    if change == 'source_bytes': h.ports['source_bytes'].raw = b'changed'
    elif change == 'native': h.native.revision += 1
    else:
        name = dict(source_version='source', source_drive='source', master='categories', ledger_value='ledger', ledger_formula='ledger').get(change, change)
        v = load(h.ports[name].raw)
        if change == 'draft': v['revision'] += 1
        elif change == 'source_version': v['source']['native_source_version'] = '2'
        elif change == 'source_drive': v['source']['drive_numeric_version'] = '2'
        elif change == 'master': v['revision'] += '-changed'
        elif change in {'ledger_value', 'ledger_formula'}: v['snapshot'][int(change == 'ledger_formula')]['支出明細'][-1][4] = 9
        else: v['revision'] += 1
        set_value(h.ports[name], v)
    with pytest.raises(StateError): run(h, phase, revision=1 if phase == 'accept' else None)
    assert h.native.writes == writes
    inspect(h, 'UNKNOWN')


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('phase', ['accept', 'consume', 'start'])
@pytest.mark.parametrize('context', [dict(actor_id='0'*64), dict(session_active=False), dict(csrf_valid=False),
    dict(purpose='DRAFT_ONLY'), dict(expires_at=NOW), dict(created_at=NOW+1), dict(expected_revision=1),
    dict(request_id='different'), dict(binding_digest='0'*64), dict(issuer='untrusted')])
def test_unchanged_financial_auth_conditions(kind, phase, context, monkeypatch):
    h = harness(kind, monkeypatch); stage(h, phase); writes = h.native.writes
    with pytest.raises((StateError, TypeError)): run(h, phase, context)
    assert h.native.writes == writes


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('phase', ['accept', 'consume', 'start'])
@pytest.mark.parametrize('fault', ['412', 'timeout', 'lost', 'readback', 'unchanged_tag'])
def test_native_cas_unknown_never_retries(kind, phase, fault, monkeypatch):
    h = harness(kind, monkeypatch); stage(h, phase); writes = h.native.writes; original = h.native.replace_versioned
    def injected(*args):
        if fault in {'412', 'timeout'}: raise StateError('HTTP_412') if fault == '412' else TimeoutError()
        rev = h.native.revision; original(*args)
        if fault == 'lost': raise TimeoutError()
        if fault == 'readback':
            v = load(h.native.raw); v['revision'] += 1; h.native.raw = encoded(v)
        if fault == 'unchanged_tag': h.native.revision = rev
    monkeypatch.setattr(h.native, 'replace_versioned', injected)
    with pytest.raises(StateError): run(h, phase)
    assert h.native.writes == writes + (fault not in {'412', 'timeout'})
    assert h.store.uncertain
    before = frozen(h)
    for p in ('accept', 'consume', 'start'):
        with pytest.raises(StateError): run(h, p)
    assert frozen(h) == before; inspect(h, 'UNKNOWN')


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('count', [2, 16])
def test_independent_facades_one_cas_winner(kind, count, monkeypatch):
    h = harness(kind, monkeypatch); gate = Barrier(count); original = h.native.replace_versioned
    def concurrent(*args): gate.wait(timeout=30); return original(*args)
    monkeypatch.setattr(h.native, 'replace_versioned', concurrent)
    def one(_):
        store = IsolatedDraftStore(h.native, h.manifest, h.spid, parent_digest=h.pin, enabled=True, isolated_synthetic=True)
        c = IsolatedDraftContract(store, h.shared, h.auth, h.readers, clock=lambda: NOW,
            fresh_process=True, enabled=True, isolated_synthetic=True)
        try: c.accept_fixture(h.c, None, expected_revision=1); return True
        except StateError: return False
    with ThreadPoolExecutor(max_workers=count) as pool: winners = list(pool.map(one, range(count)))
    assert sum(winners) == 1 and h.native.writes == 2
    v = h.store.read()[0]
    assert len(v['operations']) == 2 and v['legacy'] == h.legacy and v['revision'] == 2
    assert h.ports['ledger'].writes == 0


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('phase', ['accept', 'consume', 'start'])
def test_reconstruction_is_read_only_and_dispatch_disabled(kind, phase, monkeypatch):
    h = harness(kind, monkeypatch); stage(h, phase); run(h, phase)
    before = frozen(h); rebuild(h)
    inspect(h, 'UNKNOWN')
    for p in ('accept', 'consume', 'start'):
        with pytest.raises(StateError): run(h, p)
    assert frozen(h) == before


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('observed,expected', [('before', 'UNKNOWN'), ('prefix', 'PARTIAL'), ('full', 'UNKNOWN')])
def test_observation_never_creates_completion(kind, observed, expected, monkeypatch):
    h = harness(kind, monkeypatch); stage(h, 'start'); run(h, 'start')
    if observed != 'before':
        set_value(h.ports['ledger'], dict(complete=True, snapshot=planned_after(h.c['before'],
            h.c['plan'][:1] if observed == 'prefix' else h.c['plan'])))
    inspect(h, expected); before = frozen(h)
    with pytest.raises(StateError): h.contract.dispatch()
    assert frozen(h) == before


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('point', ['before_qualification', 'after_qualification', 'before_consume', 'after_consume', 'before_start', 'after_start'])
def test_boundary_stop_retains_all_evidence(kind, point, monkeypatch):
    h = harness(kind, monkeypatch)
    class Stop(BaseException): pass
    def stop(p):
        if p == point: raise Stop()
    h.contract.checkpoint = stop
    with pytest.raises(Stop):
        for phase in ('accept', 'consume', 'start'):
            if phase == 'consume': shared_claim(h)
            run(h, phase)
    before = frozen(h); rebuild(h); inspect(h, 'UNKNOWN')
    with pytest.raises(StateError): run(h, 'start')
    assert frozen(h) == before
    r = h.store.read()[0]['operations'].get(h.c['operation'])
    if r and r['phase'] != 'qualified': assert r['consume'] and r['pending']


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('change', ['same', 'different', 'another_request', 'stale_revision'])
def test_request_reuse_never_writes_again(kind, change, monkeypatch):
    h = harness(kind, monkeypatch); run(h, 'accept'); writes = h.native.writes
    if change == 'different': h.c['plan'][0][1][2] = 'changed'; rebound(h)
    if change == 'another_request': h.c['request_id'] = '71c059d1-e137-4d75-a5a6-366f396e8b81'; rebound(h)
    with pytest.raises(StateError): run(h, 'accept', revision=1 if change == 'stale_revision' else None)
    assert h.native.writes == writes


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('fault', ['412', 'timeout', 'lost', 'readback', 'ownership_conflict'])
def test_shared_claim_unknown_stops_native(kind, fault, monkeypatch):
    h = harness(kind, monkeypatch); run(h, 'accept'); native_before = h.native.writes
    if fault == 'ownership_conflict':
        shared_claim(h)
        v = load(h.shared.io.raw); r = v['operations'][h.c['operation']]
        r['request']['native_preflight_digest'] = '0'*64
        from app.iphone_accounting_gate import AccountingProbeRequest
        r['fingerprint'] = AccountingProbeRequest.parse(r['request']).fingerprint()
        h.shared.io.raw = encoded(v)
    else:
        original = h.shared.io.replace_versioned
        def broken(*args):
            if fault in {'412', 'timeout'}: raise StateError('HTTP_412') if fault == '412' else TimeoutError()
            original(*args)
            if fault == 'lost': raise TimeoutError()
            value = load(h.shared.io.raw); value['revision'] += 1; h.shared.io.raw = encoded(value)
        monkeypatch.setattr(h.shared.io, 'replace_versioned', broken)
        with pytest.raises((StateError, TimeoutError)): shared_claim(h)
        assert h.contract.uncertain
    with pytest.raises(StateError): run(h, 'consume')
    assert h.native.writes == native_before
    inspect(h, 'UNKNOWN')


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('change', ['v1', 'v2', 'unknown', 'missing_lineage', 'drop_old', 'legacy', 'pin', 'revision_bool'])
def test_root_versions_and_inheritance_are_strict(kind, change, monkeypatch):
    h = harness(kind, monkeypatch); value = load(h.native.raw)
    if change in {'v1', 'v2'}:
        if change == 'v1': value = deepcopy(h.legacy)
        else: value = dict(schema='receipt-reimport-v2', revision=0, legacy=h.legacy, operations={})
        h.native.raw = encoded(value)
        assert h.store.value == h.legacy
        with pytest.raises(StateError): run(h, 'accept')
        assert h.native.writes == 1
        return
    if change == 'unknown': value['schema'] = 'receipt-reimport-v9'
    elif change == 'missing_lineage': del value['operations'][LINEAGE]
    elif change == 'drop_old': value['operations'][LINEAGE]['parent_digest'] = '0'*64
    elif change == 'legacy': value['legacy']['confirmation_items'][h.c['review_id']]['inputs'][0] = 'edited'
    elif change == 'pin': h.store.parent_digest = '0'*64
    else: value['revision'] = True
    h.native.raw = encoded(value)
    with pytest.raises(StateError): h.store.read()
    inspect(h, 'UNKNOWN')


@pytest.mark.parametrize('state', ['qualified', 'pending', 'writer_started', 'complete_fixture'])
def test_nonempty_v2_history_is_preserved_and_cannot_be_removed(state, monkeypatch):
    h = old_harness('normal')
    for phase in ('accept', 'consume', 'start')[:['qualified', 'pending', 'writer_started', 'complete_fixture'].index(state)+1]: old_run(h, phase)
    if state == 'complete_fixture':
        from test_receipt_confirmation_contract import observe_full
        observe_full(h); old_run(h, 'complete')
    parent, _, _ = h.store2.read(); before = deepcopy(parent)
    store = IsolatedDraftStore(h.native, h.manifest, h.sid, parent_digest=digest(parent), enabled=True, isolated_synthetic=True)
    store.inherit_memory_v2(expected_revision=parent['revision'])
    v, raw, tag = store.read()
    assert v['legacy'] == before['legacy']
    assert v['operations'][h.c['operation']] == before['operations'][h.c['operation']]
    broken = deepcopy(v); del broken['operations'][h.c['operation']]
    with pytest.raises(StateError): store.validate(broken)
    # Rehashing a stripped parent does not change the independently pinned digest.
    broken['operations'][LINEAGE]['parent_digest'] = digest(dict(before, operations={}))
    with pytest.raises(StateError): store.validate(broken)
    assert store.read() == (v, raw, tag)


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('hold', ['G103', 'H301', 'old_p1', 'pdf', 'pending', 'applied', 'require_reconfirm', 'automatic'])
def test_saved_holds_and_pending_are_not_cleared(kind, hold, monkeypatch):
    h = harness(kind, monkeypatch); legacy = deepcopy(h.legacy); cid = h.c['case_id']; old = legacy['confirmation_items'][h.c['review_id']]
    if hold in {'G103', 'H301'}: cid = hold
    elif hold == 'old_p1':
        from app.unified_receipt_write import OLD_P1_SOURCE
        old['source']['source_id'] = OLD_P1_SOURCE
    elif hold == 'pdf': old['source']['mime_type'] = 'application/pdf'
    elif hold in {'pending', 'applied'}: old['status'] = hold
    elif hold == 'require_reconfirm': old['require_reconfirm'] = True
    else: old['decision_origin'] = 'automatic'
    with pytest.raises((StateError, ValueError)):
        prepare_inputs(legacy, kind, cid, h.c['owner'], token(*h.native.read_versioned()), '2026-10-19', h.readers)
    assert h.native.writes == 1


@pytest.mark.parametrize('change', ['provenance_ai', 'provenance_ocr', 'partial_date', 'partial_merchant', 'partial_category', 'line_candidate'])
def test_medical_is_complete_manual_only(change, monkeypatch):
    h = harness('medical', monkeypatch); d = load(h.ports['draft'].raw)
    if change.startswith('provenance_'): d['value']['provenance'] = change
    elif change.startswith('partial_'): d['value']['fields'][change.removeprefix('partial_')] = ''
    else: d['value']['items'] = [{'ocr': 'candidate'}]
    d['input_digest'] = digest(d['value']); set_value(h.ports['draft'], d)
    with pytest.raises((StateError, ValueError)):
        prepare_inputs(h.legacy, 'medical', h.c['case_id'], h.c['owner'], token(*h.native.read_versioned()), '2026-10-19', h.readers)


@pytest.mark.parametrize('port', ['draft', 'source', 'categories', 'ledger', 'authority'])
def test_read_race_is_rejected(port, monkeypatch):
    h = harness('normal', monkeypatch); p = h.ports[port]; read = p.read_versioned; count = 0
    def changing():
        nonlocal count
        count += 1
        if count == 2: p.revision += 1
        return read()
    monkeypatch.setattr(p, 'read_versioned', changing)
    with pytest.raises(StateError): run(h, 'accept')
    assert h.native.writes == 1


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_separate_process_read_only_reconstruction(kind, monkeypatch):
    h = harness(kind, monkeypatch); stage(h, 'start'); run(h, 'start'); before = frozen(h)
    payload = dict(native=h.native.raw.decode(), shared=h.shared.io.raw.decode(), manifest=h.manifest, spid=h.spid, pin=h.pin,
        ports={k: v.raw.decode() for k, v in h.ports.items() if k != 'source_bytes'}, c=h.c)
    code = '''
import json, sys
from types import SimpleNamespace
from draft_runtime_fixtures import SourceBytes, RAW, OWNER, NOW, rebuild
from test_legacy_contract_poc import mem
from app.receipt_confirmation_draft_request import DraftReaders
from app.unified_receipt_write import ConditionalRequests
from app.unified_receipt_purposes import PurposeBoundary, VerifiedRead
from accounting_gate_fixtures import reader_for
from app.unified_receipt_review import digest
v=json.load(sys.stdin); h=SimpleNamespace(**v)
h.native=mem(json.loads(v['native'])); h.shared=ConditionalRequests(mem(json.loads(v['shared'])),'rebuilt',synthetic_accounting=True)
h.ports={k:mem(json.loads(s)) for k,s in v['ports'].items()}
source=h.c['source']; own=reader_for('legacy_confirmation',source,RAW,[],dict(namespace='receipt_confirmation',scope='whole',unit_id=None,
receipt_index=None,input_snapshot=digest(h.c['inputs']),authority_revision=7,ledger_targets=[r[0] for _,r in h.c['plan']],case_id=h.c['case_id']))
h.readers=DraftReaders(**h.ports,source_bytes=SourceBytes(source['source_id'],source['drive_revision_id']),ownership=lambda:own.reader.observe('legacy_confirmation','u1'))
h.auth=PurposeBoundary(OWNER,lambda *a:None,lambda _:VerifiedRead(OWNER,NOW+600,True),lambda:[],clock=lambda:NOW)
rebuild(h); result=h.contract.inspect(h.c)
assert result['classification']=='UNKNOWN' and h.native.writes==0 and h.shared.io.writes==0
assert h.store.read()[0]['operations'][h.c['operation']]['start'] is not None
print(json.dumps(result))
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).parents[1]/'tests',
        env=dict(__import__('os').environ, PYTHONPATH=str(Path(__file__).parents[1])),
        input=json.dumps(payload), text=True, capture_output=True, check=True)
    assert json.loads(result.stdout)['writer_dispatches'] == 0 and frozen(h) == before


def test_runtime_workflow_is_isolated():
    import yaml
    root = Path(__file__).resolve().parents[1]
    expected = yaml.load((root/'.github/workflows/iphone-mvp-r0-checks.yml').read_text('utf8'), Loader=yaml.BaseLoader)
    expected['name'] = 'iPhone isolated draft native v3 runtime'
    expected['on']['pull_request']['branches'] = ['codex/iphone-draft-request-v2-design']
    expected['concurrency']['group'] = 'kakeibo-iphone-draft-runtime-${{ github.ref }}'
    job = expected['jobs'].pop('adoption'); expected['jobs']['native-draft'] = job
    steps = job['steps']
    steps[3] = dict(name='Collect new runtime tests', run='python -m pytest --collect-only -q tests/test_receipt_draft_runtime.py')
    steps[4] = dict(name='Execute new runtime tests with all writers forbidden', run='python -m pytest -vv tests/test_receipt_draft_runtime.py')
    steps.insert(5, dict(name='Preserve R0 18 and design 70 cases',
        run='python -m pytest -q tests/test_iphone_mvp_r0_adoption.py tests/test_iphone_draft_request_design.py'))
    steps[-1]['run'] = ('python -m compileall -q app/receipt_confirmation_draft_request.py staging_shadow/receipt_draft_contract.py '
        'tests/draft_runtime_fixtures.py tests/test_receipt_draft_runtime.py\ngit diff --check\n')
    source = (root/'.github/workflows/iphone-draft-runtime-checks.yml').read_text('utf8')
    assert yaml.load(source, Loader=yaml.BaseLoader) == expected
    assert all(s not in source for s in ('secrets.', 'vars.', 'environment:', 'workflow_dispatch', 'push:', 'deploy'))


@pytest.mark.parametrize('fault', ['412', 'timeout', 'lost', 'readback', 'weak_tag'])
def test_inheritance_cas_failure_never_resets_or_retries(fault, monkeypatch):
    from test_legacy_contract_poc import mem
    h = harness('normal', monkeypatch)
    parent = dict(schema='receipt-reimport-v2', revision=0, legacy=h.legacy, operations={})
    io = mem(parent); store = IsolatedDraftStore(io, h.manifest, h.spid, parent_digest=digest(parent),
        enabled=True, isolated_synthetic=True)
    original = io.replace_versioned
    def broken(*args):
        if fault in {'412', 'timeout'}: raise StateError('412') if fault == '412' else TimeoutError()
        original(*args)
        if fault == 'lost': raise TimeoutError()
        if fault == 'readback':
            v = load(io.raw); v['revision'] += 1; io.raw = encoded(v)
        if fault == 'weak_tag': io.revision = 0
    monkeypatch.setattr(io, 'replace_versioned', broken)
    with pytest.raises(StateError): store.inherit_memory_v2(expected_revision=0)
    assert store.uncertain
    before = io.read_versioned(); writes = io.writes
    with pytest.raises(StateError): store.inherit_memory_v2(expected_revision=0)
    assert io.read_versioned() == before and io.writes == writes


@pytest.mark.parametrize('point', ['shared', 'native'])
def test_consumption_refuses_stale_strong_observation(point, monkeypatch):
    h = harness('normal', monkeypatch); stage(h, 'consume'); writes = h.native.writes
    if point.startswith('shared'): h.shared.io.revision += 1
    else: h.native.revision += 1
    with pytest.raises(StateError): run(h, 'consume')
    assert h.native.writes == writes and h.contract.uncertain


@pytest.mark.parametrize('context', [dict(actor_id='0'*64), dict(expires_at=NOW), dict(session_active=False)])
def test_read_only_query_requires_authenticated_owner(context, monkeypatch):
    h = harness('medical', monkeypatch); stage(h, 'start'); run(h, 'start'); before = frozen(h)
    assert h.contract.inspect(h.c, context)['classification'] == 'UNKNOWN'
    assert frozen(h) == before


@pytest.mark.parametrize('mode', ['disabled', 'live_io', 'no_pin', 'wrong_pin'])
def test_default_off_and_no_unpinned_v3(mode, monkeypatch):
    h = harness('normal', monkeypatch)
    if mode == 'live_io': h.native.synthetic_memory_only = False
    with pytest.raises(StateError):
        store = IsolatedDraftStore(h.native, h.manifest, h.spid, enabled=mode != 'disabled', isolated_synthetic=True,
            parent_digest=None if mode == 'no_pin' else '0'*64 if mode == 'wrong_pin' else h.pin)
        store.read()


def test_medical_incomplete_baseline_is_not_prefilled(monkeypatch):
    h = harness('medical', monkeypatch); legacy = deepcopy(h.legacy)
    legacy['confirmation_items'][h.c['review_id']]['inputs'] = ['']*8
    inputs, _, plan = prepare_inputs(legacy, 'medical', h.c['case_id'], h.c['owner'],
        token(*h.native.read_versioned()), '2026-10-19', h.readers)
    assert inputs['baseline']['item']['inputs'] == ['']*8
    assert inputs['proposed']['original_plan'] is inputs['proposed']['original_parsed'] is None
    assert plan[1][1][4] == 135


@pytest.mark.parametrize('where', ['raw_value', 'raw_field', 'raw_item', 'parsed', 'parsed_item', 'line'])
def test_native_codec_rejects_nested_unknown_fields(where, monkeypatch):
    from app.receipt_confirmation_draft_request import validate_shape
    h = harness('normal', monkeypatch); i = h.c['inputs']
    target = {'raw_value': i['draft']['value'], 'raw_field': i['draft']['value']['fields'],
        'raw_item': i['draft']['value']['items'][0], 'parsed': i['proposed']['parsed'],
        'parsed_item': i['proposed']['parsed']['items'][0], 'line': i['proposed']['lines'][0]}[where]
    target['unknown'] = 'x'; i['draft']['input_digest'] = digest(i['draft']['value']); rebound(h)
    with pytest.raises(StateError): validate_shape(h.c)


@pytest.mark.parametrize('phase', ['accept', 'consume', 'start'])
def test_double_tap_on_same_instance_does_not_lose_tickets(phase, monkeypatch):
    from threading import Event
    h = harness('normal', monkeypatch); stage(h, phase); entered, release = Event(), Event()
    point = {'accept': 'before_qualification', 'consume': 'before_consume', 'start': 'before_start'}[phase]
    def pause(p):
        if p == point: entered.set(); assert release.wait(timeout=30)
    h.contract.checkpoint = pause
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, h, phase); assert entered.wait(timeout=30)
        writes = h.native.writes
        with pytest.raises(StateError, match='in_flight_read_only'): run(h, phase)
        assert h.native.writes == writes
        release.set(); first.result(timeout=30)
    assert not h.contract.uncertain and not h.store.uncertain
    assert h.native.writes == writes+1
