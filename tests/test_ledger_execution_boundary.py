"""Negative launch/SDK/update tests plus the unchanged receipt writers."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import sys
from threading import Barrier
from uuid import uuid4

import pytest

from app.drive_run_state import StateError
from app.receipt_confirmation_contract import encoded
from staging_shadow.auto_native_evidence import load
from staging_shadow.ledger_execution_boundary import (BASE,HISTORICAL,UPDATES,LEGACY,DRAIN,DeniedSink)
from ledger_execution_fixtures import Rig,PolicyRig,joint_r1,historical,DeniedNative
from test_legacy_contract_poc import mem


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    import socket
    import googleapiclient.discovery
    def deny(*args,**kwargs):pytest.fail('external IO entered')
    monkeypatch.setattr(socket.socket,'connect',deny)
    monkeypatch.setattr(googleapiclient.discovery,'build',deny)
    modules=set(sys.modules)
    yield
    for name in set(sys.modules)-modules:
        if name.startswith(('_kakeibo_accounting_sandbox_','_r256_historical_')):sys.modules.pop(name,None)


@pytest.mark.parametrize('entry',LEGACY+UPDATES)
@pytest.mark.parametrize('sha',[BASE,HISTORICAL])
def test_unmigrated_launch_refused_even_with_modern_SHA(entry,sha):
    r=PolicyRig();saved=r.frozen()
    for origin in ('workflow','CLI','old_host','local_scheduler','auth_session_test_tree'):
        with pytest.raises(StateError,match='legacy_launch_unmigrated'):
            r.boundary.legacy_launch(entry,checkout_sha=sha,origin=origin)
    assert r.frozen()==saved and r.dispatches()==0


@pytest.mark.parametrize('entry',UPDATES)
@pytest.mark.parametrize('target',['same_id','different_id','whole_table','independent_ledger'])
def test_m6_target_adapter_never_grants_financial_permission(entry,target):
    r=PolicyRig();native=mem(dict(requests={}));saved=r.frozen(),native.read_versioned()
    identity='' if target=='whole_table' else 'R-synthetic-shared-target'
    result=r.boundary.inspect_update(entry,ledger_id='synthetic-other-ledger' if target=='independent_ledger' else r.boundary.ledger_id,
        source_identity=dict(namespace=entry,identity='synthetic-bank-or-payment-ID',version='native-1'),native_io=native,
        targets=[dict(table='支出明細',id=identity if target!='different_id' else 'other-id',row=None,columns=[4,5,6])])
    assert result['classification']=='DRAIN_REQUIRED' and result['financial_dispatches']==0
    assert result['relation']==('independent_ledger_unconnected' if target=='independent_ledger' else 'shared_full_snapshot')
    assert tuple(result['requirements'])==DRAIN and not result['retry_allowed']
    for spoof in ({},{'drained':True},{'authority':'old-v1','approved':True}):
        with pytest.raises(StateError,match='M6_financial_stage_drain_required'):
            r.boundary.execute_update(entry,result,**spoof)
    assert (r.frozen(),native.read_versioned())==saved


@pytest.mark.parametrize('entry',['normal_auto','buyback_income'])
def test_existing_auto_success_under_common_boundary(entry):
    r=Rig();ids=(str(uuid4()),str(uuid4()))
    assert r.call(entry,ids=ids,revision=0)['classification']=='COMPLETE'
    saved=r.frozen();assert r.call(entry,ids=ids,revision=0)['report']['classification']=='COMPLETE'
    assert r.dispatches()==1 and r.backends[1].writes==3 and r.frozen()==saved
    prior=next(iter(r.c.shared.read()[0]['operations'].values()))
    with r.boundary._run(r.c):r.boundary._completed_native(prior,r.boundary._active.get())
    assert r.frozen()==saved


@pytest.mark.parametrize('kind',['normal','medical'])
def test_existing_r1_success_under_same_boundary(kind,monkeypatch):
    r=Rig();d=joint_r1(r,monkeypatch,kind)
    result=r.c.submit_r1(d.contract,d.c)
    assert result['classification']=='COMPLETE' and result['writer_dispatches']==1
    saved=r.frozen(),d.native.read_versioned()
    assert r.c._receipt.last_r1_writer.inspect(d.c)['classification']=='COMPLETE'
    assert (r.frozen(),d.native.read_versioned())==saved
    prior=next(iter(r.c.shared.read()[0]['operations'].values()))
    with r.boundary._run(r.c):r.boundary._completed_native(prior,r.boundary._active.get())
    assert (r.frozen(),d.native.read_versioned())==saved


@pytest.mark.parametrize('entry',['A07','A08','A04','A09','A10','A11','A12','A13'])
def test_bank_paypay_amazon_manual_category_blocked_during_r1(entry,monkeypatch):
    r=Rig();d=joint_r1(r,monkeypatch);calls=[]
    def inspect(point):
        if point=='before_dispatch':
            saved=r.frozen(),d.native.read_versioned()
            report=r.boundary.inspect_update(entry,ledger_id=r.boundary.ledger_id,source_identity=dict(kind=entry,id='different-source'),
                native_io=mem(dict(requests={})),targets=[dict(table='支出明細',id=d.c['plan'][1][1][0],row=None,columns=[4,5,6])])
            with pytest.raises(StateError):r.boundary.execute_update(report)
            assert (r.frozen(),d.native.read_versioned())==saved;calls.append(report)
    assert r.c.submit_r1(d.contract,d.c,checkpoint=inspect)['classification']=='COMPLETE'
    assert len(calls)==1 and calls[0]['relation']=='shared_full_snapshot'


@pytest.mark.parametrize('method',['append','append_raw','update_row','update_row_raw','update_rows','set_raw_range',
    'update_expense_fields','update_expense_categories','write','save','replace','release_after_reconciliation','initialize'])
def test_terminal_db_and_native_sinks_denied(method):
    sink=DeniedSink()
    with pytest.raises(StateError,match='uncontrolled_sink'):getattr(sink,method)('synthetic',[])
    assert sink.denials==1


@pytest.mark.parametrize('method',['append','update','batchUpdate','clear'])
def test_sdk_bypass_is_denied(method):
    sink=DeniedSink()
    with pytest.raises(StateError,match='uncontrolled_sink'):
        getattr(sink.spreadsheets().values(),method)(spreadsheetId='synthetic-ledger',body={})
    assert sink.denials==1


def test_direct_148_composition_or_backend_cannot_bypass_outer_gate():
    r=Rig();saved=r.frozen()
    for p in (r.boundary._shared_port,r.boundary._ledger_port):
        raw,tag=p.read_versioned()
        with pytest.raises(StateError):p.replace_versioned(raw,tag,raw)
    with pytest.raises(StateError):r.c._receipt.submit_auto('normal_auto','u1',str(uuid4()),str(uuid4()),0)
    assert r.frozen()==saved and r.dispatches()==0


@pytest.mark.parametrize('count',[2,16])
def test_shared_gate_parallel_maximum_one_dispatch(count):
    r=Rig();instances=[r.c]+[r.new(fresh=True) for _ in range(count-1)];barrier=Barrier(count)
    for c in instances:c.adapter.fault=lambda p:barrier.wait(timeout=30) if p=='before_shared_claim' else None
    def call(n):
        try:return r.call('normal_auto' if n%2==0 else 'buyback_income',c=instances[n],revision=0)['classification']
        except StateError:return 'HELD'
    with ThreadPoolExecutor(max_workers=count) as pool:results=list(pool.map(call,range(count)))
    assert results.count('COMPLETE')==1 and r.dispatches()==1 and r.backends[1].writes==3


class Crash(BaseException):pass


@pytest.mark.parametrize('point',['before_shared_claim','after_shared_claim','after_writer_start_evidence',
    'after_all_ledger','after_native_complete','after_shared_update'])
def test_crash_reconstruction_lookup_only(point):
    r=Rig();ids=(str(uuid4()),str(uuid4()))
    def stop(p):
        if p==point:raise Crash()
    r.c.adapter.fault=stop
    with pytest.raises(Crash):r.call(ids=ids,revision=0)
    saved=r.frozen();c=r.new()
    assert c.report(ids[0])['retry_allowed'] is False
    try:r.call(c=c,ids=ids,revision=0)
    except StateError:pass
    with pytest.raises(StateError):r.call(c=c)
    assert r.frozen()==saved and r.dispatches()<=1


@pytest.mark.parametrize('row',[1,2,3])
def test_partial_or_all_rows_no_completion_never_retries(row):
    r=Rig();ids=(str(uuid4()),str(uuid4()))
    def stop(title,number):
        if number==row:raise RuntimeError('synthetic stop')
    r.c.writer.fault=stop
    with pytest.raises(StateError):r.call(ids=ids,revision=0)
    assert r.backends[1].writes==row and r.dispatches()==1
    saved=r.frozen();c=r.new()
    assert c.report(ids[0])['classification']==('PARTIAL' if row<3 else 'UNKNOWN')
    assert r.call(c=c,ids=ids,revision=0)['report']['retry_allowed'] is False
    assert r.frozen()==saved


@pytest.mark.parametrize('role,index',[('shared',1),('shared',4),('native',1),('native',4),('ledger',1),('ledger',2),('ledger',3)])
@pytest.mark.parametrize('fault',['412','timeout','lost'])
def test_cas_faults_have_zero_cleanup_writes(role,index,fault,monkeypatch):
    r=Rig();io=r.backends[{'shared':0,'native':2,'ledger':1}[role]];original=io.replace_versioned;calls=[]
    def fail(*args):
        calls.append(1)
        if len(calls)!=index:return original(*args)
        if fault=='lost':original(*args)
        if fault=='412':raise StateError('HTTP_412')
        raise TimeoutError('synthetic response')
    monkeypatch.setattr(io,'replace_versioned',fail);ids=(str(uuid4()),str(uuid4()))
    with pytest.raises((StateError,TimeoutError)):r.call(ids=ids,revision=0)
    saved=r.frozen();c=r.new();c.report(ids[0])
    try:r.call(c=c,ids=ids,revision=0)
    except StateError:pass
    assert len(calls)==index and r.frozen()==saved and r.dispatches()<=1


@pytest.mark.parametrize('what',['values','formula','row_order','id','extra_table'])
def test_uncontrolled_change_between_writer_rows_detected(what):
    r=Rig()
    def change(title,number):
        if number!=1:return
        io=r.backends[1];v=load(io.raw)
        if what=='extra_table':
            for part in v['snapshot']:part['synthetic-admin']=[['other','new']]
        else:
            part=v['snapshot'][1 if what=='formula' else 0]['レシート']
            if what=='row_order':part.insert(0,['unexpected-row'])
            elif what=='id':part[0][0]='different-id'
            else:part[0][3]='=1+2' if what=='formula' else 999
        io.raw=encoded(v);io.revision+=1
    r.c.writer.fault=change
    with pytest.raises(StateError):r.call()
    assert r.backends[1].writes==1 and r.dispatches()==1


@pytest.mark.parametrize('state',['pending','applied','UNKNOWN','partial'])
def test_any_unmapped_retained_update_holds_receipt_without_replay(state):
    old=mem(dict(requests={'old':dict(state=state)}));r=Rig(retained=(old,));saved=r.frozen(),old.read_versioned()
    with pytest.raises(StateError,match='native_reconciliation'):r.call()
    assert (r.frozen(),old.read_versioned())==saved and r.dispatches()==0


@pytest.mark.parametrize('pdf',['pdf_auto','fixed_canary','explicit_unit'])
def test_pdf_ownership_fence_retained(pdf):
    r=Rig();r.history['records']=[r.h.reader.observe(pdf,'u1').value()];saved=r.frozen()
    with pytest.raises(StateError):r.call()
    assert r.frozen()==saved and r.dispatches()==0


def test_weak_backend_does_not_qualify_by_readback():
    from staging_shadow.ledger_execution_boundary import LedgerExecutionBoundary
    r=Rig();r.backends[1].supports_strong_cas=False
    with pytest.raises(StateError,match='strong_CAS_backend_required'):
        LedgerExecutionBoundary(ledger_id='synthetic-ledger',shared_io=r.backends[0],ledger_io=r.backends[1],
            retained_update_ports=(),enabled=True,isolated_synthetic=True)


@pytest.mark.parametrize('state',['queued','pending','applied','UNKNOWN'])
def test_existing_daily_correction_reader_is_readonly_and_never_dispatches(state):
    from test_daily_corrections import setup,REQUEST
    store,reader,_,db,inbox=setup()
    inbox.prepare(REQUEST,'a',{'amount':120})  # Fixture provisioning only.
    store.data['corrections']['requests'][REQUEST]['state']=state
    r=PolicyRig();v=load(r.backends[1].raw)
    for part in v['snapshot']:part['支出明細']=deepcopy(reader.rows)
    r.backends[1].raw=encoded(v)
    native=mem(store.data['corrections'])
    saved=deepcopy(store.data),r.frozen(),native.read_versioned(),deepcopy(reader.rows)
    if state=='queued':
        report=r.boundary.inspect_daily_correction(store,db,REQUEST,native_io=native)
        assert report['classification']=='DRAIN_REQUIRED'
        assert report['adapter']=='existing_DailyCorrections_readers'
        assert report['targets'][0]['id']=='a' and report['targets'][0]['row']==2
        with pytest.raises(StateError):r.boundary.execute_update(report)
    else:
        with pytest.raises(StateError,match='not_new_queued'):
            r.boundary.inspect_daily_correction(store,db,REQUEST,native_io=native)
    assert (store.data,r.frozen(),native.read_versioned(),reader.rows)==saved and db.calls==[]


@pytest.mark.parametrize('number',[2,3,None])
def test_fixed_ID_row_and_formula_observation_never_becomes_a_write(number):
    r=PolicyRig();v=load(r.backends[1].raw)
    v['snapshot'][0]['支出明細']=[['a','value']]
    v['snapshot'][1]['支出明細']=[['a','=SUM(B2:B9)']]
    r.backends[1].raw=encoded(v);saved=r.frozen()
    kwargs=dict(ledger_id=r.boundary.ledger_id,source_identity={'native_id':'a'},native_io=mem({}),
        targets=[dict(table='支出明細',id='a',row=number,columns=[1])])
    if number==3:
        with pytest.raises(StateError,match='row_identity_changed'):r.boundary.inspect_update('A11',**kwargs)
    else:
        result=r.boundary.inspect_update('A11',**kwargs)
        assert result['targets'][0]['formulas']==[(2,['a','=SUM(B2:B9)'])]
        with pytest.raises(StateError):r.boundary.execute_update(result)
    assert r.frozen()==saved


def test_reviewed_pins_cannot_be_silently_relaxed(monkeypatch):
    import staging_shadow.ledger_execution_boundary as b
    monkeypatch.setitem(b.PINS,'staging_shadow/mandatory_receipt_gate.py','0'*64)
    with pytest.raises(StateError,match='runtime_pin_changed'):PolicyRig()


@pytest.mark.parametrize('state',['accepted','executing','awaiting_readback','partial_unknown','held','complete'])
def test_independent_source_and_ID_still_share_full_ledger_fence(state):
    from dataclasses import asdict
    from app.iphone_accounting_gate import AccountingProbeRequest,SCHEMA
    from app.iphone_accounting_ownership import relation
    from auto_native_fixtures import NOW
    r=Rig();current=r.h.reader.observe('normal_auto','u1').value();old=deepcopy(current)
    old['source']['source_id']='synthetic-unrelated-bank-or-receipt'
    old['source']['sha256']='b'*64;old['partition']['source']=deepcopy(old['source'])
    from app.page_receipt_model import SourceRef,stable_page
    source=SourceRef(source_file_id=old['source']['source_id'],source_content_hash=old['source']['sha256'],
        source_kind=old['source']['source_kind'],page_count=old['source']['page_count'])
    for unit in old['partition']['units']:
        for member in unit['members']:member['identity']=stable_page(source,member['page'])
    old['operation_identity']='e'*64;old['ledger_targets']=['different-ID-in-same-ledger']
    assert relation(old,current)=='independent'  # #148 source relation alone permits this.
    req=AccountingProbeRequest.parse(dict(schema=SCHEMA,synthetic_only=True,owner=old['owner'],
        request_id=str(uuid4()),expected_state_revision=0,operation_identity=old['operation_identity'],
        ownership=old,native_preflight_digest='a'*64))
    root=dict(schema='unified-receipt-requests-v1',revision=1,operations={req.operation_identity:
        dict(request=asdict(req),fingerprint=req.fingerprint(),state=state,events=[dict(state=state,at=NOW)])})
    r.backends[0].raw=encoded(root);saved=r.frozen()
    reason=('complete_shared_without_native_proof' if state=='complete' else
            'whole_ledger_inflight_or_UNKNOWN|mandatory_UNKNOWN_read_only')
    with pytest.raises(StateError,match=reason):r.call(revision=1)
    assert r.frozen()==saved and r.dispatches()==0


def test_direct_sink_refused_even_while_valid_receipt_context_is_active():
    r=Rig();denied=[]
    def attempt(point):
        if point=='before_shared_claim':
            raw,tag=r.boundary._ledger_port.read_versioned()
            with pytest.raises(StateError,match='mandatory_receipt_context_required'):
                r.boundary._ledger_port.replace_versioned(raw,tag,raw)
            denied.append(True)
    r.c.adapter.fault=attempt
    assert r.call()['classification']=='COMPLETE'
    assert denied==[True] and r.dispatches()==1 and r.backends[1].writes==3


@pytest.mark.parametrize('page',[14,4,10])
def test_actual_old_canary_body_stops_at_native_boundary_before_writer(page):
    import test_p14_posting as p14
    import test_receipt_unit_posting as unit
    case=p14 if page==14 else unit;args=case.setup() if page==14 else case.setup(page)
    native=args[1];saved=deepcopy(native.value);sink=DeniedSink()
    with pytest.raises(StateError):case.apply((args[0],DeniedNative(native,sink),args[2],args[3]))
    assert sink.denials>=1 and native.value==saved and native.writes==0 and args[2].calls==[]


def test_historical_checkout_really_contains_ungated_writer_but_shared_sink_denies(tmp_path):
    old=historical(tmp_path);pipeline=old('receipt_pipeline');planning=old('receipt_plan.planning');models=old('models')
    from auto_native_fixtures import GOLDEN
    parsed=models.ReceiptResult.model_validate(GOLDEN['normal_auto']['inputs']['parsed'])
    categories=[tuple(p) for p in GOLDEN['normal_auto']['inputs']['categories']]
    planner=planning.PlanningDB(categories)
    pipeline.ReceiptPipeline(planner,None,clock=lambda:'synthetic-time')._materialize_result(parsed,'synthetic-old-source','',[])
    assert len(planner.plan)>=3  # Counterfactual PLAN, no persisted financial write.
    sink=DeniedSink()
    class Blocked(planning.PlanningDB):
        def append(self,*args):sink.append(*args)
        append_raw=append
    with pytest.raises(StateError,match='uncontrolled_sink'):
        pipeline.ReceiptPipeline(Blocked(categories),None,clock=lambda:'synthetic-time')._materialize_result(parsed,'synthetic-old-source','',[])
    assert sink.denials==1
