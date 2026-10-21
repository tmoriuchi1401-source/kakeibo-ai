"""Actual unchanged native bodies, independent stores and injected crash races."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from uuid import uuid4
import sys

import pytest

from app.drive_run_state import StateError
from staging_shadow.auto_native_evidence import load
from staging_shadow.isolated_update_adapters import ENTRIES, Envelope, _DB
from isolated_update_fixtures import Rig, REQUEST, mutate


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    import socket
    import googleapiclient.discovery
    def forbidden(*args, **kwargs): pytest.fail('external IO forbidden')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(googleapiclient.discovery, 'build', forbidden)
    names=set(sys.modules)
    yield
    for name in set(sys.modules)-names:
        if name.startswith(('_kakeibo_updates_', '_kakeibo_accounting_sandbox_')): sys.modules.pop(name,None)


@pytest.mark.parametrize('entry', ENTRIES)
def test_actual_native_success_replay_and_independent_readback(entry):
    r=Rig(entry); c=r.candidate()
    assert r.adapter.execute(c)['state']=='complete'
    assert load(r.ledger_io.raw)['snapshot']==c.after
    assert r.boundary.source.read()[0]['payload']==c.source_after
    assert r.adapter.dispatches==[c.operation]
    saved=r.frozen()
    with pytest.raises(StateError):r.adapter.execute(c)
    assert r.frozen()==saved
    assert r.new().inspect(c.request_id)['classification']=='COMPLETE'
    assert r.frozen()==saved
    if entry=='A10':
        assert r.boundary.native.read()[0]['payload']['corrections']['requests'][REQUEST]['state']=='applied'
    else:
        native=r.boundary.native.read()[0]['payload']['production']
        assert all(v['phase']=='ready' for v in native['sources'].values())


@pytest.mark.parametrize('entry', ENTRIES)
@pytest.mark.parametrize('part', ['ID','row','value','formula','source','native'])
def test_all_independent_changes_refuse_before_dispatch(entry,part):
    r=Rig(entry); c=r.candidate()
    if part in {'ID','row','value','formula'}:
        def change(v):
            rows=v['snapshot'][1 if part=='formula' else 0]['支出明細']
            if part=='row':rows.insert(0,deepcopy(rows[0]))
            else:rows[0][0 if part=='ID' else 4]='synthetic-changed' if part=='ID' else '=1+7' if part=='formula' else 9
        mutate(r.ledger_io,change)
    else:mutate(r.source_io if part=='source' else r.native_io,lambda v:v.update(revision=v['revision']+1))
    saved=r.frozen()
    with pytest.raises(StateError):r.adapter.execute(c)
    assert r.frozen()==saved and r.adapter.dispatches==[]


@pytest.mark.parametrize('entry', ENTRIES)
@pytest.mark.parametrize('point',['after_shared_claim','after_native_pending','after_writer_start',
                                 'after_write_1','after_all_rows','after_native_complete'])
def test_crash_retains_fence_no_retry_or_automatic_native_repair(entry,point):
    r=Rig(entry); c=r.candidate()
    def crash(name):
        if name==point:raise TimeoutError('synthetic process death')
    r.adapter.fault=crash
    with pytest.raises(TimeoutError):r.adapter.execute(c)
    saved=r.frozen(); count=len(r.adapter.dispatches)
    assert count<=1
    restarted=r.new()
    report=restarted.inspect(c.request_id)
    assert report['classification'] in {'UNKNOWN','PARTIAL'} and not report['retry_allowed']
    for _ in range(2):
        with pytest.raises(StateError):restarted.execute(c)
    assert r.frozen()==saved and restarted.dispatches==[]


@pytest.mark.parametrize('entry',ENTRIES)
@pytest.mark.parametrize('port',['shared_io','ledger_io','native_io','evidence_io'])
@pytest.mark.parametrize('mode',['412','timeout_before','lost_after'])
def test_CAS_unknown_never_retries_or_releases(entry,port,mode):
    r=Rig(entry); c=r.candidate(); io=getattr(r,port)
    original=io.replace_versioned
    def fault(*args):
        if mode=='lost_after':original(*args)
        if mode=='412':raise StateError('HTTP_412')
        raise TimeoutError('synthetic ambiguous CAS')
    io.replace_versioned=fault
    with pytest.raises((StateError,TimeoutError)):r.adapter.execute(c)
    saved=r.frozen(); count=len(r.adapter.dispatches)
    io.replace_versioned=original
    # Before durable claim a new, separately reviewed operation could be tried;
    # THIS accepted candidate is never automatically retried by the adapter.
    assert count<=1 and r.frozen()==saved


@pytest.mark.parametrize('entry',ENTRIES)
@pytest.mark.parametrize('parallel',[2,16])
@pytest.mark.parametrize('same_operation',[True,False])
def test_same_and_different_source_full_snapshot_conflict(entry,parallel,same_operation):
    r=Rig(entry); c=r.candidate()
    candidates=[c if same_operation else r.candidate() for _ in range(parallel)]
    barrier=Barrier(parallel)
    def point(name):
        if name=='before_claim':barrier.wait(timeout=20)
    r.adapter.fault=point
    def execute(candidate):
        try:return r.adapter.execute(candidate)
        except StateError:return None
    with ThreadPoolExecutor(max_workers=parallel) as pool:results=list(pool.map(execute,candidates))
    assert sum(x is not None for x in results)==1
    assert len(r.adapter.dispatches)==1 and len(set(r.adapter.dispatches))==1
    assert load(r.ledger_io.raw)['snapshot']==c.after


@pytest.mark.parametrize('state',['pending','UNKNOWN','PARTIAL'])
def test_old_pending_native_is_not_inferred_from_actions_or_reset(state):
    r=Rig('A10')
    mutate(r.native_io,lambda v:v['payload']['corrections']['requests'][REQUEST].update(state=state))
    saved=r.frozen()
    with pytest.raises(StateError):r.candidate()
    assert r.frozen()==saved and r.adapter.dispatches==[]


@pytest.mark.parametrize('entry',ENTRIES)
def test_private_native_entry_and_live_sink_refused(entry):
    r=Rig(entry); module,cls=ENTRIES[entry]
    with pytest.raises(StateError,match='direct_sink_refused'):
        getattr(r.adapter.module(module),cls).apply(None)
    with pytest.raises(StateError,match='memory_DB_required'):
        r.adapter._invoke(entry,object(),{},(),{})
    with pytest.raises(StateError):r.boundary.execute_update(entry,approved=True)
    assert r.adapter.dispatches==[]


@pytest.mark.parametrize('entry',['A04','A05','A06','A07','A08','A09','A11','A12','A13'])
def test_other_groups_stay_unconnected(entry):
    r=Rig(); saved=r.frozen()
    with pytest.raises(StateError,match='unconnected_entry'):r.adapter.prepare(entry,str(uuid4()))
    assert r.frozen()==saved


def test_apply_pending_is_bounded_fixed_ID_and_never_recovers_pending():
    r=Rig('A10')
    assert len(r.adapter.apply_pending(limit=1))==1
    assert r.adapter.apply_pending(limit=1)==[]
    assert len(r.adapter.dispatches)==1


def test_bank_native_canary_validator_is_mandatory():
    r=Rig('A03-bank');saved=r.frozen()
    with pytest.raises(RuntimeError,match='expected_head_mismatch'):
        r.adapter.prepare('A03-bank',str(uuid4()),arguments=(('synthetic-bank-1',),),
            options={'head':'a'*40,'expected_head':'b'*40})
    assert r.frozen()==saved


def test_original_core_order_and_classification_golden():
    r=Rig('A01')
    assert len(r.adapter.apply_core())==3
    rows=load(r.ledger_io.raw)['snapshot'][0]
    assert len(rows['支出明細'])==2
    assert rows['支出明細'][-1][5:7]==['食費','食料品']
    assert rows['取込データ'][0][8]=='manual_expense'
    assert len(r.adapter.dispatches)==3


@pytest.mark.parametrize('parallel',[2,16])
def test_different_native_sources_compete_for_the_same_full_snapshot(parallel):
    r=Rig('A10'); a=r.candidate()
    b=r.adapter.prepare('A03',str(uuid4()))
    barrier=Barrier(parallel)
    r.adapter.fault=lambda point:barrier.wait(timeout=20) if point=='before_claim' else None
    def call(n):
        try:return r.adapter.execute(a if n%2 else b)
        except StateError:return None
    with ThreadPoolExecutor(max_workers=parallel) as pool:out=list(pool.map(call,range(parallel)))
    assert sum(x is not None for x in out)==1 and len(r.adapter.dispatches)==1
    assert load(r.ledger_io.raw)['snapshot'] in [a.after,b.after]


def attach_receipts(r, receipt):
    receipt.boundary=r.boundary;receipt.instances=[];receipt.new(fresh=True)
    from mandatory_gate_fixtures import with_headers
    from staging_shadow.auto_native_evidence import observation_token
    receipt.h.baseline=with_headers(load(r.ledger_io.raw)['snapshot'])
    receipt.h.before_observation=observation_token(*receipt.c.auto_ledger.read_versioned())


@pytest.mark.parametrize('entry',['normal_auto','buyback_income'])
def test_receipt_inflight_holds_updates_and_completion_allows_them(entry):
    from ledger_execution_fixtures import Rig as ReceiptRig
    receipt=ReceiptRig();r=Rig(receipt=receipt);attach_receipts(r,receipt)
    observed=[]
    def checkpoint(point):
        if point=='after_writer_start_evidence':
            saved=r.frozen()
            with pytest.raises(StateError):r.candidate()
            assert r.frozen()==saved;observed.append(point)
    receipt.c.adapter.fault=checkpoint
    assert receipt.call(entry,revision=0)['classification']=='COMPLETE'
    assert observed
    c=r.candidate();r.adapter.execute(c)
    assert r.new().inspect(c.request_id)['phase']=='complete'


@pytest.mark.parametrize('entry',['normal_auto','buyback_income'])
def test_update_pending_holds_receipts_before_any_dispatch(entry):
    from ledger_execution_fixtures import Rig as ReceiptRig
    receipt=ReceiptRig();r=Rig(receipt=receipt);attach_receipts(r,receipt)
    c=r.candidate();points=[]
    def probe(point):
        if point=='after_writer_start':
            saved=r.frozen()
            with pytest.raises(StateError):receipt.call(entry,revision=receipt.c.shared.read()[0]['revision'])
            assert r.frozen()==saved and receipt.dispatches()==0;points.append(point)
    r.adapter.fault=probe;r.adapter.execute(c)
    assert points and len(r.adapter.dispatches)==1


@pytest.mark.parametrize('kind',['normal','medical'])
def test_existing_R1_normal_medical_same_boundary_and_update_fence(kind,monkeypatch):
    from ledger_execution_fixtures import Rig as ReceiptRig,joint_r1
    receipt=ReceiptRig();r=Rig(receipt=receipt);attach_receipts(r,receipt)
    d=joint_r1(receipt,monkeypatch,kind);seen=[]
    def probe(point):
        if point=='before_dispatch':
            saved=r.frozen()
            for entry in ENTRIES:
                with pytest.raises(StateError):r.adapter.prepare(entry,str(uuid4()))
            assert r.frozen()==saved;seen.append(point)
    result=receipt.c.submit_r1(d.contract,d.c,checkpoint=probe)
    assert result['classification']=='COMPLETE' and result['writer_dispatches']==1
    assert seen==['before_dispatch']
    c=r.candidate();r.adapter.execute(c)
    assert r.adapter.inspect(c.request_id)['phase']=='complete'


def test_A10_preserves_unmodified_formula_cells_and_unrelated_rows():
    r=Rig('A10')
    mutate(r.ledger_io,lambda v:v['snapshot'][1]['支出明細'][0].__setitem__(11,'="kept"'))
    c=r.candidate();r.adapter.execute(c)
    actual=load(r.ledger_io.raw)['snapshot']
    assert actual[1]['支出明細'][0][11]=='="kept"'
    assert actual[0]['支出明細'][0][4]==8


def test_arbitrary_writer_callback_cannot_be_used_as_a_plan_sink():
    r=Rig();c=r.candidate();calls=[]
    with pytest.raises(StateError,match='direct_sink_refused'):
        _DB(c.after,c.source_after,lambda operation:calls.append(operation))
    assert calls==[]
    db=_DB(c.after,c.source_after);db.sink=lambda operation:calls.append(operation)
    with pytest.raises(StateError,match='direct_sink_refused'):r.adapter._invoke('A03',db,{},(),{})
    assert calls==[]


@pytest.mark.parametrize('entry',['A01','A02','A03','A03-bank','A10'])
def test_each_adapter_competes_with_receipt_and_buyback_full_snapshot(entry):
    from ledger_execution_fixtures import Rig as ReceiptRig
    for kind in ('normal_auto','buyback_income'):
        receipt=ReceiptRig();r=Rig(entry,receipt=receipt);attach_receipts(r,receipt)
        candidate=r.candidate();barrier=Barrier(2)
        r.adapter.fault=lambda p:barrier.wait(timeout=30) if p=='before_claim' else None
        receipt.c.adapter.fault=lambda p:barrier.wait(timeout=30) if p=='before_shared_claim' else None
        def update():
            try:return r.adapter.execute(candidate)
            except StateError:return None
        def post():
            try:return receipt.call(kind,revision=0)
            except StateError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(update);b=pool.submit(post);a.result();b.result()
        assert len(r.adapter.dispatches)+receipt.dispatches()<=1
        # A conservative both-held outcome is acceptable; never retry either
        # accepted uncertain prefix to manufacture liveness.


def test_unrelated_formula_change_after_first_write_retains_partial_fence():
    r=Rig();c=r.candidate()
    def corrupt(point):
        if point=='after_write_1':
            mutate(r.ledger_io,lambda v:v['snapshot'][1]['支出明細'][0].__setitem__(11,'="changed"'))
    r.adapter.fault=corrupt
    with pytest.raises(StateError):r.adapter.execute(c)
    saved=r.frozen();assert len(r.adapter.dispatches)==1
    assert not r.new().inspect(c.request_id)['retry_allowed']
    assert r.frozen()==saved


@pytest.mark.parametrize('mode',['412','timeout_before','lost_after'])
def test_review_auxiliary_store_CAS_failure_is_a_separate_held_prefix(mode):
    r=Rig('A01');c=r.candidate();original=r.source_io.replace_versioned
    def fault(*args):
        if mode=='lost_after':original(*args)
        if mode=='412':raise StateError('HTTP_412')
        raise TimeoutError('synthetic source response')
    r.source_io.replace_versioned=fault
    with pytest.raises((StateError,TimeoutError)):r.adapter.execute(c)
    r.source_io.replace_versioned=original;saved=r.frozen()
    assert len(r.adapter.dispatches)==1
    assert not r.new().inspect(c.request_id)['retry_allowed']
    with pytest.raises(StateError):r.adapter.execute(c)
    assert r.frozen()==saved


@pytest.mark.parametrize('field,value',[('cells',[[0,'other-ID']]),('after','0'*64),
    ('changes',{'source':'different'}),('cells',[[10,'different-import']])])
def test_correction_reuses_native_prepare_validator_for_immutable_request(field,value):
    r=Rig('A10')
    mutate(r.native_io,lambda v:v['payload']['corrections']['requests'][REQUEST].update({field:value}))
    saved=r.frozen()
    with pytest.raises(Exception):r.candidate()
    assert r.frozen()==saved and r.adapter.dispatches==[]


def test_duplicate_review_ID_never_reaches_the_first_writer():
    r=Rig('A01')
    mutate(r.source_io,lambda v:v['payload']['tables']['要確認'].append(deepcopy(v['payload']['tables']['要確認'][0])))
    saved=r.frozen()
    with pytest.raises(StateError):r.candidate()
    assert r.frozen()==saved and r.adapter.dispatches==[]
