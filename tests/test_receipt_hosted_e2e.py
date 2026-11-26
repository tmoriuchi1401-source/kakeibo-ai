"""Only the new live-connection boundary is tested; no network or Google writes."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from app.drive_run_state import StateError
from app.receipt_hosted import KEY
from app.receipt_hosted_review import SCOPES
from app.receipt_reimport_production import validate_store, digest as store_digest
from app import receipt_hosted_e2e as e
from app.page_receipt_model import digest

ROOT = Path(__file__).resolve().parents[1]


def environment():
    return dict(GITHUB_ACTIONS='true',GITHUB_REPOSITORY='tmoriuchi1401-source/kakeibo-ai',GITHUB_REF=e.BRANCH,
        GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_ACTOR='tmoriuchi1401-source',GITHUB_TRIGGERING_ACTOR='tmoriuchi1401-source',
        GITHUB_RUN_ATTEMPT='1',RUNNER_ENVIRONMENT='github-hosted',RUNNER_OS='Linux',GITHUB_WORKFLOW_REF=e.WORKFLOW,
        E2E_EXPECTED_SHA='a'*40,GITHUB_SHA='a'*40)


def test_existing_readonly_job_and_inputs_are_unchanged():
    path='.github/workflows/state-cache-inspect.yml'
    baseline=subprocess.check_output(['git','show','fef0d0a:'+path],cwd=ROOT).decode()
    before=yaml.load(baseline,Loader=yaml.BaseLoader)
    current=yaml.load((ROOT/path).read_text(encoding='utf-8'),Loader=yaml.BaseLoader)
    assert current['jobs']['inspect'] == before['jobs']['inspect']
    for k,v in before['on']['workflow_dispatch']['inputs'].items():
        assert current['on']['workflow_dispatch']['inputs'][k] == v
    assert current['permissions'] == before['permissions']
    assert current['concurrency'] == before['concurrency']
    job=current['jobs']['receipt-isolated-e2e']
    assert job['runs-on']=='ubuntu-latest'
    assert "github.ref == 'refs/heads/codex/issue-207-hosted-receipts'" in job['if']
    assert 'github.sha == inputs.expected_sha' in job['if']
    assert all('GEMINI_API_KEY' not in s.get('env',{}) for s in job['steps'])


@pytest.mark.parametrize('key,value', [('GITHUB_REF','refs/heads/main'),('GITHUB_RUN_ATTEMPT','2'),
    ('RUNNER_ENVIRONMENT','self-hosted'),('RUNNER_OS','Windows'),('GITHUB_SHA','b'*40),
    ('GITHUB_ACTOR','other'),('GITHUB_TRIGGERING_ACTOR','other'),('GEMINI_API_KEY','DUMMY')])
def test_e2e_boundary_rejects_other_execution_without_effect(key,value):
    env=environment();env[key]=value
    with pytest.raises(StateError,match='hosted_boundary'):e.boundary(env,'auth')


def test_synthetic_pdf_uses_existing_real_privacy_gate_and_old_store_schema():
    from hashlib import sha256
    from app.receipt_privacy_gate import evaluate_receipt_privacy
    sources=[]
    for n,role in enumerate(e.ROLES):
        raw=e.fixture_bytes(role)
        gate=evaluate_receipt_privacy(raw,'application/pdf')
        assert gate.classification=='normal' and gate.gemini_allowed is True
        sources.append(dict(source_id='synthetic-e2e-'+str(n),version='1',mime_type='application/pdf',
            sha256=sha256(raw).hexdigest(),classification='normal',pages=1))
    value=e.initial_fixture(sources,owner_email='owner@synthetic.invalid',sa_email='sa@synthetic.invalid',
                            spreadsheet_id='synthetic-book',folder_id='synthetic-folder')
    assert validate_store(value,store_digest(value['manifest']),'synthetic-book')==value
    assert value[KEY]['financial_write_enabled'] is False and value[KEY]['records']=={}
    assert len({s['sha256'] for s in sources})==10
    ai=e.SyntheticAnalysis(sources)
    normal=ai.analyze_receipt(e.fixture_bytes('normal'),'application/pdf',e.CATEGORIES)
    invalid=ai.analyze_receipt(e.fixture_bytes('exception'),'application/pdf',e.CATEGORIES)
    assert normal.items[0].amount==100 and invalid.items[0].amount==80
    with pytest.raises(TimeoutError):ai.analyze_receipt(e.fixture_bytes('unknown'),'application/pdf',e.CATEGORIES)
    with pytest.raises(StateError,match='non_fixture'):ai.analyze_receipt(b'not a fixture','application/pdf',e.CATEGORIES)


def test_isolated_target_configuration_cannot_select_production_ids(monkeypatch):
    from app import private_state_bindings as bindings
    monkeypatch.setattr(bindings,'unwrap',lambda name,value,key:value)
    review=dict(script_id='synthetic-project',deployment_id='synthetic-deployment',owner_email='owner@synthetic.invalid',
                spreadsheet_id='synthetic-review-book',folder_id='synthetic-inbox')
    cfg=dict(schema=e.SCHEMA,state_file_id='synthetic-state-file',state_folder_id='synthetic-state-folder',
        ledger_spreadsheet_id='synthetic-ledger-book',manifest_digest='a'*64,
        fixture_source_ids=['synthetic-source-'+str(i) for i in range(10)],review_binding_digest=digest(review),
        owner_email=review['owner_email'])
    env=dict(SPREADSHEET_ID='production-financial-book',RECEIPT_DRIVE_FOLDER_ID='production-inbox',
        PROCESSED_DRIVE_FOLDER_ID='production-processed',RECEIPT_CONFIRMATION_BINDING=json.dumps({'file':'production-receipt-state'}),
        RECEIPT_HOSTED_REVIEW_BINDING=json.dumps(review),RECEIPT_HOSTED_E2E_CONFIG_JSON=json.dumps(cfg))
    env.update({name:'production-'+str(i)+'-binding' for i,name in enumerate(bindings.VARIABLES)})
    assert e.config(env,{'private_key':'SYNTHETIC'})==(cfg,review)
    for field in ('state_file_id','state_folder_id','ledger_spreadsheet_id'):
        for forbidden in (env['SPREADSHEET_ID'],env['KAKEIBO_STATE_FOLDER_ID'],'production-receipt-state'):
            altered=dict(cfg,**{field:forbidden})
            with pytest.raises(StateError,match='production_target_forbidden'):
                e.config(dict(env,RECEIPT_HOSTED_E2E_CONFIG_JSON=json.dumps(altered)),{'private_key':'SYNTHETIC'})


def test_public_or_extra_editor_target_is_rejected_before_any_state_write():
    owner,sa='owner@synthetic.invalid','sa@synthetic.invalid'
    reply=dict(id='synthetic-target',name=e.PREFIX+'state',mimeType='application/json',trashed=False,
        owners=[{'emailAddress':owner}],permissions=[dict(type='user',role='owner',emailAddress=owner),
            dict(type='user',role='writer',emailAddress=sa),dict(type='anyone',role='reader')],parents=['synthetic-folder'])
    drive=Mock();drive.files.return_value.get.return_value.execute.return_value=reply
    with pytest.raises(StateError,match='private_target_mismatch'):
        e.private_target(drive,'synthetic-target',owner=owner,sa_email=sa,mime='application/json',prefix=e.PREFIX,parent='synthetic-folder')
    assert not drive.files.return_value.update.called


@pytest.mark.parametrize('method',['append','append_raw','update_row_raw','clear','ensure_sheet','update'])
def test_financial_db_write_capability_is_always_absent(method):
    real=Mock(sid='synthetic-book',svc=object())
    db=e.ReadOnlyFinancialDB(real)
    with pytest.raises(StateError,match='financial_write_forbidden'):getattr(db,method)('anything',[])
    assert real.mock_calls==[]


def owner_info():
    return dict(type='authorized_user',token_uri='https://oauth2.googleapis.com/token',client_id='SYNTHETIC',
        client_secret='SYNTHETIC',refresh_token='SYNTHETIC',scopes=list(SCOPES)+['scope:existing'],
        owner_email='owner@synthetic.invalid',project_id='synthetic-existing-project')


def test_auth_phase_uses_only_existing_oauth_and_userinfo_without_target_binding(monkeypatch):
    import google.auth.transport.requests as transport
    session=Mock()
    session.get.return_value=SimpleNamespace(status_code=200,json=lambda:dict(email='owner@synthetic.invalid',verified_email=True))
    factory=Mock(return_value=session);monkeypatch.setattr(transport,'AuthorizedSession',factory)
    env=dict(environment(),GOOGLE_RECEIPT_OWNER_TOKEN_JSON=json.dumps(owner_info()))
    monkeypatch.setattr(e,'connect',Mock(side_effect=AssertionError('no Script, Drive or Sheets in auth phase')))
    result=e.execute(env,'auth')
    assert result['owner_match'] is True and result['google_state_writes']==result['financial_write_count']==result['real_ai_requests']==0
    assert factory.call_args.kwargs=={'max_refresh_attempts':0}
    assert session.get.call_args.args==('https://www.googleapis.com/oauth2/v2/userinfo',)
    assert session.get.call_args.kwargs=={'timeout':20,'allow_redirects':False}
    assert not session.post.called


@pytest.mark.parametrize('fault',['scope','owner','unverified','auth_rejected'])
def test_auth_wrong_scope_identity_or_rejection_fails_closed(monkeypatch,fault):
    import google.auth.transport.requests as transport
    info=owner_info()
    if fault=='scope':info['scopes']=[]
    payload=dict(email='wrong@synthetic.invalid' if fault=='owner' else info['owner_email'],verified_email=fault!='unverified')
    session=Mock()
    session.get.return_value=SimpleNamespace(status_code=401 if fault=='auth_rejected' else 200,json=lambda:payload)
    factory=Mock(return_value=session);monkeypatch.setattr(transport,'AuthorizedSession',factory)
    with pytest.raises(StateError):e.execute(dict(environment(),GOOGLE_RECEIPT_OWNER_TOKEN_JSON=json.dumps(info)),'auth')
    assert session.get.call_count==(0 if fault=='scope' else 1) and not session.post.called
