import json
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError

from app import bank_fixed_backfill as fixed


def test_fixed_duplicate_guard_reports_sites_without_rows_or_exception_text():
    secret = 'private-account-document-and-http-body'
    class DB:
        sid = 'private-spreadsheet'
        def get(self, _range):
            raise ValueError(secret)
    row = ['private-source-id'] + [''] * 11
    guarded = fixed.FixedWriteDB(DB(), [row[0]], {row[0]: row}, {}, lambda: None)
    with pytest.raises(ValueError) as caught:
        guarded.append_raw('取込データ', [row])
    result = fixed.failure_diagnostic(caught.value)
    assert result['failure_class'] == 'ValueError'
    assert [x['site'] for x in result['failure_sites']] == ['fixed_append_guard', 'fixed_expression']
    assert all(type(x['line']) is int and x['line'] > 0 for x in result['failure_sites'])
    rendered = json.dumps(result)
    assert all(s not in rendered for s in [secret, DB.sid, row[0], str(fixed.__file__)])


@pytest.mark.parametrize('status,expected', [(429, 429), (403, 403), (418, None)])
def test_http_diagnostic_only_exposes_allowlisted_numeric_status(status, expected):
    error = HttpError(SimpleNamespace(status=status, reason='private-reason'),
                      b'{"error":{"message":"private-body"}}', uri='https://private-uri')
    result = fixed.failure_diagnostic(error)
    assert result['failure_class'] == 'HttpError'
    assert result.get('failure_http_status') == expected
    assert 'private' not in json.dumps(result)


def test_unrecognized_exception_name_is_not_published():
    error_type = type('private-identifier', (RuntimeError,), {})
    assert fixed.failure_diagnostic(error_type('private-message')) == {
        'failure_class': 'OtherError', 'failure_sites': []}


@pytest.mark.parametrize('fixed_plan', [False, True])
def test_parent_failure_stays_failed_and_diagnostics_are_fixed_only(monkeypatch, capsys, fixed_plan):
    from app import production_flow, private_state_bindings
    monkeypatch.setattr('sys.argv', ['production_flow', '--scope', 'drive_bank'])
    monkeypatch.setattr(production_flow.subprocess, 'check_output', lambda *a, **k: 'a' * 40)
    monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
    if fixed_plan:
        monkeypatch.setenv('BANK_FIXED_BACKFILL_PLAN', 'private-plan-binding')
    else:
        monkeypatch.delenv('BANK_FIXED_BACKFILL_PLAN', raising=False)
    def fail(*args, **kwargs):
        raise ValueError('private-diagnostic-sentinel')
    monkeypatch.setattr(production_flow, 'verify_execution_boundary', fail)
    with pytest.raises(SystemExit) as caught:
        production_flow.main()
    assert caught.value.code == 1
    rendered = capsys.readouterr().out
    result = json.loads(rendered)
    assert result['error'] == 'production_preflight_failed' and result['success'] is False
    assert ('failure_class' in result) is fixed_plan
    assert 'private' not in rendered
