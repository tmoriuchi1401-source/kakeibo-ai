"""Daily routes with invented originals and no live Google/AI access."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import production_flow as flow
from app.drive_run_state import StateError
from app.drive_receipts import receipt_batch
from app.production_run import DEPENDENCIES, execute_serial


@pytest.mark.parametrize('cron,scope,flag', [
    ('23 20 * * *','core','KAKEIBO_CORE_SCHEDULE_ENABLED'),
    ('31 22 * * *','drive_receipts','KAKEIBO_RECEIPT_SCHEDULE_ENABLED'),
    ('47 21 * * *','drive_bank','KAKEIBO_BANK_SCHEDULE_ENABLED'),
    ('11 23 * * *','drive_paypay','KAKEIBO_PAYPAY_SCHEDULE_ENABLED'),
])
def test_independent_schedule_flags_and_payload(tmp_path,cron,scope,flag):
    event=tmp_path/'event.json';event.write_text(json.dumps({'schedule':cron}))
    env=dict(GITHUB_EVENT_NAME='schedule',GITHUB_EVENT_PATH=str(event),GITHUB_SHA='a'*40,
        WORKFLOW_SHA='a'*40,GITHUB_RUN_ATTEMPT='1',
        WORKFLOW_REF='tmoriuchi1401-source/kakeibo-ai/.github/workflows/kakeibo-production.yml@refs/heads/main',
        KAKEIBO_SCHEDULE_ENABLED='false',**{flag:'true'})
    flow.verify_schedule(env,scope=scope,mode='apply')
    for changes in ({flag:'false'},{flag:''},{flag:'TRUE'},
                    {'WORKFLOW_SHA':'b'*40},{'GITHUB_RUN_ATTEMPT':'2'},
                    {'WORKFLOW_REF':'someone/else/.github/workflows/kakeibo-production.yml@refs/heads/main'}):
        with pytest.raises(StateError):flow.verify_schedule(env|changes,scope=scope,mode='apply')
    with pytest.raises(StateError):flow.verify_schedule(env,scope='all',mode='apply')
    with pytest.raises(StateError):flow.verify_schedule(env,scope=scope,mode='preview')
    event.write_text(json.dumps({'schedule':'0 0 * * *'}))
    with pytest.raises(StateError):flow.verify_schedule(env|{'KAKEIBO_SCHEDULE_ENABLED':'true'},scope=scope,mode='apply')


def test_core_runs_only_amazon_balance_and_existing_accounting():
    calls=[]
    runners={s:(lambda s=s:calls.append(s) or {'written':0}) for s in DEPENDENCIES}
    history={s:dict(phase='ready',counts={}) for s in DEPENDENCIES}
    result=execute_serial(runners,history=history,selected_sources=flow.DRIVE_SCHEDULE_SCOPES['core'],require_omitted_ready=True)
    assert result['success']
    assert calls==['amazon','aupay_balance','review_apply','reconcile','auto_expense','review_refresh','expenses_refresh']
    for source in ('aupay_card','receipts','paypay','bank'):
        history[source]['phase']='pending';calls.clear()
        result=execute_serial(runners,history=history,selected_sources=flow.DRIVE_SCHEDULE_SCOPES['core'],require_omitted_ready=True)
        assert not result['success'] and calls==['amazon','aupay_balance']
        history[source]['phase']='ready'


@pytest.mark.parametrize('pending',[None,'amazon','aupay_card','bank','receipts'])
def test_global_pending_preflight_uses_readonly_native_inspection(monkeypatch,pending):
    calls=[]
    history={s:dict(phase='ready',attempt='',error='') for s in DEPENDENCIES}
    env=dict(SPREADSHEET_ID='synthetic-sheet',KAKEIBO_STATE_FOLDER_ID='synthetic-folder',
        AMAZON_STATE_FILE_ID='amazon-file',AUPAY_CARD_STATE_FILE_ID='card-file',BANK_STATE_FILE_ID='bank-file')
    if pending=='receipts':history[pending].update(phase='pending',attempt='synthetic')
    monkeypatch.setattr('app.google_clients.read_only_drive_service',lambda:object())
    monkeypatch.setattr('app.google_clients.drive_service',lambda:pytest.fail('write client'))
    monkeypatch.setattr(flow,'DriveStateTransport',lambda svc,b:b)
    def state(transport,binding):
        calls.append(binding.source)
        origin=flow.STATE_SOURCES.get(pending)
        return SimpleNamespace(inspect=lambda:dict(phase='pending' if binding.source==origin else 'ready'))
    monkeypatch.setattr(flow,'DurableState',state)
    if pending:
        with pytest.raises(StateError,match='source_reconciliation_required'):flow.verify_source_readiness(env,history)
    else:
        flow.verify_source_readiness(env,history)
        assert calls==['amazon_gmail','au_pay_card_gmail','bank_pdf_drive']
    if pending=='receipts':assert not calls


@pytest.mark.parametrize('limit',['1','2','3'])
def test_receipt_batch_filters_before_bounding_and_retains_original_list(limit):
    files=[dict(id='text',mimeType='text/plain')]+[dict(id=str(i),mimeType='image/png') for i in range(7)]
    before=deepcopy(files)
    assert [f['id'] for f in receipt_batch(files,limit,excluded={'0'})]==[str(i) for i in range(1,int(limit)+1)]
    assert files==before


@pytest.mark.parametrize('bad',['0','4','',None,'all'])
def test_invalid_receipt_bound_stops_before_context(monkeypatch,bad):
    from app import receipt_confirmation_production as runtime
    monkeypatch.setattr(runtime,'open_context',lambda *a:pytest.fail('context before bound validation'))
    with pytest.raises(RuntimeError,match='receipt_intake_limit_invalid'):
        runtime.execute({'RECEIPT_INTAKE_LIMIT':bad},True)


@pytest.mark.parametrize('limit',['1','2','3'])
def test_real_intake_limits_normal_plan_and_keeps_unscanned_reviews(monkeypatch,tmp_path,limit):
    from app import receipt_confirmation_production as runtime
    from app.receipt_confirmation import ReceiptConfirmation
    from test_receipt_confirmation import Store,DB
    store,db=Store(),DB()
    settings=SimpleNamespace(receipt_drive_folder_id='synthetic-inbox')
    metadata=Mock(return_value='stable')
    files=[dict(id=str(i),mimeType='image/png',version='1') for i in range(5)]
    api=Mock();api.files().list().execute.return_value={'files':files}
    monkeypatch.setattr(runtime,'open_context',lambda *a:(settings,store,db,metadata))
    monkeypatch.setattr('app.google_clients.read_only_drive_service',lambda:api)
    downloads=Mock(side_effect=lambda sid,*a:sid.encode())
    monkeypatch.setattr('app.google_clients.download_drive_file',downloads)
    monkeypatch.setattr(runtime,'evaluate_receipt_privacy',lambda *a,**k:SimpleNamespace(classification='normal',gemini_allowed=True))
    monkeypatch.setattr(runtime,'configure_ui',lambda *a:None)
    monkeypatch.setattr(runtime,'sync_review_visibility',lambda *a:None)
    review=ReceiptConfirmation(store,db,metadata)
    review.observe_intake_hold(dict(source_id='4',version='1',mime_type='image/png',sha256='a'*64),
        'synthetic-inbox',SimpleNamespace(classification='sensitive_unknown',gemini_allowed=False))
    review.render()
    path=tmp_path/'plan.json'
    result=runtime.execute(dict(RECEIPT_INTAKE_LIMIT=limit,RECEIPT_SCAN_PLAN=str(path)),True)
    assert result['found']==int(limit)==downloads.call_count
    plan=json.loads(path.read_text())
    assert len(plan['sources'])==int(limit) and not plan['multipage_sources']
    assert all(x['status']=='waiting' for x in store.value['confirmation_items'].values())
    assert not db.rows['支出明細']


def test_timeout_is_never_retried():
    import subprocess
    from unittest.mock import patch
    run=Mock(side_effect=subprocess.TimeoutExpired('synthetic',900))
    with patch.object(flow.subprocess,'run',run),pytest.raises(StateError,match='source_command_timed_out'):
        flow.invoke('receipts',apply=False,env={})
    assert run.call_count==1 and run.call_args.kwargs['timeout']==900


def test_core_unknown_write_stops_before_even_the_next_independent_source():
    calls=[]
    def unknown():
        calls.append('amazon')
        raise StateError('state_drive_write_unknown')
    runners={s:(lambda s=s:calls.append(s) or {'written':0}) for s in DEPENDENCIES}
    runners['amazon']=unknown
    result=execute_serial(runners,selected_sources=flow.DRIVE_SCHEDULE_SCOPES['core'],stop_on_failure=True)
    assert not result['success'] and calls==['amazon']
    assert result['sources']['amazon']['error']=='state_drive_write_unknown'
    assert result['sources']['aupay_balance']['status']=='skipped'
