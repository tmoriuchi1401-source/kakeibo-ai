from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
import json
from threading import Barrier
from uuid import uuid4

import pytest

from app.drive_run_state import StateError
from app.iphone_accounting_gate import ENTRIES, AccountingProbeRequest, SyntheticAccountingGate
from app.unified_receipt_write import ConditionalRequests, OperationRequest
from app.unified_receipt_purposes import PurposeRequest
from app.unified_receipt_review import digest
from staging_shadow.native_accounting_sandbox import (NativeAccountingSandbox, PATHS, DENIED,
    DISABLED_RUNNERS, verify_sink_census)
from accounting_gate_fixtures import Suite, NOW
from unified_purpose_fixtures import OWNER


@pytest.fixture(autouse=True)
def no_live_clients(monkeypatch):
    from google.cloud import firestore
    from googleapiclient import discovery
    def forbidden(*args,**kwargs): pytest.fail('live client in synthetic accounting test')
    monkeypatch.setattr(firestore,'Client',forbidden)
    monkeypatch.setattr(discovery,'build',forbidden)


@pytest.fixture
def s():
    import gc
    suite=Suite()
    try:yield suite
    finally:
        suite.sandbox.close()
        gc.collect()


@pytest.mark.parametrize('entry',sorted(ENTRIES))
def test_every_interposed_native_front_door_requires_shared_claim_and_native_preflight(s,entry):
    before=s.snapshot_native()
    result=s.call(entry)
    assert result['state']=='complete' and result['accounting_writes']==0
    assert result['native_authority'] is result['accounting_permission'] is False
    assert s.snapshot_native()==before
    assert len(s.probe.calls)==1 and s.probe.calls[0][0]==entry
    assert s.requests.io is s.server and s.pdf.bridge.requests.io.writes==0
    state=s.requests.read()[0]
    assert len(state['operations'])==1
    op=next(iter(state['operations'].values()))
    assert [e['state'] for e in op['events']]==['accepted','executing','awaiting_readback','complete']
    assert op['request']['synthetic_only'] is True
    # Neither the new probe request nor its result is a native authority format.
    for parser in (OperationRequest.parse,PurposeRequest.parse):
        with pytest.raises(StateError):parser(op['request'])
        with pytest.raises(StateError):parser(result)


@pytest.mark.parametrize('entry',sorted(ENTRIES))
def test_direct_native_methods_without_command_are_blocked_before_function_body(s,entry):
    before=s.snapshot_native();i,_=s.sandbox.bindings[entry,'u1']
    parent,name=s.sandbox._resolve(PATHS[entry])
    with pytest.raises(StateError,match='shared_gate_required'):
        getattr(parent,name)(*i.args,**i.kwargs)
    assert s.probe.calls==[] and s.server.writes==0 and s.snapshot_native()==before


@pytest.mark.parametrize('path',DENIED)
def test_writer_internal_direct_and_adapter_execute_bypasses_are_denied(s,path):
    parent,name=s.sandbox._resolve(path)
    with pytest.raises(StateError):getattr(parent,name)()
    assert not s.probe.calls and s.server.writes==0


@pytest.mark.parametrize('module',DISABLED_RUNNERS)
def test_production_cli_runner_background_paths_are_inert_in_private_composition(s,module):
    import inspect
    target=s.sandbox.module(module)
    for name,value in vars(target).items():
        if inspect.isfunction(value) and not name.startswith('__') and value.__name__=='_deny':
            with pytest.raises(StateError):value()
    assert not s.probe.calls and s.server.writes==0


def test_all_front_doors_are_present_and_gate_is_not_optional(s):
    assert set(PATHS)==ENTRIES
    for kw in ({},{'enabled':True},{'enabled':True,'isolated_synthetic':False}):
        with pytest.raises(StateError):NativeAccountingSandbox(s.gate,s.invocations,**kw)
    with pytest.raises(StateError):NativeAccountingSandbox(s.gate,s.invocations[:-1],enabled=True,isolated_synthetic=True)
    with pytest.raises(StateError):SyntheticAccountingGate(s.requests,s.router,s.auth,s.preflight,s.probe,clock=lambda:NOW)


def test_native_function_arguments_and_target_cannot_be_swapped(s):
    i,_=s.sandbox.bindings['pdf_manual','u1'];parent,name=s.sandbox._resolve(PATHS['pdf_manual'])
    with s.sandbox.command('pdf_manual','u1',str(uuid4()),0,{}):
        with pytest.raises(StateError,match='invocation_changed'):
            getattr(parent,name)(*i.args,**{**i.kwargs,'apply':False})
    with s.sandbox.command('pdf_manual','u1',str(uuid4()),0,{}):
        other,method=s.sandbox._resolve(PATHS['legacy_confirmation'])
        with pytest.raises(StateError):getattr(other,method)()
    assert s.server.writes==0 and not s.probe.calls


@pytest.mark.parametrize('left,right,key',[('normal_auto','pdf_manual','u1'),('legacy_confirmation','future_one_tab','legacy'),
    ('buyback_income','future_one_tab','u1'),('pdf_manual','future_one_tab','u1'),('pdf_manual','grouped_unit','u1'),
    ('pdf_auto','pdf_manual','u1'),('pdf_auto','grouped_unit','u1')])
def test_cross_entry_whole_unit_or_changed_partition_never_gets_a_second_probe(s,left,right,key):
    s.call(left)
    with pytest.raises(StateError):s.call(right,key=key)
    assert len(s.probe.calls)==1


@pytest.mark.parametrize('same_id',[True,False])
def test_old_confirmation_and_new_entry_compete_on_the_same_backend(s,same_id):
    start=Barrier(2);rid=str(uuid4())
    def submit(entry):
        start.wait(timeout=15)
        try:return s.call(entry,key='legacy' if entry=='future_one_tab' else 'u1',rid=rid if same_id else str(uuid4()),revision=0)
        except StateError:return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(submit,['legacy_confirmation','future_one_tab']))
    assert sum(r is not None and not r['replay'] for r in results)==1
    assert len(s.probe.calls)==len(s.requests.read()[0]['operations'])==1


def test_distinct_confirmed_units_and_independent_originals_can_claim(s):
    # Existing fixed p14 and explicit p4 share a trusted confirmed partition.
    s.call('fixed_canary');s.call('explicit_unit')
    # PDF auto belongs to another independently identified source.
    s.call('pdf_auto')
    assert len(s.probe.calls)==3 and len(s.requests.read()[0]['operations'])==3
    assert all(route.reader.ledger is s.ledger and route.reader.pending is s.pending for route in s.router.routes.values())
    assert s.auto_pdf.runner is s.pdf.runner
    assert s.pdf.db.data['レシート'] is s.legacy.db.rows['レシート'] is s.shared_ledger['レシート']


@pytest.mark.parametrize('count',[2,16])
@pytest.mark.parametrize('same_id',[True,False])
def test_multiple_gate_instances_share_one_atomic_backend(s,count,same_id):
    gates=[SyntheticAccountingGate(ConditionalRequests(s.server,s.requests.store_id,synthetic_accounting=True),
        s.router,s.auth,s.preflight,s.probe,enabled=True,isolated_synthetic=True,clock=lambda:NOW) for _ in range(count)]
    assert len({id(g.requests) for g in gates})==count
    assert all(g.requests.io is s.server for g in gates)
    start=Barrier(count);rid=str(uuid4())
    def submit(i):
        start.wait(timeout=20)
        try:return gates[i].submit('future_one_tab' if i%2 else 'pdf_manual','u1',rid if same_id else str(uuid4()),0,{})
        except StateError:return None
    with ThreadPoolExecutor(max_workers=count) as pool:list(pool.map(submit,range(count)))
    assert len(s.probe.calls)==1 and len(s.requests.read()[0]['operations'])==1


def test_same_request_id_exact_reinspection_and_mismatched_content(s):
    rid=str(uuid4());s.call(rid=rid,revision=0)
    assert s.call(rid=rid,revision=0)['replay']
    assert s.call('future_one_tab',rid=rid,revision=0)['replay']
    with pytest.raises(StateError,match='request_id_conflict'):s.call(rid=rid,revision=4)
    n=s.pdf.native.frame['pdf_manual','u1'];n['input_snapshot']='e'*64
    with pytest.raises(StateError):s.call(rid=rid,revision=0)
    assert len(s.probe.calls)==1


@pytest.mark.parametrize('mode',['lost','partial'])
def test_unknown_probe_response_is_read_only_on_every_entry(s,mode):
    s.probe.mode=mode;rid=str(uuid4())
    with pytest.raises(StateError,match='partial_unknown'):s.call(rid=rid,revision=0)
    assert s.call('future_one_tab',rid=rid,revision=0)['state']=='partial_unknown'
    with pytest.raises(StateError):s.call('future_one_tab',revision=s.requests.read()[0]['revision'])
    assert s.gate.inspect_result(rid)['state']=='partial_unknown'
    assert len(s.probe.calls)==1


def test_lost_shared_claim_response_never_dispatches_or_fills_missing_execution(s):
    rid=str(uuid4());s.server.lost=True
    with pytest.raises(TimeoutError):s.call(rid=rid,revision=0)
    s.server.lost=False
    assert s.call('future_one_tab',rid=rid,revision=0)['state']=='accepted'
    assert s.gate.inspect_result(rid)['state']=='accepted' and not s.probe.calls


@pytest.mark.parametrize('field,value',[('csrf_valid',False),('session_active',False),('actor_id','f'*64),
    ('issuer','wrong'),('expires_at',NOW),('method','GET'),('expected_revision',99)])
def test_existing_auth_boundary_rejects_before_native_observation(s,field,value):
    before=s.native_reads
    with pytest.raises(StateError):s.call(context={field:value})
    assert s.native_reads==before and s.server.writes==0 and not s.probe.calls


@pytest.mark.parametrize('which',['source','partition','ledger','pending'])
def test_mid_execution_version_snapshot_partition_or_ledger_change_stops(s,which):
    def change(suite,entry,key):
        suite.after_preflight=None
        target=getattr(suite.pdf,which).frame
        target['generation']+=1
    s.after_preflight=change
    with pytest.raises(StateError,match='partial_unknown'):s.call()
    assert not s.probe.calls and s.gate.inspect_result(next(iter(s.requests.read()[0]['operations'].values()))['request']['request_id'])['state']=='partial_unknown'


@pytest.mark.parametrize('fault',['validator','authority','native_CAS','pending','grouped_CAS','canary_authority','canary_claimed','medical'])
def test_existing_native_rejections_are_not_overridden_by_outer_claim(s,fault,monkeypatch):
    entry='pdf_manual';p=s.pdf.port;r=s.pdf.runner
    if fault=='validator':
        s.pdf.db.categories=lambda:[]
    elif fault=='authority':
        value=r.store.load();value['pages'][p.key]['units'][p.uid]['posting_authority']=None
        value['generation']+=1;r.store.save(value)
    elif fault=='native_CAS':
        original=s.adapters[entry,'u1'].read_versioned;seen=[0]
        def changing():
            raw,tag=original();seen[0]+=1
            return raw,'"native-revision-'+str(seen[0])+'"'
        s.adapters[entry,'u1'].read_versioned=changing
    elif fault=='pending':s.legacy.service.items[next(iter(s.legacy.service.items))]['status']='pending';entry='legacy_confirmation'
    elif fault=='grouped_CAS':
        s.grouped.transport.read_versioned=lambda:(_ for _ in ()).throw(StateError('synthetic-native-read-conflict'))
        entry='grouped_unit'
    elif fault=='canary_authority':
        s.canaries['explicit_unit'].store.value['grant']['owner_actor_id']='f'*64;entry='explicit_unit'
    elif fault=='canary_claimed':
        s.canaries['fixed_canary'].store.value['state']='claimed';entry='fixed_canary'
    else:s.auto_text['normal_auto']='診療 領収書 患者負担額 730円';entry='normal_auto'
    with pytest.raises(StateError):s.call(entry)
    assert s.server.writes==0 and not s.probe.calls


def test_acknowledgement_or_nonaccounting_purpose_cannot_supply_native_preflight(s):
    s.adapters['pdf_manual','u1']=lambda _:dict(status='synthetic_owner_acknowledged',native_authority=False)
    with pytest.raises(StateError,match='preflight_required'):s.call()
    assert not s.probe.calls and s.server.writes==0
    for purpose in ('STRUCTURE_REVIEW','METADATA_REVIEW','RECONCILIATION_REVIEW'):
        s.pdf.native.frame['pdf_manual','u1']['purpose']=purpose
        with pytest.raises(StateError):s.call()
    assert not s.probe.calls


@pytest.mark.parametrize('case',['G103','H301'])
def test_held_cases_cannot_probe(s,case):
    s.pdf.native.frame['pdf_manual','u1']['case_id']=case
    with pytest.raises(StateError):s.call()
    assert s.server.writes==0 and not s.probe.calls


def test_existing_unscoped_financial_history_is_not_silently_migrated(s):
    from test_unified_receipt_write import active,bridge,payload
    c=active();b,p,io=bridge(c,io=s.server);b.submit(c,payload(b,c))
    with pytest.raises(StateError,match='UNKNOWN'):s.call()
    assert not s.probe.calls


def test_new_probe_records_remain_opt_in_and_corruption_stops(s):
    s.call()
    with pytest.raises(StateError):ConditionalRequests(s.server,s.requests.store_id).read()
    state=s.requests.read()[0];op=next(iter(state['operations'].values()))
    op['request']['ownership']['scope']='whole'  # fingerprint/shape corruption
    s.server.raw=json.dumps(state).encode()
    with pytest.raises(StateError):s.call()
    assert len(s.probe.calls)==1


def test_added_unreviewed_sink_call_prevents_composition_start(tmp_path):
    path=tmp_path/'new_background.py';path.write_text('def run(writer):\n writer._materialize_result(None)\n',encoding='utf-8')
    with pytest.raises(StateError,match='unreviewed_sink'):verify_sink_census(tmp_path)


def test_private_composition_does_not_patch_production_classes_or_constructors(s):
    from app.receipt_pipeline import ReceiptPipeline
    from app.receipt_confirmation import ReceiptConfirmation
    from app import cli
    assert ReceiptPipeline.process_bytes.__module__=='app.receipt_pipeline'
    assert ReceiptConfirmation.apply_confirmations.__module__=='app.receipt_confirmation'
    assert cli.main.__module__=='app.cli'
    assert s.sandbox.module('receipt_pipeline').ReceiptPipeline is not ReceiptPipeline


def test_medical_legacy_entry_is_fully_manual_and_has_no_ai_or_native_write(s):
    before=s.snapshot_native()
    result=s.call('legacy_confirmation',key='medical')
    assert result['state']=='complete' and result['accounting_writes']==0
    assert s.snapshot_native()==before and s.probe.calls[0][:2]==('legacy_confirmation','medical')


@pytest.mark.parametrize('kind',['automatic_medical','incomplete_medical','wrong_legacy_presentation','legacy_unsent_input',
    'auto_imported','buyback_schema','known_medical_provenance'])
def test_native_manual_privacy_and_legacy_presentation_conditions_remain_enforced(s,kind):
    entry='legacy_confirmation';key='u1'
    if kind=='automatic_medical':
        s.medical.service.items[next(iter(s.medical.service.items))]['decision_origin']='automatic';key='medical'
    elif kind=='incomplete_medical':
        s.medical.service.items[next(iter(s.medical.service.items))]['inputs'][2]='';key='medical'
    elif kind=='wrong_legacy_presentation':s.legacy.db.rows['領収書確認'][0][3]='wrong-source-link'
    elif kind=='legacy_unsent_input':s.legacy.db.rows['領収書確認'][0][8]='unsent-change'
    elif kind=='auto_imported':
        s.automatic['normal_auto'].db._import_ids.add('receipt:'+s.pdf.c.source_id);entry='normal_auto'
    elif kind=='buyback_schema':
        s.automatic['buyback_income'].db.sheet_titles=lambda:[];entry='buyback_income'
    else:
        s.automatic['normal_auto'].pipeline._source_privacy[s.pdf.c.source_id]='medical';entry='normal_auto'
    with pytest.raises(StateError):s.call(entry,key=key)
    assert not s.probe.calls and s.server.writes==0


def test_memory_isolation_and_guarded_probe_are_mandatory(s):
    s.server.synthetic_memory_only=False
    with pytest.raises(StateError):SyntheticAccountingGate(s.requests,s.router,s.auth,s.preflight,s.probe,
        enabled=True,isolated_synthetic=True,clock=lambda:NOW)
    s.server.synthetic_memory_only=True
    s.pdf.source.synthetic_memory_only=False
    with pytest.raises(StateError):SyntheticAccountingGate(s.requests,s.router,s.auth,s.preflight,s.probe,
        enabled=True,isolated_synthetic=True,clock=lambda:NOW)
    with pytest.raises(StateError):s.gate._probe()
    with pytest.raises(StateError):s.gate._boundary(None)
    assert not s.probe.calls


def test_result_lookup_rechecks_owner_session_and_does_not_mutate_or_recover(s):
    rid=str(uuid4());s.call(rid=rid,revision=0);before=s.server.raw,s.server.writes,s.snapshot_native()
    assert s.gate.inspect_result(rid)['state']=='complete'
    for context in ({'actor_id':'f'*64},{'session_active':False},{'expires_at':NOW}):
        with pytest.raises(StateError):s.gate.inspect_result(rid,context)
    assert before==(s.server.raw,s.server.writes,s.snapshot_native())


def test_native_ledger_target_mismatch_and_unclaimed_raw_handles_cannot_bypass(s):
    # The catalog cannot replace the real plan's ledger target with a lookalike.
    s.pdf.native.frame['pdf_manual','u1']['ledger_targets']=['different-ledger-target']
    with pytest.raises(StateError):s.call()
    assert not s.probe.calls and s.server.writes==0
    for i,_ in s.sandbox.bindings.values():
        for value in i.args:
            assert not any(hasattr(value,method) for method in ('apply_confirmations','post_normal','post_manual','append_raw'))


def test_shared_raw_ledger_change_is_seen_by_every_reader_and_stops_before_probe(s):
    for key,route in s.router.routes.items():route.reader.observe(*key)
    s.shared_ledger['レシート'].append(['unmapped-ledger-change'])
    for key,route in s.router.routes.items():
        with pytest.raises(StateError,match='UNKNOWN'):route.reader.observe(*key)
    # A mid-command raw ledger change is caught even when no ownership record
    # or Sheet cell was changed and the frame generation was accidentally stale.
    s.shared_ledger['レシート'].pop()
    def change(suite,entry,key):
        suite.after_preflight=None
        suite.shared_ledger['レシート'].append(['unmapped-ledger-change'])
    s.after_preflight=change
    with pytest.raises(StateError,match='partial_unknown'):s.call()
    assert not s.probe.calls


def test_native_pdf_pending_is_visible_to_the_whole_source_entry(s):
    from app.pdf_intake_runner import ledger_snapshot
    p=s.pdf.port;r=s.pdf.runner
    state=r.store.load();record=state['pages'][p.key];unit=record['units'][p.uid]
    prepared=unit['candidate']['posting']
    unit['intent']=dict(request_id=str(uuid4()),plan=r.post_manual(p.key,p.uid,p.snapshot_reader(),apply=False)['plan'],
        candidate_digest=unit['candidate']['record']['digest'],source_hash=record['page']['source']['source_content_hash'],
        status='claimed',claimed_at='2026-10-11T12:00:00+00:00',before_ledger_digest=digest(ledger_snapshot(s.pdf.db)),
        posting_binding=dict(unit=prepared['unit_binding'],snapshot_digest=digest(p.snapshot_reader()),plan_digest=prepared['plan_digest']))
    state['generation']+=1;r.store.save(state)
    before=s.native_reads
    with pytest.raises(StateError,match='UNKNOWN'):s.call('normal_auto')
    assert s.native_reads==before and not s.probe.calls and s.server.writes==0


def test_case_owner_cannot_be_replaced_by_authenticated_actor(s):
    s.pdf.native.frame['pdf_manual','u1']['owner']='f'*64
    with pytest.raises(StateError,match='owner_mismatch'):s.call()
    assert not s.probe.calls and s.server.writes==0


@pytest.mark.parametrize('entry,change',[('pdf_manual','range'),('pdf_auto','segmentation'),('grouped_unit','confirmation')])
def test_live_native_partition_change_is_rejected_even_if_saved_ownership_frame_is_stale(s,entry,change):
    if change=='confirmation':
        grouping=s.grouped.grouping;source=s.grouped.own.source.frame['source_id']
        p=grouping.store.load()['records'][digest(source)]['proposal']
        grouping.review(dict(request_id=str(uuid4()),source_file_id=source,source_content_hash=p['source_content_hash'],
            proposal_digest=p['proposal_digest'],grouping_revision=p['grouping_version'],operation='hold',partition=None))
    else:
        r=s.pdf.runner;p=s.pdf.port;state=r.store.load();candidate=state['pages'][p.key]['units'][p.uid]['candidate']
        if change=='range':candidate['manifest']['units'][0]['bbox']['bottom']=.4
        else:candidate['manifest']['segmentation_digest']='f'*64
        state['generation']+=1;r.store.save(state)
    with pytest.raises(StateError):s.call(entry)
    assert not s.probe.calls and s.server.writes==0


def test_disconnect_before_claim_has_no_implicit_retry_or_probe(s,monkeypatch):
    attempts=[]
    def disconnect(*args):
        attempts.append(1);raise OSError('synthetic disconnected before CAS response')
    monkeypatch.setattr(s.server,'replace_versioned',disconnect)
    rid=str(uuid4())
    with pytest.raises(OSError):s.call(rid=rid,revision=0)
    assert attempts==[1] and not s.probe.calls and s.server.writes==0
    with pytest.raises(StateError,match='result_unknown'):s.gate.inspect_result(rid)


@pytest.mark.parametrize('change',['drive_revision','native_version','partition_revision','case_id','candidate','native_tag'])
def test_changed_reviewed_native_binding_requires_redisplay_even_before_first_claim(s,change):
    if change in {'drive_revision','native_version'}:
        field='drive_revision_id' if change=='drive_revision' else 'native_source_version'
        if change=='drive_revision':s.pdf.source.frame[field]='new-drive-revision'
        s.pdf.partition.frame['source'][field]='new-drive-revision' if change=='drive_revision' else 'new-native-version'
        for n in s.pdf.native.frame.values():
            n['source'][field]=s.pdf.partition.frame['source'][field]
    elif change=='partition_revision':s.pdf.partition.frame['revision']+=1
    elif change=='case_id':s.pdf.native.frame['pdf_manual','u1']['case_id']='another-case'
    else:
        store=s.pdf.runner.store
        if change=='candidate':
            state=store.load();state['pages'][s.pdf.port.key]['units'][s.pdf.port.uid]['candidate']['prepared_at']='2026-10-12 12:00:00'
            state['generation']+=1;store.save(state)
        else:
            raw,tag=store.io.read_versioned();store.io.replace_versioned(raw,tag,raw)
    with pytest.raises(StateError):s.call()
    assert not s.probe.calls and s.server.writes==0


def test_structure_confirmation_does_not_supply_accounting_authority():
    suite=Suite(grouped_accounting=False)
    try:
        assert suite.grouped.grouping.confirmed_units(suite.grouped.own.source.frame['source_id'])
        with pytest.raises(StateError):suite.call('grouped_unit')
        assert not suite.probe.calls and suite.server.writes==0
        assert suite.grouped.accounting.executions==0
    finally:suite.sandbox.close()


def test_native_actor_binding_and_grouping_authority_revisions_are_independent(s):
    from staging_shadow.accounting_native_preflight import verify_registered_pdf_partition
    observation=s.router.observe('pdf_manual','u1')
    with pytest.raises(StateError):
        verify_registered_pdf_partition(replace(observation,owner='f'*64),s.pdf.runner.store.load(),s.pdf.port.key,s.pdf.port.uid)
    grouped=s.router.observe('grouped_unit','u1')
    assert grouped.authority_revision!=grouped.partition['revision']
    assert s.grouped.inspector.requests is None and s.grouped.accounting.executions==0


def test_old_bridge_cannot_ignore_ownership_records_in_the_opted_in_synthetic_domain(s):
    from test_unified_receipt_write import active,bridge,payload
    case=active();old,probe,_=bridge(case)
    request=payload(old,case)
    old.requests=s.requests
    for action in (lambda:old.prepare(case,'post',str(uuid4())),lambda:old.submit(case,request),lambda:old.recover_readback(case,request)):
        with pytest.raises(StateError,match='synthetic_accounting_gate_required'):action()
    assert probe.executions==0 and not s.probe.calls and s.server.writes==0
