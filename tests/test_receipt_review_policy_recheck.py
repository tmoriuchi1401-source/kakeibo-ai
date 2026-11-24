"""Offline new amount policy and historical recheck; no network or live receipt."""
from copy import deepcopy
import json
import sqlite3
from types import SimpleNamespace

import pytest

from test_receipt_pr_b import case, ready, recalc, js, deny_finance_and_network
from test_receipt_review_tax import held
from app.page_receipt_model import digest
from app.receipt_review_cases import make_row, snapshot_from_row, validate_snapshot, VALIDATOR_REVISION
from app.receipt_review_policy_recheck import (HEADERS, RestRecheckReader,
    RecheckAttemptFence, execute_recheck, recheck_plan)
from app.receipt_sheets_authority import UNKNOWN


class Port:
    def __init__(self, old):
        self.state=deepcopy(old.state)
        self.state['policy']=[HEADERS]
        self.calls=[]
        self.fault=None
        self.source_checks=0

    def rpc(self, action, **data):
        self.calls.append(action)
        reply=js(**self.state,commands=[dict(action=action,**data)],fault=self.fault,http_ports=True,
                 tax_recalc=True)
        self.state={k:reply[k] for k in ('cases','requests','ui','tax','policy','properties')}
        if reply['errors']:
            raise RuntimeError(reply['errors'][0])
        return reply['outputs'][0]

    def source_check(self, source):
        self.source_checks+=1


class Reader:
    def __init__(self, port):
        self.port=port

    def original(self, rid):
        p=self.port
        return deepcopy(dict(row=next(r for r in p.state['requests'][1:] if r[0]==rid),
            entry=json.loads(next(r[4] for r in p.state['cases'][1:] if r[6]==rid))))

    def results(self, rid):
        class Session:
            def get(_,url,**kwargs):
                return SimpleNamespace(status_code=200,json=lambda:{'values':self.port.state['policy']})
        return RestRecheckReader(Session(),'synthetic-review-sheet',None).results(rid)


def test_unknown_tax_balanced_is_warning_and_known_tax_normal():
    e,row=case()
    for mode in ('不明','税込','税別'):
        snap=snapshot_from_row(ready(e,row,tax=mode),e)
        result=validate_snapshot(snap,e)
        assert result['state']=='検証済み・未記帳' and result['issues']==[]
        assert result['warnings']==(['tax_treatment_unknown'] if mode=='不明' else [])
        assert result['validator_revision']==VALIDATOR_REVISION
        assert snap['inputs']['tax_mode']==mode


@pytest.mark.parametrize('problem', ['total_mismatch','external_tax_omitted','included_tax_double','discount_mismatch','category'])
def test_unknown_tax_does_not_hide_amount_or_other_errors(problem):
    e,row=case()
    if problem=='included_tax_double':
        e['candidate_rows'][1][4]='内消費税'
        e['baseline_digest']=digest({k:v for k,v in e.items() if k not in {'baseline_digest','request_id'}})
        row=make_row(e)
    row=ready(e,row,tax='不明')
    if problem in {'total_mismatch','external_tax_omitted','discount_mismatch'}:
        row[11]+=100
    if problem=='included_tax_double':
        row[11]=900;row[20]=100
    if problem=='category':row[12:14]=['不正','分類']
    recalc(row)
    result=validate_snapshot(snapshot_from_row(row,e),e)
    assert result['state']!='検証済み・未記帳'
    assert result['warnings']==['tax_treatment_unknown']
    if problem!='category':assert result['state']=='保留'


def test_external_tax_candidate_cannot_be_removed_and_hidden_in_product_amount():
    e,_=case(amounts=(900,100))
    e['candidate_rows'][1][4]='外税10%'
    e['baseline_digest']=digest({k:v for k,v in e.items() if k not in {'baseline_digest','request_id'}})
    row=ready(e,make_row(e),tax='不明')
    row[25]='除外';row[11]=1000;recalc(row)
    result=validate_snapshot(snapshot_from_row(row,e),e)
    assert result['difference']==0
    assert result['state']=='保留' and 'external_tax_addition_missing' in result['issues']
    assert result['warnings']==['tax_treatment_unknown']


def setup_old(tmp_path):
    e,old,_=held(tmp_path)
    p=Port(old);r=Reader(p)
    return e,p,r


def run(tmp_path,e,p,r,duplicate=False):
    return execute_recheck(e['request_id'],reader=r,port=p,
        fence=RecheckAttemptFence(tmp_path/'base.db'),duplicate_check=lambda *_:duplicate)


def test_historical_recheck_append_only_preserves_original_and_draft(tmp_path):
    e,p,r=setup_old(tmp_path);rid=e['request_id'];original=r.original(rid)
    before=deepcopy(p.state);plan=recheck_plan(original)
    result=run(tmp_path,e,p,r)
    assert result['state']=='検証済み・未記帳' and result['difference']==0
    assert result['warnings']==['tax_treatment_unknown'] and result['issues']==[]
    assert result['original_result_digest']==plan['original_result_digest']
    assert r.original(rid)==original and p.state['cases']==before['cases']
    ui=deepcopy(p.state['ui']);ui[0][6]=before['ui'][0][6]
    assert ui==before['ui'] and ui[0][96]=='不明'
    assert r.results(rid)==[result] and p.calls==['policy_recheck']
    assert run(tmp_path,e,p,r)['state']==UNKNOWN and p.calls==['policy_recheck']
    with sqlite3.connect(tmp_path/'base.db') as db:
        assert db.execute('SELECT request_id FROM policy_recheck_attempts').fetchall()==[(rid,)]


def test_duplicate_remains_hold_and_cannot_repeat(tmp_path):
    e,p,r=setup_old(tmp_path)
    result=run(tmp_path,e,p,r,duplicate=True)
    assert result['state']=='保留' and result['issues']==['existing_receipt_duplicate']
    assert result['warnings']==['tax_treatment_unknown']


@pytest.mark.parametrize('fault',['before','after','readback'])
def test_uncertain_append_stops_without_retry(tmp_path,fault):
    e,p,r=setup_old(tmp_path);original=r.original(e['request_id']);p.fault=fault
    assert run(tmp_path,e,p,r)['state']==UNKNOWN
    assert p.state['properties']['RECEIPT_REVIEW_STOPPED']=='outcome_unconfirmed'
    p.fault=None
    assert run(tmp_path,e,p,r)['state']==UNKNOWN
    assert p.calls==['policy_recheck'] and r.original(e['request_id'])==original


def test_plan_binding_replay_and_history_digest(tmp_path):
    e,p,r=setup_old(tmp_path);plan=recheck_plan(r.original(e['request_id']))
    result=validate_snapshot(json.loads(r.original(e['request_id'])['row'][5]),r.original(e['request_id'])['entry'])
    result.update(request_id=e['request_id'],snapshot_digest=plan['snapshot_digest'],
                  original_result_digest=plan['original_result_digest'])
    for field in ('original_result_digest','snapshot_digest','original_record_digest','entry_digest','validator_revision'):
        bad=deepcopy(plan);bad[field]='invalid'
        with pytest.raises(RuntimeError):p.rpc('policy_recheck',plan=bad,result=result)
    assert len(p.state['policy'])==1
    assert run(tmp_path,e,p,r)['state']=='検証済み・未記帳'
    history=deepcopy(p.state['policy']);p.state['policy'].append(deepcopy(history[1]))
    with pytest.raises(ValueError):r.results(e['request_id'])
    p.state['policy']=history;p.state['policy'][1][4]+=' '
    with pytest.raises(ValueError):r.results(e['request_id'])


def test_off_setup_and_formula_keep_98_column_contract(tmp_path):
    e,p,r=setup_old(tmp_path);before=deepcopy(p.state)
    data={k:v for k,v in before.items() if k!='policy'}
    result=js(**data,config={'enabled':False},commands=[{'action':'policy_setup'}],http_ports=True)
    assert not result['errors'] and result['policy']==[HEADERS]
    assert result['requests']==before['requests'] and result['cases']==before['cases'] and result['ui']==before['ui']
    f=js(commands=[{'action':'formulas','policy':True}])['outputs'][0][2]
    assert '金額が一致すれば登録可能' in f and '_receipt_policy_reviews' in f
    assert '税区分未確定' in f and 'receipt-review-amount-v2' in f
    assert len(result['ui'][0])==98
    quoted=False;depth=0;i=0
    while i<len(f):
        if f[i]=='"':
            if quoted and i+1<len(f) and f[i+1]=='"':
                i+=2;continue
            quoted=not quoted
        elif not quoted:
            if f[i]=='(':depth+=1
            elif f[i]==')':depth-=1
            assert depth>=0
        i+=1
    assert depth==0 and not quoted
