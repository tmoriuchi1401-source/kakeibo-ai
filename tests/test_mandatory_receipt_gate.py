"""R2-2 isolated gate tests. Every store is disposable in-process memory."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from uuid import uuid4

import pytest

from app.drive_run_state import StateError
from app.receipt_confirmation_contract import encoded, planned_after, observe_ledger
from staging_shadow.auto_native_evidence import POINTS
from mandatory_gate_fixtures import Rig, joint_r1


class Crash(BaseException):pass


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    import socket
    import sys
    import app.sheets
    import googleapiclient.discovery
    def denied(*args, **kwargs):pytest.fail('external capability used')
    monkeypatch.setattr(socket.socket,'connect',denied)
    monkeypatch.setattr(googleapiclient.discovery,'build',denied)
    monkeypatch.setattr(app.sheets.SheetsDB,'__init__',denied)
    previous=set(sys.modules)
    yield
    for name in set(sys.modules)-previous:
        if name.startswith('_kakeibo_accounting_sandbox_'):sys.modules.pop(name,None)


@pytest.mark.parametrize('entry',['normal_auto','buyback_income'])
def test_automatic_existing_writer_success_and_readonly_replay(entry):
    r=Rig();ids=(str(uuid4()),str(uuid4()))
    result=r.call(entry,ids=ids,revision=0)
    assert result['classification']=='COMPLETE' and r.dispatches()==1
    assert r.c.writer.last_db.offset==3 and r.backends[1].writes==3
    saved=r.frozen()
    assert r.call(entry,ids=ids,revision=0)['report']['classification']=='COMPLETE'
    assert r.frozen()==saved and r.dispatches()==1
    assert r.c.journal.read()[0]['operations']


@pytest.mark.parametrize('kind',['normal','medical'])
def test_r1_still_uses_existing_writer_and_all_ledger(kind,monkeypatch):
    r=Rig();d=joint_r1(r,monkeypatch,kind)
    result=r.c.submit_r1(d.contract,d.c)
    assert result['classification']=='COMPLETE' and result['writer_dispatches']==1
    assert observe_ledger(r.c.ledger)[0]==planned_after(d.c['before'],d.c['plan'])
    assert r.backends[1].writes==len(d.c['plan'])


@pytest.mark.parametrize('entry',['normal_auto','buyback_income'])
@pytest.mark.parametrize('first',['auto','r1'])
def test_shared_whole_source_cross_entry_fence(entry,first,monkeypatch):
    r=Rig();d=joint_r1(r,monkeypatch)
    if first=='auto':
        assert r.call(entry)['classification']=='COMPLETE'
        saved=r.frozen()
        with pytest.raises(StateError):r.c.submit_r1(d.contract,d.c,revision=4)
    else:
        assert r.c.submit_r1(d.contract,d.c)['classification']=='COMPLETE'
        saved=r.frozen()
        with pytest.raises(StateError):r.call(entry)
    assert r.frozen()==saved


@pytest.mark.parametrize('entry',['normal_auto','buyback_income'])
def test_direct_sink_and_writer_calls_cannot_mutate(entry):
    r=Rig();c=r.c;saved=r.frozen()
    with pytest.raises(StateError):c.writer.execute(r.h.candidate(entry,'u1'))
    with pytest.raises(StateError):c.make_receipt_pipeline(entry,'u1')._materialize_result(None,None,None,None)
    for port in (c.shared.io,c.journal.io,c.ledger,c.auto_ledger):
        raw,tag=port.read_versioned()
        with pytest.raises(StateError):port.replace_versioned(raw,tag,raw)
    with pytest.raises(StateError):c.gate.submit(entry,'u1',str(uuid4()),0,{})
    assert r.frozen()==saved and r.dispatches()==0


def test_old_confirmation_and_v1_private_save_are_closed():
    r=Rig();saved=r.frozen()
    assert r.c.sandbox.module('receipt_confirmation_production').execute()['classification']=='HELD'
    old=r.c.sandbox.module('receipt_confirmation').ReceiptConfirmation
    assert old.apply_confirmations(None)['classification']=='HELD'
    for action in (lambda:old.save_item(None,None,None),
        lambda:r.c.sandbox.module('receipt_reimport_production').ReimportStore.save(None),
        lambda:old._write_accounting_plan(None,None,None)):
        with pytest.raises(StateError):action()
    assert r.frozen()==saved


@pytest.mark.parametrize('entry',['normal_auto','buyback_income'])
@pytest.mark.parametrize('point',POINTS)
def test_crash_retains_fence_and_reconstruction_is_readonly(entry,point):
    r=Rig();ids=(str(uuid4()),str(uuid4()))
    def stop(p):
        if p==point:raise Crash(p)
    r.c.adapter.fault=stop
    with pytest.raises(Crash):r.call(entry,ids=ids,revision=0)
    saved=r.frozen();new=r.new()
    report=new.report(ids[0])
    assert report['classification'] in {'UNKNOWN','NOT_EXECUTED','COMPLETE','PARTIAL'}
    assert report['retry_allowed'] is False
    try:r.call(entry,c=new,ids=ids,revision=0)
    except StateError:pass
    with pytest.raises(StateError):r.call(entry,c=new)
    assert r.frozen()==saved and r.dispatches()<=1


@pytest.mark.parametrize('entry',['normal_auto','buyback_income'])
@pytest.mark.parametrize('row',[1,2,3])
@pytest.mark.parametrize('failure',['crash','exception'])
def test_writer_mid_or_all_rows_crash_never_cleans_or_retries(entry,row,failure):
    r=Rig();ids=(str(uuid4()),str(uuid4()))
    def stop(title,number):
        if number==row:raise Crash() if failure=='crash' else RuntimeError('injected')
    r.c.writer.fault=stop
    with pytest.raises((Crash,StateError)):r.call(entry,ids=ids,revision=0)
    assert r.dispatches()==1 and r.backends[1].writes==row
    native=r.c.journal.read()[0]['operations']
    assert next(iter(native.values()))['phase']=='writer_started'
    saved=r.frozen();new=r.new()
    assert new.report(ids[0])['classification']==('PARTIAL' if row<3 else 'UNKNOWN')
    assert r.call(entry,c=new,ids=ids,revision=0)['report']['retry_allowed'] is False
    with pytest.raises(StateError):r.call(entry,c=new)
    assert r.frozen()==saved and r.dispatches()==1


@pytest.mark.parametrize('backend,number',[(b,n) for b in ('shared','native','ledger')
    for n in range(1,4 if b=='ledger' else 5)])
@pytest.mark.parametrize('failure',['412','timeout','lost','readback','tag'])
def test_cas_faults_leave_no_cleanup_writes(backend,number,failure,monkeypatch):
    r=Rig();io=r.backends[{'shared':0,'ledger':1,'native':2}[backend]]
    original=io.replace_versioned;attempts=[]
    def broken(*args):
        attempts.append(1)
        if len(attempts)!=number:return original(*args)
        if failure in {'lost','readback','tag'}:
            old_tag=io.revision;original(*args)
            if failure=='tag':io.revision=old_tag;return
            if failure=='readback':
                from staging_shadow.auto_native_evidence import load
                io.raw=encoded(dict(load(io.raw),unexpected=True));return
        if failure=='412':raise StateError('HTTP_412')
        raise TimeoutError('synthetic '+failure)
    monkeypatch.setattr(io,'replace_versioned',broken)
    ids=(str(uuid4()),str(uuid4()))
    with pytest.raises((StateError,TimeoutError)):r.call(ids=ids,revision=0)
    assert len(attempts)==number
    saved=r.frozen();new=r.new()
    new.report(ids[0])
    try:r.call(c=new,ids=ids,revision=0)
    except StateError:pass
    with pytest.raises(StateError):r.call(c=new)
    assert r.frozen()==saved and len(attempts)==number and r.dispatches()<=1


@pytest.mark.parametrize('count',[2,16])
def test_independent_instances_mixed_auto_and_buyback_dispatch_once(count):
    r=Rig();instances=[r.c]+[r.new(fresh=True) for _ in range(count-1)]
    barrier=Barrier(count)
    for c in instances:c.adapter.fault=lambda p:barrier.wait(timeout=30) if p=='before_shared_claim' else None
    def call(i):
        try:return r.call('normal_auto' if i%2==0 else 'buyback_income',c=instances[i],revision=0)['classification']
        except StateError:return 'HELD'
    with ThreadPoolExecutor(max_workers=count) as pool:results=list(pool.map(call,range(count)))
    assert results.count('COMPLETE')==1 and r.dispatches()==1 and r.backends[1].writes==3
    assert len(r.c.shared.read()[0]['operations'])==1


@pytest.mark.parametrize('change',['source','version','native','authority','pending','partition','ledger','history'])
def test_changed_or_unknown_evidence_denies_before_financial_write(change):
    r=Rig();saved=r.backends[1].read_versioned()
    if change=='source':r.h.source.raw=b'changed'
    elif change=='version':r.h.source.frame['drive_revision_id']='changed'
    elif change=='native':r.h.native.frame['normal_auto','u1']['state']='UNKNOWN'
    elif change=='authority':r.h.native.frame['normal_auto','u1']['authority_revision']=None
    elif change=='pending':r.h.base_pending.frame['complete']=False
    elif change=='partition':r.h.partition.frame['complete']=False
    elif change=='history':r.history['complete']=False
    else:
        from staging_shadow.auto_native_evidence import load
        v=load(r.backends[1].raw);v['snapshot'][0]['支出明細'].append(['unknown-id'])
        v['snapshot'][1]['支出明細'].append(['unknown-id']);r.backends[1].raw=encoded(v)
        saved=r.backends[1].read_versioned()
    with pytest.raises(StateError):r.call()
    assert r.backends[1].read_versioned()==saved and r.dispatches()==0
    assert all(p.writes==0 for p in r.backends)


@pytest.mark.parametrize('entry',['normal_auto','buyback_income'])
@pytest.mark.parametrize('pdf',['pdf_auto','pdf_manual','grouped_unit','fixed_canary','explicit_unit'])
def test_pdf_unit_history_conflicts_with_whole_without_connecting_pdf(entry,pdf):
    r=Rig();r.history['records']=[r.h.reader.observe(pdf,'u1').value()]
    saved=r.frozen()
    with pytest.raises(StateError):r.call(entry)
    assert r.frozen()==saved and r.dispatches()==0
    with pytest.raises(StateError):r.c.make_receipt_pipeline(pdf,'u1')


@pytest.mark.parametrize('classification',['medical','sensitive_unknown','payroll'])
def test_medical_ai_ocr_and_sensitive_auto_paths_refused(classification):
    r=Rig();r.h.known_classification=classification;saved=r.frozen()
    r.h.sandbox.module('receipt_privacy_gate')._extract_receipt_text=lambda *a,**k:pytest.fail('sensitive OCR entered')
    with pytest.raises(StateError):r.call()
    assert r.frozen()==saved and r.dispatches()==0


@pytest.mark.parametrize('old',['pending','applied','UNKNOWN'])
@pytest.mark.parametrize('version',['receipt-reimport-v1','receipt-reimport-v2','receipt-reimport-v3'])
def test_retained_legacy_state_is_read_only(old,version):
    from staging_shadow.mandatory_receipt_gate import RetainedHistoryReader
    from test_legacy_contract_poc import mem
    r=Rig();legacy=dict(schema='receipt-reimport-v1',confirmation_items={'old':dict(status=old)},records={})
    value=legacy if version.endswith('v1') else dict(schema=version,legacy=legacy,operations={})
    port=mem(value);r.c._history=RetainedHistoryReader(lambda:deepcopy(r.history),legacy_ports=(port,))
    saved=r.frozen(),port.read_versioned()
    with pytest.raises(StateError):r.call()
    assert (r.frozen(),port.read_versioned())==saved and r.dispatches()==0


def test_same_ids_with_different_content_are_rejected():
    r=Rig();ids=(str(uuid4()),str(uuid4()))
    r.call(ids=ids,revision=0);saved=r.frozen()
    r.h.golden['normal_auto']['inputs']['categories'].append(['new','new'])
    with pytest.raises(StateError,match='request_id_content_mismatch'):r.call(ids=ids,revision=0)
    assert r.frozen()==saved and r.dispatches()==1


def test_copy_hash_cannot_become_independent_by_new_source_and_request_id():
    from app.iphone_accounting_ownership import relation
    from app.page_receipt_model import SourceRef,stable_page
    from app.unified_receipt_review import digest
    r=Rig();old=deepcopy(r.h.frozen['normal_auto']);old['source']['source_id']='separate-copy'
    old['partition']['source']=deepcopy(old['source'])
    src=SourceRef(source_file_id='separate-copy',source_content_hash=old['source']['sha256'],page_count=14)
    for unit in old['partition']['units']:
        for member in unit['members']:member['identity']=stable_page(src,member['page'])
    old['operation_identity']=digest(['separate-copy']);old['ledger_targets']=['copy-ledger-target']
    assert relation(old,r.h.frozen['normal_auto'])=='independent'
    r.history['records']=[old];saved=r.frozen()
    with pytest.raises(StateError,match='source_already_owned'):r.call()
    assert r.frozen()==saved and r.dispatches()==0


@pytest.mark.parametrize('port',['binding','history'])
def test_change_between_appends_stops_without_cleanup(port):
    r=Rig()
    def change(title,number):
        if number==1:
            if port=='binding':r.h.source.frame['drive_revision_id']='changed'
            else:r.history['complete']=False
    r.c.writer.fault=change
    with pytest.raises(StateError):r.call()
    assert r.backends[1].writes==1 and r.dispatches()==1
    assert next(iter(r.c.journal.read()[0]['operations'].values()))['phase']=='writer_started'


@pytest.mark.parametrize('wrong',['shared','ledger','native'])
def test_r1_mismatched_store_is_rejected_before_any_write(wrong,monkeypatch):
    from app.unified_receipt_write import ConditionalRequests
    r=Rig();d=joint_r1(r,monkeypatch)
    if wrong=='shared':d.contract.shared=ConditionalRequests(r.backends[0],'other',synthetic_accounting=True)
    elif wrong=='ledger':d.contract.readers.ledger=r.backends[1]
    else:d.contract.store.io=d.native
    saved=r.frozen(),d.native.read_versioned()
    with pytest.raises(StateError,match='R1_shared_composition_required'):r.c.submit_r1(d.contract,d.c)
    assert (r.frozen(),d.native.read_versioned())==saved


@pytest.mark.parametrize('count',[2,16])
def test_r1_races_valid_automatic_candidates_without_double_dispatch(count,monkeypatch):
    r=Rig();d=joint_r1(r,monkeypatch)
    instances=[r.c]+[r.new(fresh=True) for _ in range(count-1)]
    # All independently constructed instances must observe the same saved R1
    # history, even before any R1 shared claim exists.
    for c in instances[1:]:
        c.r1_native_port(d.native,expected_digest=d.manifest,spreadsheet_id=d.spid,parent_digest=d.pin)
    barrier=Barrier(count)
    def call(i):
        barrier.wait(timeout=30)
        try:
            if i==0:return r.c.submit_r1(d.contract,d.c)['classification']
            return r.call('normal_auto' if i%2 else 'buyback_income',c=instances[i],revision=0)['classification']
        except StateError:return 'HELD'
    with ThreadPoolExecutor(max_workers=count) as pool:results=list(pool.map(call,range(count)))
    assert results.count('COMPLETE')==1
    assert r.dispatches()+getattr(getattr(r.c,'last_r1_writer',None),'dispatches',0)==1


@pytest.mark.parametrize('missing',['preflight','history','reader','isolated'])
def test_incomplete_composition_cannot_start(missing):
    from staging_shadow.mandatory_receipt_gate import MandatoryReceiptComposition,RetainedHistoryReader
    r=Rig();h=r.h
    args=dict(shared_io=r.backends[0],ledger_io=r.backends[1],auto_native_io=r.backends[2],auth=h.auth,
        reader=h.reader,preflight=h.preflight,candidate_reader=h.candidate,binding_reader=h.bindings,
        history_reader=RetainedHistoryReader(lambda:deepcopy(r.history),legacy_ports=()),
        enabled=True,isolated_synthetic=True,clock=lambda:0)
    args[dict(preflight='preflight',history='history_reader',reader='reader',isolated='isolated_synthetic')[missing]]=None
    saved=r.frozen()
    with pytest.raises(StateError):MandatoryReceiptComposition(**args)
    assert r.frozen()==saved


@pytest.mark.parametrize('which',['native','shared'])
def test_start_evidence_must_still_match_before_each_append(which):
    from staging_shadow.auto_native_evidence import load
    r=Rig()
    def change(title,number):
        if number!=1:return
        io=r.backends[2 if which=='native' else 0];value=load(io.raw)
        row=next(iter(value['operations'].values()))
        if which=='native':row['writer_start']['sequence']=99
        else:row['state']='awaiting_readback';row['events'].append(dict(state='awaiting_readback',at=0))
        io.raw=encoded(value);io.revision+=1
    r.c.writer.fault=change
    with pytest.raises(StateError):r.call()
    assert r.backends[1].writes==1 and r.dispatches()==1


@pytest.mark.parametrize('entry',['normal_auto','buyback_income'])
def test_registered_manual_medical_source_never_enters_automatic_writer(entry,monkeypatch):
    r=Rig();d=joint_r1(r,monkeypatch,'medical');saved=r.frozen(),d.native.read_versioned()
    with pytest.raises(StateError):r.call(entry)
    assert (r.frozen(),d.native.read_versioned())==saved and r.dispatches()==0


def test_empty_v3_cannot_hide_retained_native_history(monkeypatch):
    from staging_shadow.auto_native_evidence import load
    r=Rig();d=joint_r1(r,monkeypatch);value=load(d.native.raw);value['operations']={}
    d.native.raw=encoded(value);saved=r.frozen(),d.native.read_versioned()
    with pytest.raises(StateError):r.call()
    assert (r.frozen(),d.native.read_versioned())==saved and r.dispatches()==0


def test_old_document_token_change_between_appends_stops_financial_writes():
    from staging_shadow.mandatory_receipt_gate import RetainedHistoryReader
    from test_legacy_contract_poc import mem
    r=Rig();old=mem(dict(schema='receipt-reimport-v1',confirmation_items={},records={}))
    r.c._history=RetainedHistoryReader(lambda:deepcopy(r.history),legacy_ports=(old,))
    def change(title,number):
        if number==1:old.revision+=1
    r.c.writer.fault=change
    with pytest.raises(StateError):r.call()
    assert r.backends[1].writes==1 and r.dispatches()==1
