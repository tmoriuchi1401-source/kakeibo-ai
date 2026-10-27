"""Synthetic transports only; global conftest blocks live network."""
import base64
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app import bank_pending_recovery as recovery
from app.aupay_card_recurring import SqliteRecurringRunState
from app.drive_run_state import StateBinding, StateError, envelope, snapshot
from app.production_ledger import initial_ledger, _encode


class Transport:
    def __init__(self, source, payload, calls):
        self.source, self.payload, self.calls = source, payload, calls
        self.fail = ''

    def read(self):
        self.calls.append(('read', self.source))
        return self.payload

    def write(self, payload):
        self.calls.append(('write', self.source))
        if self.fail == 'before':
            raise ConnectionError('private-message')
        self.payload = payload
        if self.fail == 'after':
            raise ConnectionError('private-message')


@pytest.fixture
def scenario(tmp_path):
    bindings = {s: StateBinding(s, 'synthetic-sheet', 'synthetic-folder', 'synthetic-' + s)
                for s in ('bank_pdf_drive', 'production_run')}
    path = tmp_path / 'native'
    SqliteRecurringRunState(path / 'bank-recurring.sqlite3', repo_root=tmp_path / 'repo')
    bank = json.loads(envelope(bindings['bank_pdf_drive'], snapshot(path, bindings['bank_pdf_drive']),
                               generation=43, phase='pending', attempt='a' * 32))
    parent = json.loads(initial_ledger(bindings['production_run']))
    parent['generation'] = 1287
    parent['sources']['bank'].update(phase='pending', attempt='b' * 32, error='source_execution_failed')
    parent['sources']['bank']['counts'] = {'written': 0}
    parent['sources']['bank']['last_success'] = '2026-10-01T00:00:00+00:00'
    before = {'bank_pdf_drive': envelope(bindings['bank_pdf_drive'], bank['files'], generation=43,
                                        phase='pending', attempt='a' * 32),
              'production_run': _encode(parent)}
    parent_after = deepcopy(parent)
    parent_after['generation'] = 1288
    parent_after['sources']['bank'].update(phase='ready', attempt='', error='')
    after = {'bank_pdf_drive': envelope(bindings['bank_pdf_drive'], bank['files'], generation=44),
             'production_run': _encode(parent_after)}
    record = dict(run_id=40501781979, run_attempt=1, decision='GO_FOR_BOUND_PENDING_STATE_RELEASE_ONLY',
                  financial_apply_authorized_by_this_record=False, unresolved_material_write_results=[],
                  remote_sqlite_intents_requiring_resolution=[], states={},
                  sqlite_and_all_native_files_required_unchanged=bank['files'], checkpoint_before_and_required_after=[])
    for source, value in [('bank_pdf_drive', bank), ('production_run', parent)]:
        record['states'][source] = dict(spreadsheet_id='synthetic-sheet', folder_id='synthetic-folder',
            file_id=bindings[source].file_id, observed_raw_sha256=recovery.sha(before[source]),
            expected_after_raw_sha256=recovery.sha(after[source]), generation_before=value['generation'],
            generation_required_after=value['generation'] + 1, phase_before='pending',
            attempt='a' * 32 if source == 'bank_pdf_drive' else 'b' * 32)
    calls = []
    transports = {s: Transport(s, p, calls) for s, p in before.items()}
    report = dict(success=False, bank_pdf_drive='not_attempted', production_run='not_attempted')
    guard = lambda: calls.append(('guard',))
    observation = lambda: calls.append(('observe',))
    def run():
        recovery.recover(record, transports, bindings, guard, observation, report)
    return SimpleNamespace(**locals())


def test_exact_release_order_and_full_sqlite_other_source_preservation(scenario):
    s = scenario
    s.run()
    assert [(op, src) for op, *rest in s.calls if op == 'write' for src in rest] == [
        ('write', 'bank_pdf_drive'), ('write', 'production_run')]
    assert {k: t.payload for k, t in s.transports.items()} == s.after
    first = s.calls.index(('write', 'bank_pdf_drive'))
    second = s.calls.index(('write', 'production_run'))
    assert ('read', 'bank_pdf_drive') in s.calls[first + 1:second]
    assert ('observe',) in s.calls[first + 1:second]
    assert s.report['success'] is True and s.report['checkpoint_sqlite_unchanged'] is True


@pytest.mark.parametrize('source', ['bank_pdf_drive', 'production_run'])
@pytest.mark.parametrize('field,value', [('attempt', 'c' * 32), ('observed_raw_sha256', '0' * 64),
                                        ('generation_before', 99), ('phase_before', 'ready')])
def test_mismatch_stops_both_states_before_first_write(scenario, source, field, value):
    s = scenario
    s.record['states'][source][field] = value
    with pytest.raises(StateError):
        s.run()
    assert not [c for c in s.calls if c[0] == 'write']
    assert {k: t.payload for k, t in s.transports.items()} == s.before


@pytest.mark.parametrize('source', ['bank_pdf_drive', 'production_run'])
@pytest.mark.parametrize('failure', ['before', 'after'])
def test_partial_or_unknown_stops_without_retry_or_rollback(scenario, source, failure):
    s = scenario
    s.transports[source].fail = failure
    with pytest.raises(ConnectionError):
        s.run()
    assert s.report[source] == 'attempted_unknown'
    writes = [c[1] for c in s.calls if c[0] == 'write']
    assert writes == (['bank_pdf_drive'] if source == 'bank_pdf_drive' else ['bank_pdf_drive', 'production_run'])
    if source == 'bank_pdf_drive':
        assert s.transports['production_run'].payload == s.before['production_run']
    if source == 'production_run':
        assert s.report['bank_pdf_drive'] == 'ready_verified'


def test_changed_financial_readback_after_bank_prevents_parent(scenario):
    s = scenario
    def observation():
        if s.report['bank_pdf_drive'] == 'ready_verified':
            raise ValueError('private-financial-row')
    with pytest.raises(ValueError):
        recovery.recover(s.record, s.transports, s.bindings, s.guard, observation, s.report)
    assert [c[1] for c in s.calls if c[0] == 'write'] == ['bank_pdf_drive']


def test_rerun_after_success_cannot_write_again(scenario):
    s = scenario
    s.run()
    with pytest.raises(StateError):
        s.run()
    assert len([c for c in s.calls if c[0] == 'write']) == 2


def environment():
    return dict(GITHUB_ACTIONS='true', GITHUB_REPOSITORY=recovery.REPOSITORY,
        GITHUB_ACTOR='tmoriuchi1401-source', GITHUB_TRIGGERING_ACTOR='tmoriuchi1401-source',
        GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_REF='refs/heads/main',
        GITHUB_WORKFLOW_REF=recovery.REPOSITORY + '/' + recovery.WORKFLOW + '@refs/heads/main',
        GITHUB_WORKFLOW_SHA='a' * 40, GITHUB_SHA='a' * 40, KAKEIBO_VALIDATED_MAIN_SHA='a' * 40,
        EXPECTED_SHA='a' * 40, GITHUB_RUN_ATTEMPT='1', KAKEIBO_SCHEDULE_ENABLED='false',
        KAKEIBO_LEGACY_DISABLED='true', CATEGORY_BACKFILL_SHEET_RUNNER_ENABLED='false')


@pytest.mark.parametrize('field,value', [('GITHUB_ACTOR', 'other'), ('GITHUB_TRIGGERING_ACTOR', 'other'),
    ('GITHUB_SHA', 'b' * 40), ('KAKEIBO_VALIDATED_MAIN_SHA', 'b' * 40),
    ('GITHUB_WORKFLOW_REF', 'other'), ('GITHUB_REF', 'refs/heads/other'),
    ('GITHUB_RUN_ATTEMPT', '2'), ('KAKEIBO_SCHEDULE_ENABLED', ''), ('RUNNER_DEBUG', '1')])
def test_owner_sha_workflow_and_stop_boundary(field, value):
    env = environment()
    recovery.boundary(env, 'a' * 40)
    env[field] = value
    with pytest.raises(StateError):
        recovery.boundary(env, 'a' * 40)


def test_workflow_only_manual_common_lock_and_no_writer_secrets():
    root = Path(__file__).resolve().parents[1]
    source = (root / recovery.WORKFLOW).read_text(encoding='utf-8')
    workflow = yaml.load(source, Loader=yaml.BaseLoader)
    assert set(workflow['on']) == {'workflow_dispatch'}
    assert workflow['concurrency'] == dict(group='kakeibo-production', **{'cancel-in-progress': 'false'}, queue='max')
    assert set(workflow['on']['workflow_dispatch']['inputs']) == {'expected_sha', 'recovery_capsule'}
    assert 'BANK_FIXED_BACKFILL_APPROVAL_JSON' not in source
    assert 'GEMINI_API_KEY' not in source and 'GOOGLE_GMAIL_TOKEN_JSON' not in source
    assert 'github.actor' in source and 'github.triggering_actor' in source
    assert [s['run'] for s in workflow['jobs']['reconcile']['steps'] if 'run' in s][-1] == 'python -m app.bank_pending_recovery'


def test_failure_output_cannot_publish_exception_or_private_input(monkeypatch, capsys):
    monkeypatch.setattr(recovery.subprocess, 'check_output', lambda *a, **k: 'a' * 40)
    monkeypatch.setattr(recovery, 'boundary', lambda *a: (_ for _ in ()).throw(ValueError('PRIVATE_SECRET')))
    monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
    monkeypatch.setenv('RECOVERY_CAPSULE', 'PRIVATE_CAPSULE')
    with pytest.raises(SystemExit):
        recovery.main()
    text = capsys.readouterr().out
    assert 'PRIVATE' not in text and 'Traceback' not in text
    assert json.loads(text)['financial_write_calls'] == 0


def test_no_financial_api_or_native_state_write_calls():
    import ast
    source = ast.parse(Path(recovery.__file__).read_text(encoding='utf-8'))
    methods = {n.func.attr for n in ast.walk(source) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not {'append_raw', 'batchUpdate', 'commit', 'begin', 'complete', 'record'} & methods
    calls = {n.func.id for n in ast.walk(source) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not {'sheets_service', 'SheetsDB', 'execute', 'run_from_environment'} & calls


@pytest.mark.parametrize('change', ['none', 'competing_run', 'expired', 'writer', 'payroll', 'cloud', 'main'])
def test_live_controls_require_no_competition_and_fresh_stops(monkeypatch, change):
    import requests
    from datetime import datetime, timezone
    env = dict(environment(), GH_TOKEN='synthetic', GITHUB_RUN_ID='123')
    attestation = dict(at_epoch=datetime.now(timezone.utc).timestamp(), main_sha='a' * 40,
        record_sha256=recovery.RECORD_SHA256,
        writers=dict(KnownWriterCount=0, Tasks=[dict(State='Disabled')]))
    if change == 'expired':
        attestation['at_epoch'] -= 901
    if change == 'writer':
        attestation['writers']['KnownWriterCount'] = 1
    if change == 'payroll':
        attestation['writers']['Tasks'][0]['State'] = 'Ready'
    def get(url, **kwargs):
        if url.endswith('branches/main'):
            value = dict(commit=dict(sha='b' * 40 if change == 'main' else 'a' * 40))
        elif url.endswith('runs/123'):
            value = dict(path=recovery.WORKFLOW, head_sha='a' * 40, event='workflow_dispatch',
                run_attempt=1, status='in_progress', actor=dict(login='tmoriuchi1401-source'),
                triggering_actor=dict(login='tmoriuchi1401-source'))
        elif url.endswith('runs/40501781979'):
            value = dict(status='completed', conclusion='failure', run_attempt=1,
                         head_sha='89e02e0bdacf8c683846ec6cff351cb623b7bf65')
        else:
            value = dict(total_count=1 if change == 'competing_run' else 0,
                         workflow_runs=[dict(id=999)] if change == 'competing_run' else [])
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: value)
    monkeypatch.setattr(requests.Session, 'get', lambda self, *a, **k: get(*a, **k))
    monkeypatch.setattr(requests, 'head', lambda *a, **k: SimpleNamespace(status_code=200 if change == 'cloud' else 503))
    if change == 'none':
        recovery.controls(env, attestation)
    else:
        with pytest.raises(StateError):
            recovery.controls(env, attestation)


def test_observer_has_only_read_methods_and_refuses_financial_change():
    from unittest.mock import MagicMock
    response = dict(valueRanges=[dict(values=[['protected-' + str(n)] for n in range(185)]),
                                dict(values=[]), dict(values=[])])
    manifest = dict(raw_financial_sha256=recovery.sha(recovery.encode(response)),
                    selected100=['selected-' + str(n) for n in range(100)],
                    protected185=['protected-' + str(n) for n in range(185)], pdfs=[])
    sheets, drive = MagicMock(), MagicMock()
    sheets.spreadsheets().values().batchGet().execute.return_value = deepcopy(response)
    recovery.observe(drive, sheets, manifest, 'synthetic')
    response['valueRanges'][0]['values'][0].append('changed-existing-value')
    sheets.spreadsheets().values().batchGet().execute.return_value = response
    with pytest.raises(StateError):
        recovery.observe(drive, sheets, manifest, 'synthetic')
    assert not drive.mock_calls
    assert not any('append' in str(call) or 'update' in str(call) for call in sheets.mock_calls)


def test_transport_rejects_other_payloads_and_second_write():
    calls = []
    transport = Transport('bank', b'before', calls)
    wrapper = recovery.ReleaseOnlyTransport(transport, recovery.sha(b'after'))
    with pytest.raises(StateError):
        wrapper.write(b'other')
    assert not calls
    wrapper.write(b'after')
    with pytest.raises(StateError):
        wrapper.write(b'after')
    assert calls == [('write', 'bank')]
