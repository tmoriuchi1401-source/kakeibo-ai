import json
import sys
from types import SimpleNamespace

import pytest
from google.genai.errors import APIError
from googleapiclient.errors import HttpError
from httplib2 import Response

from app import production_flow as flow, production_source as source
from app.drive_run_state import StateError
from app.production_run import execute_serial


@pytest.mark.parametrize('error,expected',[
    (APIError(429, {'error':{'message':'private receipt'}}),'gemini_quota_rejected'),
    (APIError(403, {'error':{'message':'private receipt'}}),'gemini_auth_rejected'),
    (HttpError(Response({'status':'429'}),b'private receipt'),'google_api_quota_rejected'),
    (HttpError(Response({'status':'500'}),b'private receipt'),'google_api_request_failed'),
    (RuntimeError('receipt_preflight_source_changed'),'receipt_preflight_source_changed'),
    (RuntimeError('receipt_preflight_source_changed private receipt'),'source_execution_failed'),
    (ValueError('private receipt'),'source_invalid_data'),
    (TimeoutError('private receipt'),'source_timeout'),
    (ConnectionError('private receipt'),'source_connection_failed'),
    (KeyError('private receipt'),'source_internal_error'),
    (RuntimeError('receipt_response_invalid'),'receipt_response_invalid'),
])
def test_fixed_error_codes_survive_child_and_parent_without_private_details(monkeypatch,capsys,error,expected):
    def fail(*args,**kwargs):raise error
    monkeypatch.setattr(sys,'argv',['source','receipts','apply'])
    monkeypatch.setattr(source,'Settings',lambda:None)
    monkeypatch.setattr(source,'receipts',fail)
    with pytest.raises(SystemExit):source.main()
    output=capsys.readouterr().out
    assert json.loads(output)=={'failure':1,'error':expected}
    assert 'private' not in output
    monkeypatch.setattr(flow.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=1,stdout=output))
    report=execute_serial({'receipts':lambda:flow.invoke('receipts',apply=True,env={})})
    assert report['sources']['receipts']['status']=='failed'
    assert report['sources']['receipts']['error']==expected
    assert report['sources']['aupay_card']['status']=='skipped'
    assert 'private' not in json.dumps(report)


def test_incomplete_scan_summary_prevents_normal_receipt_writes(monkeypatch,tmp_path):
    calls=[]
    def run(*args,**kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0,stdout='{"written":0}')
    monkeypatch.setattr(flow.subprocess,'run',run)
    with pytest.raises(StateError,match='^receipt_intake_summary_invalid$'):
        flow.invoke('receipts',apply=True,env={'RECEIPT_CONFIRMATION_BINDING':'synthetic','RUNNER_TEMP':str(tmp_path)})
    assert len(calls)==1


def test_child_timeout_output_never_reaches_parent_summary(monkeypatch):
    def run(*a,**k):raise flow.subprocess.TimeoutExpired('private-command',900,output='private receipt')
    monkeypatch.setattr(flow.subprocess,'run',run)
    report=execute_serial({'receipts':lambda:flow.invoke('receipts',apply=True,env={})})
    assert report['sources']['receipts']['error']=='source_command_timed_out'
    assert 'private' not in json.dumps(report)


@pytest.mark.parametrize('code', ['projection_drive_read_failed', 'projection_drive_write_unknown',
                                'projection_state_changed', 'projection_readback_failed'])
def test_projection_error_codes_are_fixed_and_unknown_text_is_suppressed(code):
    from app.monthly_projection import ProjectionError
    assert source.source_error_code(ProjectionError(code)) == code
    assert source.source_error_code(ProjectionError(code + ' private receipt')) == 'source_execution_failed'


def test_untrusted_child_diagnostics_cannot_leak_values(monkeypatch):
    payload = {'error': 'source_timeout', 'stage': 'private receipt', 'written': 2,
               'found': 'private receipt', 'failure': True, 'amount': 123, 'filename': 'private receipt'}
    monkeypatch.setattr(flow.subprocess, 'run', lambda *a, **k:
                        SimpleNamespace(returncode=1, stdout=json.dumps(payload)))
    report = execute_serial({'receipts': lambda: flow.invoke('receipts', apply=True, env={})})
    assert report['sources']['receipts']['error'] == 'source_timeout'
    assert report['sources']['receipts']['counts'] == {}
    assert 'private' not in json.dumps(report)


def test_valid_stage_keeps_only_completed_integer_counts(monkeypatch):
    payload = {'error': 'source_timeout', 'stage': 'receipt_processing', 'written': 6,
               'found': 'private receipt', 'unchanged': -1, 'failure': True, 'amount': 123}
    monkeypatch.setattr(flow.subprocess, 'run', lambda *a, **k:
                        SimpleNamespace(returncode=1, stdout=json.dumps(payload)))
    report = execute_serial({'receipts': lambda: flow.invoke('receipts', apply=True, env={})})
    outcome = report['sources']['receipts']
    assert outcome['stage'] == 'receipt_processing'
    assert outcome['counts'] == {'written': 6, 'failure': 1}
    assert 'private' not in json.dumps(report)
