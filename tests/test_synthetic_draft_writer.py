"""Real existing writer -> disposable ledger -> independent v3/shared proof."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.drive_run_state import StateError
from app.receipt_confirmation import ReceiptConfirmation
from app.receipt_confirmation_contract import encoded, load, observe_ledger, planned_after, token
from app.receipt_reimport_production import ReimportStore
from app.unified_receipt_review import digest
from staging_shadow.receipt_draft_contract import SCHEMA, PROVIDER
from staging_shadow.synthetic_draft_writer import SyntheticDraftConfirmation
from staging_shadow.synthetic_legacy_writer import SyntheticLegacyConfirmation
from draft_runtime_fixtures import harness as runtime_harness, rebuild, rehash, set_value, RAW, NOW
from test_legacy_contract_poc import forbid_clients_and_writers
from test_synthetic_legacy_writer import no_external_connections


class Crash(BaseException): pass


@pytest.fixture(autouse=True)
def forbid_v1_and_real_clients(monkeypatch):
    def deny(*args, **kwargs): pytest.fail('v1 save/apply entered from v3 composition')
    monkeypatch.setattr(ReceiptConfirmation, 'apply_confirmations', deny)
    monkeypatch.setattr(ReimportStore, 'save', deny)


def compose(h, *, fresh=False):
    rebuild(h, fresh=fresh)
    h.adapter = SyntheticDraftConfirmation(h.contract, enabled=True, isolated_synthetic=True, fresh_attempt=fresh)
    return h


def harness(kind, monkeypatch): return compose(runtime_harness(kind, monkeypatch), fresh=True)
def frozen(h): return [io.read_versioned() for io in (h.native, h.shared.io, h.ports['ledger'])]


def read_only(h):
    before = frozen(h); result = h.adapter.inspect(h.c)
    assert frozen(h) == before and not result['accounting_permission'] and not result['real_native_complete']
    assert result['synthetic_only'] and not result['retry_allowed'] and not result['retry_permission']
    return result


def immutable(h, legacy, draft):
    assert h.store.value == legacy and h.f.store.value == h.f.baseline and h.f.db.writes == 0
    assert h.ports['draft'].read_versioned() == draft and h.f.financial_calls == 0


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_modified_plan_is_written_by_original_body_and_fully_read_back(kind, monkeypatch):
    h = harness(kind, monkeypatch); legacy, draft = deepcopy(h.store.value), h.ports['draft'].read_versioned()
    original = ReceiptConfirmation._write_accounting_plan; calls = []
    def audited(service, key, item):
        calls.append((key, deepcopy(item))); return original(service, key, item)
    monkeypatch.setattr(ReceiptConfirmation, '_write_accounting_plan', audited)
    result = h.adapter.submit(h.c)
    assert result['classification'] == 'COMPLETE' and result['writer_dispatches'] == 1
    assert len(calls) == 1 and calls[0][1]['inputs'] == h.c['inputs']['proposed']['confirmed']
    assert calls[0][1]['inputs'] != h.c['inputs']['baseline']['item']['inputs']
    actual, observation = observe_ledger(h.ports['ledger'])
    assert actual == planned_after(h.c['before'], h.c['plan'])
    assert h.adapter.db.events == h.adapter.db.expected and h.ports['ledger'].writes == len(h.c['plan'])
    assert h.native.writes == 5 and h.shared.io.writes == 4  # Includes one prior memory inheritance.
    op = h.store.read()[0]['operations'][h.c['operation']]
    assert op['phase'] == 'complete_fixture' and op['pending']['status'] == 'pending'
    assert op['completion']['observation'] == observation == token(*h.ports['ledger'].read_versioned())
    assert op['completion']['fingerprint'] == op['consume']['fingerprint'] == op['start']['fingerprint'] == h.c['fingerprint']
    assert op['completion']['mode'] == PROVIDER
    assert h.shared.read()[0]['operations'][h.c['operation']]['state'] == 'complete'
    before_plan = h.c['inputs']['proposed']['original_plan']
    assert [row[1][4] for row in before_plan[1:-1]] == ([70, 50] if kind == 'normal' else [120])
    assert [row[1][4] for row in h.c['plan'][1:-1]] == ([80, 40] if kind == 'normal' else [135])
    assert actual[0]['レシート'][0 if kind == 'normal' else -1][1:5] == [
        '2026-01-03', 'Synthetic edited shop' if kind == 'normal' else 'Synthetic manual clinic',
        120 if kind == 'normal' else 135, 'Synthetic card']
    rows = [r for r in actual[0]['支出明細'] if r[0].startswith('R-'+h.f.sid+'-')]
    assert [r[0] for r in rows] == [r[0] for _, r in h.c['plan'][1:-1]]
    assert rows[0][5:7] == (['日用品', '消耗品'] if kind == 'normal' else ['医療・保険', '病院'])
    if kind == 'normal':
        assert rows[0][11] == 'preserve-label'
        assert [e[0] for e in h.adapter.db.events] == ['update', 'update', 'append', 'append']
    else: assert [e[0] for e in h.adapter.db.events] == ['append']*3
    for part in (0, 1):
        assert next(r for r in actual[part]['支出明細'] if r[0] == 'unrelated-id') == next(
            r for r in h.c['before'][part]['支出明細'] if r[0] == 'unrelated-id')
    assert next(r for r in actual[1]['支出明細'] if r[0] == 'unrelated-id')[4] == '=1+2'
    immutable(h, legacy, draft)
    saved = frozen(h)
    for _ in range(2): assert h.adapter.submit(h.c)['classification'] == 'COMPLETE'
    compose(h); assert h.adapter.submit(h.c)['classification'] == 'COMPLETE'
    assert h.adapter.dispatches == 0 and len(calls) == 1 and frozen(h) == saved


POINTS = ['before_qualification', 'after_qualification', 'before_shared_claim', 'after_shared_accept', 'after_shared_claim',
    'before_consume', 'after_consume', 'before_start', 'after_start', 'before_dispatch', 'before_dispatch_lease',
    'after_dispatch_lease', 'before_ledger_1', 'after_ledger_1', 'after_ledger_2', 'after_native_applied',
    'before_completion', 'after_completion', 'after_native_completion', 'before_shared_completion',
    'after_shared_awaiting_readback', 'before_shared_complete', 'after_shared_complete', 'after_shared_completion']


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('point', POINTS)
def test_crash_keeps_pending_fence_and_forbids_additional_writes(kind, point, monkeypatch):
    h = harness(kind, monkeypatch); legacy, draft = deepcopy(h.store.value), h.ports['draft'].read_versioned()
    def stop(name):
        if name == point: raise Crash(name)
    h.contract.checkpoint = h.adapter.checkpoint = stop
    with pytest.raises(Crash): h.adapter.submit(h.c)
    assert h.adapter.dispatches <= 1
    saved = frozen(h); compose(h)
    result = read_only(h)
    expected = 'PARTIAL' if point in {'after_ledger_1', 'after_ledger_2'} else 'COMPLETE' if point in {
        'after_shared_complete', 'after_shared_completion'} else 'UNKNOWN'
    assert result['classification'] == expected
    op = h.store.read()[0]['operations'].get(h.c['operation'])
    if op and op['phase'] != 'qualified': assert op['pending'] and op['consume']
    if op: h.adapter.submit(h.c)
    else:
        with pytest.raises(StateError): h.adapter.submit(h.c)
    assert h.adapter.dispatches == 0 and frozen(h) == saved
    immutable(h, legacy, draft)


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('backend,number', [('native', 1), ('native', 2), ('native', 3), ('native', 4),
    ('shared', 1), ('shared', 2), ('shared', 3), ('shared', 4), ('ledger', 1), ('ledger', 2), ('ledger', 3)])
@pytest.mark.parametrize('fault', ['412', 'timeout', 'lost', 'readback', 'tag'])
def test_three_store_cas_ambiguity_never_retries(kind, backend, number, fault, monkeypatch):
    h = harness(kind, monkeypatch); io = {'native': h.native, 'shared': h.shared.io, 'ledger': h.ports['ledger']}[backend]
    original = io.replace_versioned; attempts = []
    def injected(*args):
        attempts.append(1)
        if len(attempts) != number: return original(*args)
        if fault == '412': raise StateError('HTTP_412')
        if fault == 'timeout': raise TimeoutError('synthetic timeout before commit')
        old_revision = io.revision; original(*args)
        if fault == 'lost': raise TimeoutError('synthetic response lost')
        if fault == 'tag': io.revision = old_revision
        else:
            v = load(io.raw)
            if backend == 'ledger': v['snapshot'][1]['支出明細'][0][4] = '=7+9'
            else: v['revision'] += 1
            io.raw = encoded(v); io.revision += 1
    monkeypatch.setattr(io, 'replace_versioned', injected)
    with pytest.raises((StateError, TimeoutError)): h.adapter.submit(h.c)
    assert len(attempts) == number and h.adapter.dispatches <= 1
    saved = frozen(h); compose(h)
    assert read_only(h)['classification'] in {'UNKNOWN', 'PARTIAL', 'COMPLETE'}
    try: h.adapter.submit(h.c)
    except StateError: pass
    assert h.adapter.dispatches == 0 and frozen(h) == saved and len(attempts) == number


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('instances', [2, 16])
def test_independent_instances_dispatch_once_and_no_duplicate_rows(kind, instances, monkeypatch):
    h = harness(kind, monkeypatch); barrier = Barrier(instances)
    def one(_):
        local = SimpleNamespace(**vars(h)); compose(local, fresh=True)
        local.contract.checkpoint = lambda p: barrier.wait(timeout=60) if p == 'before_qualification' else None
        try: local.adapter.submit(local.c)
        except StateError: pass
        return local.adapter.dispatches
    with ThreadPoolExecutor(max_workers=instances) as pool: counts = list(pool.map(one, range(instances)))
    assert sum(counts) == 1 and h.native.writes == 5 and h.shared.io.writes == 4
    actual = observe_ledger(h.ports['ledger'])[0]
    assert actual == planned_after(h.c['before'], h.c['plan']) and h.ports['ledger'].writes == len(h.c['plan'])
    for table, rows in actual[0].items(): assert len({r[0] for r in rows}) == len(rows)
    compose(h); assert read_only(h)['classification'] == 'COMPLETE'


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('point', ['before_qualification', 'before_consume', 'before_start', 'before_dispatch',
    'after_dispatch_lease', 'before_ledger_2', 'before_completion', 'before_shared_complete'])
@pytest.mark.parametrize('change', ['draft', 'source', 'master', 'authority'])
def test_independent_version_drift_stops_at_each_write_boundary(kind, point, change, monkeypatch):
    h = harness(kind, monkeypatch); changed = False; writes_at_change = None
    def drift(name):
        nonlocal changed, writes_at_change
        if name != point or changed: return
        changed = True; writes_at_change = h.ports['ledger'].writes
        port = h.ports[dict(master='categories').get(change, change)]
        v = load(port.raw)
        if change == 'source': v['source']['drive_numeric_version'] = '2'
        else: v['revision'] = str(v['revision'])+'x' if change == 'master' else v['revision']+1
        set_value(port, v)
    h.adapter.checkpoint = h.contract.checkpoint = drift
    with pytest.raises(StateError): h.adapter.submit(h.c)
    assert changed and h.ports['ledger'].writes == writes_at_change
    saved = frozen(h); compose(h); assert read_only(h)['classification'] == 'UNKNOWN'
    try: h.adapter.submit(h.c)
    except StateError: pass
    assert frozen(h) == saved and h.adapter.dispatches == 0


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('context', [dict(actor_id='0'*64), dict(session_active=False), dict(csrf_valid=False),
    dict(purpose='DRAFT_ONLY'), dict(expires_at=NOW), dict(created_at=NOW+1), dict(expected_revision=1), dict(method='GET')])
def test_financial_auth_never_relaxed(kind, context, monkeypatch):
    h = harness(kind, monkeypatch); before = frozen(h)
    with pytest.raises(StateError): h.adapter.submit(h.c, context)
    assert frozen(h) == before and h.adapter.dispatches == 0


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('where', ['baseline', 'proposed', 'plan'])
def test_public_rehash_cannot_override_independent_values(kind, where, monkeypatch):
    h = harness(kind, monkeypatch)
    if where == 'baseline': h.c['inputs']['baseline']['item']['inputs'][0] = 'edited'
    elif where == 'proposed': h.c['inputs']['proposed']['parsed']['total'] = 999
    else: h.c['plan'][0][1][2] = 'wrong merchant'
    c = h.c; c['authority']['input_fingerprint'] = digest(c['inputs'])
    c['authority']['scope_digest'] = digest([c['operation'], c['source'], c['plan'], c['before']])
    c['ownership']['input_snapshot'] = digest(c['inputs']); rehash(c)
    before = frozen(h)
    with pytest.raises(StateError): h.adapter.submit(c)
    assert frozen(h) == before and h.adapter.dispatches == 0


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_v2_adapter_and_direct_completion_stay_closed(kind, monkeypatch):
    h = harness(kind, monkeypatch); before = frozen(h)
    with pytest.raises(StateError): SyntheticLegacyConfirmation(h.contract, enabled=True, isolated_synthetic=True, fresh_attempt=True)
    with pytest.raises(StateError): h.contract.dispatch(h.c, composition=h.adapter)
    with pytest.raises(StateError): h.contract.complete_fixture(h.c, {}, composition=h.adapter, expected_revision=1)
    with pytest.raises(StateError): h.adapter._dispatch_once(h.c)
    assert frozen(h) == before


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_complete_ledger_without_writer_applied_evidence_is_unknown(kind, monkeypatch):
    h = harness(kind, monkeypatch)
    def no_writer_result(*args):
        # Negative fixture observation only; never counted as posting success.
        set_value(h.ports['ledger'], dict(complete=True, snapshot=planned_after(h.c['before'], h.c['plan'])))
        return dict(status='applied', plan=deepcopy(h.c['plan']))  # A return value is not native save evidence.
    monkeypatch.setattr(ReceiptConfirmation, '_write_accounting_plan', no_writer_result)
    with pytest.raises(StateError, match='completion_missing'): h.adapter.submit(h.c)
    op = h.store.read()[0]['operations'][h.c['operation']]
    assert op['phase'] == 'writer_started' and op['completion'] is None
    saved = frozen(h); compose(h); assert read_only(h)['classification'] == 'UNKNOWN'
    h.adapter.submit(h.c); assert frozen(h) == saved and h.adapter.dispatches == 0


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('entry', ['normal_auto', 'buyback_income', 'grouped_unit'])
def test_other_ingress_whole_or_pdf_unit_ownership_blocks_dispatch(kind, entry, monkeypatch):
    from accounting_gate_fixtures import reader_for
    from app.iphone_accounting_gate import AccountingProbeRequest, SCHEMA as PROBE_SCHEMA
    from app.page_receipt_model import SourceRef, stable_page
    h = harness(kind, monkeypatch); source = h.c['source']; pdf = entry == 'grouped_unit'
    unit = dict(unit_id='synthetic-other-unit', receipt_index=1, members=[dict(page=1,
        identity=stable_page(SourceRef(source_file_id=source['source_id'], source_content_hash=source['sha256'],
            source_kind='image', page_count=1), 1), box=[0, 0, 1, 1])])
    other = reader_for(entry, source, RAW, [unit] if pdf else [], dict(namespace='pdf_unit_processing' if pdf else 'receipt_confirmation',
        scope='unit' if pdf else 'whole', unit_id=unit['unit_id'] if pdf else None, receipt_index=1 if pdf else None,
        input_snapshot=digest('different-ingress'), authority_revision=1, ledger_targets=['other-expense'],
        action='buyback_income_post' if entry == 'buyback_income' else 'post'))
    own = other.reader.observe(entry, 'u1').value()
    req = AccountingProbeRequest.parse(dict(schema=PROBE_SCHEMA, synthetic_only=True, owner=h.c['owner'], request_id=str(uuid4()),
        expected_state_revision=0, operation_identity=own['operation_identity'], ownership=own, native_preflight_digest=digest('other-preflight')))
    v, raw, tag = h.shared.read(); after = deepcopy(v); after['revision'] += 1
    after['operations'][req.operation_identity] = dict(request=asdict(req), fingerprint=req.fingerprint(), state='accepted', events=[dict(state='accepted', at=NOW)])
    h.shared.replace(v, raw, tag, after); shared = h.shared.io.read_versioned()
    with pytest.raises(StateError): h.adapter.submit(h.c, expected_shared_revision=1)
    assert h.adapter.dispatches == 0 and h.ports['ledger'].writes == 0 and h.shared.io.read_versioned() == shared
    saved = frozen(h); compose(h)
    assert read_only(h)['classification'] == 'UNKNOWN'; h.adapter.submit(h.c)
    assert frozen(h) == saved


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('stop', [None, 'after_ledger_1', 'after_native_applied', 'after_native_completion'])
def test_separate_process_reconstructs_exact_result_without_mutation(kind, stop, monkeypatch):
    h = harness(kind, monkeypatch)
    if stop:
        def crash(name):
            if name == stop: raise Crash(name)
        h.contract.checkpoint = h.adapter.checkpoint = crash
        with pytest.raises(Crash): h.adapter.submit(h.c)
    else: h.adapter.submit(h.c)
    saved = frozen(h)
    def serial(io): return dict(value=load(io.raw), revision=io.revision)
    payload = dict(native=serial(h.native), shared=serial(h.shared.io), manifest=h.manifest, spid=h.spid, pin=h.pin, c=h.c,
        ports={k: serial(v) for k, v in h.ports.items() if k != 'source_bytes'})
    code = '''
import json,sys
from types import SimpleNamespace
from test_legacy_contract_poc import mem
from draft_runtime_fixtures import SourceBytes,RAW,OWNER,NOW,rebuild
from app.receipt_confirmation_draft_request import DraftReaders
from app.unified_receipt_write import ConditionalRequests
from app.unified_receipt_purposes import PurposeBoundary,VerifiedRead
from app.unified_receipt_review import digest
from accounting_gate_fixtures import reader_for
from staging_shadow.synthetic_draft_writer import SyntheticDraftConfirmation
v=json.load(sys.stdin);h=SimpleNamespace(**v)
def restore(s):
 io=mem(s['value']);io.revision=s['revision'];return io
h.native=restore(v['native']);h.shared=ConditionalRequests(restore(v['shared']),'restored',synthetic_accounting=True)
h.ports={k:restore(s) for k,s in v['ports'].items()}
s=h.c['source'];o=reader_for('legacy_confirmation',s,RAW,[],dict(namespace='receipt_confirmation',scope='whole',unit_id=None,receipt_index=None,
input_snapshot=digest(h.c['inputs']),authority_revision=7,ledger_targets=[r[0] for _,r in h.c['plan']],case_id=h.c['case_id']))
h.readers=DraftReaders(**h.ports,source_bytes=SourceBytes(s['source_id'],s['drive_revision_id']),ownership=lambda:o.reader.observe('legacy_confirmation','u1'))
def no_post(*a):raise AssertionError('readonly reconstruction attempted financial authentication')
h.auth=PurposeBoundary(OWNER,no_post,lambda _:VerifiedRead(OWNER,NOW+600,True),lambda:[],clock=lambda:NOW)
rebuild(h);a=SyntheticDraftConfirmation(h.contract,enabled=True,isolated_synthetic=True)
result=a.inspect(h.c);again=a.submit(h.c)
assert result==again and a.dispatches==0 and h.native.writes==h.shared.io.writes==h.ports['ledger'].writes==0
print(json.dumps(result))
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).parents[1]/'tests',
        env=dict(__import__('os').environ, PYTHONPATH=str(Path(__file__).parents[1])), input=json.dumps(payload),
        text=True, capture_output=True, check=True)
    result = json.loads(result.stdout)
    assert result['classification'] == ('COMPLETE' if stop is None else 'PARTIAL' if stop == 'after_ledger_1' else 'UNKNOWN')
    assert result['writer_dispatches'] == 0 and frozen(h) == saved


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('field,value', [('session_active', False), ('csrf_valid', False), ('purpose', 'DRAFT_ONLY'), ('expires_at', NOW)])
def test_auth_change_after_first_write_forbids_second_write(kind, field, value, monkeypatch):
    h = harness(kind, monkeypatch)
    def change(name):
        if name == 'after_ledger_1':
            verify = h.auth.verify_post
            h.auth.verify_post = lambda *a: type(verify(*a))(**dict(asdict(verify(*a)), **{field: value}))
    h.adapter.checkpoint = change
    with pytest.raises(StateError): h.adapter.submit(h.c)
    assert h.ports['ledger'].writes == 1 and h.adapter.dispatches == 1
    saved = frozen(h); compose(h); read_only(h)
    h.adapter.submit(h.c); assert frozen(h) == saved


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('change', ['G103', 'H301', 'old_p1', 'legacy_pending', 'legacy_applied', 'plain_v1', 'plain_v2', 'unknown_root'])
def test_historical_holds_and_old_roots_never_dispatch(kind, change, monkeypatch):
    from app.unified_receipt_write import OLD_P1_SOURCE
    h = harness(kind, monkeypatch)
    if change in {'G103', 'H301'}: h.c['case_id'] = h.c['ownership']['case_id'] = change; rehash(h.c)
    elif change == 'old_p1': h.c['source']['source_id'] = OLD_P1_SOURCE; rehash(h.c)
    else:
        v = load(h.native.raw)
        if change == 'plain_v1': v = deepcopy(h.legacy)
        elif change == 'plain_v2': v = dict(schema='receipt-reimport-v2', revision=0, legacy=h.legacy, operations={})
        elif change == 'unknown_root': v['schema'] = 'receipt-reimport-v99'
        else: v['legacy']['confirmation_items'][h.c['review_id']]['status'] = change.removeprefix('legacy_')
        h.native.raw = encoded(v)
    saved = frozen(h)
    with pytest.raises(StateError): h.adapter.submit(h.c)
    assert frozen(h) == saved and h.adapter.dispatches == h.ports['ledger'].writes == 0


@pytest.mark.parametrize('change', ['partial', 'ai', 'ocr', 'candidate'])
def test_medical_nonmanual_snapshot_never_dispatches(change, monkeypatch):
    h = harness('medical', monkeypatch); d = load(h.ports['draft'].raw)
    if change == 'partial': d['value']['fields']['merchant'] = ''
    elif change == 'candidate': d['value']['items'] = [{'ocr': 'not manual'}]
    else: d['value']['provenance'] = change
    d['input_digest'] = digest(d['value']); set_value(h.ports['draft'], d)
    saved = frozen(h)
    with pytest.raises(StateError): h.adapter.submit(h.c)
    assert frozen(h) == saved and h.adapter.dispatches == 0


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('change', ['formula', 'unrelated', 'order', 'id', 'ledger_tag', 'source', 'draft', 'master', 'completion', 'start', 'consume', 'claim'])
def test_complete_requires_all_current_independent_proofs(kind, change, monkeypatch):
    h = harness(kind, monkeypatch); h.adapter.submit(h.c)
    if change in {'completion', 'start', 'consume', 'claim'}:
        v = load(h.native.raw); v['operations'][h.c['operation']][change] = None; h.native.raw = encoded(v)
    elif change in {'source', 'draft', 'master'}:
        p = h.ports['categories' if change == 'master' else change]; p.revision += 1
    elif change == 'ledger_tag': h.ports['ledger'].revision += 1
    else:
        p = h.ports['ledger']; v = load(p.raw)
        if change == 'formula': v['snapshot'][1]['支出明細'][0][4] = '=8+9'
        elif change == 'unrelated':
            next(r for r in v['snapshot'][0]['支出明細'] if r[0] == 'unrelated-id')[4] = 99
        elif change == 'order':
            for part in v['snapshot']: part['支出明細'].reverse()
        else:
            for part in v['snapshot']: part['支出明細'][-1][0] = 'invented'
        set_value(p, v)
    saved = frozen(h); compose(h)
    assert read_only(h)['classification'] in {'PARTIAL', 'UNKNOWN'}
    try: h.adapter.submit(h.c)
    except StateError: pass
    assert frozen(h) == saved and h.adapter.dispatches == 0


def test_same_instance_double_tap_is_readonly_during_dispatch(monkeypatch):
    h = harness('normal', monkeypatch); entered, release = Event(), Event()
    def pause(point):
        if point == 'after_dispatch_lease': entered.set(); assert release.wait(timeout=30)
    h.adapter.checkpoint = pause
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(h.adapter.submit, h.c); assert entered.wait(timeout=30)
        with pytest.raises(StateError, match='in_flight_read_only'): h.adapter.submit(h.c)
        release.set(); assert first.result(timeout=30)['classification'] == 'COMPLETE'
    assert h.adapter.dispatches == 1 and h.ports['ledger'].writes == 4


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('change', ['request_id', 'memo'])
def test_different_reuse_after_complete_is_denied_without_any_write(kind, change, monkeypatch):
    h = harness(kind, monkeypatch); h.adapter.submit(h.c); saved = frozen(h)
    c = deepcopy(h.c)
    if change == 'request_id': c['request_id'] = c['authority']['request_id'] = str(uuid4())
    else:
        c['inputs']['draft']['value']['fields']['memo'] = c['inputs']['proposed']['confirmed'][7] = 'changed'
        c['inputs']['draft']['input_digest'] = digest(c['inputs']['draft']['value'])
    c['authority']['input_fingerprint'] = digest(c['inputs']); c['ownership']['input_snapshot'] = digest(c['inputs']); rehash(c)
    with pytest.raises(StateError): h.adapter.submit(c)
    assert frozen(h) == saved and h.adapter.dispatches == 1
    compose(h, fresh=True); assert h.adapter.submit(h.c)['classification'] == 'COMPLETE'
    assert frozen(h) == saved and h.adapter.dispatches == 0


@pytest.mark.parametrize('target', ['native', 'shared', 'ledger', 'draft', 'source', 'master', 'authority', 'bytes'])
@pytest.mark.parametrize('change', ['marker', 'client'])
def test_composition_rejects_nonisolated_port_before_any_write(target, change, monkeypatch):
    h = runtime_harness('normal', monkeypatch)
    p = h.native if target == 'native' else h.shared.io if target == 'shared' else h.ports[dict(master='categories', bytes='source_bytes').get(target, target)]
    if change == 'marker': p.synthetic_memory_only = False
    else: p.credentials = object()
    saved = frozen(h)
    with pytest.raises(StateError): SyntheticDraftConfirmation(h.contract, enabled=True, isolated_synthetic=True, fresh_attempt=True)
    assert frozen(h) == saved


def test_v3_writer_ci_is_dedicated_and_has_no_production_capability():
    import yaml
    root = Path(__file__).resolve().parents[1]
    expected = yaml.load((root/'.github/workflows/iphone-draft-runtime-checks.yml').read_text('utf8'), Loader=yaml.BaseLoader)
    expected['name'] = 'iPhone v3 draft guarded writer synthetic'
    expected['on']['pull_request']['branches'] = ['codex/iphone-draft-native-v3-runtime']
    expected['concurrency']['group'] = 'kakeibo-iphone-v3-writer-${{ github.ref }}'
    job = expected['jobs'].pop('native-draft'); expected['jobs']['synthetic-v3'] = job
    steps = job['steps']
    steps[3] = dict(name='Collect v3 writer integration tests', run='python -m pytest --collect-only -q tests/test_synthetic_draft_writer.py')
    steps[4] = dict(name='Execute v3 writer integration tests', run='python -m pytest -vv tests/test_synthetic_draft_writer.py')
    steps[5] = dict(name='Preserve R0 design and isolated runtime evidence',
        run='python -m pytest -q tests/test_iphone_mvp_r0_adoption.py tests/test_iphone_draft_request_design.py tests/test_receipt_draft_runtime.py')
    steps[-1]['run'] = ('python -m compileall -q staging_shadow/receipt_draft_contract.py staging_shadow/synthetic_draft_writer.py '
        'tests/test_synthetic_draft_writer.py\ngit diff --check\n')
    source = (root/'.github/workflows/iphone-v3-draft-writer-checks.yml').read_text('utf8')
    assert yaml.load(source, Loader=yaml.BaseLoader) == expected
    assert all(s not in source for s in ('secrets.', 'vars.', 'environment:', 'workflow_dispatch', 'push:', 'deploy'))
