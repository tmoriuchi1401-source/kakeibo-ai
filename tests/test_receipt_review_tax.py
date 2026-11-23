"""Tax-only append history over existing Script VM/validator; no live services."""
from copy import deepcopy
from hashlib import sha256
import json
import sqlite3
from types import SimpleNamespace
import pytest

from test_receipt_pr_b import case, ready, js, JsPort, execute_review, AttemptFence, deny_finance_and_network
from app.page_receipt_model import digest
from app.receipt_review_worker import checked_record
from app.receipt_review_cases import make_row, snapshot_from_row, validate_snapshot
from app.receipt_review_tax import (HEADERS, correction_plan, amended_snapshot,
    TaxAttemptFence, RestTaxReader, execute_tax_correction)
from app.receipt_sheets_authority import UNKNOWN


class TaxPort(JsPort):
    def invoke(self,command):
        self.calls.append(command['action'])
        reply=js(**self.state,commands=[command],fault=self.fault,http_ports=True,tax_recalc=getattr(self,'recalc',False))
        self.state={k:reply[k] for k in ('cases','requests','ui','properties','tax')}
        if reply['errors']:raise RuntimeError(reply['errors'][0])
        return reply['outputs'][0]
    def rpc(self,action,**kwargs):return self.invoke(dict(action=action,**kwargs))


class Reader:
    def __init__(self,p):self.p=p
    def original(self,rid):
        return deepcopy(dict(row=next(r for r in self.p.state['requests'][1:] if r[0]==rid),
            entry=json.loads(next(r[4] for r in self.p.state['cases'][1:] if r[6]==rid))))
    def events(self,rid):
        class Session:
            def get(_,url,**kwargs):return SimpleNamespace(status_code=200,json=lambda:{'values':self.p.state['tax']})
        return RestTaxReader(Session(),'synthetic-review-sheet',None).events(rid)


def held(tmp_path,tax_line=False):
    e,row=case(amounts=(900,100) if tax_line else (600,400))
    if tax_line:
        e['candidate_rows'][1][4]='消費税'
        e['baseline_digest']=digest({k:v for k,v in e.items() if k not in {'baseline_digest','request_id'}})
        row=make_row(e)
    p=TaxPort();p.state['tax']=[HEADERS]
    p.intake(e,row);p.state['ui'][0]=ready(e,row,tax='不明')
    p.invoke(dict(action='register',request_id=e['request_id']))
    result=execute_review(e['request_id'],port=p,fence=AttemptFence(tmp_path/'base.db'))
    assert result['issues']==['tax_treatment_unknown']
    return e,p,Reader(p)


def perform(tmp_path,e,p,r,mode='税込',evidence='original_print_confirmed'):
    return execute_tax_correction(e['request_id'],mode,evidence,reader=r,port=p,
        fence=TaxAttemptFence(tmp_path/'tax.db'),duplicate_check=lambda *_:False)


@pytest.mark.parametrize('mode',['税込','税別'])
def test_tax_correction_append_only_and_original_inputs_unchanged(tmp_path,mode):
    e,p,r=held(tmp_path);rid=e['request_id'];before=deepcopy(p.state);original=r.original(rid)
    plan=correction_plan(original,mode,'original_print_confirmed')
    amended=amended_snapshot(original,plan);old=checked_record(original,rid)[2]
    changed=deepcopy(amended);changed['inputs']['tax_mode']=old['inputs']['tax_mode'];changed['input_version']=old['input_version']
    assert changed==old
    result=perform(tmp_path,e,p,r,mode=mode)
    assert result['state']=='検証済み・未記帳' and result['difference']==0
    assert result['financial_write_count']==0 and result['posting_authority'] is False
    assert r.original(rid)==original and p.state['cases']==before['cases'] and p.state['requests']==before['requests']
    ui=deepcopy(p.state['ui']);ui[0][96]='不明';assert ui==before['ui']
    assert [x['event'] for x in r.events(rid)]==['intent','result']
    assert result['correction_digest']==digest(plan) and result['snapshot_digest']==original['row'][6]
    assert p.calls.count('register')==1 and p.calls.count('intake')==1
    before_calls=list(p.calls)
    assert perform(tmp_path,e,p,r)['state']==UNKNOWN
    assert p.calls==before_calls and r.original(rid)==original


@pytest.mark.parametrize('field', ['snapshot_digest','amended_snapshot_digest','entry_digest','original_record_digest','correction_no','before_mode'])
def test_plan_binding_tampering_prevents_append(tmp_path,field):
    e,p,r=held(tmp_path);plan=correction_plan(r.original(e['request_id']),'税込','original_print_confirmed')
    plan[field]=2 if field=='correction_no' else 'changed'
    with pytest.raises(RuntimeError):p.rpc('tax_begin',plan=plan)
    assert len(p.state['tax'])==1


@pytest.mark.parametrize('fault',['before','after','readback'])
def test_unknown_never_repeats_correction_and_preserves_stop_fence(tmp_path,fault):
    e,p,r=held(tmp_path);original=r.original(e['request_id']);p.fault=fault
    assert perform(tmp_path,e,p,r)['state']==UNKNOWN
    assert p.calls.count('tax_begin')==1
    assert p.state['properties']['RECEIPT_REVIEW_STOPPED']=='outcome_unconfirmed'
    p.fault=None
    assert perform(tmp_path,e,p,r)['state']==UNKNOWN
    assert p.calls.count('tax_begin')==1 and r.original(e['request_id'])==original
    with sqlite3.connect(tmp_path/'tax.db') as db:
        assert db.execute('SELECT correction_id FROM tax_attempts').fetchall()==[(e['request_id']+':tax:1',)]


def test_unknown_evidence_keeps_hold_without_rpc_or_new_history(tmp_path):
    e,p,r=held(tmp_path);before=deepcopy(p.state);calls=list(p.calls)
    result=perform(tmp_path,e,p,r,mode='不明',evidence='insufficient')
    assert result['state']=='保留' and result['correction_applied'] is False
    assert p.state==before and p.calls==calls
    assert perform(tmp_path,e,p,r,mode='税込',evidence='insufficient')['state']==UNKNOWN
    assert p.state==before


def test_included_tax_double_addition_stays_held(tmp_path):
    e,p,r=held(tmp_path,tax_line=True)
    result=perform(tmp_path,e,p,r)
    assert result['state']=='保留' and 'included_tax_not_additive' in result['issues']


@pytest.mark.parametrize('fault',['before','after','readback'])
def test_lost_finish_is_unknown_even_if_result_committed_and_never_resent(tmp_path,fault):
    e,p,r=held(tmp_path);original=r.original(e['request_id']);rpc=p.rpc
    def fail_finish(action,**kwargs):
        if action=='tax_finish':p.fault=fault
        try:return rpc(action,**kwargs)
        finally:p.fault=None
    p.rpc=fail_finish
    assert perform(tmp_path,e,p,r)['state']==UNKNOWN
    assert r.original(e['request_id'])==original
    assert p.calls.count('tax_begin')==1 and p.calls.count('tax_finish')==1
    assert p.state['properties']['RECEIPT_REVIEW_STOPPED']=='outcome_unconfirmed'
    assert perform(tmp_path,e,p,r)['state']==UNKNOWN
    assert p.calls.count('tax_begin')==1 and p.calls.count('tax_finish')==1


def test_history_digest_and_uniqueness_and_fresh_worker_replay(tmp_path):
    e,p,r=held(tmp_path);assert perform(tmp_path,e,p,r)['state']=='検証済み・未記帳'
    history=deepcopy(p.state['tax']);p.state['tax'].append(deepcopy(history[1]))
    with pytest.raises(ValueError):r.events(e['request_id'])
    p.state['tax']=deepcopy(history);p.state['tax'][1][5]+=' '
    with pytest.raises(ValueError):r.events(e['request_id'])
    p.state['tax']=history
    assert execute_tax_correction(e['request_id'],'税別','original_print_confirmed',reader=r,port=p,
        fence=TaxAttemptFence(tmp_path/'fresh.db'),duplicate_check=lambda *_:False)['state']==UNKNOWN
    assert p.calls.count('tax_begin')==1


def test_result_binding_and_finish_replay_rejected(tmp_path):
    e,p,r=held(tmp_path);assert perform(tmp_path,e,p,r)['state']=='検証済み・未記帳'
    result=json.loads(p.state['tax'][-1][5]);before=deepcopy(p.state)
    with pytest.raises(RuntimeError):p.rpc('tax_finish',correction_id=result['correction_id'],result=result)
    assert p.state==before


def test_warning_and_effective_result_formula_preserve_98_columns():
    formulas=js(commands=[dict(action='formulas',tax=True)])['outputs'][0]
    assert len(formulas)==3
    assert '登録すると保留になります' in formulas[2] and 'CS:CS' in formulas[2]
    for required in ['_receipt_tax_corrections','correction_digest','amended_snapshot_digest','financial_write_count','posting_authority','税区分修正1']:
        assert required in formulas[2]
    # Native formula lexical structure, including escaped quotes in regexes.
    f=formulas[2];quoted=False;depth=0;i=0
    while i<len(f):
        if f[i]=='"':
            if quoted and i+1<len(f) and f[i+1]=='"':i+=2;continue
            quoted=not quoted
        elif not quoted:
            if f[i]=='(':depth+=1
            if f[i]==')':depth-=1
            assert depth>=0
        i+=1
    assert depth==0 and not quoted


def test_native_result_formula_recalculation_does_not_create_false_unknown(tmp_path):
    e,p,r=held(tmp_path);p.recalc=True;original=r.original(e['request_id'])
    result=perform(tmp_path,e,p,r)
    assert result['state']=='検証済み・未記帳' and r.original(e['request_id'])==original
    assert p.state['ui'][0][6]=='calculated-tax-result' and p.state['ui'][0][96]=='税込'


@pytest.mark.parametrize('fault',[None,'setup'])
def test_explicit_offline_setup_preserves_registered_data_and_latches_failure(tmp_path,fault):
    e,p,r=held(tmp_path);before=deepcopy(p.state)
    payload={k:v for k,v in before.items() if k!='tax'}
    result=js(**payload,config={'enabled':False},commands=[dict(action='tax_setup')],fault=fault,http_ports=True)
    assert result['cases']==before['cases'] and result['requests']==before['requests'] and result['ui']==before['ui']
    assert result['tax']==[HEADERS]
    if fault:
        assert result['errors'] and result['properties']['RECEIPT_REVIEW_STOPPED']=='tax_setup_unconfirmed'
    else:
        assert not result['errors'] and 'RECEIPT_REVIEW_STOPPED' not in result['properties']
        assert len(result['formulas'])==1 and len(result['ui'][0])==98


@pytest.mark.parametrize('mode',['税込','税別'])
def test_different_frozen_input_or_finished_payload_cannot_bypass_binding(tmp_path,mode):
    e,p,r=held(tmp_path);original=r.original(e['request_id'])
    plan=correction_plan(original,mode,'original_print_confirmed');p.rpc('tax_begin',plan=plan)
    bogus=dict(request_id=e['request_id'],snapshot_digest=plan['snapshot_digest'],
        correction_id=plan['correction_id'],correction_digest=digest(plan),
        amended_snapshot_digest='a'*64,tax_mode=mode,state='検証済み・未記帳',issues=[],posting_authority=False,financial_write_count=0)
    with pytest.raises(RuntimeError):p.rpc('tax_finish',correction_id=plan['correction_id'],result=bogus)
    assert len(p.state['tax'])==2 and r.original(e['request_id'])==original
