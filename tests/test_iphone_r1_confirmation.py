from copy import deepcopy
import pytest

from app.iphone_confirmation_host import PREFIX
from app.receipt_confirmation_contract import load, observe_ledger, planned_after
from app.unified_receipt_review import digest
from draft_runtime_fixtures import set_value
from iphone_r1_fixtures import Rig


@pytest.mark.parametrize('cid',['SYN-G101','SYN-M101'])
def test_actual_host_draft_compare_explicit_post_writer_and_read_only_result(monkeypatch,tmp_path,cid):
    r=Rig(monkeypatch,tmp_path); r.h.login()
    before=r.frozen(cid)
    value=r.save(cid)
    assert r.frozen(cid)==before  # draft save issues no authority/claim/writer
    p=r.proof(cid,'post')
    assert r.frozen(cid)==before  # comparison GET stays read-only
    assert p['claims']['draft']['value']==value
    response=r.post(p,value,True)
    assert response.status_code==200,response.get_data(as_text=True)
    assert response.json['status']=='COMPLETE',response.json
    c=r.native[cid]; requests=r.ui._requests(cid); assert len(requests)==1
    request=requests[0]
    assert request['inputs']['draft']['value']==value
    assert request['request_id']==p['claims']['request_id']
    assert observe_ledger(c.readers.ledger)[0]==planned_after(request['before'],request['plan'])
    assert c.readers.ledger.writes==(4 if c.kind=='normal' else 3)
    saved=r.frozen(cid)
    assert r.post(p,value,True).json['status']=='COMPLETE'
    assert r.h.get(PREFIX+'/r1/results/'+request['request_id']).status_code==200
    assert r.h.get(PREFIX).status_code==200
    assert r.frozen(cid)==saved


@pytest.mark.parametrize('cid',['SYN-G101','SYN-M101'])
@pytest.mark.parametrize('change',['draft','source','master','ledger','csrf','origin','expired','session','content','explicit'])
def test_displayed_snapshot_and_identity_drift_rejected(monkeypatch,tmp_path,cid,change):
    r=Rig(monkeypatch,tmp_path);r.h.login();value=r.save(cid);p=r.proof(cid,'post');c=r.native[cid]
    headers={}
    if change in {'draft','source','master','ledger'}:
        port=getattr(c.readers,{'draft':'draft','source':'source','master':'categories','ledger':'ledger'}[change])
        v=load(port.read_versioned()[0])
        if change=='draft':v['revision']+=1;v['value']['fields']['memo']='other device';v['input_digest']=digest(v['value'])
        elif change=='source':v['source']['drive_numeric_version']='2'
        elif change=='master':v['revision']='new-master'
        else:v['snapshot'][1]['支出明細'][-1][4]='=2+2'
        set_value(port,v)
    elif change=='csrf':p['csrf']='bad'
    elif change=='origin':headers={'Origin':'https://foreign.test'}
    elif change=='expired':r.h.now+=600
    elif change=='session':r.h.client.set_cookie(r.h.web.COOKIE,'bad',domain='confirmation.example.test')
    elif change=='content':value['fields']['memo']='not displayed'
    before=r.frozen(cid)
    writes=c.readers.ledger.writes
    response=r.post(p,value,change!='explicit',**headers)
    assert response.status_code in {403,409,410},response.get_data(as_text=True)
    assert r.frozen(cid)==before and c.readers.ledger.writes==writes


@pytest.mark.parametrize('cid',['SYN-G101','SYN-M101'])
def test_partial_save_and_medical_origin_block(monkeypatch,tmp_path,cid):
    r=Rig(monkeypatch,tmp_path);r.h.login();p=r.proof(cid,'save_draft')
    v=deepcopy(p['claims']['draft']['value']);v['fields']['memo']='途中メモ'
    if cid=='SYN-M101':v['fields']['merchant']='手入力途中'
    assert r.post(p,v).status_code==200
    assert r.proof(cid,'save_draft')['claims']['draft']['value']==v
    assert r.h.get(PREFIX+'/r1/'+cid+'/post').status_code==409
    p=r.proof(cid,'save_draft');v['provenance']='ocr'
    assert r.post(p,v).status_code==409


@pytest.mark.parametrize('cid',['SYN-G103','SYN-H301','OLD-MEDICAL-P1','SYN-P201','SYN-P202'])
def test_holds_have_no_qualification(monkeypatch,tmp_path,cid):
    r=Rig(monkeypatch,tmp_path);r.h.login()
    assert r.h.get(PREFIX+'/r1/'+cid+'/post').status_code==409


def test_ci_is_isolated():
    from pathlib import Path
    import yaml
    text=(Path(__file__).resolve().parents[1]/'.github/workflows/iphone-r1-confirmation-checks.yml').read_text('utf8')
    w=yaml.safe_load(text)
    assert w['on']=={'pull_request':{'branches':['codex/iphone-v3-guarded-writer']}}
    assert w['permissions']=={'contents':'read'}
    assert w['env']=={'UNIFIED_RECEIPT_WRITE_ENABLED':'false'}
    assert not any(x in text for x in ('secrets.','vars.','environment:','workflow_dispatch','schedule:'))
    assert all(j['runs-on']=='ubuntu-24.04' for j in w['jobs'].values())
    assert set(w['jobs'])=={'synthetic-v3'}
    job=w['jobs']['synthetic-v3'];assert job['timeout-minutes']==30
    assert job['steps'][0]=={'uses':'actions/checkout@v4','with':{
        'ref':'${{ github.event.pull_request.head.sha }}','fetch-depth':0,'persist-credentials':False}}
    assert [s['uses'] for s in job['steps'] if 'uses' in s]==[
        'actions/checkout@v4','actions/setup-python@v5','actions/upload-artifact@v4']
    assert all('uses' in s or s['run'].split()[0] in {'pip','python'} for s in job['steps'])
