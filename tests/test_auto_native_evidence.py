from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from uuid import uuid4
import json

import pytest

from app.drive_run_state import StateError
from app.iphone_accounting_gate import ENTRIES
from app.unified_receipt_review import digest
from staging_shadow.auto_native_evidence import (POINTS,NativeEvidenceJournal,SyntheticAutoEvidenceAdapter,
    request,ledger_view,ledger_outcome)
from staging_shadow.accounting_recovery_model import Crash
from auto_native_fixtures import Harness,encoded


@pytest.fixture(autouse=True,scope='module')
def block_live_and_existing_writers():
    from google.cloud import firestore
    from googleapiclient import discovery
    from app.receipt_pipeline import ReceiptPipeline
    from app.receipt_confirmation import ReceiptConfirmation
    from app.sheets import SheetsDB
    from app.pdf_intake_runner import ScopedWriter
    def forbidden(*args,**kwargs):pytest.fail('No existing writer/client in new composition, including planning calls')
    monkeypatch=pytest.MonkeyPatch()
    for cls,name in ((firestore,'Client'),(discovery,'build'),(ReceiptPipeline,'_materialize_result'),
                     (ReceiptPipeline,'process_bytes'),(ReceiptConfirmation,'_write_accounting_plan'),
                     (ReceiptConfirmation,'apply_confirmations'),(SheetsDB,'__init__'),(ScopedWriter,'append')):
        monkeypatch.setattr(cls,name,forbidden)
    yield
    monkeypatch.undo()


@pytest.fixture(scope='module')
def private_namespace():
    seed=Harness()
    yield seed.sandbox
    seed.sandbox.close()


@pytest.fixture
def h(private_namespace):
    harness=Harness(sandbox=private_namespace)
    yield harness
    assert all(n<=1 for n in Counter(harness.adapter.probe_calls).values())
    assert all(n<=1 for n in Counter(o for _,_,o in harness.adapter.boundary_calls).values())


@pytest.mark.parametrize('kind',['normal_auto','buyback_income'])
def test_completed_synthetic_evidence_and_exact_replay_after_restart(h,kind):
    nid,sid=str(uuid4()),str(uuid4());h.call(kind,nid,sid,0)
    record=next(iter(h.journal.read()[0]['operations'].values()))
    assert [e['phase'] for e in record['events']]==['claimed','writer_ready','writer_started','complete']
    assert record['request']['ownership']['scope']=='whole' and record['request']['ownership']['unit'] is None
    assert record['request']['kind']==kind and record['request']['input_digest']==digest(record['request']['inputs'])
    assert record['validation']['native_authority'] is False
    before=h.native_io.read_versioned(),h.financial.read_versioned(),h.ledger_io.read_versioned()
    h.restart();result=h.call(kind,nid,sid,0)
    assert result['replay'] and result['report']['classification']=='COMPLETE'
    assert result['report']['real_native_complete'] is result['report']['native_authority'] is False
    assert not result['report']['retry_allowed'] and len(h.adapter.probe_calls)==1
    assert before==(h.native_io.read_versioned(),h.financial.read_versioned(),h.ledger_io.read_versioned())


@pytest.mark.parametrize('kind',['normal_auto','buyback_income'])
@pytest.mark.parametrize('point',POINTS)
@pytest.mark.parametrize('mode',['crash','timeout','lost_response','stale_revision'])
def test_all_crash_boundaries_keep_fences_and_never_resume(h,kind,point,mode):
    nid,sid=str(uuid4()),str(uuid4())
    def fail(p):
        if p==point:
            if mode=='crash':raise Crash()
            if mode=='stale_revision':raise StateError('HTTP_412')
            raise TimeoutError('synthetic lost response')
    h.fault=fail
    with pytest.raises((Crash,StateError,TimeoutError)):h.call(kind,nid,sid,0)
    count=len(h.adapter.probe_calls);boundaries=len(h.adapter.boundary_calls)
    h.restart();before=h.native_io.read_versioned(),h.financial.read_versioned(),h.ledger_io.read_versioned()
    report=h.adapter.report(nid)
    assert before==(h.native_io.read_versioned(),h.financial.read_versioned(),h.ledger_io.read_versioned())
    expected=('NOT_EXECUTED' if point in {'after_native_evidence','after_native_validation','before_writer_start_evidence'}
        else 'PARTIAL' if point in {'after_probe','after_partial_ledger'}
        else 'COMPLETE' if point in {'after_native_complete','after_shared_update'} else 'UNKNOWN')
    assert report['classification']==expected,report
    assert not report['retry_allowed'] and not report['accounting_permission']
    if h.journal.read()[0]['operations']:
        assert h.call(kind,nid,sid,0)['replay']
    if point!='before_shared_claim':
        for entry in ENTRIES:
            with pytest.raises(StateError):h.call(entry)
    assert len(h.adapter.probe_calls)==count and len(h.adapter.boundary_calls)==boundaries


@pytest.mark.parametrize('store,number',[(store,n) for store in ('native','financial','ledger') for n in range(1,3 if store=='ledger' else 5)])
@pytest.mark.parametrize('mode',['lost','stale'])
def test_real_conditional_replace_lost_response_and_etag_conflict(h,store,number,mode,monkeypatch):
    io={'native':h.native_io,'financial':h.financial,'ledger':h.ledger_io}[store]
    original=io.replace_versioned;calls=[]
    def fail(raw,tag,after):
        calls.append(1)
        if len(calls)==number:
            io.replace_versioned=original
            if mode=='stale':original(raw,tag,raw);return original(raw,tag,after)
            original(raw,tag,after);raise TimeoutError('persisted response unknown')
        return original(raw,tag,after)
    monkeypatch.setattr(io,'replace_versioned',fail)
    nid,sid=str(uuid4()),str(uuid4())
    with pytest.raises((StateError,TimeoutError)):h.call(native_id=nid,shared_id=sid,revision=0)
    count=len(h.adapter.probe_calls);h.restart()
    report=h.adapter.report(nid)
    assert not report['retry_allowed']
    if h.shared.read()[0]['operations']:
        with pytest.raises(StateError):h.call('pdf_manual')
    assert len(h.adapter.probe_calls)==count


@pytest.mark.parametrize('kind',['normal_auto','buyback_income'])
@pytest.mark.parametrize('change',['native_id','shared_id','revision','input','plan'])
def test_request_ids_cannot_be_reused_for_different_content(h,kind,change):
    nid,sid=str(uuid4()),str(uuid4());h.call(kind,nid,sid,0)
    if change=='native_id':nid=str(uuid4())
    elif change=='shared_id':sid=str(uuid4())
    elif change=='input':h.golden[kind]['inputs']['parsed']['note']='changed'
    elif change=='plan':h.golden[kind]['plan']['operations'][0]['rows'][0][5]='changed-url'
    with pytest.raises(StateError):h.call(kind,nid,sid,1 if change=='revision' else 0)
    assert len(h.adapter.probe_calls)==1


@pytest.mark.parametrize('kind',['normal_auto','buyback_income'])
@pytest.mark.parametrize('mode',['partial','no_ledger'])
def test_absence_or_partial_ledger_does_not_complete_or_retry(h,kind,mode):
    h.mode=mode;nid=str(uuid4())
    with pytest.raises(StateError):h.call(kind,native_id=nid)
    result=h.adapter.report(nid)
    assert result['classification']==('PARTIAL' if mode=='partial' else 'UNKNOWN')
    record=next(iter(h.journal.read()[0]['operations'].values()))
    assert record['phase']==('partial' if mode=='partial' else 'unknown')
    assert record['writer_start'] and record['completion'] is None
    with pytest.raises(StateError):h.call(kind)


@pytest.mark.parametrize('fault',['native_missing','native_corrupt','marker_missing','completion_missing','shared_missing','shared_fingerprint',
    'ledger_incomplete','duplicate','formula','commit_missing','commit_only','income_missing','float_amount','unrelated_row',
    'source_revision','native_version','partition','authority','old_pending','weak_native','weak_shared','weak_ledger',
    'read_race_native','read_race_shared','read_race_ledger','read_race_source'])
def test_corruption_version_changes_and_incomplete_observations_fail_closed(h,fault,monkeypatch):
    kind='buyback_income';nid=str(uuid4());h.call(kind,native_id=nid)
    if fault.startswith('weak_'):
        io={'weak_native':h.native_io,'weak_shared':h.financial,'weak_ledger':h.ledger_io}[fault]
        original=io.read_versioned;monkeypatch.setattr(io,'read_versioned',lambda:(original()[0],'W/"weak"'))
    elif fault.startswith('read_race_'):
        def race():
            h.adapter.read_hook=None
            if fault=='read_race_source':h.source.frame['generation']+=1
            else:
                io={'read_race_native':h.native_io,'read_race_shared':h.financial,'read_race_ledger':h.ledger_io}[fault]
                raw,tag=io.read_versioned();io.replace_versioned(raw,tag,raw)
        h.adapter.read_hook=race
    elif fault in {'source_revision','native_version','partition','authority','old_pending'}:
        if fault=='source_revision':h.source.frame['drive_revision_id']='changed-same-hash'
        elif fault=='native_version':h.native.frame[kind,'u1']['source']['native_source_version']='changed'
        elif fault=='partition':h.partition.frame['revision']+=1
        elif fault=='authority':h.native.frame[kind,'u1']['authority_revision']+=1
        else:h.base_pending.frame['complete']=False
    else:
        io=h.native_io if fault in {'native_missing','native_corrupt','marker_missing','completion_missing'} else h.financial if fault.startswith('shared_') else h.ledger_io
        raw,tag=io.read_versioned();v=json.loads(raw)
        if fault in {'native_missing','shared_missing'}:v['operations']={}
        elif fault in {'native_corrupt','shared_fingerprint'}:next(iter(v['operations'].values()))['fingerprint']='f'*64
        elif fault=='marker_missing':next(iter(v['operations'].values()))['writer_start']=None
        elif fault=='completion_missing':next(iter(v['operations'].values()))['completion']=None
        elif fault=='ledger_incomplete':v['complete']=False
        else:
            for part in v['snapshot']:
                if fault=='income_missing':part['収入明細']=part['収入明細'][:1]
                elif fault=='commit_missing':part['取込データ']=part['取込データ'][:1]
                elif fault=='commit_only':
                    for table in ('レシート','収入明細'):part[table]=part[table][:1]
                elif fault=='duplicate':part['収入明細'].append(deepcopy(part['収入明細'][-1]))
                elif fault=='float_amount':part['収入明細'][-1][2]=float(part['収入明細'][-1][2])
                elif fault=='unrelated_row':part['支出明細'].append(['unmapped'])
            if fault=='formula':v['snapshot'][1]['収入明細'][-1][2]='=155'
        io.replace_versioned(raw,tag,encoded(v))
    result=h.adapter.report(nid)
    assert result['classification'] in {'UNKNOWN','PARTIAL'} and result['quarantined'],result
    assert not result['retry_allowed']


@pytest.mark.parametrize('left,right',[('normal_auto','legacy_confirmation'),('normal_auto','pdf_manual'),('normal_auto','pdf_auto'),
    ('buyback_income','legacy_confirmation'),('buyback_income','future_one_tab'),('normal_auto','grouped_unit'),
    ('normal_auto','fixed_canary'),('normal_auto','explicit_unit'),('normal_auto','buyback_income')])
def test_sixteen_instances_share_one_financial_and_native_backend(h,left,right):
    gates=[h.new_gate() for _ in range(16)];barrier=Barrier(16)
    assert all(g.requests.io is h.financial and g.reader is h.reader for g in gates)
    def submit(i):
        barrier.wait(timeout=30)
        try:return h.call(left if i%2 else right,revision=0,gate=gates[i])
        except StateError:return None
    with ThreadPoolExecutor(max_workers=16) as pool:list(pool.map(submit,range(16)))
    assert len(h.shared.read()[0]['operations'])==1
    assert len(h.adapter.probe_calls)<=1 and len(h.adapter.boundary_calls)==1


@pytest.mark.parametrize('kind',['normal_auto','buyback_income'])
@pytest.mark.parametrize('change',['amount','category','privacy','kind','imported','old_pending'])
def test_original_privacy_validator_income_and_duplicate_conditions_are_kept(h,kind,change):
    if change=='amount':h.golden[kind]['plan']['operations'][1]['rows'][0][4 if kind=='normal_auto' else 2]+=1
    elif change=='category':h.golden[kind]['plan']['operations'][1]['rows'][0][5 if kind=='normal_auto' else 3]='invalid'
    elif change=='privacy':
        extraction=h.sandbox.module('receipt_text_extraction')
        h.sandbox.module('receipt_privacy_gate')._extract_receipt_text=lambda *a,**kw:extraction._ReceiptTextExtraction(
            'extracted','pdf_text','医療費 領収証 病院 保険診療 診療費 730円')
    elif change=='kind':h.golden[kind]['inputs']['parsed']['transaction_kind']='unknown'
    elif change=='imported':
        raw,tag=h.ledger_io.read_versioned();v=json.loads(raw)
        for part in v['snapshot']:part['取込データ'].append(deepcopy(h.golden[kind]['plan']['operations'][-1]['rows'][0]))
        h.ledger_io.replace_versioned(raw,tag,encoded(v))
    else:h.base_pending.frame['records']=[deepcopy(h.frozen[kind])]
    with pytest.raises((StateError,AttributeError,TypeError)):h.call(kind)
    assert not h.adapter.probe_calls and not h.native_io.writes and not h.financial.writes


def test_start_marker_is_durable_before_any_probe_and_stores_are_not_one_transaction(h):
    nid=str(uuid4())
    def stop(p):
        if p=='after_writer_start_evidence':
            r=next(iter(h.journal.read()[0]['operations'].values()))
            assert r['phase']=='writer_started' and r['writer_start'] and not h.adapter.probe_calls
            assert ledger_view(h.ledger_io)[0]==h.baseline
            raise Crash()
    h.fault=stop
    with pytest.raises(Crash):h.call(native_id=nid)
    assert len({id(h.financial),id(h.native_io),id(h.ledger_io)})==3
    assert h.adapter.report(nid)['classification']=='UNKNOWN'


def test_reports_have_no_writer_or_recovery_credential_and_are_owner_only(h):
    nid=str(uuid4());h.call(native_id=nid)
    for c in ({'session_active':False},{'actor_id':'f'*64},{'expires_at':0}):
        with pytest.raises(StateError):h.adapter.report(nid,c)
    report=h.adapter.report(nid)
    assert report['real_native_complete'] is report['accounting_permission'] is False
    from app.unified_receipt_write import OperationRequest
    from app.unified_receipt_purposes import PurposeRequest
    for parser in (OperationRequest.parse,PurposeRequest.parse):
        with pytest.raises(StateError):parser(report)
        with pytest.raises(StateError):parser(next(iter(h.journal.read()[0]['operations'].values()))['request'])
    assert '&lt;script&gt;' in h.adapter.render_report({'memo':'<script>'})
    assert not any(hasattr(h.adapter,n) for n in ('execute','append','retry','release','issue_authority'))
    with pytest.raises(StateError):NativeEvidenceJournal(h.native_io)


def test_all_old_direct_front_doors_still_require_the_shared_gate(h):
    from staging_shadow.native_accounting_sandbox import PATHS,DENIED
    for path in list(set(PATHS.values()))+list(DENIED):
        parent,name=h.sandbox._resolve(path)
        with pytest.raises(StateError):getattr(parent,name)()
    assert not h.adapter.boundary_calls and not h.adapter.probe_calls


@pytest.mark.parametrize('kind',['normal_auto','buyback_income'])
def test_automatic_gate_cannot_be_used_without_the_native_attempt_adapter(h,kind):
    with pytest.raises(StateError,match='native_attempt_required'):
        h.sandbox.call(kind,'u1',str(uuid4()),0,{})
    assert not h.financial.writes and not h.native_io.writes and not h.adapter.probe_calls


@pytest.mark.parametrize('classification',['medical','payroll','sensitive_unknown'])
def test_sticky_sensitive_source_provenance_blocks_the_new_journal(h,classification):
    h.known_classification=classification
    with pytest.raises(StateError):h.call()
    assert not h.financial.writes and not h.native_io.writes and not h.adapter.probe_calls


@pytest.mark.parametrize('kind',['normal_auto','buyback_income'])
@pytest.mark.parametrize('same_id',[True,False])
def test_sixteen_independent_native_journals_and_adapters_share_the_same_backends(h,kind,same_id):
    from app.iphone_accounting_gate import SyntheticAccountingGate
    from auto_native_fixtures import NOW
    adapters=[];gates=[]
    for _ in range(16):
        journal=NativeEvidenceJournal(h.native_io,enabled=True)
        adapter=SyntheticAutoEvidenceAdapter(journal,h.shared,h.auth,h.candidate,h.bindings,h.ledger_io,enabled=True)
        adapter.fault=h.on_point;adapters.append(adapter)
        gates.append(SyntheticAccountingGate(h.shared,h.reader,h.auth,h.preflight,adapter.observe_boundary,
            enabled=True,isolated_synthetic=True,clock=lambda:NOW,checkpoint=adapter.checkpoint))
    h.instances=adapters;barrier=Barrier(16);nid,sid=str(uuid4()),str(uuid4())
    assert len({id(a.journal) for a in adapters})==16
    assert all(a.journal.io is h.native_io and a.shared.io is h.financial and a.ledger_io is h.ledger_io for a in adapters)
    def submit(i):
        barrier.wait(timeout=30);n=nid if same_id else str(uuid4());s=sid if same_id else str(uuid4())
        try:
            with adapters[i].attempt(kind,'u1',n,s,0) as replay:
                if replay is None:return gates[i].submit(kind,'u1',s,0,{})
                return replay
        except StateError:return None
    with ThreadPoolExecutor(max_workers=16) as pool:list(pool.map(submit,range(16)))
    assert len(h.shared.read()[0]['operations'])==len(h.journal.read()[0]['operations'])==1
    assert sum(len(a.probe_calls) for a in adapters)==1
    assert sum(len(a.boundary_calls) for a in adapters)==1
