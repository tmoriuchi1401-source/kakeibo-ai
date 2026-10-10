"""Synthetic owner inputs and existing backends; no live credentials or files."""
import base64
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import json

import pytest
from app import pdf_page_manual_canary as manual
from app.drive_run_state import StateError
from app.pdf_page_kind import PageKindConfirmation
from app.pdf_page_review import cards
from app.pdf_page_review_worker import medical_factory
from app.pdf_page_medical import manual_source, manual_values
from app.receipt_pdf_units import _digest
from test_pdf_page_review import Sheet, snapshot, CATEGORIES, FIELDS
from test_pdf_production_authority import setup
from test_pdf_unit_processing import context as completion_context
from test_receipt_confirmation import Store, DB
from test_receipt_pdf_units import local_ocr

HEAD='a'*40
MAIN='b'*40


def env():
    return dict(GITHUB_ACTIONS='true',GITHUB_EVENT_NAME='workflow_dispatch',
        GITHUB_REPOSITORY=manual.REPO,GITHUB_REF='refs/heads/main',
        GITHUB_WORKFLOW_REF=manual.REPO+'/'+manual.WORKFLOW+'@refs/heads/main',
        RUNNER_ENVIRONMENT='github-hosted',RUNNER_OS='Linux',
        KAKEIBO_PRODUCTION_ENABLED='true',GITHUB_SHA=MAIN,
        KAKEIBO_VALIDATED_MAIN_SHA=MAIN,PDF_MANUAL_APPROVED_SHA=HEAD,
        PDF_MANUAL_MODE='preflight',PDF_MANUAL_CONFIRM='MANUAL_ONLY',
        GITHUB_RUN_ID='123',GOOGLE_SERVICE_ACCOUNT_JSON='synthetic-config-not-a-key',
        SPREADSHEET_ID=manual.SID,PDF_GROUPING_BINDING='synthetic-binding',
        RECEIPT_CONFIRMATION_BINDING='synthetic-binding',
        KAKEIBO_PROJECTION_FOLDER_ID='synthetic-projection',
        KAKEIBO_STATE_FOLDER_ID='synthetic-private',
        PDF_ARCHIVE_ENABLED='false',PDF_AUTOMATIC_ENABLED='false',GITHUB_TOKEN='synthetic-token')


@pytest.mark.parametrize('field,value',[
    ('GITHUB_ACTIONS','false'),('GITHUB_EVENT_NAME','schedule'),
    ('GITHUB_REPOSITORY','other/repo'),('GITHUB_REF','refs/heads/codex/pdf-page-privacy'),
    ('GITHUB_WORKFLOW_REF','different'),('RUNNER_ENVIRONMENT','self-hosted'),
    ('RUNNER_OS','Windows'),('KAKEIBO_PRODUCTION_ENABLED','false'),
    ('GITHUB_SHA','c'*40),('KAKEIBO_VALIDATED_MAIN_SHA',''),
    ('PDF_MANUAL_APPROVED_SHA','c'*40),('PDF_MANUAL_MODE','apply'),
    ('PDF_MANUAL_CONFIRM',''),('GITHUB_RUN_ID',''),('GOOGLE_SERVICE_ACCOUNT_JSON',''),
    ('SPREADSHEET_ID','other'),('PDF_GROUPING_BINDING',''),
    ('RECEIPT_CONFIRMATION_BINDING',''),('KAKEIBO_PROJECTION_FOLDER_ID',''),
    ('KAKEIBO_STATE_FOLDER_ID',''),('GEMINI_API_KEY','forbidden'),
    ('PDF_ARCHIVE_ENABLED','true'),('PDF_AUTOMATIC_ENABLED','true'),
    ('GOOGLE_APPLICATION_CREDENTIALS','file'),('GOOGLE_SERVICE_ACCOUNT_FILE','file'),
    ('ACTIONS_STEP_DEBUG','true'),('RUNNER_DEBUG','1'),
])
def test_host_boundary_stops_before_google_clients(field,value,monkeypatch):
    e=env();e[field]=value
    opening=Mock(side_effect=AssertionError('No Google client before boundary'))
    monkeypatch.setattr(manual,'authority_context',opening)
    with pytest.raises(StateError):manual.execute(e,HEAD)
    opening.assert_not_called()


def test_review_requires_capture_uuid_and_no_local_apply():
    e=env();manual.require_context(e,HEAD)
    e['PDF_MANUAL_MODE']='review'
    with pytest.raises(StateError,match='captured_uuid'):manual.require_context(e,HEAD)
    e['PDF_MANUAL_REQUEST_ID']='00000000-0000-0000-0000-000000000001'
    manual.require_context(e,HEAD)


def github_fixture():
    pr={'state':'open','draft':True,'base':{'ref':'main'},
        'head':{'repo':{'full_name':manual.REPO},'ref':manual.BRANCH,'sha':HEAD}}
    checks={'check_runs':[{'name':n,'status':'completed','conclusion':'success'} for n in manual.CHECKS]}
    def get(path,token):
        assert token=='synthetic-token'
        return deepcopy(pr if '/pulls/' in path else checks if '/check-runs' in path else {'commit':{'sha':MAIN}})
    return pr,checks,get


def test_exact_draft_head_main_and_ci_are_required():
    pr,checks,get=github_fixture();manual.check_code(env(),get)
    pr['draft']=False
    with pytest.raises(StateError):manual.check_code(env(),get)
    pr['draft']=True;checks['check_runs'][0]['conclusion']='failure'
    with pytest.raises(StateError):manual.check_code(env(),get)
    checks['check_runs'][0]['conclusion']='success';checks['check_runs'].append(checks['check_runs'][0])
    with pytest.raises(StateError):manual.check_code(env(),get)


def test_medical_record_requires_exact_main_whole_file_isolation():
    def get(path,token):
        name=path.split('/app/')[1].split('?')[0]
        raw=(Path(manual.__file__).parent/name).read_bytes()
        return {'encoding':'base64','content':base64.b64encode(raw).decode()}
    manual.require_legacy_isolation(env(),get)
    def old_main(path,token):
        raw=(b'class ReceiptConfirmation:\n def items(self):\n  return self.store.value\n'
             if 'receipt_confirmation.py?' in path else b'def archive_confirmations():\n pass\n')
        return {'encoding':'base64','content':base64.b64encode(raw).decode()}
    with pytest.raises(StateError,match='legacy_isolation'):manual.require_legacy_isolation(env(),old_main)


def service():
    http=Mock();http.request.return_value=('response',b'synthetic')
    return SimpleNamespace(_http=http),http.request


@pytest.mark.parametrize('path,method,tag',[
    ('/upload/drive/v2/files/completion','PUT','"strong"'),
    ('/upload/drive/v2/files/completion','PATCH','"strong"'),
    ('/upload/drive/v3/files/existing','PATCH',None),
])
def test_drive_fence_only_existing_state_or_conditional_completion(path,method,tag):
    svc,http=service();manual.drive_fence(svc,completion_id='completion',existing_state_id='existing')
    svc._http.request('https://www.googleapis.com'+path,method=method,headers={'If-Match':tag} if tag else {})
    assert http.call_count==1


@pytest.mark.parametrize('path,method,tag',[
    ('/upload/drive/v2/files/completion','PUT',None),
    ('/upload/drive/v2/files/completion','PUT','W/"weak"'),
    ('/upload/drive/v2/files/completion','PUT','*'),
    ('/upload/drive/v3/files/completion','PATCH','"strong"'),
    ('/upload/drive/v2/files/source','PUT','"strong"'),
    ('/drive/v3/files/source','PATCH',None),
    ('/drive/v3/files/existing','DELETE',None),
    ('/upload/drive/v3/files','POST',None),
])
def test_no_create_delete_move_or_unconditional_completion(path,method,tag):
    svc,http=service();manual.drive_fence(svc,completion_id='completion',existing_state_id='existing')
    with pytest.raises(StateError):svc._http.request('https://www.googleapis.com'+path,method=method,headers={'If-Match':tag})
    http.assert_not_called()


def accounting_uri(title='支出明細',option='RAW'):
    return 'https://sheets.googleapis.com/v4/spreadsheets/'+manual.SID+'/values/'+title+'!A:A:append?valueInputOption='+option


def test_accounting_allows_only_exact_pending_row_once_even_after_unknown_delivery():
    svc,http=service();row=['synthetic-id',500];owner=Mock();pending=Mock(return_value=[row])
    attempts=manual.accounting_fence(svc,pending,owner)
    http.side_effect=OSError('Synthetic unacknowledged delivery')
    body=json.dumps({'values':[row]})
    with pytest.raises(OSError):svc._http.request(accounting_uri(),method='POST',body=body)
    assert len(attempts)==http.call_count==1 and owner.call_count==1
    with pytest.raises(StateError):svc._http.request(accounting_uri(),method='POST',body=body)
    assert len(attempts)==http.call_count==1


@pytest.mark.parametrize('uri,method,row',[
    (accounting_uri(option='USER_ENTERED'),'POST',['synthetic-id',500]),
    (accounting_uri('収入明細'),'POST',['synthetic-id',500]),
    (accounting_uri(),'PUT',['synthetic-id',500]),
    (accounting_uri(),'DELETE',['synthetic-id',500]),
    (accounting_uri(),'POST',['synthetic-id',501]),
    (accounting_uri().replace(manual.SID,'another-sheet'),'POST',['synthetic-id',500]),
    (accounting_uri().replace('sheets.googleapis.com','evil.example'),'POST',['synthetic-id',500]),
])
def test_accounting_unplanned_or_other_target_never_reaches_http(uri,method,row):
    svc,http=service();attempts=manual.accounting_fence(svc,lambda _:[['synthetic-id',500]],lambda:None)
    with pytest.raises(StateError):svc._http.request(uri,method=method,body=json.dumps({'values':[row]}))
    assert not attempts;http.assert_not_called()


def test_owner_snapshot_change_before_append_is_zero_io():
    svc,http=service()
    def refuse():raise StateError('pdf_manual_owner_snapshot_changed')
    attempts=manual.accounting_fence(svc,lambda _:[['synthetic-id',500]],refuse)
    with pytest.raises(StateError):svc._http.request(accounting_uri(),method='POST',body=json.dumps({'values':[['synthetic-id',500]]}))
    assert not attempts;http.assert_not_called()


@pytest.mark.parametrize('rng', ["'PDFページ確認'!B9","'_PDF確認受付'!B2","'_PDF確認受付'!E2:F2"])
def test_ui_projection_only_exact_status_result_and_captured_queue(rng):
    svc,http=service();allowed={"'PDFページ確認'!B9","'_PDF確認受付'!B2","'_PDF確認受付'!E2:F2"}
    verify=Mock();manual.projection_fence(svc,lambda:allowed,verify)
    svc._http.request('https://sheets.googleapis.com/v4/spreadsheets/'+manual.SID+'/values:batchUpdate',
        method='POST',body=json.dumps({'valueInputOption':'RAW','data':[{'range':rng,'values':[['synthetic']]}]}))
    assert http.call_count==verify.call_count==1


@pytest.mark.parametrize('rng',["'PDFページ確認'!B6", "'レシート'!A1", "'_PDF確認受付'!A2:F2", "'PDFページ確認'!A1:P100"])
def test_ui_projection_cannot_overwrite_owner_fields_or_accounting(rng):
    svc,http=service();manual.projection_fence(svc,lambda:{"'PDFページ確認'!B9"},lambda:None)
    with pytest.raises(StateError):svc._http.request('https://sheets.googleapis.com/v4/spreadsheets/'+manual.SID+'/values:batchUpdate',
        method='POST',body=json.dumps({'valueInputOption':'RAW','data':[{'range':rng,'values':[['synthetic']]}]}))
    http.assert_not_called()


def owner_medical_context(monkeypatch):
    g,live,authority_drive,provider=setup(('unknown','normal'),{1:'医療'})
    kinds=PageKindConfirmation(g);value,p,answers=kinds.current('drive-source-id')
    monkeypatch.setattr(manual,'SOURCE','drive-source-id')
    monkeypatch.setattr(manual,'SOURCE_HASH',p['source_content_hash'])
    monkeypatch.setattr(manual,'SOURCE_KEY',_digest('drive-source-id'))
    card=next(c for c in cards(g.view(value['records'][_digest('drive-source-id')]),answers,CATEGORIES) if c['identity']['kind']=='medical')
    fields={**FIELDS,'facility':'本人が入力した架空の施設'}
    sheet=Sheet(snapshot(card,fields));store=Store();db=DB()
    # Match the existing private backend's serialized payload/read-back contract.
    store.payload=b''
    original=store.save
    def save(value):
        original(value);store.payload=json.dumps(store.value,ensure_ascii=False,sort_keys=True).encode()
    store.save=save;store.transport=SimpleNamespace(read=lambda:store.payload)
    real=medical_factory(kinds,store,db,'synthetic-folder')(manual_source(p['pages'][0],answers[1]),
        lambda:manual_values({r[0]:r[2] for r in sheet.live['rows']},CATEGORIES))
    return kinds,live,authority_drive,provider,sheet,store,db,real


def test_medical_completion_is_existing_manual_readback_only_and_replay_no_writes(local_ocr,monkeypatch):
    kinds,live,ad,provider,sheet,store,db,real=owner_medical_context(monkeypatch)
    manual.target_card(kinds,sheet,sheet.capture)
    completion_drive,completion,_=completion_context()
    with pytest.raises((KeyError,StateError)):manual.medical_completion(real,provider,completion)
    assert completion_drive.updates==[] and db.writes==0
    assert real.confirm(submitted_inputs=real.read_owner_inputs())=='医療確定済み'
    assert db.writes==3
    assert manual.medical_completion(real,provider,completion)=='医療確定済み'
    value=completion.load();before=(completion_drive.payload,deepcopy(store.value),db.writes,len(completion_drive.updates))
    assert manual.medical_completion(real,provider,completion)=='医療確定済み'
    assert before==(completion_drive.payload,store.value,db.writes,len(completion_drive.updates))
    item=next(iter(value['records'].values()))
    assert item['route']=='medical_manual' and item['phase']=='applied' and item['planned_rows']=={}
    assert '本人が入力' not in json.dumps(value,ensure_ascii=False)
    assert not any(n in json.dumps(value) for n in ('facility','amount','inputs','patient'))


@pytest.mark.parametrize('failure',['source','owner','accounting','backend_readback'])
def test_medical_completion_needs_fresh_source_owner_exact_rows_and_durable_backend(failure,local_ocr,monkeypatch):
    kinds,live,ad,provider,sheet,store,db,real=owner_medical_context(monkeypatch)
    assert real.confirm()=='医療確定済み'
    drive,completion,_=completion_context();before=db.writes
    if failure=='source':live.content+=b'changed'
    if failure=='owner':next(r for r in sheet.live['rows'] if r[0]=='amount')[2]='101'
    if failure=='accounting':db.rows['支出明細'][0][4]=999
    if failure=='backend_readback':store.transport.read=lambda:b'changed'
    with pytest.raises(StateError):manual.medical_completion(real,provider,completion)
    assert drive.updates==[] and db.writes==before


def test_selected_card_requires_explicit_complete_owner_action(local_ocr,monkeypatch):
    kinds,live,ad,provider,sheet,store,db,real=owner_medical_context(monkeypatch)
    for row in sheet.live['rows']:
        if row[0]=='medical_action':row[2]=''
    with pytest.raises(StateError):manual.target_card(kinds,sheet,sheet.live)
    assert not store.value['confirmation_items'] and db.writes==0


def test_completion_save_failure_is_repaired_from_existing_backend_without_accounting_retry(local_ocr,monkeypatch):
    kinds,live,ad,provider,sheet,store,db,real=owner_medical_context(monkeypatch)
    assert real.confirm()=='医療確定済み'
    drive,completion,_=completion_context();before=(deepcopy(db.rows),db.writes,store.payload)
    drive.status=412
    with pytest.raises(StateError):manual.medical_completion(real,provider,completion)
    assert (db.rows,db.writes,store.payload)==before
    drive.status=None
    assert manual.medical_completion(real,provider,completion)=='医療確定済み'
    assert (db.rows,db.writes,store.payload)==before


def test_target_projection_preserves_every_owner_value_and_other_cards(local_ocr,monkeypatch):
    kinds,live,ad,provider,sheet,store,db,real=owner_medical_context(monkeypatch)
    capture=deepcopy(sheet.capture)
    native=[]
    for field,label,value in capture['rows']:
        native.append([label,value,'pdf-page-review-v1',capture['token'],json.dumps(capture['identity']),field])
    native.append(['他ページの本人入力','入力途中','pdf-page-review-v1','other','{}','amount'])
    before=deepcopy(native)
    page=manual.TargetProjection(Mock(),manual.SID,CATEGORIES,target=capture['token'])
    page._rows=lambda:native
    page.read_card=lambda token:{**deepcopy(capture),'rows':[[row[5],row[0],row[1]] for row in native if row[3]==token]}
    def write(requests):
        assert len(requests)==2
        for request in requests:
            pos=int(request['range'].split('!B')[1])-1
            assert native[pos][5] in {'state','result'}
            native[pos][1]=request['values'][0][0]
    page._write=write
    updated=deepcopy(capture)
    for row in updated['rows']:
        if row[0]=='state':row[2]='医療確定済み'
        if row[0]=='result':row[2]='read-back検証済み'
    page.publish_cards([updated])
    assert native[-1]==before[-1]
    assert all(a==b for a,b in zip(native,before) if a[5] not in {'state','result'})


def test_safe_main_never_logs_sensitive_exception_or_partial_outputs(capsys,monkeypatch):
    monkeypatch.setattr(manual.subprocess,'check_output',lambda *a,**k:HEAD)
    def fail(*args):
        print('synthetic-private-owner-input')
        raise RuntimeError('synthetic-secret-sensitive-details')
    monkeypatch.setattr(manual,'execute',fail)
    assert manual.main()==1
    out=capsys.readouterr()
    assert out.err=='' and 'synthetic-private' not in out.out and 'synthetic-secret' not in out.out
    assert json.loads(out.out)=={'status':'manual_held','reason':'manual_request_not_completed'}


def test_manual_workflow_is_off_schedule_exact_main_default_read_only_and_no_ai_secret():
    import yaml
    root=Path(manual.__file__).parents[1]
    text=(root/manual.WORKFLOW).read_text(encoding='utf-8')
    workflow=yaml.load(text,Loader=yaml.BaseLoader)
    assert set(workflow['on'])=={'workflow_dispatch'}
    inputs=workflow['on']['workflow_dispatch']['inputs']
    assert inputs['mode']['default']=='preflight' and inputs['mode']['options']==['preflight','review']
    assert workflow['concurrency']=={'group':'kakeibo-production','cancel-in-progress':'false','queue':'max'}
    job=workflow['jobs']['canary']
    assert "github.sha == vars.KAKEIBO_VALIDATED_MAIN_SHA" in job['if']
    assert "github.ref == 'refs/heads/main'" in job['if']
    step=job['steps'][-1]
    assert step['run']=='python -m app.pdf_page_manual_canary'
    assert step['env']['PDF_AUTOMATIC_ENABLED']==step['env']['PDF_ARCHIVE_ENABLED']=='false'
    assert 'GEMINI_API_KEY' not in text and 'PROCESSED' not in text and 'upload-artifact' not in text
    first_secret=next(i for i,s in enumerate(job['steps']) if 'secrets.' in json.dumps(s))
    assert first_secret==len(job['steps'])-1
    assert 'checks' in json.dumps(job['steps'][1]) and 'persist-credentials' in json.dumps(job['steps'][2])
