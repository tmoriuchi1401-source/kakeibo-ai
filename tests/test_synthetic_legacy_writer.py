"""Execute the real legacy writer, exclusively against disposable memory IO."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import ast
import json
from pathlib import Path
import socket
import subprocess
import sys
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.drive_run_state import StateError
from app.iphone_accounting_gate import AccountingProbeRequest, SCHEMA as PROBE_SCHEMA
from app.receipt_confirmation import ReceiptConfirmation
from app.receipt_confirmation_contract import (IsolatedConfirmationContract, SCHEMA,
    observe_ledger, planned_after, token)
from app.receipt_reimport_production import ReimportStore
from app.unified_receipt_review import digest
from app.unified_receipt_purposes import VerifiedRead
from app.unified_receipt_write import ConditionalRequests
from staging_shadow.synthetic_legacy_writer import SyntheticLegacyConfirmation
from test_receipt_confirmation_contract import harness as v2_harness, Crash
from test_legacy_contract_poc import mem, NOW, forbid_clients_and_writers


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('external network prohibited in synthetic writer')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


def compose(h, *, fresh=False):
    h.store2 = ReimportStore.isolated_v2(h.native, h.manifest, h.sid, enabled=True, isolated_synthetic=True)
    h.contract = IsolatedConfirmationContract(h.store2, h.shared, h.auth, h.readers,
        enabled=True, isolated_synthetic=True, clock=lambda: NOW)
    h.adapter = SyntheticLegacyConfirmation(h.contract, enabled=True, isolated_synthetic=True, fresh_attempt=fresh)
    return h


def harness(kind):
    h = v2_harness(kind)
    h.auth.verify_read = lambda _: VerifiedRead(h.c['owner'], NOW+600, True)
    h.service.synthetic_memory_only = h.db.synthetic_memory_only = h.store.synthetic_memory_only = True
    # Remove the old PoC's pre-seeded shared fixture. The NEW adapter must
    # acquire the actual shared CAS and ownership fence itself.
    h.shared = ConditionalRequests(mem(dict(schema='unified-receipt-requests-v1', revision=0, operations={})),
        'synthetic-shared', synthetic_accounting=True)
    return compose(h, fresh=True)


def unchanged_legacy(h, old):
    assert h.store2.value == old
    assert h.service.items[h.key]['status'] == 'waiting'
    h.service._write_accounting_plan.assert_not_called()
    assert h.db.writes == 0  # Validation DB is not the writer DB.


def readonly(h):
    before = [io.read_versioned() for io in (h.native, h.shared.io, h.ledger)]
    report = h.adapter.inspect(h.c)
    assert [io.read_versioned() for io in (h.native, h.shared.io, h.ledger)] == before
    assert report['synthetic_only'] and not report['accounting_permission'] and not report['real_native_complete']
    assert not report['retry_allowed']
    return report


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_original_writer_records_all_rows_and_exact_readback(kind, monkeypatch):
    h = harness(kind); legacy = deepcopy(h.store2.value)
    original = ReceiptConfirmation._write_accounting_plan
    calls = []
    def audited(service, key, item):
        calls.append((key, deepcopy(item)))
        return original(service, key, item)  # Execute the ORIGINAL implementation.
    monkeypatch.setattr(ReceiptConfirmation, '_write_accounting_plan', audited)
    result = h.adapter.submit(h.c)
    assert len(calls) == 1 and calls[0][1]['status'] == 'pending'
    assert result['classification'] == 'COMPLETE' and result['writer_dispatches'] == 1
    assert observe_ledger(h.ledger)[0] == planned_after(h.c['before'], h.c['plan'])
    assert h.adapter.db.events == h.adapter.db.expected
    assert len(h.adapter.db.events) == len(h.c['plan'])
    assert h.ledger.writes == len(h.c['plan']) and h.native.writes == 4
    assert h.shared.read()[0]['operations'][h.c['operation']]['state'] == 'complete'
    op = h.store2.read()[0]['operations'][h.c['operation']]
    assert op['phase'] == 'complete_fixture' and op['pending']['status'] == 'pending'
    assert op['consume'] and op['start'] and op['completion']
    assert op['completion']['observation'] == token(*h.ledger.read_versioned())
    if kind == 'normal':
        assert [e[0] for e in h.adapter.db.events] == ['update', 'append', 'append', 'append']
    else:
        assert [e[0] for e in h.adapter.db.events] == ['append'] * 3
        assert op['pending']['decision_origin'] == 'human'
    assert observe_ledger(h.ledger)[0][1]['支出明細'][0][4] == '=1+2'
    unchanged_legacy(h, legacy)
    saved = [io.read_versioned() for io in (h.native, h.shared.io, h.ledger)]
    for _ in range(3): assert h.adapter.submit(h.c)['classification'] == 'COMPLETE'
    compose(h)
    assert h.adapter.submit(h.c)['classification'] == 'COMPLETE'
    assert h.adapter.dispatches == 0  # Reconstructed adapter did not re-dispatch.
    assert [io.read_versioned() for io in (h.native, h.shared.io, h.ledger)] == saved


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('point', ['before_qualification', 'after_qualification', 'before_shared_claim',
    'after_shared_claim', 'before_consume', 'after_consume', 'before_start', 'after_start',
    'before_dispatch', 'after_dispatch_lease', 'before_ledger_1', 'after_ledger_1', 'after_ledger_2',
    'after_native_applied', 'before_completion', 'after_completion', 'after_native_completion',
    'before_shared_completion', 'after_shared_completion'])
def test_crash_matrix_never_reenters_native_writer(kind, point):
    h = harness(kind); old = deepcopy(h.store2.value)
    def crash(name):
        if name == point: raise Crash(point)
    h.contract.checkpoint = h.adapter.checkpoint = crash
    with pytest.raises(Crash): h.adapter.submit(h.c)
    dispatches = h.adapter.dispatches
    assert dispatches <= 1
    persisted = [io.read_versioned() for io in (h.native, h.shared.io, h.ledger)]
    compose(h)
    result = readonly(h)
    if point in {'after_ledger_1', 'after_ledger_2'}:
        assert result['classification'] == 'PARTIAL'
    elif point == 'after_shared_completion':
        assert result['classification'] == 'COMPLETE'
    else:
        assert result['classification'] == 'UNKNOWN'
    # Any persisted qualified/pending/start/completion is read-only. Even
    # before qualification, the held shared/no-start state is never repaired.
    if h.store2.read()[0]['operations']:
        h.adapter.submit(h.c)
    assert h.adapter.dispatches == 0
    assert [io.read_versioned() for io in (h.native, h.shared.io, h.ledger)] == persisted
    unchanged_legacy(h, old)


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('store,number', [('native', 1), ('native', 2), ('native', 3), ('native', 4),
    ('shared', 1), ('shared', 2), ('shared', 3), ('shared', 4), ('ledger', 1), ('ledger', 2), ('ledger', 3)])
@pytest.mark.parametrize('fault', ['lost', '412', 'mismatch'])
def test_transport_uncertainty_has_no_retry_or_gap_fill(kind, store, number, fault):
    h = harness(kind)
    io = {'native': h.native, 'shared': h.shared.io, 'ledger': h.ledger}[store]
    original = io.replace_versioned
    attempts = []
    def replace(raw, tag, after):
        attempts.append((raw, tag, after))
        if len(attempts) == number:
            if fault == '412': raise StateError('HTTP_412')
            original(raw, tag, after)
            if fault == 'lost': raise TimeoutError('synthetic response lost')
            io.revision += 1  # ACK followed by a conflicting observation tag.
            if store == 'ledger':
                v = json.loads(io.raw); v['snapshot'][1]['支出明細'][0][4] = '=1+3'
                io.raw = json.dumps(v).encode()
            else:
                v = json.loads(io.raw); v['revision'] += 1
                io.raw = json.dumps(v).encode()
            return
        original(raw, tag, after)
    io.replace_versioned = replace
    with pytest.raises((StateError, TimeoutError)): h.adapter.submit(h.c)
    assert len(attempts) == number
    assert h.adapter.dispatches <= 1
    assert h.adapter.inspect(h.c)['classification'] != 'COMPLETE'
    saved = [x.read_versioned() for x in (h.native, h.shared.io, h.ledger)]
    compose(h)
    # A native completion ACK loss leaves shared executing; no completion repair.
    assert readonly(h)['classification'] in {'UNKNOWN', 'PARTIAL', 'COMPLETE'}
    if h.store2.read()[0]['operations']:
        h.adapter.submit(h.c)
    else:
        with pytest.raises(StateError, match='attempt_read_only'): h.adapter.submit(h.c)
    assert h.adapter.dispatches == 0
    assert [x.read_versioned() for x in (h.native, h.shared.io, h.ledger)] == saved


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('instances', [2, 16])
def test_independent_instances_cas_one_dispatch_zero_duplicate_posting(kind, instances):
    h = harness(kind); barrier = Barrier(instances)
    def contender(_):
        state = SimpleNamespace(**vars(h))
        compose(state, fresh=True)
        state.contract.checkpoint = lambda p: barrier.wait(timeout=30) if p == 'before_qualification' else None
        try:
            result = state.adapter.submit(state.c)
            return state.adapter.dispatches, result['classification']
        except StateError:
            return state.adapter.dispatches, 'UNKNOWN'
    with ThreadPoolExecutor(max_workers=instances) as pool:
        results = list(pool.map(contender, range(instances)))
    assert sum(n for n, _ in results) == 1
    assert sum(result == 'COMPLETE' for _, result in results) == 1
    observed = observe_ledger(h.ledger)[0]
    assert observed == planned_after(h.c['before'], h.c['plan'])
    for part in observed:
        for rows in part.values(): assert len([r[0] for r in rows]) == len(set(r[0] for r in rows))
    assert h.native.writes == 4 and h.ledger.writes == len(h.c['plan'])
    assert readonly(compose(h))['classification'] == 'COMPLETE'


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('point', ['before_shared_claim', 'after_shared_claim', 'before_consume', 'before_start',
    'after_start', 'before_dispatch', 'before_ledger_1'])
@pytest.mark.parametrize('change', ['source', 'inputs', 'authority', 'session', 'ledger'])
def test_live_binding_changes_block_dispatch_or_first_write(kind, point, change):
    h = harness(kind)
    def mutate(name):
        if name != point: return
        if change == 'source': h.current['source']['drive_numeric_version'] = '2'
        elif change == 'inputs': h.current['inputs']['confirmed'][-1] = 'different'
        elif change == 'authority': h.current['authority']['revision'] += 1
        elif change == 'ledger': h.ledger.revision += 1
        else:
            verify = h.auth.verify_post
            h.auth.verify_post = lambda *args: type(verify(*args))(**dict(asdict(verify(*args)), session_active=False))
    h.contract.checkpoint = h.adapter.checkpoint = mutate
    with pytest.raises(StateError): h.adapter.submit(h.c)
    assert h.adapter.dispatches <= (1 if point == 'before_ledger_1' else 0)
    assert h.ledger.writes == 0


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_direct_internal_writer_db_and_completion_cannot_bypass_lease(kind):
    h = harness(kind)
    h.adapter.submit(h.c)
    writer = h.adapter.writer
    item = deepcopy(h.store2.read()[0]['operations'][h.c['operation']]['pending'])
    saved = [io.read_versioned() for io in (h.native, h.shared.io, h.ledger)]
    attacks = [lambda: writer.apply_confirmations(), lambda: writer._write_accounting_plan(h.key, item),
        lambda: ReceiptConfirmation._write_accounting_plan(writer, h.key, item),
        lambda: writer._delegate(h.adapter._capability, h.key, item),
        lambda: h.adapter.db.append_raw('支出明細', [['evil']]),
        lambda: h.adapter.db.update_row_raw('支出明細', 2, ['evil']),
        lambda: writer.save_item(h.key, dict(item, status='applied')),
        lambda: h.contract.dispatch(lambda: None), lambda: h.store2.save(h.store2.value)]
    for attack in attacks:
        with pytest.raises(StateError): attack()
    assert h.adapter.dispatches == 1
    assert [io.read_versioned() for io in (h.native, h.shared.io, h.ledger)] == saved


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('stop', ['after_ledger_1', 'last_ledger'])
def test_native_lost_response_readback_cannot_clear_sticky_unknown(kind, stop):
    h = harness(kind)
    point = 'after_ledger_'+str(len(h.c['plan'])) if stop == 'last_ledger' else stop
    def timeout(name):
        if name == point: raise TimeoutError('synthetic timeout after durable ledger write')
    h.adapter.checkpoint = timeout
    with pytest.raises(StateError): h.adapter.submit(h.c)
    assert h.adapter.dispatches == 1
    assert h.ledger.writes == (len(h.c['plan']) if stop == 'last_ledger' else 1)
    assert h.store2.read()[0]['operations'][h.c['operation']]['phase'] == 'writer_started'
    assert readonly(compose(h))['classification'] == ('UNKNOWN' if stop == 'last_ledger' else 'PARTIAL')


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_in_body_callbacks_cannot_reenter_writer_write_db_or_forge_native_result(kind):
    h = harness(kind); rejected = []
    def attack(point):
        if point != 'before_ledger_1': return
        item = deepcopy(h.store2.read()[0]['operations'][h.c['operation']]['pending'])
        attacks = [lambda: ReceiptConfirmation._write_accounting_plan(h.adapter.writer, h.key, deepcopy(item)),
            lambda: h.adapter.db.append_raw('支出明細', [h.c['plan'][1][1]]),
            lambda: h.adapter.writer.save_item(h.key, dict(item, status='applied')),
            lambda: h.adapter._native_completed(h.adapter.writer, h.key, dict(item, status='applied'))]
        for operation in attacks:
            with pytest.raises(StateError): operation()
            rejected.append(True)
        assert h.ledger.writes == 0
    h.adapter.checkpoint = attack
    assert h.adapter.submit(h.c)['classification'] == 'COMPLETE'
    assert len(rejected) == 4 and h.adapter.dispatches == 1
    assert h.ledger.writes == len(h.c['plan'])


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_full_ledger_after_crash_cannot_manually_inject_applied_result(kind):
    h = harness(kind)
    def crash(point):
        if point == 'after_ledger_'+str(len(h.c['plan'])): raise Crash(point)
    h.adapter.checkpoint = crash
    with pytest.raises(Crash): h.adapter.submit(h.c)
    saved = h.native.read_versioned()
    item = deepcopy(h.store2.read()[0]['operations'][h.c['operation']]['pending'])
    with pytest.raises(StateError):
        h.adapter._native_completed(h.adapter.writer, h.key, dict(item, status='applied'))
    assert h.native.read_versioned() == saved
    assert readonly(compose(h))['classification'] == 'UNKNOWN'


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('entry', ['normal_auto', 'buyback_income', 'pdf_auto', 'pdf_manual', 'grouped_unit'])
def test_other_ingress_whole_or_unit_fence_blocks_before_native_consume(kind, entry):
    from app.page_receipt_model import SourceRef, stable_page
    from app.page_receipt_model import PageUnit
    from app.pdf_intake_authority import review_identity
    from accounting_gate_fixtures import reader_for
    h = harness(kind); source = deepcopy(h.c['source'])
    if entry in {'pdf_auto', 'pdf_manual'}:
        # Conflicting independent source-kind observation must remain UNKNOWN;
        # never reinterpret an image as a PDF to make a legacy writer eligible.
        source['source_kind'] = 'pdf'
    ref = SourceRef(source_file_id=source['source_id'], source_content_hash=source['sha256'],
                    source_kind=source['source_kind'], page_count=1)
    unit = dict(unit_id='synthetic-other-unit', receipt_index=1, members=[dict(page=1,
        identity=stable_page(ref, 1), box=[0, 0, 1, 1])])
    pdf = entry in {'pdf_auto', 'pdf_manual', 'grouped_unit'}
    page = PageUnit(source=ref, page_number=1, stable_page_identity=stable_page(ref, 1),
        automatic_classification='unknown', automatic_reason='privacy_unresolved', observation_complete=True,
        extraction_status='extracted', observation_render_hash='a'*64, review_identity='0'*64,
        authority_revision=1).model_dump()
    page['review_identity'] = review_identity(page)
    other = reader_for(entry, source, b'fully-synthetic-legacy-confirmation-source', [unit] if pdf else [],
        dict(namespace=('pdf_unit_processing' if entry == 'grouped_unit' else 'pdf_intake') if pdf else 'receipt_confirmation',
             action='buyback_income_post' if entry == 'buyback_income' else 'post',
             native_page=page if pdf else None, scope='unit' if pdf else 'whole',
             unit_id=unit['unit_id'] if pdf else None, receipt_index=1 if pdf else None,
             input_snapshot=digest('other'), authority_revision=1, ledger_targets=['R-synthetic-other-unit']))
    ownership = other.reader.observe(entry, 'u1').value()
    req = AccountingProbeRequest.parse(dict(schema=PROBE_SCHEMA, synthetic_only=True, owner=h.c['owner'],
        request_id=str(uuid4()), expected_state_revision=0, operation_identity=ownership['operation_identity'],
        ownership=ownership, native_preflight_digest='a'*64))
    v, raw, tag = h.shared.read(); after = deepcopy(v); after['revision'] += 1
    after['operations'][req.operation_identity] = dict(request=asdict(req), fingerprint=req.fingerprint(),
        state='accepted', events=[dict(state='accepted', at=NOW)])
    h.shared.replace(v, raw, tag, after)
    with pytest.raises(StateError):
        h.adapter.submit(h.c, expected_shared_revision=1)
    op = h.store2.read()[0]['operations'][h.c['operation']]
    assert op['phase'] == 'qualified' and op['consume'] is None
    assert h.adapter.dispatches == h.ledger.writes == 0


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('status', ['pending', 'applied'])
def test_legacy_status_never_promoted(kind, status):
    h = harness(kind)
    v = json.loads(h.native.raw); v['legacy']['confirmation_items'][h.key]['status'] = status
    h.native.raw = json.dumps(v).encode()
    with pytest.raises(StateError): h.adapter.submit(h.c)
    assert h.adapter.dispatches == h.native.writes == h.shared.io.writes == h.ledger.writes == 0


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_same_request_different_content_rejected_and_complete_needs_every_evidence(kind):
    h = harness(kind); h.adapter.submit(h.c)
    changed = deepcopy(h.c); changed['shared_request_id'] = str(uuid4())
    changed['fingerprint'] = digest({k: v for k, v in changed.items() if k != 'fingerprint'})
    with pytest.raises(StateError, match='request_reuse'): h.adapter.submit(changed)
    assert h.adapter.dispatches == 1
    v = json.loads(h.native.raw); r = v['operations'][h.c['operation']]
    r['phase'] = 'writer_started'; r['completion'] = None
    h.native.raw = json.dumps(v).encode()
    compose(h)
    assert readonly(h)['classification'] == 'UNKNOWN'
    assert h.ledger.writes == len(h.c['plan'])


def test_delegate_call_inventory_is_explicit_and_has_no_production_client():
    root = Path(__file__).resolve().parents[1]
    source = root / 'staging_shadow/synthetic_legacy_writer.py'
    tree = ast.parse(source.read_text('utf-8'))
    delegates = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == '_write_accounting_plan']
    assert len(delegates) == 1 and ast.unparse(delegates[0].func) == 'ReceiptConfirmation._write_accounting_plan'
    imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not any(x and any(s in x for s in ('google', 'sheets', 'drive_clients', 'firestore', 'oauth')) for x in imports)
    names = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not {'apply_confirmations', 'save', 'execute', 'move_processed'} & names


@pytest.mark.parametrize('target', ['ledger', 'shared', 'native', 'service', 'validation_db', 'old_store'])
@pytest.mark.parametrize('change', ['marker', 'client'])
def test_composition_rejects_external_capability_before_any_state_write(target, change):
    h = harness('normal')
    obj = dict(ledger=h.ledger, shared=h.shared.io, native=h.native, service=h.service,
               validation_db=h.db, old_store=h.store)[target]
    if change == 'marker': obj.synthetic_memory_only = False
    else: obj.client = object()
    with pytest.raises(StateError):
        SyntheticLegacyConfirmation(h.contract, enabled=True, isolated_synthetic=True, fresh_attempt=True)
    assert h.native.writes == h.shared.io.writes == h.ledger.writes == 0


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('entry', ['normal_auto', 'buyback_income'])
def test_existing_shared_gate_cannot_reown_source_after_legacy_complete(kind, entry):
    from app.iphone_accounting_gate import SyntheticAccountingGate, NativePreflight
    from accounting_gate_fixtures import reader_for
    h = harness(kind); h.adapter.submit(h.c)
    other = reader_for(entry, h.c['source'], b'fully-synthetic-legacy-confirmation-source', [],
        dict(namespace='receipt_confirmation', scope='whole', unit_id=None, receipt_index=None,
             action='buyback_income_post' if entry == 'buyback_income' else 'post',
             input_snapshot=digest('different-ingress'), authority_revision=1,
             ledger_targets=['R-synthetic-other-unit']))
    def preflight(_, __, observation):
        # A qualified competing synthetic preflight is deliberately supplied.
        # It cannot replace the LEGACY authority, and must never reach a writer.
        return NativePreflight(digest(observation.value()), digest('validator'), digest('native-state'), '1')
    def forbidden(*args): pytest.fail('competing ingress bypassed shared ownership')
    gate = SyntheticAccountingGate(h.shared, other.reader, h.auth, preflight, forbidden,
        enabled=True, isolated_synthetic=True, clock=lambda: NOW)
    saved = [io.read_versioned() for io in (h.native, h.shared.io, h.ledger)]
    with pytest.raises(StateError): gate.submit(entry, 'u1', str(uuid4()), h.shared.read()[0]['revision'], None)
    assert [io.read_versioned() for io in (h.native, h.shared.io, h.ledger)] == saved
    assert h.adapter.dispatches == 1


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('change', ['G103', 'H301', 'old_p1', 'missing_authority', 'expired', 'revoked',
    'actor', 'purpose', 'automatic', 'partial_medical', 'plain_v1', 'duplicate'])
def test_forbidden_candidate_never_enters_writer(kind, change):
    from app.unified_receipt_write import OLD_P1_SOURCE
    h = harness(kind)
    if change in {'G103', 'H301'}:
        h.c['case_id'] = h.c['ownership']['case_id'] = change
    elif change == 'old_p1': h.c['source']['source_id'] = OLD_P1_SOURCE
    elif change == 'missing_authority': del h.c['authority']['actor']
    elif change == 'expired': h.contract.clock = lambda: NOW+600
    elif change == 'revoked': h.c['authority']['revoked'] = True
    elif change == 'actor': h.c['authority']['actor'] = '0'*64
    elif change == 'purpose': h.c['authority']['purpose'] = 'METADATA_REVIEW'
    elif change == 'automatic': h.c['inputs']['old']['decision_origin'] = 'automatic'
    elif change == 'partial_medical': h.c['inputs']['confirmed'][2 if kind == 'medical' else 5] = ''
    elif change == 'plain_v1': h.native.raw = json.dumps(h.store2.value).encode()
    else: h.db.rows['支出明細'].append(deepcopy(h.c['plan'][1][1]))
    h.c['fingerprint'] = digest({k: v for k, v in h.c.items() if k != 'fingerprint'})
    with pytest.raises((StateError, ValueError)): h.adapter.submit(h.c)
    assert h.adapter.dispatches == h.ledger.writes == h.native.writes == h.shared.io.writes == 0


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('change', ['formula', 'unrelated_value', 'target_id', 'duplicate', 'incomplete',
    'ledger_generation', 'start', 'consume', 'claim', 'completion'])
def test_real_completed_writer_requires_every_independent_evidence(kind, change):
    h = harness(kind); h.adapter.submit(h.c)
    if change in {'start', 'consume', 'claim', 'completion'}:
        v = json.loads(h.native.raw); v['operations'][h.c['operation']][change] = None
        h.native.raw = json.dumps(v).encode()
    else:
        v = json.loads(h.ledger.raw)
        if change == 'formula': v['snapshot'][1]['支出明細'][0][4] = '=1+3'
        elif change == 'unrelated_value': v['snapshot'][0]['支出明細'][0][4] = 4
        elif change == 'target_id': v['snapshot'][0]['支出明細'][-1][0] = 'wrong'
        elif change == 'duplicate': v['snapshot'][0]['支出明細'].append(deepcopy(v['snapshot'][0]['支出明細'][-1]))
        elif change == 'incomplete': v['complete'] = False
        h.ledger.raw = json.dumps(v).encode(); h.ledger.revision += 1
    assert readonly(compose(h))['classification'] in {'UNKNOWN', 'PARTIAL'}
    assert h.ledger.writes == len(h.c['plan'])


def test_existing_general_expense_is_updated_using_original_native_conditions():
    from app.receipt_reimport_production import target_snapshot
    from accounting_gate_fixtures import reader_for
    h = harness('normal'); c = h.c
    existing = deepcopy(c['plan'][1][1]); existing[3] = 'old item'; existing[4] = 60
    h.db.rows['支出明細'].append(existing)
    item = h.service.items[h.key]
    item['before'] = target_snapshot(h.service.tables(), item['source']['source_id'])
    c['inputs']['old'] = deepcopy(item)
    c['before'][0]['支出明細'].append(deepcopy(existing))
    c['before'][1]['支出明細'].append(deepcopy(existing))
    c['plan'] = h.service._plan(item, h.service._parsed(item), '')
    c['authority']['input_fingerprint'] = digest(c['inputs'])
    c['authority']['scope_digest'] = digest([c['operation'], c['source'], c['plan'], c['before']])
    proof = h.auth.verify_post(None, 'ACCOUNTING_POST', c['request_id'], digest(c['inputs']), 0)
    c['authority']['authenticated_action_digest'] = digest(asdict(proof))
    own = reader_for('legacy_confirmation', c['source'], b'fully-synthetic-legacy-confirmation-source', [],
        dict(namespace='receipt_confirmation', scope='whole', unit_id=None, receipt_index=None,
             input_snapshot=digest(c['inputs']), authority_revision=7, ledger_targets=[r[0] for _, r in c['plan']],
             case_id=c['case_id']))
    c['ownership'] = own.reader.observe('legacy_confirmation', 'u1').value()
    c['fingerprint'] = digest({k: v for k, v in c.items() if k != 'fingerprint'})
    h.current = dict(source=c['source'], inputs=c['inputs'], authority=c['authority'])
    h.readers.current = lambda: deepcopy(h.current)
    h.readers.ownership = lambda: own.reader.observe('legacy_confirmation', 'u1')
    native = json.loads(h.native.raw); native['legacy']['confirmation_items'][h.key] = deepcopy(item)
    h.native.raw = json.dumps(native).encode()
    h.ledger.raw = json.dumps(dict(complete=True, snapshot=c['before'])).encode()
    h.readers.before_observation = token(*h.ledger.read_versioned())
    assert h.adapter.submit(c)['classification'] == 'COMPLETE'
    assert [e[0] for e in h.adapter.db.events] == ['update', 'update', 'append', 'append']
    assert observe_ledger(h.ledger)[0] == planned_after(c['before'], c['plan'])


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('stop', [None, 'after_ledger_1', 'after_native_applied', 'after_native_completion'])
def test_separate_process_rebuilds_saved_evidence_read_only(kind, stop):
    h = harness(kind)
    if stop:
        def crash(p):
            if p == stop: raise Crash(p)
        h.adapter.checkpoint = crash
        with pytest.raises(Crash): h.adapter.submit(h.c)
    else: h.adapter.submit(h.c)
    packet = dict(candidate=h.c, manifest=h.manifest, spreadsheet=h.sid,
        stores=[dict(raw=io.raw.decode(), revision=io.revision) for io in (h.native, h.shared.io, h.ledger)])
    script = '''
import json,sys,socket
def forbidden(*args,**kwargs):raise AssertionError('external connection in child')
socket.create_connection=socket.socket.connect=forbidden
from test_synthetic_legacy_writer import harness,compose,readonly
p=json.load(sys.stdin);h=harness(p['candidate']['kind']);h.c=p['candidate']
h.current=dict(source=h.c['source'],inputs=h.c['inputs'],authority=h.c['authority'])
# Reconstruct readers from only synthetic saved input, with NO writer invocation.
h.readers.current=lambda:h.current
h.readers.ownership=lambda:type('O',(),{'value':lambda self:h.c['ownership']})()
for io,s in zip((h.native,h.shared.io,h.ledger),p['stores']):io.raw=s['raw'].encode();io.revision=s['revision']
h.manifest=p['manifest'];h.sid=p['spreadsheet'];compose(h)
r=readonly(h);assert h.adapter.dispatches==0
print(json.dumps(r))
'''
    env = __import__('os').environ.copy()
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1]) + __import__('os').pathsep + str(Path(__file__).parent)
    proc = subprocess.run([sys.executable, '-c', script], input=json.dumps(packet), text=True,
        capture_output=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    classification = json.loads(proc.stdout)['classification']
    assert classification == ('COMPLETE' if stop is None else 'PARTIAL' if stop == 'after_ledger_1' else 'UNKNOWN')
