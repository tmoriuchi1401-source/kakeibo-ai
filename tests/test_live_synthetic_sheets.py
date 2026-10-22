from copy import deepcopy
from types import SimpleNamespace
import json
from hashlib import sha256
from pathlib import Path
import subprocess
import sys

import pytest

from app.drive_run_state import StateError
from app.iphone_sheet_reader import original_formula
from registration_fixtures import Rig
from sheets_only_fixtures import no_clients,evaluate
from staging_shadow.live_synthetic_sheets import (LiveSyntheticBridge,GoogleResultTransport,
    validate_writes,SLOT)


def provision(r):
    raw=deepcopy(r.sheet.raw);raw['spreadsheetId']='synthetic-google-book'
    sources={}
    for cid,c in r.sheet.contract['cases'].items():
        if not c['identity']:continue
        i=dict(c['identity'],source_id='external-'+c['identity']['source_id'],case_id=cid)
        sources[i['source_id']]=i
        row=raw['sheets'][0]['data'][0]['rowData'][c['row_index']]['values']
        row[4]={'userEnteredValue':{'formulaValue':original_formula(cid,i,c['kind'])}}
        row[18]={'userEnteredValue':{'stringValue':i['source_id']}}
    profile=dict(id='synthetic-connected-owner',email='owner@example.test')
    acl=dict(id=raw['spreadsheetId'],title='完全合成 test',shared=False,current_user_can_share=True,
             permissions=[dict(type='user',role='owner',emailAddress=profile['email'])])
    return raw,sources,profile,acl


@pytest.fixture
def rig(monkeypatch,tmp_path):
    no_clients(monkeypatch);r=Rig(monkeypatch,tmp_path)
    raw,sources,profile,acl=provision(r)
    b=LiveSyntheticBridge(r.worker,raw,sources,profile,acl,enabled=True)
    yield SimpleNamespace(r=r,raw=raw,baseline=deepcopy(raw),sources=sources,profile=profile,acl=acl,b=b)
    r.assert_safe()


def row(rig,cid='SYN-G101'):
    return next(r['values'] for r in rig.raw['sheets'][0]['data'][0]['rowData'][1:]
                if r['values'][0]['userEnteredValue']['stringValue']==cid)


def poll(x):
    return x.b.poll(x.raw,deepcopy(x.raw),x.sources,x.profile,x.acl)


def accept(x,cid='SYN-G101'):
    row(x,cid)[1]['userEnteredValue']={'stringValue':'登録 1'}
    assert poll(x)['results'][-1]['status']=='SYNC_WAIT'
    x.r.old.h.now+=3
    return poll(x)


def apply(x,requests):
    for q in requests:
        u=q['updateCells'];p=u['range'];c=x.raw['sheets'][0]['data'][0]['rowData'][p['startRowIndex']]['values'][p['startColumnIndex']]
        c[u['fields']]=deepcopy(u['rows'][0]['values'][0][u['fields']])


def test_external_request_real_identity_alias_keeps_fixture_owner_and_finance_separate(rig):
    x=rig;before=deepcopy(x.raw)
    result=accept(x);pending=result['pending']
    assert result['results'][-1]['status']=='VALIDATED_UNPOSTED' and len(pending)==1
    requests=x.b.ready(pending[0]['id'],x.raw,x.sources,x.profile,x.acl)
    with pytest.raises(StateError):x.b.ready(pending[0]['id'],x.raw,x.sources,x.profile,x.acl)
    apply(x,requests)
    assert not poll(x)['pending']
    assert next(iter(x.b._records().values()))['state']=='DELIVERED'
    for a,b in zip(before['sheets'][0]['data'][0]['rowData'],x.raw['sheets'][0]['data'][0]['rowData']):
        assert a['values'][:1]+a['values'][3:]==b['values'][:1]+b['values'][3:]
    assert x.r.permission.setup_actor!=x.b.principal
    assert x.r.draft_writes()==1
    for _ in range(3):assert not poll(x)['pending']
    assert x.r.draft_writes()==1


def test_lost_response_without_applied_result_blocks_all_new_adoption_and_never_resends(rig):
    x=rig;first=accept(x);assert first['pending']
    x.b.ready(first['pending'][0]['id'],x.raw,x.sources,x.profile,x.acl)
    n=x.r.draft_writes()
    row(x)[14]['userEnteredValue']={'stringValue':'changed after transport timeout'}
    for _ in range(3):
        result=poll(x)
        assert result['results'][0]['status']=='UNKNOWN' and not result['pending']
    x.r.worker=x.r.rebuild();x.b=LiveSyntheticBridge(x.r.worker,x.baseline,x.sources,x.profile,x.acl,enabled=True)
    assert poll(x)['results'][0]['status']=='UNKNOWN'
    assert x.r.draft_writes()==n


def test_changed_input_cancels_only_an_unclaimed_delivery(rig):
    x=rig;first=accept(x);key=first['pending'][0]['id']
    recovered=poll(x)
    assert recovered['pending'][0]['id']==key and recovered['results'][0]['status']=='WAITING'
    row(x)[14]['userEnteredValue']={'stringValue':'changed before send claim'}
    result=poll(x)
    assert x.b._records()[key]['state']=='CANCELLED'
    assert all(r['status']!='UNKNOWN' for r in result['results'])
    with pytest.raises(StateError):x.b.ready(key,x.raw,x.sources,x.profile,x.acl)
    assert x.r.draft_writes()==1


def test_display_changed_before_claim_is_not_overwritten(rig):
    x=rig;pending=accept(x)['pending'][0]
    row(x)[2]={'userEnteredValue':{'stringValue':'another display edit'}}
    with pytest.raises(StateError,match='display_changed_before_publish'):
        x.b.ready(pending['id'],x.raw,x.sources,x.profile,x.acl)


def test_other_case_edit_never_reimports_unchanged_completed_request(rig):
    x=rig
    for p in accept(x)['pending']:apply(x,p['requests'])
    poll(x)
    row(x,'SYN-M101')[14]['userEnteredValue']={'stringValue':'another case edit'}
    row(x,'SYN-M101')[1]['userEnteredValue']={'stringValue':'登録 1'}
    assert next(r for r in poll(x)['results'] if r['case_id']=='SYN-G101')['status']=='VALIDATED_UNPOSTED'
    x.r.old.h.now+=3
    result=poll(x)
    assert next(r for r in result['results'] if r['case_id']=='SYN-G101')['status']=='VALIDATED_UNPOSTED'
    records=x.r.worker._state()[1]['requests']
    assert len([r for r in records.values() if r['case_id']=='SYN-G101'])==1


def test_partial_medical_receipt_stays_held_after_other_case_edit_and_restart(rig):
    x=rig;row(x,'SYN-M101')[8].pop('userEnteredValue',None)
    for p in accept(x,'SYN-M101')['pending']:apply(x,p['requests'])
    poll(x);before=x.r.draft_writes()
    x.r.worker=x.r.rebuild();x.b=LiveSyntheticBridge(x.r.worker,x.baseline,x.sources,x.profile,x.acl,enabled=True)
    row(x)[14]['userEnteredValue']={'stringValue':'unrelated edit'}
    result=poll(x)
    assert next(r for r in result['results'] if r['case_id']=='SYN-M101')['status']=='HELD'
    assert x.r.draft_writes()==before
    records=x.r.worker._state()[1]['requests']
    assert len([r for r in records.values() if r['case_id']=='SYN-M101'])==1


@pytest.mark.parametrize('edit',['input','id','hash','source'])
def test_read_write_race_is_guarded_in_google_formula_even_without_sheet_cas(rig,edit):
    x=rig;pending=accept(x)['pending'][0]
    requests=x.b.ready(pending['id'],x.raw,x.sources,x.profile,x.acl)
    index=dict(input=14,id=0,hash=17,source=18)[edit]
    row(x)[index]['userEnteredValue']={'stringValue':'changed'}
    with pytest.raises(StateError):x.b.ready(pending['id'],x.raw,x.sources,x.profile,x.acl)
    apply(x,requests)  # Edit happened AFTER last read, BEFORE Google atomic batch.
    raw=x.raw
    sheet=SimpleNamespace(raw=raw,row=lambda cid:raw['sheets'][0]['data'][0]['rowData'][1]['values'])
    assert '競合' in evaluate(sheet,'SYN-G101')
    assert raw['sheets'][0]['data'][0]['rowData'][1]['values'][index]['userEnteredValue']=={'stringValue':'changed'}


@pytest.mark.parametrize('attack',['principal','shared','acl','permission_missing','revision','note','header','table','protection','held','sort','extra','formula'])
def test_fresh_permission_source_structure_and_input_guards(rig,attack):
    x=rig
    if attack=='principal':x.profile['id']='another'
    elif attack=='shared':x.acl['shared']=True
    elif attack=='acl':x.acl['permissions'].append(dict(type='anyone',role='writer'))
    elif attack=='permission_missing':x.acl.pop('permissions')
    elif attack=='revision':next(iter(x.sources.values()))['source_version']='replaced'
    elif attack=='note':row(x)[14]['note']='tampered'
    elif attack=='header':x.raw['sheets'][0]['data'][0]['rowData'][0]['values'][0]['userEnteredValue']={'stringValue':'tampered'}
    elif attack=='table':x.raw['sheets'][0]['tables'][0]['range']['endRowIndex']+=1
    elif attack=='protection':x.raw['sheets'][0]['protectedRanges']=[]
    elif attack=='held':row(x,'SYN-G103')[14]['userEnteredValue']={'stringValue':'mixed'}
    elif attack=='sort':x.raw['sheets'][0]['data'][0]['rowData'][1:]=list(reversed(x.raw['sheets'][0]['data'][0]['rowData'][1:]))
    elif attack=='extra':x.raw['sheets'][0]['data'][0]['rowData'].append(deepcopy(x.raw['sheets'][0]['data'][0]['rowData'][1]))
    elif attack=='formula':row(x)[14]['userEnteredValue']={'formulaValue':'=1+1'}
    n=x.r.draft_writes()
    with pytest.raises(StateError):poll(x)
    assert x.r.draft_writes()==n


def test_double_capture_edit_rejected_before_backend_adoption(rig):
    x=rig;second=deepcopy(x.raw)
    second['sheets'][0]['data'][0]['rowData'][1]['values'][14]['userEnteredValue']={'stringValue':'concurrent'}
    with pytest.raises(StateError,match='concurrent_edit'):
        x.b.poll(x.raw,second,x.sources,x.profile,x.acl)
    assert x.r.draft_writes()==0


def test_unknown_formula_stays_unknown_even_after_input_edit(rig):
    x=rig
    _,batch,_,_=x.b.capture(x.raw,x.sources,x.profile,x.acl)
    formula=x.b.formula('SYN-G101','登録 1','UNKNOWN・結果照合待ち',batch)
    row(x)[2]={'userEnteredValue':{'formulaValue':formula}}
    row(x)[14]['userEnteredValue']={'stringValue':'edited'}
    sheet=SimpleNamespace(raw=x.raw,row=lambda cid:row(x,cid))
    assert evaluate(sheet,'SYN-G101').startswith('UNKNOWN')


def test_native_unknown_never_mirrors_new_inputs_or_reexecutes(rig,monkeypatch):
    x=rig
    x.b=LiveSyntheticBridge(x.r.worker,x.baseline,x.sources,x.profile,x.acl,enabled=True,display_cases=['SYN-G101'])
    def lost(*a,**k):raise TimeoutError('synthetic lost response')
    monkeypatch.setattr(x.r.old.old.native['SYN-G101'].readers.draft,'replace_versioned',lost)
    result=accept(x)
    assert result['results'][0]['status']=='UNKNOWN' and len(result['pending'])==1
    p=result['pending'][0];apply(x,x.b.ready(p['id'],x.raw,x.sources,x.profile,x.acl))
    poll(x)
    before=deepcopy(x.r.sheet.raw)
    row(x)[14]['userEnteredValue']={'stringValue':'must not mirror'}
    row(x)[1]['userEnteredValue']={'stringValue':'登録 2'}
    result=poll(x)
    assert result['results'][0]['status']=='UNKNOWN' and not result['pending']
    assert x.r.sheet.raw==before and x.r.draft_writes()==0


def test_native_table_dropdown_is_preserved_without_per_cell_validation_write(rig):
    x=rig
    x.raw['sheets'][0]['tables'][0]['columnProperties']=[dict(columnIndex=1,columnType='DROPDOWN',
        columnName='操作依頼',dataValidationRule=dict(condition=dict(type='ONE_OF_LIST',values=[dict(userEnteredValue='登録 1')])))]
    for r in x.raw['sheets'][0]['data'][0]['rowData'][1:]:r['values'][1].pop('dataValidation',None)
    x.b=LiveSyntheticBridge(x.r.worker,x.raw,x.sources,x.profile,x.acl,enabled=True)
    pending=accept(x)['pending'][0]
    assert len(pending['requests'])==1
    assert pending['requests'][0]['updateCells']['range']['startColumnIndex']==2
    apply(x,pending['requests'])
    assert not poll(x)['pending']


def test_injected_transport_requires_existing_intent_and_disables_retries(rig):
    x=rig;pending=accept(x)['pending'][0];events=[]
    class Request:
        def __init__(self,value):self.value=value
        def execute(self,**kwargs):events.append(kwargs);return self.value
    class Sheets:
        def get(self,**kwargs):
            assert kwargs['spreadsheetId']==x.b.book and kwargs['ranges']==["'確認'!A1:Z1000"]
            return Request(x.raw)
        def batchUpdate(self,**kwargs):
            assert kwargs['spreadsheetId']==x.b.book
            validate_writes(kwargs['body']['requests'],x.b.sid)
            return Request({'ok':True})
    service=SimpleNamespace(spreadsheets=lambda:Sheets())
    with pytest.raises(StateError):GoogleResultTransport(service,x.b)
    transport=GoogleResultTransport(service,x.b,enabled=True)
    assert transport.publish(pending['id'],sources=x.sources,profile=x.profile,acl=x.acl)=={'ok':True}
    assert events[-1]==dict(num_retries=0)
    with pytest.raises(StateError):transport.publish('foreign',sources=x.sources,profile=x.profile,acl=x.acl)


@pytest.mark.parametrize('attack',['input_column','wrong_tab','authority_request','value_instead_of_formula','request_value','retry'])
def test_transport_never_can_write_inputs_native_authority_or_ledger(rig,attack):
    x=rig;requests=accept(x)['pending'][0]['requests'];sid=x.b.sid
    if attack=='input_column':requests[0]['updateCells']['range'].update(startColumnIndex=8,endColumnIndex=9)
    elif attack=='wrong_tab':requests[0]['updateCells']['range']['sheetId']=sid+1
    elif attack=='authority_request':requests.append(dict(addSheet=dict(properties=dict(title='authority'))))
    elif attack=='value_instead_of_formula':requests[0]['updateCells']['rows'][0]['values'][0]['userEnteredValue']={'stringValue':'posted'}
    elif attack=='request_value':requests[1]['updateCells']['fields']='userEnteredValue'
    elif attack=='retry':requests[0]['updateCells']['retry']=True
    with pytest.raises(StateError,match='write_scope'):validate_writes(requests,sid)


def test_checkpoint_process_restart_keeps_outbox_unknown_and_no_duplicate_import(rig,tmp_path):
    x=rig;directory=tmp_path/'probe';directory.mkdir()
    config=dict(baseline=x.baseline,sources=x.sources,profile=x.profile,acl=x.acl,allow_checkpoint_archive=True)
    (directory/'config.json').write_text(json.dumps(config),encoding='utf8')
    row(x)[1]['userEnteredValue']={'stringValue':'登録 1'}
    request=dict(op='poll',first=x.raw,second=x.raw,sources=x.sources,profile=x.profile,acl=x.acl)
    def run(v):
        f=directory/'request.json';f.write_text(json.dumps(v),encoding='utf8')
        p=subprocess.run([sys.executable,'tests/live_google_sheets_probe.py',str(directory),str(f)],capture_output=True,text=True,timeout=90)
        assert p.returncode==0,p.stderr
        result=json.loads((directory/'response.json').read_bytes());assert 'error' not in result,result
        return result
    assert run(request)['results'][-1]['status']=='SYNC_WAIT'
    accepted=run(request)
    assert accepted['results'][-1]['status']=='VALIDATED_UNPOSTED'
    ready=run(dict(op='ready',id=accepted['pending'][0]['id'],raw=x.raw,sources=x.sources,profile=x.profile,acl=x.acl))
    assert ready['requests']
    assert run(request)['results'][0]['status']=='UNKNOWN'
    saved=run(dict(op='inspect'))
    assert saved['draft_writes']==1
    assert len(saved['outbox'])==1 and next(iter(saved['outbox'].values()))['state']=='SENDING'
    before=(directory/'checkpoint.json').read_bytes();identity=sha256(before).hexdigest()
    archived=run(dict(op='archive_checkpoint',expected_checkpoint_sha256=identity))
    assert archived['archived']==identity
    assert (directory/('archive-'+identity+'.json')).read_bytes()==before
    after=run(dict(op='inspect'))
    assert after['outbox']==saved['outbox'] and after['registrations']==saved['registrations']
    assert after['draft_writes']==1
    assert run(request)['results'][0]['status']=='UNKNOWN'
    # Archive integrity loss cannot silently create a new history/generation.
    (directory/('archive-'+identity+'.json')).write_bytes(b'changed')
    f=directory/'request.json';f.write_text(json.dumps(dict(op='inspect')),encoding='utf8')
    p=subprocess.run([sys.executable,'tests/live_google_sheets_probe.py',str(directory),str(f)],capture_output=True,text=True,timeout=90)
    assert p.returncode==0
    assert 'archive changed' in json.loads((directory/'response.json').read_bytes())['reason']
