"""Synthetic child/process/Google boundaries; no credentials or live writers."""
from copy import deepcopy
from datetime import datetime
import json
import sqlite3
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from app import cli, production_flow as flow
from app.drive_run_state import StateError
from app.production_run import (bank_diagnostic_fields, bank_failure_diagnostic,
                                execute_serial, SourceFailure)
from test_bank_income_recurring import Rig
from test_bank_income import bank, imported
from app.bank_income import classify_deposit, INCOME_SHEET


@pytest.mark.parametrize('status', [429, 403, 500])
def test_http_diagnostic_has_status_and_counts_without_response_or_uri(status):
    error = HttpError(Response({'status': str(status)}), b'private API body', uri='private-file-id')
    db = SimpleNamespace(sheet_read_metrics=lambda: dict(logical=2, attempts=6, retries=4))
    result = bank_failure_diagnostic(error, 'bank_cli_execution', db)
    assert result['failure_http_status'] == status
    assert result['http_429_seen'] == 1
    assert result['sheets_reads'] == 2 and result['sheets_read_attempts'] == 6
    assert result['sheets_read_retries'] == 4
    assert 'private' not in json.dumps(result)


@pytest.mark.parametrize('error', [ValueError('private pdf contents'), KeyError('private file'),
                                  RuntimeError('private exception'), TimeoutError('private uri')])
def test_non_http_diagnostic_never_includes_exception_text(error):
    result = bank_failure_diagnostic(error, 'bank_cli_settings')
    assert result['stage'] == 'bank_cli_settings' and result['http_429_seen'] == 0
    assert 'failure_http_status' not in result
    assert 'private' not in json.dumps(result)


def test_wrapped_drive_quota_retains_known_http_status():
    result = bank_failure_diagnostic(StateError('state_drive_read_failed_429'), 'bank_parent_ledger')
    assert result['stage'] == 'bank_drive_read'
    assert result['failure_http_status'] == 429 and result['http_429_seen'] == 1


def test_runner_caught_quota_keeps_safe_diagnostic_without_extra_checkpoint_write(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.add('synthetic', bank())
    before = rig.state.successful_window_end()
    def download(_): raise HttpError(Response({'status':'429'}), b'private pdf', uri='private-file')
    result = rig.run(download=download, dry_run=True)
    assert result['failure'] == 1 and result['failure_http_status'] == 429
    assert result['http_429_seen'] == 1 and rig.state.successful_window_end() == before
    assert not rig.db.writes and not rig.drive._files.updated
    with sqlite3.connect(rig.state.path) as connection:
        persisted = json.loads(connection.execute('SELECT summary_json FROM recurring_runs ORDER BY rowid DESC LIMIT 1').fetchone()[0])
    assert 'failure_http_status' not in persisted and 'sheets_reads' not in persisted


def test_read_diagnostics_are_not_production_ledger_count_fields():
    from app.production_run import COUNT_KEYS, BANK_READ_COUNTS
    assert not COUNT_KEYS & BANK_READ_COUNTS


def test_untrusted_child_fields_are_allowlisted_and_typed():
    assert bank_diagnostic_fields(dict(stage=['private'], failure_class={}, failure_code=['private'],
        failure_http_status=True, sheets_reads='private', sheets_read_retries=-1,
        http_429_seen=2, response='private', file_id='private')) == {}


def test_failed_bank_child_preserves_diagnostics_but_not_raw_output(monkeypatch):
    payload = dict(error='google_api_quota_rejected', stage='bank_sheets_read', failure_class='HttpError',
                   failure_http_status=429, http_429_seen=1, sheets_reads=3,
                   sheets_read_attempts=7, sheets_read_retries=4, response='private contents')
    monkeypatch.setattr(flow.subprocess, 'run', lambda *a, **k:
        SimpleNamespace(returncode=1, stdout=json.dumps(payload), stderr='private API response'))
    report = execute_serial({'bank': lambda: flow.invoke('bank', apply=False, env={})},
                            preview=True, selected_sources=frozenset({'bank'}))
    result = report['sources']['bank']
    assert not report['success'] and result['stage'] == 'bank_sheets_read'
    assert result['failure_http_status'] == 429 and result['counts']['sheets_read_retries'] == 4
    assert 'private' not in json.dumps(report)


def test_later_review_failure_retains_completed_bank_counts(monkeypatch, capsys, tmp_path):
    from test_production_flow import valid_env
    env = dict(valid_env(), BANK_REVIEW_ENABLED='true', SPREADSHEET_ID='synthetic',
               KAKEIBO_STATE_FOLDER_ID='synthetic', KAKEIBO_RUN_LEDGER_FILE_ID='synthetic')
    for k,v in env.items(): monkeypatch.setenv(k,v)
    monkeypatch.delenv('BANK_FIXED_BACKFILL_PLAN', raising=False)
    monkeypatch.setattr(flow.sys, 'argv', ['flow','--scope','drive_bank','--mode','preview'])
    monkeypatch.setattr(flow.tempfile, 'tempdir', str(tmp_path))
    monkeypatch.setattr(flow.subprocess, 'check_output', lambda *a,**k:'a'*40)
    monkeypatch.setattr('app.private_state_bindings.decode_environment', lambda env,**k:(env,''))
    monkeypatch.setattr('app.google_clients.read_only_drive_service', lambda:object())
    ledger = SimpleNamespace(transport=object(), value={'sources':{'bank':{'last_success':None,'phase':'ready','counts':{}}}})
    monkeypatch.setattr(flow, 'ProductionLedger', lambda *a:ledger)
    monkeypatch.setattr(flow, 'assemble', lambda *a,**k:{})
    monkeypatch.setattr(flow, 'execute_serial', lambda *a,**k:dict(success=True,
        sources={'bank':dict(status='success', counts={'files_seen':1,'written':0,'preview_windows':3})}))
    monkeypatch.setattr(flow, 'preview_authority_summary', lambda *a:{'bank':{'policy_valid':True}})
    def fail(*a,**k): raise ValueError('bank_review_cross_pdf_identity_collision')
    monkeypatch.setattr('app.bank_review_refresh.refresh_bank_review_after_sources', fail)
    with pytest.raises(SystemExit): flow.main()
    result = json.loads(capsys.readouterr().out)
    assert result['error'] == 'production_preflight_failed'
    assert result['failure_code'] == 'bank_review_cross_pdf_identity_collision'
    assert result['stage'] == 'bank_review_refresh'
    assert result['sources']['bank']['counts']['preview_windows'] == 3
    assert not result['success']


@pytest.mark.parametrize('dry_run', [True, False])
def test_normal_cli_uses_same_bounded_reader_in_preview_and_apply(tmp_path, monkeypatch, dry_run):
    rig = Rig(tmp_path, monkeypatch)
    observed, constructed = {}, {}
    monkeypatch.setenv('BANK_REVIEW_ENABLED', 'false')
    monkeypatch.setattr(cli, 'Settings', lambda:SimpleNamespace(spreadsheet_id=rig.db.sid,
        bank_pdf_drive_folder_id='', bank_pdf_processed_drive_folder_id='', validate=lambda **k:None,
        bank_confirmed_internal_transfers=lambda:frozenset(),
        bank_confirmed_non_own_classifications=lambda:frozenset()))
    def db(*a,**k): constructed.update(k); return rig.db
    monkeypatch.setattr(cli, 'SheetsDB', db)
    monkeypatch.setattr(cli, 'read_only_sheets_service', lambda:object())
    monkeypatch.setattr(cli, 'read_only_drive_service', lambda:rig.drive)
    monkeypatch.setattr(cli, 'drive_service', lambda:rig.drive)
    monkeypatch.setattr(cli, 'run_bank_pdf_catch_up_preview', lambda **k:observed.update(k) or {'status':'dry_run_noop'})
    monkeypatch.setattr(cli, 'run_bank_pdf_recurring', lambda **k:observed.update(k) or {'status':'noop'})
    args = SimpleNamespace(state_dir=str(tmp_path), authority_file=str(rig.path), dry_run=dry_run,
        audit_key_file='', now='2026-09-14T12:00:00+09:00')
    result = cli.bank_recurring_cli(args)
    assert result.get('failure', 0) == 0
    assert constructed['read_pacer'].interval == 1.25
    assert constructed['read_retry_attempts'] == 5 and constructed['read_retry_base'] == 20
    assert observed['dry_run'] is dry_run
    assert not rig.db.writes and not rig.drive._files.updated


def test_old_pdf_preserves_191_imports_and_withholds_117_unknown_ids(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    saved = [bank('給与', seed=f'posted-{i}') for i in range(6)]
    nonposting = [bank('確認済非記帳', seed=f'saved-{i}') for i in range(185)]
    held = [bank('振込 不明', seed=f'held-{i}') for i in range(117)]
    rig.db.tables['取込データ'] = [imported(t, **{'8':'bank_income'}) for t in saved]
    rig.db.tables['取込データ'] += [imported(t, **{'8':'bank_non_expense'}) for t in nonposting]
    rig.db.tables[INCOME_SHEET].extend(classify_deposit(t).row() for t in saved)
    before = deepcopy(rig.db.tables)
    rig.add('old-original', *saved, *nonposting, *held)
    rig.drive._files.response['files'][0]['modifiedTime'] = '2026-09-01T00:00:00Z'
    checkpoint = rig.state.successful_window_end()
    result = rig.run(dry_run=True)
    assert not result['failure'] and result['outside_write_window'] == 1
    assert rig.db.tables == before and not rig.db.writes
    assert rig.state.successful_window_end() == checkpoint
    assert not rig.expense_calls and not rig.drive._files.updated
    assert all(t.source_row_identity not in {r[0] for r in rig.db.tables['取込データ']} for t in held)


def test_cross_pdf_content_collision_reports_fixed_code_without_private_values(monkeypatch):
    from dataclasses import replace
    from test_bank_review_source import rig as source_rig
    db, drive, reader, parsed = source_rig(monkeypatch)
    drive.items.append({**drive.items[0], 'id':'second_synthetic_pdf'})
    reader.download = lambda file_id: file_id.encode()
    changed = replace(parsed, transactions=(replace(parsed.transactions[0], signed_amount=501),))
    monkeypatch.setattr('app.bank_review_source.BankPdfPipeline.parse',
        lambda self, path, **kwargs: changed if path.read_bytes() == b'second_synthetic_pdf' else parsed)
    with pytest.raises(ValueError) as caught: reader()
    diagnostic = bank_failure_diagnostic(caught.value, 'bank_review_refresh')
    assert diagnostic['failure_code'] == 'bank_review_cross_pdf_identity_collision'
    assert 'second_synthetic_pdf' not in json.dumps(diagnostic)
