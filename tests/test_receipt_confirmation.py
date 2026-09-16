from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
import re

import pytest

from app.receipt_confirmation import ReceiptConfirmation,TITLE,HEADERS,review_id
from app.drive_run_state import StateError
from app import production_flow


class Store:
    def __init__(self):self.value={'confirmation_items':{},'records':{},'manifest':{'sources':[]}};self.fail=False
    def save(self,value):
        if self.fail:raise StateError('store_failed')
        self.value=deepcopy(value)


class DB:
    def __init__(self):
        self.rows={x:[] for x in ['レシート','取込データ','支出明細','要確認']}
        self.headers={};self.writes=0;self.after_write=False;self.before_write=False
    def categories(self):return [('医療・保険','病院'),('食費','食品')]
    def sheet_titles(self):return list(self.rows)
    def get(self,rng):
        title=rng.split('!')[0].strip("'")
        if rng.endswith('1:1'):return [self.headers[title]]
        return deepcopy(self.rows.get(title,[]))
    def ensure_sheet(self,title,headers):self.rows.setdefault(title,[]);self.headers[title]=headers
    def append_raw(self,title,rows):
        if self.before_write and title!=TITLE:raise OSError('lost before commit')
        self.rows[title].extend(deepcopy(rows));self.writes+=1
        if self.after_write and title!=TITLE:raise OSError('lost response after commit')
    def update_row_raw(self,title,n,row):self.rows[title][n-2]=deepcopy(row);self.writes+=1
    def set_raw_range(self,rng,values):
        pos=re.search(r'!([A-Z]+)(\d+)',rng);col=ord(pos[1])-65;n=int(pos[2])-2
        row=self.rows[TITLE][n]
        row.extend(['']*max(0,16-len(row)))
        row[col:col+len(values[0])]=values[0]


def medical():
    store=Store();db=DB();verify=Mock()
    service=ReceiptConfirmation(store,db,verify)
    source={'source_id':'synthetic-medical','version':'1','sha256':'a'*64,'mime_type':'application/pdf'}
    service.observe_medical(source,'synthetic-folder');service.render()
    return service,store,db,verify,source


def confirm(db,**changes):
    row=db.rows[TITLE][0];row[7:15]=['2026-09-01','Synthetic clinic',100,'医療・保険｜病院','現金','医療費を確定','','']
    for k,v in changes.items():row[int(k)]=v


def test_medical_missing_inputs_never_post_and_refresh_preserves_typing():
    service,store,db,verify,s=medical()
    db.rows[TITLE][0][8]='User typing'
    service.capture_inputs();service.render()
    assert db.rows[TITLE][0][8]=='User typing'
    assert service.apply_confirmations()==0
    assert not db.rows['支出明細'] and not db.rows['取込データ']
    restarted=ReceiptConfirmation(store,db,verify);restarted.render()
    assert db.rows[TITLE][0][8]=='User typing'
    assert not restarted.observe_medical(s,'synthetic-folder')
    assert len(restarted.items)==1


def test_medical_confirm_post_readback_restart_replay_no_duplicate():
    service,store,db,verify,s=medical();confirm(db)
    service.capture_inputs();assert service.apply_confirmations()==1
    assert len(db.rows['レシート'])==len(db.rows['取込データ'])==len(db.rows['支出明細'])==1
    assert db.rows['支出明細'][0][0]=='R-synthetic-medical-01'
    assert db.rows['支出明細'][0][4]==100
    restarted=ReceiptConfirmation(store,db,verify);restarted.capture_inputs();restarted.render()
    assert restarted.apply_confirmations()==0
    assert len(db.rows['支出明細'])==1 and db.rows[TITLE][0][2]=='反映済み'


def test_medical_ai_candidate_requires_explicit_adoption_and_preserves_inputs():
    from app.medical_candidate_state import MedicalCandidateState
    service,store,db,verify,s=medical();key=next(iter(service.items))
    MedicalCandidateState(store).publish(key,s,{'date':'2026-09-01','issuer':'Synthetic clinic',
        'amount_yen':123,'category':'医療・保険｜病院','provenance':{'amount':'IMAGE_AI_CANDIDATE'}})
    service.render()
    assert db.rows[TITLE][0][7:15]==['']*8
    assert '画像AI' in db.rows[TITLE][0][6] and service.apply_confirmations()==0
    # The human may correct an incorrect AI amount; those values take precedence.
    db.rows[TITLE][0][9]=125;db.rows[TITLE][0][12]='候補で医療費を確定'
    service.capture_inputs();assert service.apply_confirmations()==1
    assert db.rows['支出明細'][0][4]==125
    assert service.apply_confirmations()==0


def test_medical_changed_candidate_does_not_reuse_old_human_confirmation():
    from app.medical_candidate_state import MedicalCandidateState
    service,store,db,verify,s=medical();key=next(iter(service.items));state=MedicalCandidateState(store)
    fields={'date':'2026-09-01','issuer':'Synthetic clinic','amount_yen':100,'category':'医療・保険｜病院'}
    state.publish(key,s,fields);service.render()
    db.rows[TITLE][0][12]='候補で医療費を確定';service.capture_inputs()
    state.publish(key,s,dict(fields,amount_yen=101));service.render()
    assert db.rows[TITLE][0][12]=='候補で医療費を確定'
    assert service.apply_confirmations()==0 and not db.rows['支出明細']
    db.rows[TITLE][0][12]='保留';service.capture_inputs();service.render()
    db.rows[TITLE][0][12]='候補で医療費を確定';service.capture_inputs()
    assert service.apply_confirmations()==1


def test_medical_candidate_incomplete_never_posts_and_does_not_change_general_inputs():
    from app.medical_candidate_state import MedicalCandidateState
    service,store,db,verify,s=medical();key=next(iter(service.items));state=MedicalCandidateState(store)
    state.publish(key,s,{'amount_yen':123});service.render()
    db.rows[TITLE][0][8]='Owner typing';db.rows[TITLE][0][12]='候補で医療費を確定'
    service.capture_inputs();service.render()
    assert service.apply_confirmations()==0 and db.rows[TITLE][0][8]=='Owner typing'


def test_medical_send_intent_survives_unknown_results_and_completed_results_replay():
    from app.medical_candidate_state import MedicalCandidateState
    service,store,db,verify,s=medical();key=next(iter(service.items));state=MedicalCandidateState(store)
    args=dict(review_id=key,source=s,mapping={'crop':'synthetic'},model='synthetic-model',prompt='synthetic-prompt')
    assert state.begin('analysis',**args)
    with pytest.raises(StateError,match='reconciliation_required'):MedicalCandidateState(store).begin('analysis',**args)
    state.complete('analysis',{'status':'unreadable'})
    assert MedicalCandidateState(store).begin('analysis',**args) is False
    store.fail=True
    with pytest.raises(StateError):state.begin('another-analysis',**args)
    assert state.get('another-analysis') is None


@pytest.mark.parametrize('col,value',[(7,''),(8,''),(9,''),(9,0),(9,-1),(10,'食費｜食品'),(12,'候補明細で確定')])
def test_missing_or_wrong_medical_confirmation_cannot_post(col,value):
    service,store,db,verify,s=medical();confirm(db);db.rows[TITLE][0][col]=value
    service.capture_inputs();assert service.apply_confirmations()==0
    assert not db.rows['支出明細']


def test_unknown_successful_write_readbacks_without_repeat():
    service,store,db,verify,s=medical();confirm(db);db.after_write=True
    service.capture_inputs();assert service.apply_confirmations()==1
    assert all(len(db.rows[t])==1 for t in ['レシート','取込データ','支出明細'])


def test_missing_unknown_write_stays_pending_and_never_blindly_reappends():
    service,store,db,verify,s=medical();confirm(db);db.before_write=True
    service.capture_inputs()
    with pytest.raises(StateError,match='write_unknown'):service.apply_confirmations()
    db.before_write=False
    with pytest.raises(StateError,match='reconciliation_required'):ReceiptConfirmation(store,db,verify).apply_confirmations()
    assert not db.rows['支出明細']
    assert next(iter(store.value['confirmation_items'].values()))['status']=='pending'


def test_state_save_failure_prevents_accounting_write():
    service,store,db,verify,s=medical();confirm(db);service.capture_inputs();store.fail=True
    with pytest.raises(StateError):service.apply_confirmations()
    assert not db.rows['レシート']


def test_source_version_change_requires_new_blank_confirmation():
    service,store,db,verify,s=medical();confirm(db);service.capture_inputs()
    newer=dict(s,version='2',sha256='b'*64);service.observe_medical(newer,'synthetic-folder');service.render()
    assert len(db.rows[TITLE])==2 and db.rows[TITLE][1][7:15]==['']*8
    assert service.apply_confirmations()==0


def test_duplicate_card_payment_requires_explicit_link_and_adds_no_expense():
    service,store,db,verify,s=medical();confirm(db)
    db.rows['支出明細']=[['card-existing','2026-09-01','Synthetic clinic','Payment',100,'医療費','医療費','card','card','','card-import','','active']]
    service.capture_inputs();assert service.apply_confirmations()==0
    assert len(db.rows['支出明細'])==1
    db.rows[TITLE][0][12:14]=['既存支出と重複（紐付け）','card-existing']
    service.capture_inputs();assert service.apply_confirmations()==1
    assert len(db.rows['支出明細'])==1 and db.rows['取込データ'][0][9]=='card-existing'


def test_receipt_preflight_child_has_no_ai_key(monkeypatch,tmp_path):
    calls=[]
    def run(args,**kwargs):
        calls.append((args,kwargs['env']))
        if 'app.receipt_confirmation_production' in args:
            return SimpleNamespace(returncode=0,stdout='{"found":2,"medical_pending":1,"blocked":1,"medical_detected":1,"written":0}')
        return SimpleNamespace(returncode=0,stdout='{"found":0,"needs_review":0,"written":0}')
    monkeypatch.setattr(production_flow.subprocess,'run',run)
    result=production_flow.invoke('receipts',apply=True,env={'GEMINI_API_KEY':'synthetic','RECEIPT_CONFIRMATION_BINDING':'synthetic','RUNNER_TEMP':str(tmp_path)})
    assert len(calls)==2 and 'GEMINI_API_KEY' not in calls[0][1]
    assert calls[1][1]['GEMINI_API_KEY']=='synthetic'
    assert calls[0][1]['RECEIPT_SCAN_PLAN']==calls[1][1]['RECEIPT_SCAN_PLAN']
    assert result['found']==2 and result['needs_review']==2


def test_changed_visible_candidate_cannot_authorize_hidden_original_values():
    service,store,db,verify,s=medical();confirm(db);db.rows[TITLE][0][6]='user replaced display'
    service.capture_inputs();assert service.apply_confirmations()==0
    service.render();assert service.apply_confirmations()==0
    assert not db.rows['支出明細']


def test_medical_changed_source_is_displayed_for_reconfirmation_without_post():
    service,store,db,verify,s=medical();confirm(db);service.capture_inputs()
    verify.side_effect=StateError('confirmation_source_changed')
    assert service.apply_confirmations()==0
    service.render();assert db.rows[TITLE][0][2]=='要再確認'
    assert not db.rows['支出明細']


def test_only_merchant_label_can_be_closed_mechanically():
    from tests.test_receipt_reimport import fixture
    from app.receipt_reimport import accounting_equal_keeping_labels
    parsed,rows=fixture();parsed.merchant='Synthetic shop branch label'
    assert accounting_equal_keeping_labels('s1',parsed,**rows)
    parsed.items[0].minor_category='other'
    assert not accounting_equal_keeping_labels('s1',parsed,**rows)


def test_general_missing_details_require_explicit_confirmation_and_replay_once():
    from tests.test_receipt_reimport import fixture
    from app.receipt_reimport_production import target_snapshot
    parsed,rows=fixture();rows.pop('categories');rows['expense_rows']=[]
    rows['receipt_rows'][0][6]='要確認';rows['import_rows'][0][8]='要確認'
    store=Store();db=DB()
    for key,title in [('receipt_rows','レシート'),('import_rows','取込データ'),('expense_rows','支出明細'),('review_rows','要確認')]:db.rows[title]=deepcopy(rows[key])
    source={'source_id':'s1','version':'1','sha256':'b'*64,'mime_type':'application/pdf'}
    store.value['manifest']={'sources':[source],'folder_id':'synthetic-folder'}
    store.value['records']={'s1':{'phase':'complete','parsed':parsed.model_dump(),'before':target_snapshot(rows,'s1')}}
    service=ReceiptConfirmation(store,db,Mock());service.prepare_general();service.render()
    assert service.apply_confirmations()==0
    db.rows[TITLE][0][12]='候補明細で確定';service.capture_inputs()
    assert service.apply_confirmations()==1
    assert len(db.rows['支出明細'])==1
    assert db.rows['レシート'][0][8].startswith('manual-note; ')
    assert ReceiptConfirmation(store,db,Mock()).apply_confirmations()==0


def test_production_scan_persists_medical_to_ui_and_passes_only_normal_bytes(monkeypatch,tmp_path):
    import json
    from app import receipt_confirmation_production as runtime,google_clients,settings,private_state_bindings
    store=Store();store.value['manifest'].update(owner_email='owner@example.invalid',sa_email='sa@example.invalid')
    db=DB()
    class Request:
        def __init__(self,result):self.result=result
        def execute(self,**kw):return self.result
    class Files:
        def get(self,**kw):
            if kw['fileId'] in {'synthetic-store','synthetic-folder'}:
                return Request({'owners':[{'emailAddress':'owner@example.invalid'}], 'permissions':[
                    {'type':'user','role':'owner','emailAddress':'owner@example.invalid'},
                    {'type':'user','role':'writer','emailAddress':'sa@example.invalid'}]})
            return Request({'id':kw['fileId'],'parents':['synthetic-inbox'],'version':'1','mimeType':'application/pdf'})
        def list(self,**kw):return Request({'files':[{'id':kind,'version':'1','mimeType':'application/pdf'} for kind in ['medical','normal','unknown']]})
    api=SimpleNamespace(files=lambda:Files())
    monkeypatch.setattr(google_clients,'drive_service',lambda:api)
    monkeypatch.setattr(google_clients,'read_only_drive_service',lambda:api)
    monkeypatch.setattr(google_clients,'download_drive_file',lambda sid,*a:sid.encode())
    monkeypatch.setattr(settings,'Settings',lambda:SimpleNamespace(spreadsheet_id='synthetic-sheet',receipt_drive_folder_id='synthetic-inbox',validate=lambda **kw:None))
    monkeypatch.setattr(settings,'service_account_source',lambda:(None,{'private_key':'synthetic-key'}))
    monkeypatch.setattr(private_state_bindings,'unwrap',lambda *a:'synthetic-store')
    monkeypatch.setattr(runtime,'ReimportStore',lambda *a:store)
    monkeypatch.setattr('app.sheets.SheetsDB',lambda *a,**k:db)
    monkeypatch.setattr(runtime,'configure_ui',lambda db:None)
    gate=Mock(side_effect=lambda payload,*a,**kw:SimpleNamespace(classification='sensitive_unknown' if payload==b'unknown' else payload.decode(),gemini_allowed=payload==b'normal'))
    monkeypatch.setattr(runtime,'evaluate_receipt_privacy',gate)
    env={'RECEIPT_CONFIRMATION_BINDING':json.dumps({'file':'cipher','manifest':'synthetic'}),'KAKEIBO_STATE_FOLDER_ID':'synthetic-folder','RECEIPT_SCAN_PLAN':str(tmp_path/'plan.json')}
    result=runtime.execute(env,True)
    assert result['medical_detected']==result['medical_pending']==result['blocked']==1
    assert len(db.rows[TITLE])==1 and db.rows[TITLE][0][7:15]==['']*8
    plan=json.loads((tmp_path/'plan.json').read_text())
    assert [s['source_id'] for s in plan['sources']]==['normal']
    assert list(tmp_path.glob('*.bin'))[0].read_bytes()==b'normal'
    runtime.execute(env,True)
    assert len(db.rows[TITLE])==1 and not db.rows['支出明細']


def test_medical_runtime_rejects_any_ai_key_before_google_calls():
    from app.receipt_confirmation_production import execute
    with pytest.raises(StateError,match='must_not_receive_ai_key'):execute({'GEMINI_API_KEY':'synthetic'},True)


def test_view_refresh_is_recovered_after_accounting_saved_before_display_failure():
    service,store,db,verify,s=medical();confirm(db);service.capture_inputs();service.apply_confirmations()
    restarted=ReceiptConfirmation(store,db,verify)
    assert restarted.apply_confirmations()==0 and restarted.refresh_needed()
    restarted.mark_refreshed();assert not ReceiptConfirmation(store,db,verify).refresh_needed()


def test_input_changed_immediately_before_write_does_not_post():
    service,store,db,verify,s=medical();confirm(db);service.capture_inputs()
    count=[0]
    def changed(*args):
        count[0]+=1
        if count[0]==2:db.rows[TITLE][0][9]=200
    verify.side_effect=changed
    assert service.apply_confirmations()==0
    assert not db.rows['支出明細']
    item=next(iter(service.items.values()))
    assert item['status']=='waiting' and item['require_reconfirm'] and item['aborted_before_accounting']


def test_production_cannot_silently_drop_review_persistence_configuration():
    with pytest.raises(StateError,match='binding_required'):
        production_flow.invoke('receipts',apply=True,env={'GITHUB_ACTIONS':'true'})


def test_normal_worker_never_downloads_medical_or_unapproved_originals(monkeypatch,tmp_path):
    from hashlib import sha256
    from app import drive_receipts
    payload=tmp_path/'normal.bin';payload.write_bytes(b'normal-only')
    service=Mock();service.files.return_value.list.return_value.execute.return_value={'files':[
        {'id':'medical','name':'private','mimeType':'application/pdf','version':'1'},
        {'id':'normal','name':'ordinary','mimeType':'application/pdf','version':'1'}]}
    monkeypatch.setattr(drive_receipts,'drive_service',lambda:service)
    download=Mock(side_effect=AssertionError('no second download'))
    monkeypatch.setattr(drive_receipts,'download_drive_file',download)
    pipeline=Mock();pipeline.process_bytes.return_value={'status':'imported'}
    drive_receipts.process_inbox('synthetic-inbox',pipeline,approved_sources=[{'source_id':'normal','version':'1','mime_type':'application/pdf','path':str(payload),'sha256':sha256(b'normal-only').hexdigest()}])
    assert pipeline.process_bytes.call_count==1
    assert pipeline.process_bytes.call_args.args[0]==b'normal-only'
    download.assert_not_called()
