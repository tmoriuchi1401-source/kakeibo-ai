"""Credential-free request tests; native fake DBs, fake grants, no cloud IO."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict,replace
import json
from threading import Lock,Barrier
from uuid import uuid4

import pytest

from app.drive_run_state import StateError
from app.unified_receipt_write import (WriteConfig,Pin,NativeInspection,ConditionalRequests,
    UnifiedWriteBridge,OperationRequest,SCHEMA,LABELS,OLD_P1_SOURCE,OLD_P1_HASH)
from app.unified_receipt_native import (ReceiptConfirmationPort,ExplicitUnitPostingPort,
    PDFManualUnitPort,ExistingAuthorityPort,confirmation_operation_identity,pdf_operation_identity)
from app.unified_receipt_controller import UnifiedReceiptController,render_request_detail
from app.unified_receipt_review import digest,LEGACY_UNMAPPED_HOLD
from app.unified_receipt_eligibility import current_snapshot
from test_unified_receipt_eligibility import clean,evidence,old_pair

NOW=1791700000


class Memory:
    def __init__(self):
        self.raw=json.dumps(dict(schema=SCHEMA,revision=0,operations={})).encode()
        self.revision=0;self.lock=Lock();self.writes=0;self.lost=False
    def read_versioned(self):
        with self.lock:return self.raw,'"'+str(self.revision)+'"'
    def replace_versioned(self,before,tag,after):
        with self.lock:
            if (before,tag)!=(self.raw,'"'+str(self.revision)+'"'):raise StateError('HTTP_412')
            self.raw=after;self.revision+=1;self.writes+=1
            if self.lost:raise TimeoutError('fake lost CAS response')


def active(name='single-pdf',**kw):
    # A clean NEW fixture stands in for a prospective real case. Production
    # synthetic-labelled cases themselves remain inoperable.
    c=clean(name,**kw);c.synthetic=False
    return c


class FakeNative:
    """Coordinator faults only. Native writer integrations tested separately."""
    def __init__(self,c,*,target=None):
        self.case=c;self.target=target or digest(['fake-native-operation',c.unit_key])
        self.e=evidence(c);self.e['routes'][0]['target_id']=self.target
        self.owner=True;self.shared=True;self.expiry=NOW+600;self.revision=1
        self.executions=0;self.rows=[];self.mode='complete';self.barrier=None
        self.ai_calls=self.moves=self.authority_updates=0
    def inspect(self,case,action):
        if self.barrier is not None and self.executions==0:self.barrier.wait(timeout=5)
        return NativeInspection(deepcopy(self.case),deepcopy(self.e),self.target,self.revision,
                                self.owner,self.expiry,self.shared)
    def execute(self,request):
        self.executions+=1
        self.rows.extend(['receipt','expense'] if self.mode=='partial' else ['receipt','expense','import'])
        if self.mode in {'partial','lost'}:raise TimeoutError('fake native outcome')
    def readback(self,request):return self.rows==['receipt','expense','import']


def bridge(c,port=None,action='post',io=None):
    port=port or FakeNative(c);io=io or Memory();store=ConditionalRequests(io,'fake-private-cas')
    config=WriteConfig(True,Pin(c.case_id,c.identity['source_content_hash'],c.identity['receipt_unit_id'],0),store.store_id)
    b=UnifiedWriteBridge(config,store,{(c.kind,action):port},clock=lambda:NOW)
    return b,port,io


def payload(b,c,action='post',rid=None):
    targets=b.prepare(c,action,rid or str(uuid4()))['operation_targets']
    assert len(targets)==1
    return targets[0]


@pytest.mark.parametrize('config',[WriteConfig(),WriteConfig(True),WriteConfig(True,Pin('x','a'*64,'u',0))])
def test_off_or_flag_alone_never_touches_a_client(config):
    c=active()
    class Bomb:
        store_id='fake-private-cas'
        def read(self):pytest.fail('must not read without complete configuration')
    b=UnifiedWriteBridge(config)
    if config.enabled:
        with pytest.raises(StateError):b.prepare(c,'post',str(uuid4()))
    else:assert b.prepare(c,'post',str(uuid4()))['operation_targets']==[]


@pytest.mark.parametrize('flag',['false','1','TRUE','',True])
def test_environment_defaults_strict_off(flag):
    assert not WriteConfig.from_env({'UNIFIED_RECEIPT_WRITE_ENABLED':flag}).enabled


def test_environment_requires_all_pins():
    with pytest.raises(StateError):WriteConfig.from_env({'UNIFIED_RECEIPT_WRITE_ENABLED':'true'})


@pytest.mark.parametrize('name,kind,pages,index',[
    ('image','normal',[1],1),('single-pdf','general_receipt_item_review',[1],1),
    ('multiple','general_receipt_item_review',[1],1),('multiple','general_receipt_item_review',[1],2),
    ('grouped','general_receipt_item_review',[1,2],1),('mixed','general_receipt_item_review',[1],1),
    ('mixed','medical',[2],2),('medical','medical',[1],1)])
def test_clean_coordinator_receipt_shapes_native_exact_readback(name,kind,pages,index):
    c=active(name,kind=kind,pages=pages,index=index);b,p,io=bridge(c)
    r=payload(b,c);assert b.submit(c,r)['state']=='complete'
    for entry in ('legacy','unified'):
        again={**r,'request_id':str(uuid4())}
        assert b.submit(c,again,entry=entry)['replay']
    assert p.executions==1 and p.rows==['receipt','expense','import']
    assert p.ai_calls==p.moves==p.authority_updates==0
    states=[e['state'] for e in b.requests.read()[0]['operations'][r['operation_identity']]['events']]
    assert states==['accepted','executing','awaiting_readback','complete']


@pytest.mark.parametrize('kind',['receipt_reconciliation','medical'])
def test_resolved_existing_is_link_only(kind):
    from unified_purpose_fixtures import existing_review_fixture
    f=existing_review_fixture('link_existing');r=f.prepare()
    assert f.boundary.submit(r,{})['state']=='complete'
    assert f.boundary.submit(r,{},entry='legacy')['accounting_writes']==0
    assert f.accounting_appends==0 and f.repo.get_current(f.ident)['status']=='reconciled_existing'
    assert f.ident['receipt_unit_id']==''  # Native page-local contract, no fake Unit.


@pytest.mark.parametrize('change',['snapshot','version','hash','page','unit','index','purpose'])
def test_stale_request_rejected_before_claim(change):
    c=active();b,p,io=bridge(c);r=payload(b,c)
    altered=deepcopy(c)
    if change=='snapshot':altered.fields[2][2]=731
    else:altered.identity[{'version':'source_version','hash':'source_content_hash','page':'stable_page_identity',
          'unit':'receipt_unit_id','index':'receipt_index','purpose':'review_purpose'}[change]]='changed'
    with pytest.raises(StateError):b.submit(altered,r)
    assert p.executions==io.writes==0


@pytest.mark.parametrize('missing',['source_version','source_content_hash','stable_page_identity','receipt_unit_id','receipt_index'])
def test_incomplete_identity_never_dispatches(missing):
    c=active();b,p,io=bridge(c);c.identity.pop(missing)
    assert b.prepare(c,'post',str(uuid4()))['operation_targets']==[]
    assert p.executions==io.writes==0


@pytest.mark.parametrize('bad',['expiry','shared_claim','duplicate','authority_scope','authority_snapshot','readback','replay','snapshot','page_binding'])
def test_native_preconditions_fail_closed(bad):
    c=active();b,p,io=bridge(c)
    if bad=='expiry':p.expiry=NOW
    elif bad=='shared_claim':p.shared=False
    elif bad=='duplicate':p.e['duplicate_check']['status']='pending'
    elif bad=='authority_scope':p.e['permissions']['scope_digest']='0'*64
    elif bad=='authority_snapshot':p.e['permissions']['snapshot_digest']='0'*64
    elif bad in {'readback','replay'}:p.e['writer']['readback' if bad=='readback' else 'replay_protected']=False
    elif bad=='snapshot':p.e['snapshot_digest']='0'*64
    else:p.e['page_bindings'].append(deepcopy(p.e['page_bindings'][0]))
    with pytest.raises(StateError):b.prepare(c,'post',str(uuid4()))
    assert io.writes==p.executions==0


@pytest.mark.parametrize('bad',['synthetic','history','superseded','hold','related','p1','old_review','pending','unknown','partial'])
def test_prohibited_old_and_terminal_cases_never_produce_operation(bad):
    c=active();b,p,io=bridge(c)
    if bad=='synthetic':c.synthetic=True
    elif bad=='history':c.history=True
    elif bad=='hold':c.disposition=LEGACY_UNMAPPED_HOLD
    elif bad=='related':c.related_to='old-case'
    elif bad=='p1':c.identity.update(source_file_id=OLD_P1_SOURCE,source_content_hash=OLD_P1_HASH)
    elif bad=='old_review':c.identity['review_id']='review-da12dd1ff1262d92d5e98e6a'
    else:c.authority_status=bad
    assert b.prepare(c,'post',str(uuid4()))['operation_targets']==[]
    with pytest.raises(StateError):b.submit(c,{})
    assert p.executions==io.writes==0


@pytest.mark.parametrize('n',[0,1,2])
def test_operation_target_count_is_zero_or_one_or_safety_error(n):
    c=active();b,p,io=bridge(c)
    if n==0:p.e['routes']=[]
    if n==2:p.e['routes']+=[{**p.e['routes'][0],'target_id':'0'*64}]
    if n==1:assert len(payload(b,c))>0
    else:
        with pytest.raises(StateError):b.prepare(c,'post',str(uuid4()))
    assert p.executions==io.writes==0


def test_owner_confirmation_wait_has_no_request_or_authority():
    c=active();b,p,io=bridge(c);p.owner=False
    assert b.prepare(c,'post',str(uuid4()))==dict(state='validated_awaiting_owner',label=LABELS['validated_awaiting_owner'],operation_targets=[])
    assert io.writes==p.executions==0


@pytest.mark.parametrize('mode',['partial','lost'])
def test_faults_stop_replay_and_recover_only_complete_three_table_readback(mode):
    c=active();b,p,io=bridge(c);r=payload(b,c);p.mode=mode
    with pytest.raises(StateError,match='partial_unknown'):b.submit(c,r)
    before=deepcopy(p.rows)
    assert b.submit(c,r)['state']=='partial_unknown'
    assert p.executions==1 and p.rows==before
    result=b.recover_readback(c,r)
    assert result['state']==('complete' if mode=='lost' else 'partial_unknown')
    assert result['accounting_writes']==0 and p.executions==1 and p.rows==before


def test_lost_cas_acceptance_never_dispatches_or_fills_gap():
    c=active();b,p,io=bridge(c);r=payload(b,c);io.lost=True
    with pytest.raises(TimeoutError):b.submit(c,r)
    io.lost=False
    assert b.submit(c,r)['state']=='accepted' and p.executions==0 and not p.rows
    assert b.recover_readback(c,r)['state']=='partial_unknown' and p.executions==0


def test_double_click_and_old_new_concurrent_claim_one_execution():
    c=active();b,p,io=bridge(c);r=payload(b,c)
    barrier=Barrier(8)
    def send(n):
        barrier.wait(timeout=5)
        try:return b.submit(c,{**r,'request_id':str(uuid4())},entry='legacy' if n%2 else 'unified')
        except StateError:return None
    with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(send,range(8)))
    assert p.executions==1 and len(p.rows)==3
    assert len(b.requests.read()[0]['operations'])==1
    assert any(x and x['state']=='complete' for x in results)


def test_different_final_identity_for_one_unit_never_makes_second_operation():
    c=active();b,p,io=bridge(c);r=payload(b,c);b.submit(c,r)
    first=deepcopy((p.rows,b.requests.read()[0],io.writes));assert p.executions==1
    other=deepcopy(c);other.legacy_id+='other-purpose'
    q=FakeNative(other);fresh=io.revision
    b2=UnifiedWriteBridge(WriteConfig(True,Pin(other.case_id,other.identity['source_content_hash'],other.identity['receipt_unit_id'],fresh),'fake-private-cas'),
                          ConditionalRequests(io,'fake-private-cas'),{(other.kind,'post'):q},clock=lambda:NOW)
    r2=payload(b2,other)
    # A different card still resolves to the SAME native final operation.
    # Its scope/fingerprint conflicts; this is not the distinct-operation case.
    assert r2['operation_identity']==r['operation_identity']
    with pytest.raises(StateError,match='^unified_operation_conflict$'):b2.submit(other,r2)
    assert (p.rows,b.requests.read()[0],io.writes)==first and q.executions==0
    # Now deliberately attempt another native operation on the same Unit.
    # This is a negative fixture; the Unit fence must refuse its claim.
    q.target=digest(['fake-other-native-operation',other.unit_key]);q.e['routes'][0]['target_id']=q.target
    r2=payload(b2,other)
    assert r2['operation_identity']!=r['operation_identity']
    assert r2['receipt_unit_id']==r['receipt_unit_id']
    with pytest.raises(StateError,match='^unified_request_id_conflict$'):
        b2.submit(other,{**r2,'request_id':r['request_id']})
    with pytest.raises(StateError,match='already_owned'):b2.submit(other,r2)
    assert q.executions==0 and p.executions==1 and len(b.requests.read()[0]['operations'])==1
    assert (p.rows,b.requests.read()[0],io.writes)==first and p.readback(OperationRequest.parse(r)) is True
    assert b.submit(c,r)['replay'] and b.submit(c,r)['accounting_writes']==0
    assert (p.rows,b.requests.read()[0],io.writes)==first


@pytest.mark.parametrize('extra',['evidence','actor','routes','permission'])
def test_browser_cannot_supply_authority_fields(extra):
    c=active();b,p,io=bridge(c);r={**payload(b,c),extra:True}
    with pytest.raises(StateError,match='request_invalid'):b.submit(c,r)
    assert p.executions==io.writes==0


@pytest.mark.parametrize('field',['case_id','source_file_id','source_content_hash','source_version','stable_page_identity','receipt_unit_id',
    'receipt_index','review_purpose','current_snapshot_digest','authority_revision','requested_action','request_id','expected_state_revision','operation_identity'])
def test_every_required_request_field_is_fixed(field):
    c=active();b,p,io=bridge(c);r=payload(b,c);r.pop(field)
    with pytest.raises(StateError):b.submit(c,r)
    assert p.executions==io.writes==0


def medical_native():
    from test_receipt_confirmation import medical,confirm,TITLE
    service,store,db,verify,src=medical();verify.return_value={'version':'1'}
    confirm(db);service.capture_inputs();key=next(iter(service.items));item=service.items[key]
    c=active('medical',kind='medical');c.identity.update(source_file_id=src['source_id'],source_content_hash=src['sha256'],
                                                        source_version=src['version'],review_id=key)
    markers=('date','merchant','amount','category','payment','action','duplicate_target','memo')
    c.fields=[[k,k,deepcopy(v)] for k,v in zip(markers,item['inputs'])]
    fake=FakeNative(c,target=confirmation_operation_identity(item))
    port=ReceiptConfirmationPort(service,key,lambda *args:fake.inspect(c,'post'))
    b,_,io=bridge(c,port)
    r=payload_after_gate(b,c,port)
    return c,b,port,r,service,db,store,fake,io


def payload_after_gate(b,c,port):
    holder={};port.bind_shared_gate(b,lambda:c,lambda:holder['payload'])
    holder['payload']=payload(b,c)
    return holder['payload']


def test_medical_actual_manual_validator_writer_and_legacy_gate_replay():
    c,b,p,r,service,db,store,fake,io=medical_native()
    before=deepcopy(db.rows['領収書確認'])
    assert b.submit(c,r)['state']=='complete'
    assert len(db.rows['レシート'])==len(db.rows['支出明細'])==len(db.rows['取込データ'])==1
    assert db.rows['支出明細'][0][4:7]==[100,'医療・保険','病院']
    assert service.apply_confirmations(keys=[p.review_id])==0
    assert db.rows['領収書確認']==before and fake.ai_calls==fake.moves==0


def test_actual_medical_unified_and_legacy_race_same_identity_one_native_write():
    c,b,p,r,service,db,store,fake,io=medical_native();barrier=Barrier(2)
    def call(legacy):
        barrier.wait(timeout=5)
        try:return service.apply_confirmations(keys=[p.review_id]) if legacy else b.submit(c,r)
        except StateError:return None
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(call,[False,True]))
    assert len(db.rows['レシート'])==len(db.rows['支出明細'])==len(db.rows['取込データ'])==1
    ops=b.requests.read()[0]['operations'];assert list(ops)==[confirmation_operation_identity(service.items[p.review_id])]


def test_unsent_native_cell_edit_stops_before_claim_no_overwrite():
    c,b,p,r,service,db,store,fake,io=medical_native();db.rows['領収書確認'][0][8]='未送信の施設'
    before=deepcopy((db.rows,store.value))
    with pytest.raises(StateError,match='unsent'):b.submit(c,r)
    assert (db.rows,store.value)==before and io.writes==0


def test_ui_draft_uses_manual_validator_preserves_input_and_requires_native_owner():
    c,b,p,r,service,db,store,fake,io=medical_native()
    controller=UnifiedReceiptController(b,lambda:[c],lambda ctx:ctx=='verified-post')
    before=deepcopy((db.rows,store.value));fresh=digest(current_snapshot(c))
    result=controller.validate_inputs(c.case_id,{'amount':101},fresh,'verified-post')
    assert result['state']=='validated_awaiting_owner' and result['operation_targets']==[]
    assert result['draft'][2][2]==101 and (db.rows,store.value)==before and io.writes==0
    with pytest.raises(StateError):controller.submit(r,'unverified-get')


@pytest.mark.parametrize('value',['',0,-1,'AI候補',True])
def test_invalid_medical_manual_amount_never_issues_permission(value):
    c,b,p,r,service,db,store,fake,io=medical_native()
    controller=UnifiedReceiptController(b,lambda:[c],lambda _:True)
    before=deepcopy((db.rows,store.value))
    with pytest.raises((StateError,ValueError)):controller.validate_inputs(c.case_id,{'amount':value},digest(current_snapshot(c)),None)
    assert (db.rows,store.value)==before and io.writes==0


def test_scoped_confirmation_cannot_close_unrelated_reviews():
    c,b,p,r,service,db,store,fake,io=medical_native()
    item=deepcopy(service.items[p.review_id]);item['status']='waiting';item['inputs'][5]='既存値を維持'
    service.items['unrelated-review']=deepcopy(item);before=deepcopy(service.items['unrelated-review'])
    b.submit(c,r)
    assert service.items['unrelated-review']==before


def test_old_unmapped_case_does_not_stop_unrelated_new_case():
    old,recon=old_pair();c=active();b,p,io=bridge(c)
    controller=UnifiedReceiptController(b,lambda:[old,recon,c],lambda _:True)
    assert controller.submit(payload(b,c),None)['state']=='complete'
    with pytest.raises(StateError):controller.submit({'case_id':old.case_id},None)
    assert p.executions==1


def test_request_ui_off_held_owner_and_enabled_single_target():
    c=active();b,p,io=bridge(c);r=b.prepare(c,'post',str(uuid4()))
    assert '<form ' in render_request_detail(c,r,submit_url='/request',csrf_token='fake-csrf')
    assert '<form ' not in render_request_detail(c,UnifiedWriteBridge().prepare(c,'post',str(uuid4())),submit_url='/request',csrf_token='fake-csrf')
    assert '<form ' not in render_request_detail(c,dict(state='held',operation_targets=[]))
    owner=render_request_detail(c,dict(state='validated_awaiting_owner',operation_targets=[]),owner_url='/owner')
    assert '本人確認を開く' in owner
    with pytest.raises(StateError):render_request_detail(c,dict(state='not_received',operation_targets=[{},{}]))


def test_ui_fields_escape_html_and_no_fixed_rows():
    c=active();c.fields.append(['memo','メモ','<script>bad()</script>'])
    view=render_request_detail(c,dict(state='held',operation_targets=[]))
    assert '<script>' not in view and '&lt;script&gt;' in view and 'width=device-width' in view
    assert c.case_id in view and 'data-field="memo"' in view and '案件終了' in view


def explicit_native(page=4):
    from test_receipt_unit_posting import setup,RID,CONTEXT,ATTEMPT,NOW as time
    rd,store,writer,projection=setup(page)
    ident=deepcopy(rd.snapshot['identity']);c=active('explicit')
    c.identity.update(ident);c.identity.update(source_version='fake-version-1',review_purpose='general_receipt_item_review',
        page_numbers=[page],page_identities=[ident['stable_page_identity']])
    c.fields=deepcopy(rd.snapshot['rows'])
    fake=FakeNative(c,target=digest(['receipt-unit-posting',CONTEXT,RID]))
    fake.e['writer']['name']='receipt_unit_posting';fake.e['routes'][0]['writer']='receipt_unit_posting'
    fake.expiry=time+600
    port=ExplicitUnitPostingPort(rd,store,writer,projection,RID,CONTEXT,ATTEMPT,
        lambda *_:fake.inspect(c,'post'),clock=lambda:time)
    b,_,io=bridge(c,port);b.clock=lambda:time
    return c,b,port,payload(b,c,rid=RID),rd,store,writer,projection


@pytest.mark.parametrize('page',[4,10])
def test_real_unit_snapshot_authority_materializer_fake_tables(page):
    c,b,p,r,rd,store,writer,projection=explicit_native(page)
    assert b.submit(c,r)['state']=='complete' and store.value['state']=='complete'
    calls=deepcopy(writer.calls);assert len(calls)==3
    assert b.submit(c,r,entry='legacy')['accounting_writes']==0 and writer.calls==calls


@pytest.mark.parametrize('failure',['before','partial','after'])
def test_native_unit_faults_and_readback_recovery_never_append_again(failure):
    c,b,p,r,rd,store,writer,projection=explicit_native()
    original=writer.append_raw
    def fail(title,rows):
        if title==('取込データ' if failure=='after' else '支出明細'):writer.fail=failure
        original(title,rows)
    writer.append_raw=fail
    if failure=='after':assert b.submit(c,r)['state']=='complete'
    else:
        with pytest.raises(StateError,match='partial_unknown'):b.submit(c,r)
        assert b.recover_readback(c,r)['state']=='partial_unknown'
    calls=deepcopy(writer.calls);b.submit(c,r)
    assert writer.calls==calls


def test_native_unit_lost_coordinator_response_recovers_readonly_three_tables():
    c,b,p,r,rd,store,writer,projection=explicit_native();original=p.execute
    def lost(request):original(request);raise TimeoutError('fake coordinator response loss')
    p.execute=lost
    with pytest.raises(StateError,match='partial_unknown'):b.submit(c,r)
    before=deepcopy((rd.tables,store.value,writer.calls))
    assert b.recover_readback(c,r)['state']=='complete'
    assert (rd.tables,store.value,writer.calls)==before


@pytest.mark.parametrize('terminal',[False,True])
def test_no_source_move_capability_even_if_a_unit_completes(terminal):
    c=active('mixed',pages=[1,2]);b,p,io=bridge(c)
    if terminal:b.submit(c,payload(b,c))
    assert p.moves==0 and not hasattr(b,'move_source') and not hasattr(p,'archive_source')


def test_normal_image_actual_receipt_confirmation_existing_writer():
    from test_receipt_confirmation import Store,DB,TITLE,ReceiptConfirmation
    from app.models import ReceiptResult,ReceiptItem
    from app.receipt_reimport_production import target_snapshot
    from hashlib import sha256
    from app.page_receipt_model import SourceRef,PageUnit,PageReceiptExtraction,stable_page,build_receipt_units
    from app.pdf_intake_authority import review_identity
    from app.receipt_plan.planning import PlanningDB
    from app.receipt_pipeline import ReceiptPipeline
    from app.unified_receipt_review import Case
    raw=b'FAKE-new-image-only';store,db=Store(),DB()
    src=dict(source_id='fake-new-image',version='1',sha256=sha256(raw).hexdigest(),mime_type='image/png')
    parsed=ReceiptResult(date='2026-10-11',merchant='Fake shop',total=120,
        items=[ReceiptItem(name='Fake food',amount=120,major_category='食費',minor_category='食品')])
    # ReceiptConfirmation compares the initial native needs_review receipt and
    # import rows. No expense exists until the explicit confirmation.
    initial=PlanningDB(db.categories())
    ReceiptPipeline(initial,None,clock=lambda:'2026-10-11 12:00:00')._materialize_result(
        parsed,src['source_id'],'https://drive.google.com/file/d/'+src['source_id']+'/view',['fixture_owner_review_required'])
    for title,row in initial.plan:db.rows[title].append(deepcopy(row))
    assert len(db.rows['レシート'])==len(db.rows['取込データ'])==1 and not db.rows['支出明細']
    store.value['manifest']=dict(sources=[src],folder_id='fake-folder')
    store.value['records'][src['source_id']]=dict(phase='complete',parsed=parsed.model_dump(),before=target_snapshot(
        dict(receipt_rows=db.rows['レシート'],import_rows=db.rows['取込データ'],expense_rows=[],review_rows=[]),src['source_id']))
    service=ReceiptConfirmation(store,db,lambda *_:dict(version='1'));service.prepare_general();service.render()
    db.rows[TITLE][0][12]='候補明細で確定';service.capture_inputs();key=next(iter(service.items))
    source=SourceRef(source_file_id=src['source_id'],source_content_hash=src['sha256'],source_kind='image',page_count=1)
    page=PageUnit(source=source,page_number=1,stable_page_identity=stable_page(source,1),automatic_classification='normal',
        automatic_reason='fixture_normal',observation_complete=True,extraction_status='extracted',observation_render_hash=digest(raw.hex()),
        review_identity='0'*64,authority_revision=1).model_dump();page['review_identity']=review_identity(page)
    reading=PageReceiptExtraction.model_validate(dict(receipts=[dict(bbox=dict(left=0,top=0,right=1,bottom=1),
        receipt=parsed.model_dump(),item_boxes=[])],separation_complete=True,mixed_page_kind_suspected=False,cross_page_continuation_suspected=False))
    units=build_receipt_units(PageUnit.model_validate(page),reading,reading,db.categories())
    assert units['status']=='receipts_observed' and len(units['units'])==1
    unit=units['units'][0]
    identity=dict(source_file_id=src['source_id'],source_content_hash=src['sha256'],source_version=src['version'],review_id=key,
        stable_page_identity=page['stable_page_identity'],page_number=1,page_numbers=[1],page_identities=[page['stable_page_identity']],
        receipt_unit_id=unit['receipt_unit_id'],receipt_index=unit['receipt_index'],review_purpose='normal')
    c=Case(key,'receipt-confirmation-v1',identity,[],'領収書確認','normal',authority_status='waiting')
    markers=('date','merchant','amount','category','payment','action','duplicate_target','memo')
    c.fields=[[k,k,v] for k,v in zip(markers,service.items[key]['inputs'])]
    fake=FakeNative(c,target=confirmation_operation_identity(service.items[key]))
    fake.e['writer']['name']='receipt_confirmation';fake.e['routes'][0]['writer']='receipt_confirmation'
    port=ReceiptConfirmationPort(service,key,lambda *_:fake.inspect(c,'post'));b,_,io=bridge(c,port)
    r=payload_after_gate(b,c,port);assert b.submit(c,r)['state']=='complete'
    assert db.rows['支出明細'][0][3:7]==['Fake food',120,'食費','食品']
    assert r['current_snapshot_digest']==digest(current_snapshot(c)) and r['authority_revision']==1
    before=deepcopy(db.rows);writes=db.writes
    assert b.submit(c,r,entry='legacy')['replay'] and db.rows==before and db.writes==writes


def pdf_native(*,index=1,count=1,number=1,page_count=1):
    from hashlib import sha256
    from types import SimpleNamespace
    from app.page_receipt_model import SourceRef,PageUnit,PageReceiptExtraction,stable_page,build_receipt_units
    from app.pdf_intake_authority import review_identity,binding
    from app.pdf_intake_registry import RegistryStore
    from app.pdf_intake_runner import PageIntake
    from app.general_receipt_completion import drafts_for_page
    from app.receipt_item_review import prepare,fields,card,snapshot as native_snapshot
    from app.receipt_item_confirmation import annotated,ConfirmedItems
    from app.receipt_item_review_ui import encoded_rows,read_snapshot
    from app.models import ReceiptResult
    from test_pdf_intake_authority import proof
    from test_pdf_intake_registry import Memory as RegistryMemory
    from test_pdf_intake_runner import DB,CATEGORIES
    raw=b'%PDF-FAKE-UNIT-WRITER';source=SourceRef(source_file_id='fake-complete-new-pdf',source_content_hash=sha256(raw).hexdigest(),page_count=page_count)
    page=PageUnit(source=source,page_number=number,stable_page_identity=stable_page(source,number),automatic_classification='normal',
        automatic_reason='fake_local_normal',observation_complete=True,extraction_status='extracted',observation_render_hash='b'*64,
        review_identity='0'*64,authority_revision=1).model_dump();page['review_identity']=review_identity(page);p=PageUnit.model_validate(page)
    receipts=[]
    for n in range(count):
        result=dict(date='2026-10-11',merchant='Fake shop '+str(n),total=730+n,payment_method='',transaction_kind='purchase',note='',
            items=[dict(name='牛乳',quantity=1,amount=250+n,major_category='食費',minor_category='食料品',note='',confidence=.99),
                   dict(name='洗剤',quantity=1,amount=480,major_category='日用品',minor_category='洗剤',note='',confidence=.99)])
        top=n/count;bottom=(n+1)/count;height=bottom-top
        boxes=[dict(left=.1,top=top+height*offset,right=.9,bottom=top+height*(offset+.15)) for offset in (.1,.5)]
        receipts.append(dict(bbox=dict(left=0,top=top,right=1,bottom=bottom),receipt=result,item_boxes=boxes))
    reading=PageReceiptExtraction.model_validate(dict(receipts=receipts,separation_complete=True,mixed_page_kind_suspected=False,cross_page_continuation_suspected=False))
    extracted=build_receipt_units(p,reading,reading,CATEGORIES)
    assert extracted['status']=='receipts_observed' and len(extracted['units'])==count
    assert len({u['receipt_unit_id'] for u in extracted['units']})==count
    assert [u['receipt_index'] for u in extracted['units']]==list(range(1,count+1))
    manifest={k:extracted[k] for k in ('page_key','segmentation_digest')}
    manifest.update(source=source.model_dump(),page_number=number,stable_page_identity=p.stable_page_identity,
                    units=[{k:u[k] for k in ('receipt_unit_id','receipt_index','parent_page_identity','bbox','accounting_status')} for u in extracted['units']])
    draft=drafts_for_page(p,manifest,reading,reading,categories=CATEGORIES)[index-1]
    parsed=ReceiptResult.model_validate(draft['parsed']);record=annotated(prepare(draft,CATEGORIES),[parsed,parsed],CATEGORIES)
    uid=draft['identity']['receipt_unit_id'];store=RegistryStore(RegistryMemory(),'anchor');key=store.register(page);db=DB()
    state=store.load();state['pages'][key]['units'][uid]=dict(status='needs_review',candidate=dict(record=record,manifest=manifest,
        view=card(record,categories=CATEGORIES),prepared_at='2026-10-11 12:00:00'),posting_authority=None,intent=None,readback=None)
    state['generation']+=1;store.save(state)
    runner=PageIntake(store,lambda _:raw,db,SimpleNamespace(verify=lambda *_:None),lambda *_:pytest.fail('no AI posting'))
    current=fields(record);current.update(action='記帳する',structure_confirmation='確認済み')
    snapshot=native_snapshot(card(record,categories=CATEGORIES),current)
    assert runner.prepare_manual(key,uid,snapshot)['status']=='awaiting_authenticated_confirmation'
    state=store.load();unit=state['pages'][key]['units'][uid];prepared=unit['candidate']['posting']
    expected=binding(page,'receipt_posting',unit=prepared['unit_binding'],snapshot_digest=digest(snapshot),plan_digest=prepared['plan_digest'])
    sealer,sealed=proof(expected);verified=sealer.verify(sealed,expected);rid=verified['request_id']
    owner=ConfirmedItems(rid,record['digest'],digest(snapshot),digest(current),digest(record['legacy']['identity']),
                         verified['actor_id'],100,'e'*64)
    unit['candidate']['confirmation_journal']=dict(requests={rid:dict(proof=asdict(owner))})
    unit['posting_authority']=sealed;state['generation']+=1;store.save(state)
    runner.proof_client=SimpleNamespace(verify=sealer.verify)
    c=active('single-pdf',index=index,pages=[number]);c.identity.update(source_file_id=source.source_file_id,
        source_content_hash=source.source_content_hash,source_version='fake-version-1',stable_page_identity=page['stable_page_identity'],
        page_identities=[page['stable_page_identity']],receipt_unit_id=uid,receipt_index=index)
    c.fields=deepcopy(snapshot['rows']);fake=FakeNative(c,target=pdf_operation_identity(page,uid))
    port=PDFManualUnitPort(runner,key,uid,lambda:deepcopy(snapshot),lambda *_:fake.inspect(c,'post'))
    b,_,io=bridge(c,port)
    return c,b,port,payload(b,c,rid=rid),db,runner


@pytest.mark.parametrize('index,count,number,page_count',[(1,1,1,1),(1,2,1,1),(2,2,1,1),(1,1,2,3)])
def test_registered_pdf_real_snapshot_gate_authority_and_materializer(index,count,number,page_count):
    c,b,p,r,db,runner=pdf_native(index=index,count=count,number=number,page_count=page_count)
    assert b.submit(c,r)['state']=='complete'
    assert len(db.writes)==3 and [len(db.data[t]) for t in ('レシート','支出明細','取込データ')]==[1,2,1]
    assert runner.store.load()['pages'][p.key]['units'][p.uid]['status']=='imported'
    before=deepcopy(db.data);assert b.submit(c,r,entry='legacy')['replay'] and db.data==before
    assert r['current_snapshot_digest']==digest(current_snapshot(c)) and r['authority_revision']==1
    if count==2 and index==1:
        # Process BOTH spatial Units against the SAME native registry/ledger
        # and shared request CAS, not two unrelated one-Unit databases.
        c2,b2,p2,_,_,runner2=pdf_native(index=2,count=count,number=number,page_count=page_count)
        assert c2.source_id==c.source_id and c2.identity['stable_page_identity']==c.identity['stable_page_identity']
        assert c2.identity['source_content_hash']==c.identity['source_content_hash']
        assert c2.identity['receipt_unit_id']!=c.identity['receipt_unit_id'] and c2.identity['receipt_index']==2
        latest=runner.store.load();latest['pages'][p.key]['units'][p2.uid]=deepcopy(runner2.store.load()['pages'][p2.key]['units'][p2.uid])
        latest['generation']+=1;runner.store.save(latest);p2.runner=runner
        b2.requests=b.requests
        b2.config=WriteConfig(True,Pin(c2.case_id,c2.identity['source_content_hash'],p2.uid,b.requests.read()[0]['revision']),b.requests.store_id)
        r2=payload(b2,c2)
        assert r2['operation_identity']!=r['operation_identity']
        assert b2.submit(c2,r2)['state']=='complete'
        assert [len(db.data[t]) for t in ('レシート','支出明細','取込データ')]==[2,4,2]
        assert len(db.writes)==6 and len(b.requests.read()[0]['operations'])==2
        assert all(len({row[0] for row in rows})==len(rows) for rows in db.data.values())
        before=deepcopy(db.data);assert b2.submit(c2,r2,entry='legacy')['replay'] and db.data==before and len(db.writes)==6


@pytest.mark.parametrize('action',['confirm_grouping','confirm_page_kind','confirm_privacy','link_existing'])
def test_native_authority_route_arguments_and_idempotent_dispatch(action):
    from unified_purpose_fixtures import existing_review_fixture
    f=existing_review_fixture(action);r=f.prepare()
    expected=f.observe(f.case_id,f.purpose,action,f.rid).native_arguments
    assert f.boundary.submit(r,{})['state']=='complete' and f.native_calls==1
    assert f.saved_arguments==[expected]
    assert f.boundary.submit(r,{},entry='legacy')['replay'] and f.native_calls==1
    assert f.accounting_appends==f.ai_sends==f.source_moves==0
