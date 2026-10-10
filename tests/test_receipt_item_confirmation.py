from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
import json
import pytest
from app import receipt_item_review as r,receipt_item_confirmation as c
from app.receipt_item_runner import accounting_plan,validate_plan
from app.receipt_item_auth_transport import AuthenticatedItemReview,validate_item_auth
from app.human_general_auth_transport import AuthRequestStore,empty_auth_state,ISSUER
from app.drive_run_state import StateError
from app.models import ReceiptResult
from app.page_receipt_model import digest
from test_receipt_item_review import candidate,CATS,service
from test_human_general_auth_transport import Rig,keys,REDIRECT,OWNER
from test_page_receipts import UUID,BINDING


def annotated():
    record,p,raw,manifest=candidate(unstable=True)
    first=ReceiptResult.model_validate(record['legacy']['parsed'])
    second=first.model_copy(deep=True);second.items[0].name='牛乳'
    return c.annotated(record,[first,second],CATS),p,raw,manifest


def test_item_local_evidence_does_not_blank_stable_sibling_or_clear_gate():
    record,*_=annotated()
    assert [i['category'] for i in record['items']]==['','日用品｜消耗品']
    assert r.evaluate(record,r.fields(record),CATS)['status']=='needs_review'
    assert r.card(record,categories=CATS)['review_items']==[record['items'][0]['item_id']]


def test_width_only_difference_can_prefill_but_never_attests_structure():
    record,*_=candidate(unstable=True)
    one=ReceiptResult.model_validate(record['legacy']['parsed']);one.items[0].name='Ａ商品'
    x=deepcopy(record['legacy']);x['parsed']=one.model_dump();x.pop('candidate_digest');x['candidate_digest']=digest(x)
    record=r.prepare(x,CATS);two=one.model_copy(deep=True);two.items[0].name='A商品'
    record=c.annotated(record,[one,two],CATS)
    assert record['items'][0]['category']=='食費｜食品'
    assert r.evaluate(record,r.fields(record),CATS)['status']=='needs_review'


def session(keys):
    record,p,raw,manifest=annotated();current=r.fields(record)
    current['items'][0].update(name='牛乳',category='食費｜食品')
    current.update(action='記帳する',structure_confirmation='確認済み')
    view=r.card(record,categories=CATS);snapshot=r.snapshot(view,current)
    rig=Rig(keys);rig.transport.payload=json.dumps(empty_auth_state(BINDING)).encode()
    store=AuthRequestStore(rig.transport,BINDING,preflight=lambda:None,validator=validate_item_auth)
    data={'snapshot':snapshot,'record':record}
    def readers():return data['record'],data['snapshot'],CATS
    gateway=AuthenticatedItemReview(store,rig.policy,readers=readers,redirect_uri=REDIRECT,
        exchange_code=rig.exchange,clock=lambda:rig.now)
    gateway.prepare(c.binding(record,snapshot,UUID),request_id=UUID)
    ticket=gateway.begin(UUID);rig.ticket=ticket
    gateway.callback(ticket,cookie=ticket.cookie,state=ticket.state,code='synthetic-code')
    return gateway,ticket,record,current,snapshot,rig,data


def test_signed_oidc_explicit_snapshot_and_existing_writer_plan(keys):
    gateway,ticket,record,current,snapshot,rig,data=session(keys);saved=[]
    class Save:
        def confirm(self,record,snapshot,current,proof):
            plan=r.evaluate(record,current,CATS,confirmation=proof)
            assert plan['status']=='ready_to_confirm'
            rows=accounting_plan(record,plan,CATS,timestamp='synthetic-clock')
            assert sum(t=='レシート' for t,_ in rows)==1 and sum(t=='支出明細' for t,_ in rows)==2
            assert sum(row[4] for t,row in rows if t=='支出明細')==730
            saved.append(proof);return {'authority_digest':proof.authority_digest}
    # A selected dropdown is only intent before explicit authenticated POST.
    assert r.evaluate(record,current,CATS)['status']=='needs_review'
    result=gateway.confirm(ticket,cookie=ticket.cookie,csrf=ticket.csrf,origin=gateway.origin,
        method='POST',confirmation_factory=lambda actor:Save())
    assert result['status']=='complete' and len(saved)==1
    assert r.evaluate(record,current,CATS,confirmation=saved[0])['item_field_provenance'][record['items'][0]['item_id']]['name']=='human_override'
    with pytest.raises(StateError):gateway.confirm(ticket,cookie=ticket.cookie,csrf=ticket.csrf,origin=gateway.origin,
        method='POST',confirmation_factory=lambda _:pytest.fail('replay'))
    assert len(saved)==1


@pytest.mark.parametrize('change',['name','amount','category','action','selection','source','item_id','expired','wrong_origin','null_origin','csrf'])
def test_stale_or_tampered_request_cannot_confirm(keys,change):
    gateway,ticket,record,current,snapshot,rig,data=session(keys)
    origin=gateway.origin;csrf=ticket.csrf
    if change in {'name','amount','category','item_id'}:
        data['snapshot']['items'][0][change]=999 if change=='amount' else 'changed'
    if change in {'action','selection'}:
        field='action' if change=='action' else 'structure_confirmation'
        next(row for row in data['snapshot']['rows'] if row[0]==field)[2]='未選択'
    if change=='source':data['record']['legacy']['identity']['source_content_hash']='f'*64
    if change=='expired':rig.now+=601
    if change=='wrong_origin':origin='https://attacker.example.test'
    if change=='null_origin':origin='null'
    if change=='csrf':csrf='changed'
    with pytest.raises(StateError):gateway.confirm(ticket,cookie=ticket.cookie,csrf=csrf,origin=origin,
        method='POST',confirmation_factory=lambda _:pytest.fail('must not reach store'))


def test_human_confirmation_never_resolves_segmentation_or_fake_adjustment(keys):
    gateway,ticket,record,current,snapshot,rig,data=session(keys)
    # Trusted new candidate containing an additional non-overridable gate.
    record['legacy']['hard_issues'].append('segmentation_unstable')
    record['legacy'].pop('candidate_digest');record['legacy']['candidate_digest']=digest(record['legacy'])
    # This changes candidate identity: old auth binding must fail, not clear it.
    record.pop('digest');record['digest']=digest(record)
    with pytest.raises(StateError):gateway.fresh(gateway.store.load()['requests'][UUID])


def test_correction_roundtrip_keeps_ids_and_signed_amounts():
    record,*_=annotated();current=r.fields(record)
    current['items'][0]['name']='本人訂正';current['items'][0]['amount']=249
    current['items'][1]['amount']=481;current['amount']=730
    view=r.card(record,categories=CATS);snapshot=r.snapshot(view,current)
    assert r.check_snapshot(snapshot,view)==current
    assert r.evaluate(record,current,CATS)['item_sum']==730
    assert [i['item_id'] for i in current['items']]==[i['item_id'] for i in record['items']]


def test_prefill_and_bulk_preserve_existing_owner_input():
    record,*_=annotated();current=r.fields(record);current['items'][0]['category']='日用品｜消耗品'
    result=r.bulk_category(record,current,'食費｜食品',CATS)
    assert [i['category'] for i in result['items']]==['日用品｜消耗品','日用品｜消耗品']


def completed(keys):
    gateway,ticket,record,current,snapshot,rig,data=session(keys);saved=[]
    class Save:
        def confirm(self,record,snapshot,current,proof):
            saved.append(proof);return {'authority_digest':proof.authority_digest}
    gateway.confirm(ticket,cookie=ticket.cookie,csrf=ticket.csrf,origin=gateway.origin,method='POST',
        confirmation_factory=lambda _:Save())
    proof=saved[0];plan=r.evaluate(record,current,CATS,confirmation=proof)
    request={'binding':c.binding(record,snapshot,UUID),'proof':asdict(proof),'snapshot':snapshot,
        'plan':{'input':current,'validation':plan},'status':'validated_not_written'}
    return record,current,snapshot,proof,request


@pytest.mark.parametrize('change',[None,'actor','snapshot','request','proof','category','etag'])
def test_runner_private_authenticated_plan_and_tamper(keys,change):
    from services.human_general.receipt_review_runtime import validate_journal
    record,current,snapshot,proof,request=completed(keys)
    journal={'schema':'receipt-item-confirmations-v1','scope':BINDING,'requests':{UUID:request},'generation':1}
    validate_journal(journal,BINDING)
    readers=SimpleNamespace(read=lambda:(record,snapshot,CATS));count=[0]
    def read():
        count[0]+=1
        return json.dumps(journal).encode(),('"changed"' if change=='etag' and count[0]>1 else '"current"')
    store=SimpleNamespace(read_versioned=read);owner=proof.actor_id
    if change=='actor':owner='f'*64
    if change=='snapshot':snapshot=deepcopy(snapshot);snapshot['items'][0]['name']='changed'
    if change=='request':request['binding']['request_id']='changed'
    if change=='proof':request['proof']['authority_digest']='f'*64
    if change=='category':readers.read=lambda:(record,snapshot,[CATS[1]])
    if change:
        with pytest.raises(StateError):validate_plan(readers,store,UUID,snapshot,owner)
    else:assert validate_plan(readers,store,UUID,snapshot,owner)[1]['status']=='ready_to_confirm'


def test_receipt_confirmation_factory_cas_then_compact_history(keys):
    from services.human_general.receipt_review_runtime import ReceiptReviewRuntime,Journal
    from services.human_general.backend import canonical
    from app.receipt_audit import MemoryAuditRepository
    record,current,snapshot,proof,request=completed(keys);written=[]
    value={'schema':'receipt-item-confirmations-v1','scope':BINDING,'requests':{},'generation':0}
    rt=object.__new__(ReceiptReviewRuntime)
    rt.readers=SimpleNamespace(owner_actor_id=proof.actor_id,read=lambda:(record,snapshot,CATS))
    rt.audit=MemoryAuditRepository()
    def replace(before,tag,after):
        assert before==canonical(value) and tag=='"current"'
        value.clear();value.update(json.loads(after));written.append(after)
    rt.journal=SimpleNamespace(read_versioned=lambda:(canonical(value),'"current"'),replace=replace)
    actor=SimpleNamespace(actor_id=proof.actor_id)
    # The real web host expects factory(actor), not a pre-created adapter.
    save=rt.factory(UUID,expected_tag='"current"')(actor)
    assert save.confirm(record,snapshot,current,proof)['authority_digest']==proof.authority_digest
    assert len(written)==rt.audit.writes==1
    marker=rt.audit.lookup(UUID);event=rt.audit.get_event(marker['event_ref'])
    assert event['event_type']=='item_structure_confirmed' and event['retention_class']=='permanent'
    assert 'snapshot' not in event and 'items' not in event
    with pytest.raises(StateError):save.confirm(record,snapshot,current,proof)
    assert len(written)==rt.audit.writes==1


@pytest.mark.parametrize('failure',['read_timeout','HTTP_412','write_response_lost'])
def test_claimed_request_never_retries_ambiguous_authority(keys,failure):
    gateway,ticket,record,current,snapshot,rig,data=session(keys);attempts=[];durable=[]
    class Save:
        def confirm(self,record,snapshot,current,proof):
            attempts.append(1)
            if failure=='write_response_lost':durable.append(proof)
            raise StateError(failure)
    with pytest.raises(StateError):gateway.confirm(ticket,cookie=ticket.cookie,csrf=ticket.csrf,origin=gateway.origin,
        method='POST',confirmation_factory=lambda _:Save())
    assert gateway.store.load()['requests'][UUID]['status']=='claimed'
    with pytest.raises(StateError):gateway.confirm(ticket,cookie=ticket.cookie,csrf=ticket.csrf,origin=gateway.origin,
        method='POST',confirmation_factory=lambda _:Save())
    assert len(attempts)==1 and len(durable)==int(failure=='write_response_lost')


def test_google_sheets_trailing_empty_queue_cells_readback_and_replay():
    from app.receipt_item_queue import capture
    record,*_=candidate();current=r.fields(record);current['action']='記帳する'
    view=r.card(record,categories=CATS);snapshot=r.snapshot(view,current);rows=[];writes=[]
    def write(data):rows.append(data[0]['values'][0]);writes.append(1)
    sheet=SimpleNamespace(_get=lambda *_:[row[:4] for row in rows],_write=write)
    assert capture(sheet,view,snapshot,request_id=UUID,clock=lambda:'synthetic',write_enabled=True)['appended']==1
    assert capture(sheet,view,snapshot,request_id=UUID,clock=lambda:'synthetic',write_enabled=True)['appended']==0
    assert len(writes)==1
