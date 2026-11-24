"""PR-B integration: actual Apps Script in Node + actual Python validator.

All originals are fabricated bytes. No HTTP/auth, production workbook or writer.
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest

from app.drive_receipts import process_review_inbox
from app.models import ReceiptItem, ReceiptResult
from app.page_receipt_model import digest
from app.receipt_pipeline import ReceiptPipeline
from app.receipt_review_cases import (MAX_ITEMS, MACHINE_START, WIDTH, ReviewPipeline, check_case,
    APPROVAL_METHOD, APPROVAL_EVIDENCE, make_case, make_row, snapshot_from_row, validate_snapshot)
from app.receipt_review_snapshot import candidate_rows
from app.receipt_review_worker import AttemptFence, ScriptPort, SourceMismatch, execute_review
from app.receipt_sheets_authority import HEADERS, UNKNOWN, canonical

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def deny_finance_and_network(monkeypatch):
    from app.sheets import SheetsDB
    from app.gemini_ai import GeminiAI
    from google import genai
    calls=[]
    def deny(*args, **kwargs):
        calls.append(True)
        raise AssertionError('financial/network call forbidden')
    monkeypatch.setattr(SheetsDB,'__init__',deny)
    monkeypatch.setattr(GeminiAI,'__init__',deny)
    monkeypatch.setattr(genai.Client,'__init__',deny)
    monkeypatch.setattr(socket.socket,'connect',deny)
    monkeypatch.setattr(socket,'create_connection',deny)
    yield
    assert not calls


def case(suffix='one', amounts=(600,400), total=1000):
    sid='synthetic-source-b-'+suffix
    source=dict(source_id=sid,version='12',sha256=sha256(sid.encode()).hexdigest(),
        mime_type='image/png',classification='normal',transaction_kind='purchase')
    result=ReceiptResult(merchant='合成商店',date='2026-11-22',total=total,
        items=[ReceiptItem(name='合成商品'+str(i),amount=a,major_category='食費',minor_category='食品')
               for i,a in enumerate(amounts)])
    hashed=digest(result.model_dump())
    imported=['receipt:'+sid,'2026-11-22T10:00:00+09:00','receipt',sid,result.date,result.merchant,total,
              '現金','要確認','',hashed,'']
    entry=make_case(source,imported,candidate_rows(imported[0],hashed,result),[['食費','食品'],['日用品','消耗品']])
    row=make_row(entry)
    row[7]='途中の合成メモを保持'
    return entry,row


def ready(entry,row,tax='税込'):
    row=deepcopy(row)
    row[8]='register:'+entry['request_id']
    row[-2]=tax
    recalc(row)
    return row


def recalc(row):
    row[4]=sum(row[11+9*i] for i in range(8) if row[9+9*i] and row[16+9*i]=='採用')
    row[5]=row[3]-row[4]


def js(**payload):
    reply=subprocess.run(['node','apps-script/receipt-validation/Review.test.cjs'],
        input=json.dumps(payload,ensure_ascii=True),encoding='utf-8',capture_output=True,cwd=ROOT)
    assert reply.returncode==0,reply.stderr
    return json.loads(reply.stdout)


class JsPort:
    """Keep durable Sheet/property state between real Apps Script invocations."""
    def __init__(self):
        self.state={}
        self.calls=[]
        self.fault=None
        self.source_fault=None
        self.checks=0
        self.http_ports=False
    def invoke(self,command):
        self.calls.append(command['action'])
        reply=js(**self.state,commands=[command],fault=self.fault,http_ports=self.http_ports)
        self.state={k:reply[k] for k in ('cases','requests','ui','properties')}
        if reply['errors']:
            raise RuntimeError(reply['errors'][0])
        return reply['outputs'][0]
    def lookup_source(self,sid,hashed): return self.invoke(dict(action='lookup',source_id=sid,sha256=hashed))
    def intake(self,entry,row): return self.invoke(dict(action='intake',entry=entry,row=row))
    def read(self,rid): return self.invoke(dict(action='read',request_id=rid))
    def claim(self,rid,before): return self.invoke(dict(action='claim',request_id=rid,before=before))
    def finish(self,rid,before,result): return self.invoke(dict(action='finish',request_id=rid,before=before,result=result))
    def source_check(self,source):
        self.checks+=1
        if self.source_fault=='network': raise TimeoutError('private canary')
        if self.source_fault in {'id','hash','version','mime','owner','parent'} or self.source_fault=='after' and self.checks==2:
            raise SourceMismatch('private canary')


def registered():
    entry,row=case()
    port=JsPort()
    port.intake(entry,row)
    port.state['ui'][0]=ready(entry,row)
    port.invoke(dict(action='register',request_id=entry['request_id']))
    return entry,port


def test_full_intake_iphone_register_backend_and_frozen_result(tmp_path):
    entry,port=registered()
    before=deepcopy(port.state['requests'][1][:7])
    # A later iPhone edit remains a draft and never modifies the accepted snapshot.
    port.state['ui'][0][9]='登録後の下書き'
    port.state['ui'][0][7]+='／追記'
    result=execute_review(entry['request_id'],port=port,fence=AttemptFence(tmp_path/'attempts.db'))
    assert result['state']=='検証済み・未記帳'
    assert (result['receipt_total'],result['item_total'],result['difference'])==(1000,1000,0)
    assert result['financial_write_count']==0 and result['posting_authority'] is False
    assert port.state['requests'][1][:7]==before
    assert '"state":"検証済み・未記帳"' in port.state['requests'][1][8]
    assert port.state['ui'][0][9]=='登録後の下書き' and '追記' in port.state['ui'][0][7]
    assert port.calls.count('claim')==port.calls.count('finish')==1


@pytest.mark.parametrize('operation',['rename','amount','quantity_amount','add','remove','discount','external_tax'])
def test_item_edits_add_exclude_quantity_tax_discount_match_js(operation):
    entry,row=case()
    row=ready(entry,row)
    if operation=='rename': row[9]='修正商品🍎'
    if operation=='amount': row[11]=650;row[20]=350
    if operation=='quantity_amount': row[10]=2;row[11]=700;row[20]=300
    if operation in {'add','discount','external_tax'}:
        row[11]=500 if operation!='discount' else 700
        kind,amount=('値引き',-100) if operation=='discount' else ('外税',100) if operation=='external_tax' else ('商品',100)
        row[27:36]=['外税10%' if kind=='外税' else '実商品C' if kind=='商品' else '商品値引',1,amount,'食費','食品',kind,'指定なし','採用',True]
        if operation=='external_tax':row[-2]='税別'
    if operation=='remove': row[25]='除外';row[11]=1000
    recalc(row)
    snapshot=snapshot_from_row(row,entry)
    reply=js(commands=[dict(action='snapshot',row=row,entry=entry)])
    assert reply['errors']==[]
    assert reply['outputs'][0]==snapshot
    assert validate_snapshot(snapshot,entry)['state']=='検証済み・未記帳'
    assert snapshot['inputs']['note']==row[7]


@pytest.mark.parametrize('change,state,issue',[
    ('amount','保留','item_total_mismatch'),('category','修正が必要','category_not_in_master'),
    ('quantity','修正が必要','quantity_line_amount_confirmation_required'),
    ('unknown_tax','検証済み・未記帳','tax_treatment_unknown'),
    ('included_tax','保留','included_tax_not_additive'),('mixed_discount','保留','tax_discount_allocation_review')])
def test_reasoned_corrections_and_holds(change,state,issue,tmp_path):
    entry,row=case()
    row=ready(entry,row)
    if change=='amount':row[11]=601
    if change=='category':row[12:14]=['不正','カテゴリ']
    if change=='quantity':row[10]=2
    if change=='unknown_tax':row[-2]='不明'
    if change in {'included_tax','mixed_discount'}:
        row[11]=500 if change=='included_tax' else 700
        if change=='mixed_discount':row[21:23]=['日用品','消耗品']
        row[27:36]=['外税' if change=='included_tax' else '値引き',1,100 if change=='included_tax' else -100,
                    '食費','食品','外税' if change=='included_tax' else '値引き','指定なし','採用',True]
    recalc(row)
    port=JsPort();port.intake(entry,make_row(entry));port.state['ui'][0]=row
    port.invoke(dict(action='register',request_id=entry['request_id']))
    result=execute_review(entry['request_id'],port=port,fence=AttemptFence(tmp_path/'attempts.db'))
    assert result['state']==state
    assert any(x.startswith(issue) for x in result.get('issues',[])+result.get('warnings',[]))
    assert result['financial_write_count']==0
    assert 'RECEIPT_REVIEW_STOPPED' not in port.state['properties']


@pytest.mark.parametrize('field',[0,1,2,3,MACHINE_START,MACHINE_START+1,MACHINE_START+2,MACHINE_START+3,MACHINE_START+12,MACHINE_START+14])
def test_identity_version_input_binding_cannot_move_to_other_case(field):
    entry,row=case();row=ready(entry,row)
    row[field]='wrong' if field!=3 else 999
    with pytest.raises(ValueError): snapshot_from_row(row,entry)
    assert js(commands=[dict(action='snapshot',row=row,entry=entry)])['errors']


def test_sort_insert_delete_partial_sort_and_duplicate_cases():
    a,ar=case('a');b,br=case('b')
    p=JsPort();p.intake(a,ar);p.intake(b,br)
    p.state['ui']=[ready(b,br),ready(a,ar)]
    p.invoke(dict(action='register',request_id=a['request_id']))
    assert p.state['requests'][1][1]==a['case_id']
    assert json.loads(p.state['requests'][1][5])['inputs']['note']==ar[7]
    # Partial sorts, duplicate rows and deleted rows must never pick a neighbor.
    for rows in ([ready(a,ar),ready(a,ar)], [ready(a,ar)], [ready(a,ar)[:-17]+ready(b,br)[-17:]]):
        reply=js(cases=p.state['cases'],ui=rows,commands=[dict(action='register',request_id=b['request_id'])])
        assert reply['errors'] and reply['writes']==0


@pytest.mark.parametrize('duplicate',['case','source','hash','uuid'])
def test_persistent_intake_duplicates_even_after_restart(duplicate):
    a,ar=case('a');b,br=case('b')
    p=JsPort();p.intake(a,ar)
    if duplicate=='case':b,br=deepcopy(a),deepcopy(ar)
    elif duplicate=='source':b,br=deepcopy(a),deepcopy(ar);b['request_id']=case('c')[0]['request_id']
    elif duplicate=='hash':
        b['source']['sha256']=a['source']['sha256'];b['baseline_digest']=digest({k:v for k,v in b.items() if k not in {'request_id','baseline_digest'}});br=make_row(b)
    else:b['request_id']=a['request_id'];br=make_row(b)
    reply=js(**p.state,commands=[dict(action='intake',entry=b,row=br)])
    assert reply['errors'] and reply['writes']==0
    assert reply['ui']==p.state['ui']


@pytest.mark.parametrize('gate',['false','TRUE','',None])
def test_double_acceptance_gate_before_first_mutation(gate):
    entry,row=case();p=JsPort();p.intake(entry,row)
    p.state['ui'][0]=ready(entry,row)
    reply=js(**p.state,gate=gate,commands=[dict(action='register',request_id=entry['request_id'])])
    if gate is None: # null in a fake response is not an enabled string
        # Harness default uses ??; use malformed definite value instead.
        reply=js(**p.state,gate='unknown',commands=[dict(action='register',request_id=entry['request_id'])])
    assert reply['errors'] and reply['writes']==0 and len(reply['requests'])==1
    assert 'RECEIPT_REVIEW_STOPPED' not in reply['properties']


def test_local_off_busy_conflict_and_capture_change():
    entry,row=case();p=JsPort();p.intake(entry,row);p.state['ui'][0]=ready(entry,row)
    for options in [dict(config={'enabled':False}),dict(busy=True),dict(fault='capture'),dict(fault='gate'),dict(gate_status=503)]:
        reply=js(**p.state,**options,commands=[dict(action='register',request_id=entry['request_id'])])
        assert reply['errors'] and reply['writes']==0


@pytest.mark.parametrize('fault',['before','after','readback'])
def test_lost_registration_response_never_resends(fault):
    entry,row=case();p=JsPort();p.intake(entry,row);p.state['ui'][0]=ready(entry,row)
    reply=js(**p.state,fault=fault,commands=[dict(action='register',request_id=entry['request_id'])]*2)
    assert len(reply['errors'])==2 and reply['writes']==1
    assert reply['properties']['RECEIPT_REVIEW_STOPPED']=='outcome_unconfirmed'
    assert len(reply['requests'])== (1 if fault=='before' else 2)


@pytest.mark.parametrize('source_fault',['id','hash','version','mime','owner','parent','after','network'])
def test_original_mismatch_reason_or_unknown(source_fault,tmp_path):
    entry,p=registered();p.source_fault=source_fault
    result=execute_review(entry['request_id'],port=p,fence=AttemptFence(tmp_path/'attempts.db'))
    assert result['state']==(UNKNOWN if source_fault=='network' else '保留')
    if source_fault!='network':assert result['issues']==['source_identity_mismatch']


@pytest.mark.parametrize('phase',['claim','finish'])
@pytest.mark.parametrize('fault',['before','after','readback'])
def test_worker_response_loss_and_persistent_no_retry(phase,fault,tmp_path):
    entry,p=registered();rid=entry['request_id'];path=tmp_path/'attempts.db'
    original=getattr(p,phase)
    def broken(*args):
        p.fault=fault
        return original(*args)
    setattr(p,phase,broken)
    assert execute_review(rid,port=p,fence=AttemptFence(path))['state']==UNKNOWN
    count=p.calls.count('claim')
    p.fault=None
    assert execute_review(rid,port=p,fence=AttemptFence(path))['state']==UNKNOWN
    assert p.calls.count('claim')==count
    assert p.state['properties'].get('RECEIPT_REVIEW_STOPPED')


def test_two_workers_and_replay_claim_atomic_under_same_lock(tmp_path):
    entry,p=registered();rid=entry['request_id'];before=p.read(rid)['row']
    claimed=p.claim(rid,before)
    with pytest.raises(RuntimeError):p.claim(rid,before)
    assert claimed[7]=='検証中'
    assert execute_review(rid,port=p,fence=AttemptFence(tmp_path/'other.db'))['state']==UNKNOWN
    assert p.state['requests'][1][7]=='検証中'


def test_capacity_and_fabricated_adjustment_are_held():
    entry,row=case(amounts=(100,)*9,total=900)
    assert row[6]=='保留：商品数上限8件' and len(row)==WIDTH
    with pytest.raises(ValueError,match='capacity'):snapshot_from_row(ready(entry,row),entry)
    entry,row=case();row=ready(entry,row);row[9]='差額調整'
    with pytest.raises(ValueError,match='fabricated'):validate_snapshot(snapshot_from_row(row,entry),entry)


def test_input_version_operations_and_snapshot_tampering_fail():
    entry,row=case();snapshot=snapshot_from_row(ready(entry,row),entry)
    for field,value in [('input_version','a'*64),('baseline_digest','a'*64),('operations',[{'action':'remove'}])]:
        bad=deepcopy(snapshot);bad[field]=value
        with pytest.raises((ValueError,KeyError,TypeError)):validate_snapshot(bad,entry)


@pytest.mark.parametrize('known,text,mime',[
    ('medical','診療 領収書 患者負担額 3250円','image/png'),
    (None,'診療 領収書 患者負担額 3250円','image/png'),
    ('sensitive_unknown','レシート 商品 小計1000円 現金1000円','image/png'),
    (None,'種類が不明な文書','image/png'),(None,'レシート 商品 小計1000円 現金1000円','application/pdf'),
    (None,'レシート 商品 小計1000円 診療 患者負担額3250円','application/pdf')])
def test_medical_unknown_mixed_pdf_external_ai_zero(known,text,mime,monkeypatch,tmp_path):
    from app import receipt_privacy_gate as gate
    from app.receipt_text_extraction import _ReceiptTextExtraction
    monkeypatch.setattr(gate,'_extract_receipt_text',lambda *_:_ReceiptTextExtraction('extracted','image_ocr',text))
    calls=[]
    pipeline=SimpleNamespace(_analyze=lambda *a,**k:calls.append('AI'),db=SimpleNamespace(categories=lambda:[['食費','食品']]))
    port=SimpleNamespace(lookup_source=lambda *_:None,intake=lambda *_:calls.append('intake'))
    review=ReviewPipeline(pipeline,port,metadata=lambda *_:calls.append('metadata'),fence=AttemptFence(tmp_path/'attempts.db'))
    result=review.process_bytes(b'fabricated-private-image',mime,'synthetic-medical-b',known_source_classification=known)
    assert result['status'] in {'privacy_blocked','held'}
    assert calls==[]


def test_normal_drive_intake_uses_pipeline_without_materializer(monkeypatch,tmp_path):
    from app import receipt_privacy_gate as gate
    from app.receipt_text_extraction import _ReceiptTextExtraction
    monkeypatch.setattr(gate,'_extract_receipt_text',lambda *_:_ReceiptTextExtraction('extracted','image_ocr','レシート 商品 小計1000円 現金1000円'))
    entry,_=case();sid=entry['source']['source_id']
    result=ReceiptResult(merchant='合成商店',date='2026-11-22',total=1000,
        items=[ReceiptItem(name='合成商品',amount=1000,major_category='食費',minor_category='食品')])
    calls=[]
    class AI:
        def analyze_receipt(self,*args,**kwargs):calls.append('AI');return result
    pipeline=ReceiptPipeline(SimpleNamespace(categories=lambda:[['食費','食品']]),AI(),clock=lambda:'2026-11-22T10:00:00+09:00')
    monkeypatch.setattr(pipeline,'_materialize_result',lambda *_:pytest.fail('financial materializer'))
    p=JsPort()
    metadata=lambda _:dict(id=sid,version='12',mimeType='image/png',trashed=False)
    review=ReviewPipeline(pipeline,p,metadata=metadata,fence=AttemptFence(tmp_path/'attempts.db'))
    service=SimpleNamespace(files=lambda:SimpleNamespace(list=lambda **_:SimpleNamespace(execute=lambda:{'files':[dict(id=sid,version='12',mimeType='image/png')]})))
    response=process_review_inbox('synthetic-folder-b',review,service=service,download=lambda _:b'fabricated-normal-image')
    assert response==[dict(status='review_created',financial_write_count=0)]
    assert calls==['AI'] and len(p.state['cases'])==2 and len(p.state['ui'])==1
    assert process_review_inbox('synthetic-folder-b',review,service=service,download=lambda _:b'fabricated-normal-image')[0]['status']=='skipped'
    assert calls==['AI']


def test_formula_and_setup_contract_preserves_one_tab_and_drafts():
    reply=js(commands=[dict(action='formulas')])
    assert not reply['errors']
    item,total,result=reply['outputs'][0]
    assert item.count('"採用"')==8 and 'ROW()' in item and 'INDEX(D:D,ROW())' in total
    for part in ['受付済み','検証中','検証済み・未記帳','修正が必要','保留','登録時の内容','商品合計と総額が不一致','原本ID・版・hashを確認']:
        assert part in result
    setup=(ROOT/'apps-script/receipt-validation/ReviewSetup.gs').read_text(encoding='utf-8')
    assert 'setFrozenRows(1)' in setup and 'setFrozenColumns(1)' in setup and 'requireCheckbox' in setup
    assert 'getLastRow()>1' in setup and 'existing_catalog_preserved' in setup


def test_script_port_single_attempt_rpc_and_source_version(monkeypatch):
    entry,_=case();s=entry['source'];calls=[]
    class Response:
        status_code=200
        content=s['source_id'].encode()
        def __init__(self,body):self.body=body
        def json(self):return deepcopy(self.body)
    class Session:
        def post(self,url,**kw):
            calls.append(('POST',url,kw));return Response(dict(done=True,response={'@type':'type.googleapis.com/google.apps.script.v1.ExecutionResponse','result':['ok']}))
        def get(self,url,**kw):
            calls.append(('GET',url,kw))
            return Response(dict(id=s['source_id'],version='12',mimeType='image/png',trashed=False,
                parents=['synthetic-folder-b'],owners=[dict(emailAddress='owner@synthetic.invalid')]))
    p=ScriptPort(Session(),script_id='synthetic-script-b',folder_id='synthetic-folder-b',owner_email='owner@synthetic.invalid')
    assert p.pending()==['ok'];p.source_check(s)
    assert len(calls)==4
    assert all(c[2]['allow_redirects'] is False for c in calls)
    assert calls[0][2]['json']['function']=='receiptReviewBackend'
    assert all('/drive/v3/files/' in url for method,url,_ in calls if method=='GET')


def test_production_workflows_and_writer_files_unchanged_from_p1a():
    paths=['.github/workflows','app/sheets.py','app/receipt_pipeline.py','app/receipt_sheets_worker.py',
           'app/receipt_sheets_authority.py','app/receipt_sheets_context.py','app/receipt_sheet_cells.py',
           'apps-script/receipt-validation/Code.gs',
           # New secret-free CI only; all existing production entries stay byte-identical.
           ':(exclude).github/workflows/receipt-prb-safety.yml']
    completed=subprocess.run(['git','diff','d2440c4b43c5a54411e0abd91cfa7ded0ad674c5','--',*paths],
        cwd=ROOT,text=True,capture_output=True,check=True)
    assert completed.stdout==''


def test_actual_sheet_port_raw_batches_validations_and_formula_safety(tmp_path):
    entry,row=case()
    row[7]='=IMPORTXML("https://invalid.invalid","x")'
    reply=js(http_ports=True,commands=[dict(action='intake',entry=entry,row=row)])
    assert not reply['errors'] and reply['writes']==1
    assert reply['validationCount']==42 and len(reply['formulas'])==3
    assert reply['checkboxes']==[['register:'+entry['request_id'],'ready:'+entry['request_id']]]
    assert reply['clearedValidations']==[[2,18+9*i] for i in range(8)]+[[2,98]]
    assert reply['hiddenColumns']==[[82,15]]+[[18+9*i,1] for i in range(8)]+[[98,1]]
    assert reply['ui']==[row]  # Decoration never approves or changes draft inputs.
    assert reply['notes'][0][:2]==[2,9] and '一括承認' in reply['notes'][0][2]
    note=reply['batches'][0]['requests'][1]['updateCells']['rows'][0]['values'][7]
    assert note=={'userEnteredValue':{'stringValue':row[7]}}
    port=JsPort();port.http_ports=True
    port.state={k:reply[k] for k in ('cases','requests','ui','properties')}
    port.state['ui'][0]=ready(entry,row)
    port.invoke(dict(action='register',request_id=entry['request_id']))
    result=execute_review(entry['request_id'],port=port,fence=AttemptFence(tmp_path/'attempts.db'))
    assert result['state']=='検証済み・未記帳'


def test_safe_start_stop_preserves_latches_requests_drafts_and_gate_off():
    entry,p=registered();before=deepcopy(p.state)
    reply=js(**p.state,commands=[dict(action='stop'),dict(action='register',request_id=entry['request_id'])])
    assert json.loads(reply['properties']['RECEIPT_REVIEW_CONFIG_JSON'])['enabled'] is False
    assert 'RECEIPT_GITHUB_TOKEN' not in reply['properties']
    assert reply['requests']==before['requests'] and reply['ui']==before['ui']
    for options in [dict(gate='false'),dict(no_trigger=True),dict(properties={'RECEIPT_REVIEW_STOPPED':'outcome_unconfirmed'})]:
        stopped=js(config={'enabled':False},**options,commands=[dict(action='start')])
        assert stopped['errors'] and stopped['writes']==0
        assert json.loads(stopped['properties']['RECEIPT_REVIEW_CONFIG_JSON'])['enabled'] is False
    enabled=js(config={'enabled':False},commands=[dict(action='start'),dict(action='stop')])
    assert not enabled['errors'] and enabled['gateGets']==1 and enabled['writes']==0
    assert not json.loads(enabled['properties']['RECEIPT_REVIEW_CONFIG_JSON'])['enabled']


def test_draft_regeneration_is_rejected_and_exclusion_is_batch_approved():
    entry,row=case();p=JsPort();p.intake(entry,row)
    p.state['ui'][0][7]='編集中の値を保持'
    with pytest.raises(RuntimeError):p.intake(entry,row)
    assert p.state['ui'][0][7]=='編集中の値を保持'
    edited=ready(entry,row);edited[16]='除外';edited[17]=False;recalc(edited)
    snapshot=snapshot_from_row(edited,entry)
    assert snapshot['inputs']['items'][0]['confirmed'] is True
    assert snapshot['operations'][0]['action']=='remove'
    assert snapshot['operations'][0]['evidence']==APPROVAL_EVIDENCE
    assert js(commands=[dict(action='snapshot',row=edited,entry=entry)])['outputs'][0]==snapshot


def test_decimal_quantity_and_unknown_response_are_not_rounded_or_replayed(tmp_path):
    entry,row=case();row=ready(entry,row);row[10]=1.125;row[11]=650;row[20]=350;recalc(row)
    snap=snapshot_from_row(row,entry)
    assert js(commands=[dict(action='snapshot',row=row,entry=entry)])['outputs'][0]==snap
    row[10]=1.1251
    with pytest.raises(ValueError,match='quantity'):snapshot_from_row(row,entry)
    fence=AttemptFence(tmp_path/'intake.db');fence.consume_source('synthetic-source-b','a'*64)
    with pytest.raises(Exception):AttemptFence(tmp_path/'intake.db').consume_source('synthetic-source-b','b'*64)
    with pytest.raises(Exception):AttemptFence(tmp_path/'intake.db').consume_source('another-source-b','a'*64)


def test_worker_detects_catalog_changes_and_snapshot_category_tampering(tmp_path):
    entry,p=registered();original=p.claim
    def changed(*args):
        result=original(*args)
        data=json.loads(p.state['cases'][1][4]);data['source']['version']='13'
        # A broken immutable catalog cannot be used to validate any request.
        p.state['cases'][1][4]=canonical(data)
        return result
    p.claim=changed
    result=execute_review(entry['request_id'],port=p,fence=AttemptFence(tmp_path/'attempts.db'))
    assert result['state']==UNKNOWN and 'finish' not in p.calls


@pytest.mark.parametrize('fault',['id','version','mimeType','parents','owners','trashed','hash','after','network'])
def test_actual_source_port_rejects_changed_identity_and_unknown_http(fault):
    entry,_=case();s=entry['source'];calls=[]
    meta=dict(id=s['source_id'],version='12',mimeType='image/png',trashed=False,
              parents=['synthetic-folder-b'],owners=[dict(emailAddress='owner@synthetic.invalid')])
    class Response:
        status_code=500 if fault=='network' else 200
        content=b'changed' if fault=='hash' else s['source_id'].encode()
        def json(self):
            out=deepcopy(meta)
            if fault in {'id','version','mimeType'}:out[fault]='changed'
            if fault in {'parents','owners'}:out[fault]=[]
            if fault=='trashed':out['trashed']=True
            if fault=='after' and len(calls)==3:out['version']='13'
            return out
    class Session:
        def get(self,*args,**kwargs):calls.append((args,kwargs));return Response()
    port=ScriptPort(Session(),script_id='synthetic-script-b',folder_id='synthetic-folder-b',owner_email='owner@synthetic.invalid')
    with pytest.raises(RuntimeError if fault in {'network','after'} else ValueError):port.source_check(s)
    assert len(calls)<=3


@pytest.mark.parametrize('current_version,drift',[('12',False),('5',True)])
def test_source_port_accepts_stable_bytes_and_audits_version_drift(current_version,drift):
    entry,_=case();source=entry['source'];calls=[]
    class Session:
        def get(self,url,**kwargs):
            calls.append(url)
            if kwargs.get('params')=={'alt':'media'}:
                return SimpleNamespace(status_code=200,content=source['source_id'].encode())
            return SimpleNamespace(status_code=200,json=lambda:dict(id=source['source_id'],version=current_version,
                mimeType=source['mime_type'],trashed=False,parents=['synthetic-folder-b'],
                owners=[dict(emailAddress='owner@synthetic.invalid')]))
    port=ScriptPort(Session(),script_id='synthetic-script-b',folder_id='synthetic-folder-b',owner_email='owner@synthetic.invalid')
    audit=port.source_check(source)
    assert audit==dict(source_saved_version='12',source_current_version=current_version,
        source_metadata_drift=drift)
    assert port.source_check(source)==audit and len(calls)==6


@pytest.mark.parametrize('change',['hash','during_call','between_checks'])
def test_source_port_rejects_changed_bytes_or_live_version(change):
    entry,_=case();source=entry['source'];metadata_calls=0
    class Session:
        def get(self,url,**kwargs):
            nonlocal metadata_calls
            if kwargs.get('params')=={'alt':'media'}:
                content=b'changed' if change=='hash' else source['source_id'].encode()
                return SimpleNamespace(status_code=200,content=content)
            metadata_calls+=1
            version='6' if change=='during_call' and metadata_calls==2 or change=='between_checks' and metadata_calls>=3 else '5'
            return SimpleNamespace(status_code=200,json=lambda:dict(id=source['source_id'],version=version,
                mimeType=source['mime_type'],trashed=False,parents=['synthetic-folder-b'],
                owners=[dict(emailAddress='owner@synthetic.invalid')]))
    port=ScriptPort(Session(),script_id='synthetic-script-b',folder_id='synthetic-folder-b',owner_email='owner@synthetic.invalid')
    if change=='between_checks':port.source_check(source)
    with pytest.raises(SourceMismatch if change=='hash' else RuntimeError):port.source_check(source)


def test_version_audit_is_kept_in_result_without_changing_snapshot(tmp_path):
    entry,port=registered();original=deepcopy(port.state['requests'][1][:7])
    audit=dict(source_saved_version='3',source_current_version='5',source_metadata_drift=True)
    port.source_check=lambda source:audit
    result=execute_review(entry['request_id'],port=port,fence=AttemptFence(tmp_path/'attempts.db'))
    assert result['state']=='検証済み・未記帳',result
    assert {k:result[k] for k in audit}==audit
    assert port.state['requests'][1][:7]==original
    assert {k:json.loads(port.state['requests'][1][8])[k] for k in audit}==audit
    assert result['financial_write_count']==0 and result['posting_authority'] is False


@pytest.mark.parametrize('mode',['disabled','financial','missing_token'])
def test_runner_preflight_blocks_network_and_attempt_writes(mode,monkeypatch,tmp_path):
    from scripts.run_receipt_review import main
    import sys
    config=dict(schema='receipt-review-runner-b1',enabled=mode!='disabled',financial_write_enabled=mode=='financial',
        script_id='synthetic-script-b',folder_id='synthetic-folder-b',owner_email='owner@synthetic.invalid',categories=[['食費','食品']])
    path=tmp_path/'config.json';path.write_text(json.dumps(config),encoding='utf-8')
    attempt=tmp_path/'attempts.db'
    monkeypatch.setattr(sys,'argv',['runner','--config',str(path),'--attempt-db',str(attempt),'pending'])
    monkeypatch.delenv('RECEIPT_REVIEW_OWNER_ACCESS_TOKEN',raising=False)
    with pytest.raises(ValueError):main()
    assert not attempt.exists()


def test_watch_stops_on_unknown_without_poll_or_mutation_retry(monkeypatch,tmp_path,capsys):
    from scripts.run_receipt_review import main
    from app import receipt_review_worker as worker
    import requests
    import sys
    calls=[];entry,_=case()
    class Session:
        headers={}
        def __enter__(self):return self
        def __exit__(self,*_):pass
    class Port:
        def __init__(self,*args,**kw):pass
        def pending(self):calls.append('pending');return [entry['request_id']]
    def execute(*args,**kw):calls.append('execute');return dict(state=UNKNOWN)
    monkeypatch.setattr(requests,'Session',Session)
    monkeypatch.setattr(worker,'ScriptPort',Port)
    monkeypatch.setattr(worker,'execute_review',execute)
    monkeypatch.setenv('RECEIPT_REVIEW_OWNER_ACCESS_TOKEN','DUMMY')
    config=dict(schema='receipt-review-runner-b1',enabled=True,financial_write_enabled=False,
        script_id='synthetic-script-b',folder_id='synthetic-folder-b',owner_email='owner@synthetic.invalid',categories=[['食費','食品']])
    path=tmp_path/'config.json';path.write_text(json.dumps(config),encoding='utf-8')
    monkeypatch.setattr(sys,'argv',['runner','--config',str(path),'--attempt-db',str(tmp_path/'attempts.db'),'watch','--max-polls','2'])
    assert main()==3 and calls==['pending','execute']
    assert UNKNOWN in capsys.readouterr().out


@pytest.mark.parametrize('count',[1,2,8])
def test_one_registration_zero_individual_checks_all_used_slots_frozen(count,tmp_path):
    entry,row=case('batch-'+str(count),amounts=(100,)*count,total=100*count)
    before=deepcopy(row)
    assert len(row)==98 and row[-1] is False
    assert all(row[17+9*i] is False for i in range(8))
    with pytest.raises(ValueError,match='row_binding_invalid'):
        snapshot_from_row(row,entry)  # Never approve before a register event.
    port=JsPort();port.http_ports=True;port.intake(entry,row)
    assert port.state['ui'][0]==before
    # Known tax is an ordinary correction. No hidden confirmation cell is edited.
    port.state['ui'][0][-2]='税込'
    port.state['ui'][0][8]='register:'+entry['request_id']
    draft=deepcopy(port.state['ui'][0])
    result=port.invoke(dict(action='register',request_id=entry['request_id']))
    assert result['state']=='受付済み' and port.calls.count('register')==1
    assert port.state['ui'][0]==draft
    snapshot=json.loads(port.state['requests'][1][5])
    assert snapshot==snapshot_from_row(draft,entry)
    inputs=snapshot['inputs']
    assert inputs['approval_method']==APPROVAL_METHOD and inputs['total_confirmed'] is True
    assert [item['confirmed'] for item in inputs['items']]==[True]*count+[False]*(8-count)
    assert inputs['receipt_total']==100*count and inputs['tax_mode']=='税込'
    assert snapshot['input_version']==digest(inputs)
    frozen=deepcopy(port.state['requests'][1][:7])
    port.state['ui'][0][11]=999
    port.state['ui'][0][-2]='不明'
    assert execute_review(entry['request_id'],port=port,fence=AttemptFence(tmp_path/'batch.db'))['state']=='検証済み・未記帳'
    assert port.state['requests'][1][:7]==frozen
    with pytest.raises(RuntimeError,match='replay_or_duplicate'):
        port.invoke(dict(action='register',request_id=entry['request_id']))
    assert port.state['requests'][1][:7]==frozen


def test_hidden_legacy_flags_do_not_claim_visual_checks_or_approve_empty_slots():
    entry,row=case();row=ready(entry,row)
    row[9]='一括承認で修正した商品'
    row[17]=False;row[26]='';row[-1]=''
    row[35]=True  # A stale hidden flag cannot manufacture an approved item.
    snapshot=snapshot_from_row(row,entry)
    assert js(commands=[dict(action='snapshot',row=row,entry=entry)])['outputs'][0]==snapshot
    assert snapshot['inputs']['items'][2]['confirmed'] is False
    assert snapshot['operations'][0]['evidence']==APPROVAL_EVIDENCE
    assert '原本照合済み' not in canonical(snapshot)
    assert validate_snapshot(snapshot,entry)['state']=='検証済み・未記帳'


@pytest.mark.parametrize('change',['method','null_method','total','used','empty','count','evidence'])
def test_batch_approval_tampering_rejected_even_with_rehashed_inputs(change):
    entry,row=case();row=ready(entry,row);row[9]='修正した商品'
    snapshot=snapshot_from_row(row,entry)
    inputs=snapshot['inputs']
    if change=='method':inputs['approval_method']='visual_original_checked'
    if change=='null_method':inputs['approval_method']=None
    if change=='total':inputs['total_confirmed']=False
    if change=='used':inputs['items'][0]['confirmed']=False
    if change=='empty':inputs['items'][2]['confirmed']=True
    if change=='count':inputs['items'].pop()
    if change=='evidence':snapshot['operations'][0]['evidence']='原本照合済み'
    snapshot['input_version']=digest(inputs)
    with pytest.raises(ValueError):validate_snapshot(snapshot,entry)


def test_pre_change_legacy_snapshot_and_result_remain_unchanged(tmp_path):
    # Synthetic golden captured using PR #204's unmodified code, no real records.
    fixture=json.loads((ROOT/'tests/fixtures/receipt_pr_b_legacy.json').read_text(encoding='utf-8'))
    before=deepcopy(fixture);entry=fixture['entry'];snapshot=fixture['snapshot']
    assert 'approval_method' not in snapshot['inputs']
    revised=validate_snapshot(snapshot,entry)
    assert {k:v for k,v in revised.items() if k not in {'warnings','validator_revision'}}==fixture['result']
    assert revised['warnings']==[]
    port=JsPort();port.intake(entry,make_row(entry))
    stored=[entry['request_id'],entry['case_id'],entry['source']['source_id'],entry['source']['version'],
        entry['source']['sha256'],canonical(snapshot),digest(snapshot),'受付済み','','2026-11-22T00:00:00.000Z']
    port.state['requests'].append(stored)
    assert port.read(entry['request_id'])['row']==stored
    assert execute_review(entry['request_id'],port=port,fence=AttemptFence(tmp_path/'legacy.db'))['state']==fixture['result']['state']
    assert port.state['requests'][1][:7]==stored[:7] and fixture==before
    # New parsing must not reinterpret legacy operation evidence as batch approval.
    assert snapshot_from_row(ready(entry,make_row(entry)),entry)['inputs']['approval_method']==APPROVAL_METHOD
