"""Synthetic only: selected intake, never recovery of a quarantined card."""
from copy import deepcopy
import json
import pytest

from app import production_flow as flow
from app.drive_run_state import StateError, envelope, validate
from test_production_flow import valid_env
from test_production_integration import integrated, SID, STATE


def history():
    return {source: dict(phase='ready', attempt='', error='', counts={})
            for source in flow.DEPENDENCIES}


def quarantined():
    value = history()
    value['aupay_card'].update(phase='pending', attempt='a' * 32)
    return value


def quarantine(f):
    ledger = json.loads(f.payloads['ledger'])
    ledger['sources']['aupay_card'].update(phase='pending', attempt='a' * 32)
    f.payloads['ledger'] = json.dumps(ledger).encode()
    native = validate(f.payloads['aupay_card-state'], f.bindings['aupay_card'])
    f.payloads['aupay_card-state'] = envelope(f.bindings['aupay_card'], native['files'],
        generation=native['generation'] + 1, phase='pending', attempt='b' * 32)


def main_env(monkeypatch, scope):
    values = valid_env() | {'SPREADSHEET_ID': SID, 'KAKEIBO_STATE_FOLDER_ID': STATE,
        'AMAZON_STATE_FILE_ID': 'amazon-state', 'AUPAY_CARD_STATE_FILE_ID': 'aupay_card-state',
        'BANK_STATE_FILE_ID': 'bank-state', 'KAKEIBO_RUN_LEDGER_FILE_ID': 'ledger',
        'GITHUB_EVENT_NAME': 'workflow_dispatch'}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
    monkeypatch.delenv('KAKEIBO_AUTHORITY_RECIPIENT', raising=False)
    monkeypatch.setattr(flow.sys, 'argv', ['production_flow', '--mode', 'apply', '--scope', scope])
    monkeypatch.setattr(flow.subprocess, 'check_output', lambda *a, **k: 'a' * 40)
    monkeypatch.setattr('app.private_state_bindings.decode_environment', lambda env, **k: (env, ''))


def test_card_pending_does_not_require_unrelated_native_reads(monkeypatch):
    value = quarantined()
    before = deepcopy(value)
    monkeypatch.setattr('app.google_clients.read_only_drive_service',
        lambda: pytest.fail('unrelated_native_client_forbidden'))
    flow.verify_isolated_intake_readiness({}, value, scope='drive_paypay')
    assert value == before
    with pytest.raises(StateError):
        flow.verify_source_readiness({}, value)


@pytest.mark.parametrize('change', [dict(phase='pending', attempt='c' * 32),
    dict(phase='UNKNOWN'), dict(error='source_execution_failed'), dict(attempt='c' * 32)])
def test_selected_source_pending_unknown_or_unresolved_is_rejected(change):
    value = quarantined()
    value['paypay'].update(change)
    with pytest.raises(StateError):
        flow.verify_isolated_intake_readiness({}, value, scope='drive_paypay')


@pytest.mark.parametrize('scope', ['core', 'all', 'drive_bank', 'drive_receipts', 'amazon_catchup'])
def test_intake_exception_cannot_be_used_by_other_scopes(scope):
    with pytest.raises(StateError):
        flow.verify_isolated_intake_readiness({}, quarantined(), scope=scope)


def test_selected_scope_cannot_expand_to_shared_postprocessing(monkeypatch):
    monkeypatch.setitem(flow.DRIVE_SCHEDULE_SCOPES, 'drive_paypay', frozenset({'paypay', 'auto_expense'}))
    with pytest.raises(StateError):
        flow.verify_isolated_intake_readiness({}, quarantined(), scope='drive_paypay')


@pytest.mark.parametrize('change', [dict(phase='pending', attempt='c' * 32),
                                  dict(phase='UNKNOWN'), dict(error='source_execution_failed')])
def test_required_dependency_is_not_excluded(monkeypatch, change):
    monkeypatch.setitem(flow.DEPENDENCIES, 'paypay', ('amazon',))
    value = quarantined()
    value['amazon'].update(change)
    with pytest.raises(StateError):
        flow.verify_isolated_intake_readiness({}, value, scope='drive_paypay')


@pytest.mark.parametrize('phase,attempt', [('ready', ''), ('pending', 'c' * 32),
                                         ('UNKNOWN', ''), ('ready', 'c' * 32)])
def test_required_native_has_its_own_strict_gate(monkeypatch, phase, attempt):
    monkeypatch.setitem(flow.DEPENDENCIES, 'paypay', ('amazon',))
    inspected = []
    class Native:
        def __init__(self, transport, binding): inspected.append(binding.source)
        def inspect(self): return dict(phase=phase, attempt=attempt)
    monkeypatch.setattr(flow, 'DurableState', Native)
    monkeypatch.setattr('app.google_clients.read_only_drive_service', lambda: object())
    env = {'SPREADSHEET_ID': SID, 'KAKEIBO_STATE_FOLDER_ID': STATE,
           'AMAZON_STATE_FILE_ID': 'amazon-state'}
    if phase == 'ready' and not attempt:
        flow.verify_isolated_intake_readiness(env, quarantined(), scope='drive_paypay')
    else:
        with pytest.raises(StateError):
            flow.verify_isolated_intake_readiness(env, quarantined(), scope='drive_paypay')
    assert inspected == ['amazon_gmail']


def test_transitive_dependency_still_rejects_card_pending(monkeypatch):
    monkeypatch.setitem(flow.DEPENDENCIES, 'paypay', ('amazon',))
    monkeypatch.setitem(flow.DEPENDENCIES, 'amazon', ('aupay_card',))
    with pytest.raises(StateError):
        flow.verify_isolated_intake_readiness({}, quarantined(), scope='drive_paypay')


@pytest.mark.parametrize('event', ['workflow_dispatch', 'schedule'])
def test_real_paypay_writer_isolated_with_card_pending_and_duplicate_replay(integrated, monkeypatch, capsys, tmp_path, event):
    f = integrated
    quarantine(f)
    main_env(monkeypatch, 'drive_paypay')
    monkeypatch.setattr(flow.tempfile, 'tempdir', str(tmp_path))
    if event == 'schedule':
        payload = tmp_path/'schedule.json'
        payload.write_text(json.dumps({'schedule': '11 23 * * *'}))
        for key, value in {'GITHUB_EVENT_NAME': event, 'GITHUB_EVENT_PATH': str(payload),
            'KAKEIBO_PAYPAY_SCHEDULE_ENABLED': 'true', 'GITHUB_RUN_ATTEMPT': '1',
            'WORKFLOW_SHA': 'a' * 40,
            'WORKFLOW_REF': 'tmoriuchi1401-source/kakeibo-ai/.github/workflows/kakeibo-production.yml@refs/heads/main'}.items():
            monkeypatch.setenv(key, value)
    before = deepcopy(f.payloads)
    card = json.loads(before['ledger'])['sources']['aupay_card']
    original_rows = deepcopy(f.rows)
    def forbidden(*a, **k): pytest.fail('shared_postprocessing_forbidden')
    monkeypatch.setattr(flow, 'finish_ledger_updates', forbidden)
    monkeypatch.setattr('app.bank_review_refresh.refresh_bank_review_after_sources', forbidden)
    monkeypatch.setattr('app.category_operations.run_category_operations', forbidden)
    monkeypatch.setattr('app.daily_runtime.run_daily_requests', forbidden)
    flow.main()
    report = json.loads(capsys.readouterr().out)
    assert report['success'] and set(report['sources']) == {'paypay'}
    assert f.calls == ['paypay']
    assert report['sources']['paypay']['counts']['imported_files'] == 1
    assert report['sources']['paypay']['confirmation_pending'] is False
    assert json.loads(f.payloads['ledger'])['sources']['aupay_card'] == card
    assert all(f.payloads[k] == value for k, value in before.items() if k != 'ledger')
    assert len(f.rows['取込データ']) == len(original_rows['取込データ']) + 1
    assert all(f.rows[k] == value for k, value in original_rows.items() if k != '取込データ')
    assert not f.ai_calls
    imported = deepcopy(f.rows['取込データ'])
    flow.main()
    assert json.loads(capsys.readouterr().out)['success']
    assert f.rows['取込データ'] == imported
    assert f.calls == ['paypay', 'paypay']
    assert json.loads(f.payloads['ledger'])['sources']['aupay_card'] == card
    assert f.payloads['aupay_card-state'] == before['aupay_card-state']


@pytest.mark.parametrize('phase,error', [('pending', ''), ('ready', 'source_execution_failed'), ('UNKNOWN', '')])
def test_main_rejects_own_unknown_before_child_or_write(integrated, monkeypatch, capsys, phase, error):
    f = integrated
    quarantine(f)
    ledger = json.loads(f.payloads['ledger'])
    ledger['sources']['paypay'].update(phase=phase, error=error, attempt='c' * 32 if phase == 'pending' else '')
    f.payloads['ledger'] = json.dumps(ledger).encode()
    main_env(monkeypatch, 'drive_paypay')
    before = deepcopy(f.payloads)
    with pytest.raises(SystemExit): flow.main()
    assert not json.loads(capsys.readouterr().out)['success']
    assert f.calls == f.swrites == f.dwrites == []
    assert f.payloads == before


@pytest.mark.parametrize('scope,ready_source', [('core', 'amazon'), ('core', 'aupay_balance'),
                                             ('amazon_catchup', 'amazon')])
def test_amazon_or_balance_ready_does_not_unlock_core_or_catchup(integrated, monkeypatch, capsys, scope, ready_source):
    f = integrated
    quarantine(f)
    assert json.loads(f.payloads['ledger'])['sources'][ready_source]['phase'] == 'ready'
    main_env(monkeypatch, scope)
    before = deepcopy(f.payloads)
    with pytest.raises(SystemExit): flow.main()
    assert not json.loads(capsys.readouterr().out)['success']
    assert f.calls == f.swrites == f.dwrites == []
    assert f.payloads == before


def test_existing_bank_scope_is_not_routed_through_new_gate(integrated, monkeypatch):
    f = integrated
    quarantine(f)
    before = deepcopy(f.payloads)
    monkeypatch.setattr(flow, 'verify_isolated_intake_readiness',
        lambda *a, **k: pytest.fail('bank_must_keep_existing_boundary'))
    result = f.run(True, scope='drive_bank')
    assert result['success'], (result, f.failures)
    assert f.calls == ['bank']
    assert f.payloads['bank-state'] == before['bank-state']
    assert f.payloads['aupay_card-state'] == before['aupay_card-state']
    assert json.loads(f.payloads['ledger'])['sources']['aupay_card'] == json.loads(before['ledger'])['sources']['aupay_card']


def test_changed_parent_readback_is_not_reported_as_success(integrated, monkeypatch, capsys, tmp_path):
    f = integrated
    quarantine(f)
    main_env(monkeypatch, 'drive_paypay')
    monkeypatch.setattr(flow.tempfile, 'tempdir', str(tmp_path))
    before = deepcopy(f.payloads)
    original = flow.ProductionLedger
    observations = []
    def read(transport, binding):
        ledger = original(transport, binding)
        observations.append(True)
        if len(observations) == 2:
            changed = deepcopy(ledger.value)
            changed['sources']['aupay_card']['attempt'] = 'c' * 32
            ledger.payload = json.dumps(changed).encode()
            ledger.value = changed
        return ledger
    monkeypatch.setattr(flow, 'ProductionLedger', read)
    monkeypatch.setattr('app.bank_review_refresh.refresh_bank_review_after_sources',
        lambda *a, **k: pytest.fail('shared_postprocessing_forbidden'))
    with pytest.raises(SystemExit): flow.main()
    report = json.loads(capsys.readouterr().out)
    assert not report['success'] and report['error'] == 'ledger_final_read_failed'
    assert report['sources']['paypay']['confirmation_pending'] is None
    assert f.calls == ['paypay']  # No retry or compensating write.
    assert f.payloads['aupay_card-state'] == before['aupay_card-state']
    assert json.loads(f.payloads['ledger'])['sources']['aupay_card'] == json.loads(before['ledger'])['sources']['aupay_card']


def test_unknown_paypay_append_keeps_own_pending_and_blocks_next_execution(integrated, monkeypatch, capsys, tmp_path):
    f = integrated
    quarantine(f)
    main_env(monkeypatch, 'drive_paypay')
    monkeypatch.setattr(flow.tempfile, 'tempdir', str(tmp_path))
    before = deepcopy(f.payloads)
    def lost_response(method, kwargs):
        if method == 'append' and kwargs.get('range', '').startswith('取込データ!'):
            raise TimeoutError('synthetic_lost_append_response')
    f.sheets.after_write = lost_response
    monkeypatch.setattr('app.bank_review_refresh.refresh_bank_review_after_sources',
        lambda *a, **k: pytest.fail('shared_postprocessing_forbidden'))
    with pytest.raises(SystemExit): flow.main()
    assert not json.loads(capsys.readouterr().out)['success']
    ledger = json.loads(f.payloads['ledger'])
    assert ledger['sources']['paypay']['phase'] == 'pending'
    assert ledger['sources']['paypay']['error'] == 'source_execution_failed'
    assert ledger['sources']['aupay_card'] == json.loads(before['ledger'])['sources']['aupay_card']
    assert f.payloads['aupay_card-state'] == before['aupay_card-state']
    assert f.calls == ['paypay']
    appended = deepcopy(f.rows['取込データ'])
    with pytest.raises(SystemExit): flow.main()
    assert not json.loads(capsys.readouterr().out)['success']
    assert f.calls == ['paypay'] and f.rows['取込データ'] == appended
