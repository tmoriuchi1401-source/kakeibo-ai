from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from uuid import uuid4

import pytest

from app.drive_run_state import StateError
from app.iphone_accounting_gate import ENTRIES
from app.iphone_accounting_ownership import relation
from app.unified_receipt_review import digest
from staging_shadow.accounting_recovery_model import Crash, POINTS, EvidenceModel, encoded
from accounting_gate_fixtures import Suite
from accounting_recovery_fixtures import Harness, plans_for, future_aliases


@pytest.fixture(scope='module')
def foundation():
    s=Suite();plans=plans_for(s);future_aliases(s,plans)
    yield s,plans
    s.sandbox.close()


@pytest.fixture
def h(foundation):
    s,plans=foundation
    before=s.snapshot_native();ports=[(p,deepcopy(p.frame)) for p in
        {id(x):x for route in s.router.routes.values() for x in (route.source,route.partition,route.native,s.ledger,s.pending)}.values()]
    harness=Harness(s,plans)
    yield harness
    from collections import Counter
    assert all(n<=1 for n in Counter(op for _,_,op in harness.boundary_calls).values())
    harness.close()
    for port,frame in ports:port.frame=frame;port.on_read=None
    s.after_preflight=None
    assert s.snapshot_native()==before  # No native state/writer/ledger mutations.


@pytest.fixture(autouse=True)
def no_live(monkeypatch):
    from google.cloud import firestore
    from googleapiclient import discovery
    def forbidden(*args,**kwargs):pytest.fail('live client forbidden')
    monkeypatch.setattr(firestore,'Client',forbidden);monkeypatch.setattr(discovery,'build',forbidden)


def starts(h):return sum(r['writer_starts'] for r in h.model.read()[0]['records'].values())


@pytest.mark.parametrize('entry',sorted(ENTRIES))
@pytest.mark.parametrize('point',POINTS)
@pytest.mark.parametrize('mode',['crash','timeout','lost_response','stale_revision'])
def test_all_ingresses_all_failure_points_are_fenced_after_restart(h,entry,point,mode):
    rid=str(uuid4())
    def fail(current):
        if current==point:
            if mode=='crash':raise Crash(point)
            if mode=='stale_revision':raise StateError('HTTP_412')
            raise TimeoutError(mode)
    h.model.fault=fail
    with pytest.raises((Crash,StateError,TimeoutError)):h.call(entry,rid=rid,revision=0)
    count=starts(h);boundary_count=len(h.boundary_calls)
    assert count<=1 and boundary_count<=1
    h.restart()
    stored=h.requests.read()[0]
    before=(h.server.read_versioned(),h.evidence.read_versioned(),h.s.snapshot_native())
    report=h.model.report(rid)
    assert report['retry_allowed'] is report['native_authority'] is report['accounting_permission'] is False
    assert before==(h.server.read_versioned(),h.evidence.read_versioned(),h.s.snapshot_native())
    if point=='before_shared_claim':
        assert not stored['operations'] and count==0 and report['classification']=='UNKNOWN'
    else:
        assert len(stored['operations'])==1
        # Exact same ID retrieves only the stored state, never fills a gap.
        assert h.gate.inspect_result(rid)['replay']
        for other in ENTRIES:
            # Isolate the original ownership conflict: aliases to the same
            # trusted source/Unit are rejected across every financial front door.
            candidate=h.s.router.routes[other,'u1'].native.frame[other,'u1']['source']['source_id']
            source=next(iter(stored['operations'].values()))['request']['ownership']['source']['source_id']
            prior=next(iter(stored['operations'].values()))['request']['ownership']
            route=h.s.router.routes[other,'u1']
            try: independent=relation(prior,route.reader.observe(other,'u1').value())=='independent'
            except StateError:independent=False
            if candidate==source and not independent:
                with pytest.raises(StateError):h.call(other,rid=str(uuid4()))
        assert starts(h)==count and len(h.boundary_calls)==boundary_count
        if entry in {'normal_auto','buyback_income'}:
            assert report['classification']==('PARTIAL' if point=='after_partial_ledger' else 'UNKNOWN')
        elif point in {'after_shared_claim','after_native_validation','before_native_claim','after_native_claim','before_writer_start'}:
            assert report['classification']=='NOT_EXECUTED',report
        elif point=='after_writer_start':assert report['classification']=='UNKNOWN'
        elif point=='after_partial_ledger':assert report['classification']=='PARTIAL',report
        else:assert report['classification']=='COMPLETE',report


@pytest.mark.parametrize('entry',sorted(ENTRIES))
def test_normal_completion_readback_is_read_only_and_no_native_permission(h,entry):
    rid=str(uuid4());h.call(entry,rid=rid)
    before=h.server.read_versioned(),h.evidence.read_versioned()
    for _ in range(2):
        report=h.model.report(rid)
        assert report['classification']==('UNKNOWN' if entry in {'normal_auto','buyback_income'} else 'COMPLETE')
        assert '再実行は禁止' in h.model.render_report(report)
        assert h.gate.inspect_result(rid)['state']=='complete'
    assert starts(h)==1 and before==(h.server.read_versioned(),h.evidence.read_versioned())


@pytest.mark.parametrize('fault',['native_complete_without_ledger','ledger_complete_without_native','shared_complete_without_native',
    'partial','source','same_hash_revision','partition','native_version','authority','pending_corrupt','pending_missing',
    'ledger_incomplete','weak_etag','weak_shared_etag','readback_source_change','readback_evidence_change','readback_shared_change',
    'foreign_pending','duplicate_ledger','formula_mismatch','unrelated_row_change','missing_evidence'])
def test_inconsistent_or_corrupt_evidence_never_proves_success_or_nonexecution(h,fault,monkeypatch):
    rid=str(uuid4());h.call(rid=rid)
    op=next(iter(h.model.read()[0]['records']))
    def mutate(fn):h.model._update(op,fn)
    if fault=='native_complete_without_ledger':mutate(lambda r:r.update(ledger=deepcopy(r['before'])))
    elif fault=='ledger_complete_without_native':mutate(lambda r:r.update(native_state='claimed'))
    elif fault=='shared_complete_without_native':mutate(lambda r:r.update(native_state='absent',writer_starts=0))
    elif fault=='partial':
        def partial(r):r['ledger']=deepcopy(r['before']);h.model._project_rows(r,partial=True)
        mutate(partial)
    elif fault in {'source','same_hash_revision'}:h.s.pdf.source.frame['drive_revision_id']='changed-same-bytes'
    elif fault=='partition':h.s.pdf.partition.frame['revision']+=1
    elif fault=='native_version':h.s.pdf.native.frame['pdf_manual','u1']['source']['native_source_version']='changed'
    elif fault=='authority':h.s.pdf.native.frame['pdf_manual','u1']['authority_revision']+=1
    elif fault=='pending_corrupt':h.s.pending.frame['complete']=False
    elif fault=='pending_missing':
        state,raw,tag=h.model.read();state['records'][op].pop('native_state')
        h.evidence.replace_versioned(raw,tag,encoded(state))
    elif fault=='ledger_incomplete':h.s.ledger.frame['complete']=False
    elif fault.startswith('weak_'):
        io=h.server if fault=='weak_shared_etag' else h.evidence
        original=io.read_versioned;monkeypatch.setattr(io,'read_versioned',lambda:(original()[0],'W/"weak"'))
    elif fault.startswith('readback_'):
        def race():
            h.model.read_hook=None
            if fault=='readback_source_change':h.s.pdf.source.frame['generation']+=1
            else:
                io=h.evidence if fault=='readback_evidence_change' else h.server
                raw,tag=io.read_versioned();io.replace_versioned(raw,tag,raw)
        h.model.read_hook=race
    elif fault=='foreign_pending':
        old=deepcopy(h.model.read()[0]['records'][op]['ownership']);old['case_id']='old-pending-case'
        h.s.pending.frame['records'].append(old)
    elif fault in {'duplicate_ledger','formula_mismatch','unrelated_row_change'}:
        def corrupt(r):
            title=r['expected']['operations'][0]['table']
            if fault=='duplicate_ledger':r['ledger'][0][title].append(deepcopy(r['ledger'][0][title][-1]))
            elif fault=='formula_mismatch':r['ledger'][1][title][-1][-1]='=1+2'
            else:
                for part in r['ledger']:part[title].append(['unrelated'])
        mutate(corrupt)
    else:
        state,raw,tag=h.model.read();state['records']={};h.evidence.replace_versioned(raw,tag,encoded(state))
    result=h.model.report(rid)
    assert result['classification'] in {'UNKNOWN','PARTIAL'} and result['quarantined'],result
    assert not result['retry_allowed'] and starts_safe(h)<=1


def starts_safe(h):
    try:return starts(h)
    except Exception:return 0


@pytest.mark.parametrize('entry',sorted(ENTRIES))
def test_request_id_reuse_replay_and_partial_unknown_keep_fences(h,entry):
    rid=str(uuid4())
    h.model.fault=lambda p:(_ for _ in ()).throw(TimeoutError()) if p=='after_partial_ledger' else None
    with pytest.raises(StateError):h.call(entry,rid=rid)
    h.restart()
    assert h.gate.inspect_result(rid)['state']=='partial_unknown'
    with pytest.raises(StateError):h.call(entry,rid=rid,revision=99)
    with pytest.raises(StateError):h.call(entry,rid=str(uuid4()))
    assert starts(h)==1


@pytest.mark.parametrize('left,right,key',[('normal_auto','legacy_confirmation','u1'),('normal_auto','pdf_manual','u1'),
    ('buyback_income','pdf_auto','u1'),('pdf_auto','pdf_manual','u1'),('pdf_manual','grouped_unit','u1'),
    ('fixed_canary','explicit_unit','u1'),('legacy_confirmation','future_one_tab','legacy')])
def test_sixteen_instances_compete_on_one_backend_and_shared_observers(h,left,right,key):
    gates=[h.new_gate() for _ in range(16)];barrier=Barrier(16)
    assert all(g.requests.io is h.server and g.reader is h.s.router for g in gates)
    assert all(r.reader.pending is h.pending and r.reader.ledger is h.s.ledger for r in h.s.router.routes.values())
    def run(i):
        entry=left if i%2 else right;k=key if entry==right else 'u1'
        barrier.wait(timeout=30)
        try:return gates[i].submit(entry,k,str(uuid4()),0,{})
        except Exception:return None
    with ThreadPoolExecutor(max_workers=16) as pool:list(pool.map(run,range(16)))
    # Some pairs are truly independent: legacy image vs PDF, and p14 vs p4.
    # A single CAS revision still permits only one initial request at rev zero.
    assert len(h.requests.read()[0]['operations'])==1 and starts(h)<=1


def test_distinct_units_are_independent_only_without_old_pending(h):
    h.call('fixed_canary')
    # Old native pending, including a complete but unreviewed synthetic record,
    # conservatively blocks a new Unit in the same original.
    with pytest.raises(StateError):h.call('explicit_unit')
    assert starts(h)==1


def test_report_requires_owner_session_and_escapes_html(h):
    rid=str(uuid4());h.call(rid=rid)
    for context in ({'session_active':False},{'actor_id':'f'*64},{'expires_at':0}):
        with pytest.raises(StateError):h.model.report(rid,context)
    assert '&lt;script&gt;' in h.model.render_report({'memo':'<script>'})


def test_readback_never_converts_empty_ledger_alone_into_not_executed(h):
    assert h.model.report(str(uuid4()))['classification']=='UNKNOWN'
    h.model.fault=lambda p:(_ for _ in ()).throw(Crash()) if p=='after_writer_start' else None
    rid=str(uuid4())
    with pytest.raises(Crash):h.call(rid=rid)
    result=h.model.report(rid)
    assert result['ledger_outcome']=='not_written' and result['classification']=='UNKNOWN'


def test_recovery_model_is_not_a_native_authority_or_writer_api(h):
    with pytest.raises(StateError):EvidenceModel(h.evidence,h.requests,h.s.auth,h.bindings,lambda *_:None)
    assert not any(hasattr(h.model,name) for name in ('execute','append','retry','release','recover_write','issue_authority'))


@pytest.mark.parametrize('entry',sorted(ENTRIES-{'future_one_tab'}))
@pytest.mark.parametrize('stop',[None,'after_partial_ledger','after_native_complete'])
def test_future_alias_and_every_legacy_entry_share_the_exact_fence(h,entry,stop):
    rid=str(uuid4())
    if stop:
        h.model.fault=lambda p:(_ for _ in ()).throw(Crash()) if p==stop else None
        with pytest.raises(Crash):h.call(entry,rid=rid)
    else:h.call(entry,rid=rid)
    h.restart();count=starts(h)
    assert count==1
    for request_id in (rid,str(uuid4())):
        with pytest.raises(StateError):h.call('future_one_tab',key='alias-'+entry,rid=request_id)
    assert starts(h)==count
    assert h.gate.inspect_result(rid)['replay']


@pytest.mark.parametrize('entry',sorted(ENTRIES-{'future_one_tab'}))
def test_sixteen_future_legacy_aliases_compete_for_one_operation(h,entry):
    gates=[h.new_gate() for _ in range(16)];barrier=Barrier(16)
    def run(i):
        alias=bool(i%2);barrier.wait(timeout=30)
        try:return gates[i].submit('future_one_tab' if alias else entry,'alias-'+entry if alias else 'u1',str(uuid4()),0,{})
        except StateError:return None
    with ThreadPoolExecutor(max_workers=16) as pool:list(pool.map(run,range(16)))
    assert len(h.requests.read()[0]['operations'])==1 and starts(h)==1
    state=h.model.read()[0]
    assert all(r['ledger']==state['ledger'] for r in state['records'].values())


def test_disjoint_unit_can_progress_when_first_claim_never_reached_native_pending(h):
    h.model.fault=lambda p:(_ for _ in ()).throw(Crash()) if p=='after_shared_claim' else None
    with pytest.raises(Crash):h.call('fixed_canary')
    h.restart();h.call('explicit_unit')
    assert len(h.requests.read()[0]['operations'])==2 and starts(h)==1


@pytest.mark.parametrize('store',['shared','evidence'])
def test_lost_atomic_replace_response_never_retries_or_unfences(h,store):
    io=h.server if store=='shared' else h.evidence
    io.lost=True;rid=str(uuid4())
    with pytest.raises(TimeoutError):h.call(rid=rid)
    io.lost=False;h.restart()
    assert starts(h)==0 and len(h.requests.read()[0]['operations'])==1
    assert h.gate.inspect_result(rid)['state']=='accepted'
    with pytest.raises(StateError):h.call('future_one_tab')
    assert starts(h)==0


def test_actual_stale_etag_and_expected_revision_reject_before_probe(h,monkeypatch):
    original=h.server.replace_versioned
    def race(raw,tag,after):
        h.server.replace_versioned=original
        original(raw,tag,raw)  # Another process advances the strong tag only.
        return original(raw,tag,after)
    monkeypatch.setattr(h.server,'replace_versioned',race)
    with pytest.raises(StateError,match='412'):h.call()
    assert not h.requests.read()[0]['operations'] and starts(h)==0
    with pytest.raises(StateError,match='stale_revision'):h.call(revision=99)


def test_human_report_contains_full_target_rows_and_pending_without_an_action(h):
    rid=str(uuid4());h.call(rid=rid)
    report=h.model.report(rid)
    assert report['review']['ledger']
    assert all(set(r)=={'table','transaction_id','expected','before','observed','formulas'} for r in report['review']['ledger'])
    assert report['review']['pending_state']=='complete'
    assert not any(k in report for k in ('authority','grant','operation_target','csrf','post_url'))


@pytest.mark.parametrize('number',range(1,16))
@pytest.mark.parametrize('mode',['lost_response','stale_tag'])
def test_each_evidence_replace_failure_is_closed_without_a_second_dispatch(h,number,mode,monkeypatch):
    original=h.evidence.replace_versioned;calls=[]
    def fail(raw,tag,after):
        calls.append(1)
        if len(calls)==number:
            h.evidence.replace_versioned=original
            if mode=='stale_tag':
                original(raw,tag,raw)
                return original(raw,tag,after)
            original(raw,tag,after)
            raise TimeoutError('saved but lost evidence response')
        return original(raw,tag,after)
    monkeypatch.setattr(h.evidence,'replace_versioned',fail)
    rid=str(uuid4())
    with pytest.raises((StateError,TimeoutError)):h.call(rid=rid)
    h.restart();count=starts(h)
    assert count<=1 and len(calls)==number
    with pytest.raises(StateError):h.call('future_one_tab')
    assert starts(h)==count
    report=h.model.report(rid)
    assert not report['retry_allowed']


@pytest.mark.parametrize('number',range(1,5))
def test_lost_response_at_each_existing_shared_transition_is_not_redispatched(h,number,monkeypatch):
    original=h.server.replace_versioned;calls=[]
    def fail(raw,tag,after):
        calls.append(1);original(raw,tag,after)
        if len(calls)==number:
            h.server.replace_versioned=original
            raise TimeoutError('lost shared transition response')
    monkeypatch.setattr(h.server,'replace_versioned',fail)
    rid=str(uuid4())
    with pytest.raises((StateError,TimeoutError)):h.call(rid=rid)
    h.restart();count=starts(h)
    assert count<=1
    h.gate.inspect_result(rid)
    with pytest.raises(StateError):h.call('future_one_tab')
    assert starts(h)==count


def test_shared_accepted_with_native_complete_is_an_inconsistent_state(h):
    rid=str(uuid4());h.call(rid=rid)
    before,raw,tag=h.requests.read();after=deepcopy(before)
    op=next(iter(after['operations'].values()));op['state']='accepted';op['events'][-1]['state']='accepted'
    after['revision']+=1;h.requests.replace(before,raw,tag,after)
    assert h.model.report(rid)['classification']=='UNKNOWN'


@pytest.mark.parametrize('point',POINTS)
@pytest.mark.parametrize('mode',['crash','timeout'])
def test_manual_medical_legacy_faults_do_not_restart_automatic_processing(h,point,mode):
    rid=str(uuid4())
    def fail(current):
        if current==point:
            if mode=='crash':raise Crash()
            raise TimeoutError()
    h.model.fault=fail
    with pytest.raises((Crash,StateError,TimeoutError)):h.call('legacy_confirmation',key='medical',rid=rid)
    h.restart();count=starts(h)
    assert count<=1
    if point!='before_shared_claim':
        h.gate.inspect_result(rid)
        with pytest.raises(StateError):h.call('legacy_confirmation',key='medical')
    assert starts(h)==count
    assert h.model.report(rid)['retry_allowed'] is False


def test_manual_medical_positive_readback_is_only_synthetic(h):
    rid=str(uuid4());h.call('legacy_confirmation',key='medical',rid=rid)
    report=h.model.report(rid)
    assert report['classification']=='COMPLETE' and report['synthetic_only']
    assert not report['native_authority'] and not report['accounting_permission']


def test_grouped_projection_uses_the_actual_pinned_terminal_phase(foundation):
    import ast,inspect,textwrap,importlib
    from staging_shadow.accounting_recovery_model import PROFILES
    native=importlib.import_module('app._unified_pinned.pdf_unit_processing')
    tree=ast.parse(textwrap.dedent(inspect.getsource(native.DriveUnitProcessingStore.complete)))
    # Pin the actual phase assignment, not the UI's imported label.
    values=[n.value.elts[0].value for n in ast.walk(tree) if isinstance(n,ast.Assign)
        and isinstance(n.value,ast.Tuple) and isinstance(n.value.elts[0],ast.Constant)
        and any(isinstance(t,ast.Tuple) and isinstance(t.elts[0],ast.Subscript)
            and isinstance(t.elts[0].slice,ast.Constant) and t.elts[0].slice.value=='phase' for t in n.targets)]
    assert values==[PROFILES['pdf_unit_processing'][2]]==['applied']


@pytest.mark.parametrize('intent',[None,'f'*64])
def test_native_intent_binding_is_required_even_with_exact_completed_ledger(h,intent):
    rid=str(uuid4());h.call(rid=rid)
    op=next(iter(h.model.read()[0]['records']))
    h.model._update(op,lambda r:r.update(intent_binding=intent))
    assert h.model.report(rid)['classification']=='UNKNOWN'
    with pytest.raises(StateError):h.call('future_one_tab')
    assert starts(h)==1
