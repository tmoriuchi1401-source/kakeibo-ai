from pathlib import Path
from unittest.mock import Mock
from types import SimpleNamespace
import pytest,yaml
from app import pdf_unit_write_canary as w
from app.drive_run_state import StateError

def environment():
    return dict(GITHUB_ACTIONS='true',GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_REPOSITORY=w.REPO,
        GITHUB_REF='refs/heads/main',GITHUB_WORKFLOW_REF=w.REPO+'/'+w.WRITE_WORKFLOW+'@refs/heads/main',
        RUNNER_ENVIRONMENT='github-hosted',RUNNER_OS='Linux',KAKEIBO_PRODUCTION_ENABLED='true',
        GITHUB_SHA='a'*40,KAKEIBO_VALIDATED_MAIN_SHA='a'*40,PDF_CANARY_APPROVED_SHA='b'*40,
        PDF_CANARY_ANALYSIS_SHA='c'*40,PDF_CANARY_CONFIRM='WRITE_CANARY',PDF_CANARY_STAGE='p11',
        KAKEIBO_PROJECTION_FOLDER_ID='existing',GOOGLE_SERVICE_ACCOUNT_JSON='synthetic',SPREADSHEET_ID=w.SID,
        PDF_GROUPING_BINDING='existing',PDF_MEDICAL_ENABLED='false',PDF_ARCHIVE_ENABLED='false',
        PDF_CANARY_P2_RUN_ID='1',PDF_CANARY_REMAINING_RUN_ID='2',GITHUB_TOKEN='synthetic')

@pytest.mark.parametrize('patch',[{'GITHUB_ACTIONS':'false'},{'GITHUB_EVENT_NAME':'schedule'},
    {'GITHUB_REF':'refs/heads/codex/pdf-page-privacy'},{'RUNNER_ENVIRONMENT':'self-hosted'},
    {'KAKEIBO_VALIDATED_MAIN_SHA':'d'*40},{'KAKEIBO_PROJECTION_FOLDER_ID':''},
    {'PDF_CANARY_CONFIRM':'READ_ONLY'},{'GEMINI_API_KEY':'must-not-inject'},
    {'PDF_MEDICAL_ENABLED':'true'},{'PDF_ARCHIVE_ENABLED':'true'},
    {'PDF_CANARY_P2_RUN_ID':''},{'PDF_CANARY_STAGE':'all'},{'RUNNER_DEBUG':'1'}])
def test_hosted_boundary_before_any_client_or_secret_use(patch):
    with pytest.raises(StateError):w.require_context({**environment(),**patch},'b'*40)

def test_exact_hosted_boundary():w.require_context(environment(),'b'*40)

def test_remaining_requires_completed_small_canary():
    rows={n:{'status':'would_import'} for n in (5,6,8)}
    expected={'unit_ids':{n:str(n) for n in rows}}
    store=SimpleNamespace(value={'records':{}})
    with pytest.raises(StateError,match='small_stage_required'):w.require_small_stage(store,rows,expected)
    store.value['records']={str(n):{'phase':'applied'} for n in rows}
    w.require_small_stage(store,rows,expected)
    store.value['records']['6']['phase']='pending'
    with pytest.raises(StateError,match='small_stage_required'):w.require_small_stage(store,rows,expected)

def test_registered_workflow_manual_only_same_mutex_and_no_ai_secret():
    text=Path(w.WRITE_WORKFLOW).read_text(encoding='utf-8')
    flow=yaml.load(text,Loader=yaml.BaseLoader)
    assert set(flow['on'])=={'workflow_dispatch'}
    assert flow['concurrency']['group']=='kakeibo-production'
    assert flow['concurrency']['cancel-in-progress']=='false'
    assert 'GEMINI_API_KEY' not in text and 'PROCESSED_DRIVE_FOLDER_ID' not in text
    assert 'github.sha == vars.KAKEIBO_VALIDATED_MAIN_SHA' in flow['jobs']['canary']['if']
    secret_steps=[s for s in flow['jobs']['canary']['steps'] if 'secrets.' in str(s)]
    assert len(secret_steps)==1 and secret_steps[0]['run']=='python -m app.pdf_unit_write_canary'
    assert flow['on']['workflow_dispatch']['inputs']['stage']['default']=='preflight'

@pytest.mark.parametrize('file',['app/receipt_validation.py','app/receipt_privacy_gate.py','app/models.py',
    'app/gemini_ai.py','app/pdf_grouping_authority_v2.py','app/receipt_pipeline.py',
    'app/pdf_page_medical.py','app/receipt_confirmation.py','app/manual_entry.py'])
def test_old_analysis_rejected_if_any_policy_or_authority_code_changes(file):
    env=environment()
    pr={'state':'open','draft':True,'base':{'ref':'main'},'head':{
        'sha':'b'*40,'ref':w.BRANCH,'repo':{'full_name':w.REPO}}}
    checks={'check_runs':[{'name':n,'status':'completed','conclusion':'success'} for n in w.CHECKS]}
    get=Mock(side_effect=[pr,{'commit':{'sha':'a'*40}},checks,checks,
        {'status':'ahead','merge_base_commit':{'sha':'c'*40},'files':[{'filename':file}]}])
    with pytest.raises(StateError,match='analysis_policy_changed'):w.check_code(env,get)
