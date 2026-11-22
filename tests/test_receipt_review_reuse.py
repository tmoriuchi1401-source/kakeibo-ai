"""AI-free adapter -> real Apps Script VM -> snapshot -> real backend validator."""
from copy import deepcopy
from hashlib import sha256
import json
import socket
import sqlite3
from types import SimpleNamespace

import pytest

from app.receipt_review_diagnostics import ReviewDiagnostics
from app.receipt_review_reuse import ReuseFence, ReuseUnknown, build_reused_case, intake_reused
from app.receipt_review_cases import snapshot_from_row, validate_snapshot
from app.receipt_review_worker import execute_review
from app.receipt_sheets_authority import UNKNOWN
from test_receipt_pr_b import JsPort, ready, js

RAW=b'fabricated normal original PNG'
CANARY='PRIVATE_CONTENT_TOKEN_CANARY'


@pytest.fixture(autouse=True)
def forbid_ai_finance_network(monkeypatch):
    from app.gemini_ai import GeminiAI
    from google import genai
    from app.sheets import SheetsDB
    from app import receipt_privacy_gate
    calls=[]
    def deny(*a,**k):
        calls.append(True)
        raise AssertionError('AI/finance/network forbidden')
    for cls in [GeminiAI,genai.Client,SheetsDB]:monkeypatch.setattr(cls,'__init__',deny)
    monkeypatch.setattr(socket.socket,'connect',deny)
    monkeypatch.setattr(socket,'create_connection',deny)
    monkeypatch.setattr(receipt_privacy_gate,'evaluate_receipt_privacy',lambda *a,**k:
        SimpleNamespace(classification='normal',gemini_allowed=True,buyback_evidence=False))
    yield
    assert calls==[]


def manifest():
    sid='synthetic-original-reuse';copy='synthetic-copy-reuse';hashed=sha256(RAW).hexdigest()
    original=dict(source_id=sid,version='6',sha256=hashed,mime_type='image/png',classification='normal',transaction_kind='purchase')
    replica={**original,'source_id':copy,'version':'1'}
    old=['receipt:'+sid,'2026-11-22 12:00:00','receipt',sid,'2026-11-22','合成商店',100,'現金','解析済','','a'*64,'']
    receipt=['R-'+sid,old[4],old[5],100,'現金','https://drive.google.com/file/d/'+sid+'/view','解析済',old[1],'']
    expense=['synthetic-expense',old[4],old[5],'合成商品',100,'食費','食品','現金','receipt',receipt[0],old[0],'','計上済']
    return dict(schema='receipt-review-reuse-v1',original=original,copy=replica,
        records=dict(import_row=old,receipt_row=receipt,expense_rows=[expense]),
        owner_confirmation=dict(image_matches_records=True,distinct_from_unknown=True,categories_confirmed=True,
            item_count=1,quantity=2,tax_treatment='税込'),categories=[['食費','食品']])


def perform(tmp_path, *, data=None, port=None, original_check=lambda s:True):
    data=data or manifest();port=port or JsPort()
    fence=ReuseFence(tmp_path/'attempts.sqlite');diag=ReviewDiagnostics(tmp_path/'private.jsonl')
    result=intake_reused(data,original_bytes=RAW,copy_bytes=RAW,original_check=original_check,
        port=port,fence=fence,diagnostics=diag,clock=lambda:'2026-11-22 12:00:00')
    return result,port,fence,diag


def test_ai_zero_one_registration_backend_and_fixed_snapshot(tmp_path):
    data=manifest();before=deepcopy(data)
    result,port,fence,diag=perform(tmp_path,data=data)
    entry=json.loads(port.state['cases'][1][4]);row=port.state['ui'][0]
    assert result['ai_http_attempt_count']==0 and result['financial_write_count']==0
    assert entry['source']==data['copy'] and entry['case_id']!='receipt:'+data['original']['source_id']
    origin=json.loads(entry['import_row'][11])
    assert origin['source_import_id']==data['records']['import_row'][0]
    assert origin['original_analysis_hash']=='a'*64 and entry['import_row'][10]!='a'*64
    assert origin['evidence_kind']=='owner_confirmed_ledger_projection_not_original_ai_response'
    assert row[10]==2 and row[-2]=='税込' and row[-1] is False and row[17] is False
    row=ready(entry,row);row[9]='iPhoneで修正した合成商品'
    snapshot=snapshot_from_row(row,entry)
    assert validate_snapshot(snapshot,entry)['state']=='検証済み・未記帳'
    port.state['ui'][0]=row
    port.invoke(dict(action='register',request_id=entry['request_id']))
    assert port.calls.count('register')==1
    frozen=deepcopy(port.state['requests'][1][:7])
    port.state['ui'][0][9]='登録後の下書き'
    validated=execute_review(entry['request_id'],port=port,fence=fence)
    assert validated['state']=='検証済み・未記帳' and validated['financial_write_count']==0
    assert port.state['requests'][1][7]=='検証済み・未記帳'
    assert port.state['requests'][1][:7]==frozen
    assert port.calls.count('claim')==port.calls.count('finish')==1
    assert data==before
    assert all(json.loads(x)['ai_http_attempt_count']==0 for x in diag.path.read_text().splitlines())
    assert port.state['ui'][0][9]=='登録後の下書き'
    formulas=js(commands=[dict(action='formulas')])['outputs'][0]
    assert formulas and '検証済み・未記帳' in json.dumps(formulas,ensure_ascii=False)


@pytest.mark.parametrize('field,value',[('quantity',None),('quantity',True),('quantity',0),
    ('quantity',1.2345),('item_count',2),('item_count',None),('tax_treatment','不明'),
    ('tax_treatment','税別'),('image_matches_records',False),('distinct_from_unknown',False)])
def test_missing_or_unconfirmed_data_is_not_filled(field,value):
    data=manifest();data['owner_confirmation'][field]=value
    with pytest.raises(ValueError):build_reused_case(data,clock=lambda:'synthetic')


@pytest.mark.parametrize('change',['multiple','old_hash','new_id','amount','category','record_id','original_id','mime'])
def test_bad_bindings_and_multiple_rows_stop(change):
    data=manifest()
    if change=='multiple':data['records']['expense_rows']*=2
    if change=='old_hash':data['records']['import_row'][10]='bad'
    if change=='new_id':data['copy']['source_id']=data['original']['source_id']
    if change=='amount':data['records']['expense_rows'][0][4]=99
    if change=='category':data['records']['expense_rows'][0][5]='不正'
    if change=='record_id':data['records']['receipt_row'][0]='R-other'
    if change=='original_id':data['records']['import_row'][3]='other'
    if change=='mime':data['original']['mime_type']='application/pdf'
    with pytest.raises(ValueError):build_reused_case(data,clock=lambda:'synthetic')


def test_independent_duplicate_and_persistent_restart_fence(tmp_path):
    result,port,fence,diag=perform(tmp_path)
    entry=json.loads(port.state['cases'][1][4])
    second=ReviewDiagnostics(tmp_path/'second.jsonl')
    with pytest.raises(sqlite3.IntegrityError):
        intake_reused(manifest(),original_bytes=RAW,copy_bytes=RAW,original_check=lambda s:True,
            port=port,fence=ReuseFence(fence.path),diagnostics=second,clock=lambda:'synthetic')
    assert port.calls.count('intake')==1
    # A new local DB still cannot defeat the independent Script catalog.
    with pytest.raises(ReuseUnknown):
        intake_reused(manifest(),original_bytes=RAW,copy_bytes=RAW,original_check=lambda s:True,
            port=port,fence=ReuseFence(tmp_path/'other.db'),diagnostics=ReviewDiagnostics(tmp_path/'other.jsonl'),clock=lambda:'synthetic')
    assert port.calls.count('intake')==1 and entry['source']['sha256']==sha256(RAW).hexdigest()


@pytest.mark.parametrize('fault',['before','after','readback'])
def test_unknown_intake_is_never_resent_and_keeps_three_fences(tmp_path,fault,capsys):
    port=JsPort();port.fault=fault
    if fault=='readback':
        lookup=port.lookup_source
        def lost_lookup(*args):
            if 'intake' in port.calls:raise TimeoutError(CANARY)
            return lookup(*args)
        port.lookup_source=lost_lookup
    with pytest.raises(ReuseUnknown):perform(tmp_path,port=port)
    assert port.calls.count('intake')==1
    with sqlite3.connect(tmp_path/'attempts.sqlite') as db:
        assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==3
    log=(tmp_path/'private.jsonl').read_text()
    assert json.loads(log.splitlines()[-1])['result']=='UNKNOWN'
    assert CANARY not in log+capsys.readouterr().out


def test_unknown_old_content_fence_cannot_be_bypassed_with_new_copy_id(tmp_path):
    data=manifest();fence=ReuseFence(tmp_path/'attempts.sqlite')
    fence.consume_source('synthetic-old-unknown',data['copy']['sha256'])
    before=fence.path.read_bytes();port=JsPort()
    with pytest.raises(sqlite3.IntegrityError):
        intake_reused(data,original_bytes=RAW,copy_bytes=RAW,original_check=lambda s:True,port=port,
            fence=fence,diagnostics=ReviewDiagnostics(tmp_path/'private.jsonl'),clock=lambda:'synthetic')
    assert port.calls==[] and fence.path.read_bytes()==before


def test_privacy_and_original_recheck_stop_before_intake(tmp_path,monkeypatch):
    port=JsPort()
    with pytest.raises(ReuseUnknown):perform(tmp_path,port=port,original_check=lambda s:False)
    assert 'intake' not in port.calls
    from app import receipt_privacy_gate
    monkeypatch.setattr(receipt_privacy_gate,'evaluate_receipt_privacy',lambda *a,**k:
        SimpleNamespace(classification='medical',gemini_allowed=False,buyback_evidence=False))
    data=manifest();folder=tmp_path/'blocked';folder.mkdir()
    with pytest.raises(ValueError):perform(folder,data=data,port=JsPort())


def test_backend_unknown_does_not_reclaim(tmp_path):
    _,port,fence,_=perform(tmp_path)
    entry=json.loads(port.state['cases'][1][4]);port.state['ui'][0]=ready(entry,port.state['ui'][0])
    port.invoke(dict(action='register',request_id=entry['request_id']))
    port.source_fault='network'
    assert execute_review(entry['request_id'],port=port,fence=fence)['state']==UNKNOWN
    assert execute_review(entry['request_id'],port=port,fence=fence)['state']==UNKNOWN
    assert port.calls.count('claim')==1 and port.calls.count('finish')==0


def test_diagnostic_failure_before_intake_keeps_fence(tmp_path,monkeypatch):
    from app.receipt_review_diagnostics import DiagnosticFailure
    fence=ReuseFence(tmp_path/'attempts.sqlite');diag=ReviewDiagnostics(tmp_path/'private.jsonl');port=JsPort()
    event=diag.event
    def broken(stage,*args,**kwargs):
        if stage=='intake':raise DiagnosticFailure()
        return event(stage,*args,**kwargs)
    monkeypatch.setattr(diag,'event',broken)
    with pytest.raises(ReuseUnknown):
        intake_reused(manifest(),original_bytes=RAW,copy_bytes=RAW,original_check=lambda s:True,
            port=port,fence=fence,diagnostics=diag,clock=lambda:'synthetic')
    assert 'intake' not in port.calls
    with sqlite3.connect(fence.path) as db:assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==3


def test_wrong_bytes_stop_before_any_port_call(tmp_path):
    port=JsPort();fence=ReuseFence(tmp_path/'attempts.sqlite')
    with pytest.raises(ValueError):
        intake_reused(manifest(),original_bytes=RAW,copy_bytes=b'changed',original_check=lambda s:True,
            port=port,fence=fence,diagnostics=ReviewDiagnostics(tmp_path/'private.jsonl'),clock=lambda:'synthetic')
    assert port.calls==[]


def test_bad_calendar_date_not_admitted():
    data=manifest();data['records']['import_row'][4]='2026-02-30'
    with pytest.raises(ValueError):build_reused_case(data,clock=lambda:'synthetic')


@pytest.mark.parametrize('enabled',[False,True])
def test_cli_disabled_or_expired_does_not_create_attempt_db(tmp_path,monkeypatch,enabled):
    import sys
    from scripts.run_receipt_review_reuse import main
    config=dict(schema='receipt-review-reuse-runner-v1',enabled=enabled,financial_write_enabled=False,
        script_id='synthetic-deployment',folder_id='synthetic-folder',owner_email='owner@synthetic.invalid',
        original_parent_id='synthetic-original-parent',oauth_expiry='2000-01-01T00:00:00+00:00')
    p=tmp_path/'config.json';p.write_text(json.dumps(config))
    args=['runner','--config',str(p),'--manifest',str(tmp_path/'not-read'),
        '--attempt-db',str(tmp_path/'attempts.sqlite'),'--diagnostic',str(tmp_path/'diag'),
        '--original-file',str(tmp_path/'not-read-1'),'--copy-file',str(tmp_path/'not-read-2'),
        '--result',str(tmp_path/'result'),'--enable-intake']
    monkeypatch.setattr(sys,'argv',args)
    with pytest.raises(ValueError):main()
    assert not (tmp_path/'attempts.sqlite').exists() and not (tmp_path/'diag').exists()
