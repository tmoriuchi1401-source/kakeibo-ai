"""Real receipt materializer, wholly synthetic ledger, guarded by 5B CAS/evidence."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier
from uuid import uuid4

import pytest

from app.drive_run_state import StateError
from app.bank_income import (INCOME_SHEET, INCOME_HEADERS, RECEIPT_BUYBACK_REASON,
                             RECEIPT_BUYBACK_SOURCE, income_id)
from app.sheets import HEADERS
from app.unified_receipt_review import digest
from staging_shadow.auto_native_evidence import ledger_outcome, ledger_view, observation_token
from staging_shadow.accounting_recovery_model import Crash
from staging_shadow.synthetic_auto_writer import SyntheticReceiptWriter, existing_plan
from auto_native_fixtures import Harness, encoded


@pytest.fixture
def h():
    value = Harness(connect_existing_writer=True)
    yield value


@pytest.fixture(autouse=True)
def deny_live_clients_and_ai(monkeypatch):
    from google.cloud import firestore
    from googleapiclient import discovery
    from app.receipt_pipeline import ReceiptPipeline
    from app.sheets import SheetsDB
    from app import google_clients

    def forbidden(*args, **kwargs):
        pytest.fail('synthetic writer must not construct or call a live client, AI, or process_bytes')

    for owner, name in ((ReceiptPipeline, 'process_bytes'), (ReceiptPipeline, '_require_ai'),
                        (SheetsDB, '__init__'), (firestore, 'Client'), (discovery, 'build'),
                        (google_clients, 'sheets_service'), (google_clients, 'drive_service'),
                        (google_clients, 'credentials'), (google_clients, 'download_drive_file')):
        if hasattr(owner, name):
            monkeypatch.setattr(owner, name, forbidden)


@pytest.mark.parametrize('kind', ['normal_auto', 'buyback_income'])
def test_existing_planner_golden_and_materializer_write_synthetic_ledger(h, kind):
    native_id, shared_id = str(uuid4()), str(uuid4())
    req = h.candidate(kind, 'u1')
    req.update(native_request_id=native_id, shared_request_id=shared_id, expected_revision=0)
    _, _, plan = existing_plan(req)
    assert digest(plan) == req['plan_digest']

    result = h.call(kind, native_id, shared_id, 0, gate=h.gate)
    assert result['state'] == 'complete'
    record = h.journal.read()[0]['operations'][req['ownership']['operation_identity']]
    saved = record['request']
    snapshot = ledger_view(h.ledger_io)[0]
    assert ledger_outcome(saved, snapshot) == 'complete'
    assert record['phase'] == 'complete'
    assert h.writer.dispatches == [req['ownership']['operation_identity']]
    assert [table for table, _ in h.writer.last_db.append_events] == [op['table'] for op in plan['operations']]
    assert h.adapter.report(native_id)['classification'] == 'COMPLETE'
    assert h.adapter.report(native_id)['real_native_complete'] is False
    if kind == 'normal_auto':
        assert [r[0] for r in snapshot[0]['支出明細'][1:]] == [r[0] for r in plan['operations'][1]['rows']]
        assert snapshot[0][INCOME_SHEET][1:] == []
    else:
        assert snapshot[0]['支出明細'][1:] == []
        assert [r[0] for r in snapshot[0][INCOME_SHEET][1:]] == [plan['operations'][1]['rows'][0][0]]
        assert snapshot[0][INCOME_SHEET][1][6:10] == plan['operations'][1]['rows'][0][6:10]


def test_writer_cannot_be_called_without_one_time_gate_capability(h):
    before = h.ledger_io.read_versioned()
    req = h.candidate('normal_auto', 'u1')
    writer = SyntheticReceiptWriter(h.ledger_io)
    with pytest.raises(StateError, match='guarded_lease'):
        writer.execute(req)
    with pytest.raises(StateError, match='guarded_lease'):
        h.writer.execute(req)
    assert h.writer.dispatches == []
    assert h.ledger_io.read_versioned() == before


@pytest.mark.parametrize('kind', ['normal_auto', 'buyback_income'])
def test_exact_replay_after_adapter_restart_never_dispatches_writer_twice(h, kind):
    native_id, shared_id = str(uuid4()), str(uuid4())
    h.call(kind, native_id, shared_id, 0, gate=h.gate)
    writer_before_restart = h.writer
    h.restart()
    replay = h.call(kind, native_id, shared_id, 0)
    assert replay['replay'] and replay['report']['classification'] == 'COMPLETE'
    assert sum(len(writer.dispatches) for writer in [*h.writer_runs, h.writer]) == 1
    assert writer_before_restart.dispatches == [next(iter(h.journal.read()[0]['operations']))]


@pytest.mark.parametrize('kind', ['normal_auto', 'buyback_income'])
def test_readback_in_separate_process_from_exported_synthetic_backend_state(h, kind):
    native_id, shared_id = str(uuid4()), str(uuid4())
    h.call(kind, native_id, shared_id, 0, gate=h.gate)
    state = {
        'financial': h.financial.raw.decode('utf-8'), 'financial_revision': h.financial.revision,
        'native': h.native_io.raw.decode('utf-8'), 'native_revision': h.native_io.revision,
        'ledger': h.ledger_io.raw.decode('utf-8'), 'ledger_revision': h.ledger_io.revision,
        'native_request_id': native_id,
    }
    code = r'''
import json,sys
from auto_native_fixtures import Harness
h=Harness(connect_existing_writer=True)
s=json.load(sys.stdin)
for io,key,rev in ((h.financial,'financial','financial_revision'),
                   (h.native_io,'native','native_revision'),
                   (h.ledger_io,'ledger','ledger_revision')):
    io.raw=s[key].encode('utf-8');io.revision=s[rev]
h.restart()
print(json.dumps(h.adapter.report(s['native_request_id']),sort_keys=True))
h.sandbox.close()
'''
    env = dict(os.environ)
    tests_root = str(Path(__file__).resolve().parent)
    env['PYTHONPATH'] = tests_root + os.pathsep + env.get('PYTHONPATH', '')
    child = subprocess.run([sys.executable, '-c', code], input=json.dumps(state), text=True,
                           capture_output=True, cwd=Path(__file__).resolve().parents[1],
                           env=env, timeout=40, check=True)
    report = json.loads(child.stdout.strip().splitlines()[-1])
    assert report['classification'] == 'COMPLETE'
    assert report['real_native_complete'] is False and report['retry_allowed'] is False


@pytest.mark.parametrize(('kind', 'stop_after'), [
    ('normal_auto', 'レシート'), ('normal_auto', '支出明細'), ('normal_auto', '取込データ'),
    ('buyback_income', 'レシート'), ('buyback_income', INCOME_SHEET), ('buyback_income', '取込データ'),
])
def test_crash_after_existing_writer_append_is_readback_only_and_never_reappends(h, kind, stop_after):
    native_id, shared_id = str(uuid4()), str(uuid4())

    def crash(title, count):
        if title == stop_after:
            raise Crash()

    h.writer.fault = crash
    with pytest.raises(Crash):
        h.call(kind, native_id, shared_id, 0, gate=h.gate)
    old_writer = h.writer
    old_dispatches = len(old_writer.dispatches)
    h.restart()
    outcome = h.adapter.report(native_id)
    expected = 'UNKNOWN' if stop_after == '取込データ' else 'PARTIAL'
    assert outcome['classification'] == expected
    before = h.ledger_io.read_versioned()
    with pytest.raises(StateError):
        h.call(kind, str(uuid4()), str(uuid4()), h.shared.read()[0]['revision'], gate=h.gate)
    replay = h.call(kind, native_id, shared_id, 0, gate=h.gate)
    assert replay['replay'] and replay['report']['classification'] == expected
    assert h.ledger_io.read_versioned() == before
    assert old_dispatches == 1 and sum(len(w.dispatches) for w in [*h.writer_runs, h.writer]) == 1


@pytest.mark.parametrize('kind', ['normal_auto', 'buyback_income'])
def test_import_marker_written_but_completion_response_lost_stays_unknown(h, kind):
    native_id, shared_id = str(uuid4()), str(uuid4())

    def lost(title, count):
        if title == '取込データ':
            raise TimeoutError('synthetic response lost after marker')

    h.writer.fault = lost
    with pytest.raises(StateError):
        h.call(kind, native_id, shared_id, 0, gate=h.gate)
    assert ledger_outcome(next(iter(h.journal.read()[0]['operations'].values()))['request'],
                         ledger_view(h.ledger_io)[0]) == 'complete'
    h.restart()
    report = h.adapter.report(native_id)
    assert report['classification'] == 'UNKNOWN'
    assert report['retry_allowed'] is False
    before = h.ledger_io.read_versioned()
    replay = h.call(kind, native_id, shared_id, 0, gate=h.gate)
    assert replay['replay'] and replay['report']['classification'] == 'UNKNOWN'
    assert h.ledger_io.read_versioned() == before
    assert sum(len(w.dispatches) for w in [*h.writer_runs, h.writer]) == 1


@pytest.mark.parametrize('kind', ['normal_auto', 'buyback_income'])
def test_direct_auto_route_without_native_evidence_attempt_stops_before_claim(h, kind):
    with pytest.raises(StateError, match='native_attempt_required'):
        h.gate.submit(kind, 'u1', str(uuid4()), 0, {})
    assert h.shared.read()[0]['revision'] == 0
    assert h.writer.dispatches == []


def test_writer_plan_cannot_be_replaced_by_browser_candidate(h):
    req = h.candidate('normal_auto', 'u1')
    req['plan']['operations'][1]['rows'][0][4] = 251
    req['plan_digest'] = digest(req['plan'])
    with pytest.raises(StateError, match='golden_mismatch'):
        existing_plan(req)


@pytest.mark.parametrize('kind', ['normal_auto', 'buyback_income'])
def test_sixteen_writer_instances_share_claim_journal_and_fake_ledger(h, kind):
    from app.iphone_accounting_gate import SyntheticAccountingGate
    from app.unified_receipt_write import ConditionalRequests
    from auto_native_fixtures import NOW
    adapters, gates, writers = [], [], []
    for _ in range(16):
        journal = type(h.journal)(h.native_io, enabled=True)
        writer = SyntheticReceiptWriter(h.ledger_io)
        adapter = type(h.adapter)(journal, h.shared, h.auth, h.candidate, h.bindings, h.ledger_io,
                                  enabled=True, writer=writer)
        adapter.fault = h.on_point
        gate = SyntheticAccountingGate(ConditionalRequests(h.financial, h.shared.store_id,
            synthetic_accounting=True), h.reader, h.auth, h.preflight, adapter.observe_boundary,
            enabled=True, isolated_synthetic=True, clock=lambda: NOW, checkpoint=adapter.checkpoint)
        adapters.append(adapter);gates.append(gate);writers.append(writer)
    h.instances = adapters
    barrier = Barrier(16)
    native_id, shared_id = str(uuid4()), str(uuid4())

    def submit(index):
        barrier.wait(timeout=30)
        try:
            with adapters[index].attempt(kind, 'u1', native_id, shared_id, 0) as replay:
                if replay is not None:
                    return replay
                return gates[index].submit(kind, 'u1', shared_id, 0, {})
        except StateError:
            return None

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(submit, range(16)))
    assert len(h.shared.read()[0]['operations']) == 1
    assert len(h.journal.read()[0]['operations']) == 1
    assert sum(len(writer.dispatches) for writer in writers) == 1
    assert sum(len(adapter.probe_calls) for adapter in adapters) == 1
    saved = next(iter(h.journal.read()[0]['operations'].values()))['request']
    assert ledger_outcome(saved, ledger_view(h.ledger_io)[0]) == 'complete'


def test_writer_target_has_only_synthetic_memory_backend(h):
    assert h.writer.synthetic_memory_only is True
    assert h.writer.io is h.ledger_io and h.ledger_io.synthetic_memory_only is True
    assert not any(hasattr(h.writer, name) for name in ('service', 'credentials', 'sheets', 'drive', 'client', 'network'))
    assert set(ledger_view(h.ledger_io)[0][0]) == {'レシート', '支出明細', '取込データ', INCOME_SHEET}


@pytest.mark.parametrize('kind', ['normal_auto', 'buyback_income'])
def test_existing_unrelated_rows_and_formulas_are_preserved(h, kind):
    import_id = 'receipt:synthetic-prior-source'
    source_hash = 'a' * 64
    receipt = ['R-synthetic-prior-source', '2020-01-02', '既存店舗', 42, '現金', '', '解析済',
               '2020-01-02 12:00:00', '既存備考']
    expense = ['R-synthetic-prior-source-01', '2020-01-02', '既存店舗', '旧商品', 42,
               '食費', '食品', '現金', 'receipt', receipt[0], import_id, '旧備考', 'active']
    imported = [import_id, '2020-01-02 12:00:00', 'receipt', 'synthetic-prior-source',
                '2020-01-02', '既存店舗', 42, '現金', '解析済', '', source_hash, '']
    income = [income_id(import_id), '2020-01-02', 50, 'その他確認済収入', '既存買取', '', import_id,
              RECEIPT_BUYBACK_SOURCE, RECEIPT_BUYBACK_REASON, source_hash]
    seed = {'レシート': [deepcopy(HEADERS['レシート']), receipt],
            '支出明細': [deepcopy(HEADERS['支出明細']), expense],
            '取込データ': [deepcopy(HEADERS['取込データ']), imported],
            INCOME_SHEET: [deepcopy(INCOME_HEADERS), income]}
    h.baseline = [deepcopy(seed), deepcopy(seed)]
    h.ledger_io.raw = encoded({'schema': 'synthetic-auto-ledger-observation-v1', 'generation': 0,
                               'complete': True, 'snapshot': h.baseline})
    h.before_observation = observation_token(*h.ledger_io.read_versioned())
    before = deepcopy(h.baseline)
    native_id, shared_id = str(uuid4()), str(uuid4())
    h.call(kind, native_id, shared_id, 0, gate=h.gate)
    after = ledger_view(h.ledger_io)[0]
    for title in seed:
        assert after[0][title][0] == before[0][title][0]
        assert after[0][title][1:] == before[0][title][1:] + [
            row for op in h.journal.read()[0]['operations'].values()
            for item in op['request']['plan']['operations'] if item['table'] == title
            for row in item['rows']]
        assert after[1][title][0] == before[1][title][0]
        assert after[1][title][1:] == before[1][title][1:] + [
            row for op in h.journal.read()[0]['operations'].values()
            for item in op['request']['plan']['operations'] if item['table'] == title
            for row in item['rows']]

