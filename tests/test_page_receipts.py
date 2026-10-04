from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image
from app import human_general_authority as authority,page_receipt_ai as ai,page_receipt_model as model
from app.page_receipt_readonly import ReadonlyPageReceipts
from app.page_receipt_review import general_review_card,process_general_request
from app.drive_run_state import StateError
from app.receipt_privacy_gate import ReceiptPrivacyBlocked
from test_receipt_pdf_units import synthetic_pdf,local_ocr,COLORS

ACTOR={'provider':'google','subject':'owner@example.test'}
TIME='2026-10-05T00:00:00+00:00'
UUID='00000000-0000-4000-8000-000000000001'
BINDING='b'*64
CATEGORIES=[('食費','食品')]

def page(kind='normal',number=1,kinds=None,source_kind='pdf',**changes):
    kinds=kinds or [kind]
    if source_kind=='pdf':raw=synthetic_pdf(kinds)
    else:raw=png(kind)
    source=model.SourceRef(source_file_id='synthetic-source-id',source_content_hash=sha256(raw).hexdigest(),
        page_count=len(kinds),source_kind=source_kind)
    data=dict(source=source,page_number=number,stable_page_identity=model.stable_page(source,number),
        automatic_classification='sensitive_unknown' if kind=='unknown' else kind,
        automatic_reason='insufficient_evidence' if kind=='unknown' else 'normal_receipt_evidence',
        observation_complete=True,extraction_status='extracted',observation_render_hash='a'*64,
        review_identity='0'*64,authority_revision=1)
    data.update(changes)
    p=model.PageUnit(**data);data['review_identity']=authority.review_identity(p)
    return model.PageUnit(**data),raw

def png(kind='normal'):
    output=BytesIO()
    with Image.new('RGB',(100,100),COLORS[kind]) as image:image.save(output,format='PNG')
    return output.getvalue()

def reading(count=1,reverse=False):
    receipts=[]
    for index in range(count):
        top=index/count+.02;bottom=(index+1)/count-.02
        box=dict(left=.05,top=top,right=.95,bottom=bottom)
        receipts.append(dict(bbox=box,item_boxes=[box],receipt=dict(date='2026-10-05',total=(index+1)*100,
            merchant='',payment_method='',transaction_kind='purchase',
            items=[dict(name='商品'+str(index+1),amount=(index+1)*100,major_category='食費',minor_category='食品')])))
    return model.PageReceiptExtraction(receipts=list(reversed(receipts)) if reverse else receipts,
        separation_complete=True,mixed_page_kind_suspected=False,cross_page_continuation_suspected=False)

class MemoryTransport:
    def __init__(self):self.payload=json.dumps(authority.empty_state(BINDING)).encode();self.version=1;self.writes=0
    def read_versioned(self):return self.payload,'"v'+str(self.version)+'"'
    def replace_versioned(self,before,tag,after):
        self.writes+=1
        if before!=self.payload or tag!='"v'+str(self.version)+'"':raise StateError('changed_since_read')
        self.payload=after;self.version+=1

def confirmation(p,transport=None,actor=ACTOR):
    t=transport or MemoryTransport()
    store=authority.HumanGeneralAuthorityStore(t,BINDING,preflight=lambda:None)
    c=authority.HumanGeneralConfirmation(store,lambda *args:p,lambda _:actor,clock=lambda:TIME)
    return c,t

@pytest.mark.parametrize('count',[1,2,3])
def test_normal_receipt_array_spatial_order_replay_and_individual_policy(count):
    p,_=page();a,b=reading(count),reading(count,reverse=True)
    first=model.build_receipt_units(p,a,b,CATEGORIES)
    assert len(first['units'])==count and all(u['analysis_status']=='would_import' for u in first['units'])
    assert [u['parsed']['total'] for u in first['units']]==[100*(i+1) for i in range(count)]
    assert all(u['parsed']['payment_method']=='' for u in first['units'])
    replay=model.build_receipt_units(p,b,a,CATEGORIES,previous=first)
    assert [u['receipt_unit_id'] for u in replay['units']]==[u['receipt_unit_id'] for u in first['units']]
    assert not first['terminal']

def test_source_and_page_changes_invalidate_stable_identity():
    p,_=page();different=model.SourceRef(**{**p.source.model_dump(),'source_content_hash':'f'*64})
    assert model.stable_page(different,1)!=p.stable_page_identity
    inserted=model.SourceRef(**{**p.source.model_dump(),'page_count':2})
    assert model.stable_page(inserted,1)!=p.stable_page_identity
    with pytest.raises(ValueError):model.PageUnit(**{**p.model_dump(),'source':inserted})

def test_bbox_jitter_reuses_frozen_manifest_and_not_sdk_order():
    p,_=page();a=reading(2);first=model.build_receipt_units(p,a,a,CATEGORIES)
    moved=a.model_dump();moved['receipts'][0]['bbox']['left']+=.005
    b=model.PageReceiptExtraction.model_validate(moved)
    replay=model.build_receipt_units(p,b,b,CATEGORIES,previous=first)
    assert [u['receipt_unit_id'] for u in replay['units']]==[u['receipt_unit_id'] for u in first['units']]
    assert replay['segmentation_digest']==first['segmentation_digest']

@pytest.mark.parametrize('problem',['count','overlap','duplicate','mixed','continuation','missing_positions','incomplete','shared_item','ownership'])
def test_unstable_segmentation_holds_entire_page(problem):
    p,_=page();a=reading(2);data=a.model_dump()
    if problem=='count':data['receipts'].pop()
    if problem=='overlap':data['receipts'][1]['bbox']=data['receipts'][0]['bbox']
    if problem=='duplicate':data['receipts'][1]['receipt']=data['receipts'][0]['receipt']
    if problem=='mixed':data['mixed_page_kind_suspected']=True
    if problem=='continuation':data['cross_page_continuation_suspected']=True
    if problem=='incomplete':data['separation_complete']=False
    if problem=='missing_positions':data['receipts'][0]['item_boxes']=[]
    if problem=='shared_item':data['receipts'][1]['item_boxes']=data['receipts'][0]['item_boxes']
    if problem=='ownership':data['receipts'][0]['receipt'],data['receipts'][1]['receipt']=data['receipts'][1]['receipt'],data['receipts'][0]['receipt']
    result=model.build_receipt_units(p,a,model.PageReceiptExtraction.model_validate(data),CATEGORIES)
    assert result['status']=='receipt_segmentation_review' and result['units']==[] and not result['terminal']

def test_partial_receipt_success_never_completes_page_or_pdf():
    p,_=page();a=reading(3);result=model.build_receipt_units(p,a,a,CATEGORIES)
    result['units'][0]['accounting_status']='imported';result['units'][1]['accounting_status']='manual_imported'
    pages=[dict(source=p.source.model_dump(),page_number=1,stable_page_identity=p.stable_page_identity,
        authority_current=True,units=result['units'])]
    assert not model.source_terminal(p.source,pages,lambda _:True)
    result['units'][2]['accounting_status']='medical_manual_imported'
    assert model.source_terminal(p.source,pages,lambda _:True)
    assert not model.source_terminal(p.source,pages,lambda _:False)
    pages[0]['units'].append(deepcopy(result['units'][0]))
    assert not model.source_terminal(p.source,pages,lambda _:True)

@pytest.mark.parametrize('kind',['medical','payroll'])
def test_clear_sensitive_never_accepts_general_authority(kind):
    p,_=page(kind);c,t=confirmation(p)
    with pytest.raises(StateError):c.confirm(p,operation='confirm_general_receipt_ai',request_id=UUID)
    assert t.writes==0

def test_human_authority_unknown_only_source_binding_explicit_actor_audit_replay():
    p,_=page('unknown');c,t=confirmation(p)
    grant=c.confirm(p,operation='confirm_general_receipt_ai',request_id=UUID)
    assert p.automatic_classification=='sensitive_unknown' and grant['confirmed_kind']=='general_receipt'
    assert not any(grant[k] for k in ('accounting_allowed','medical_handoff_allowed','archive_allowed'))
    before=t.payload;replay=c.confirm(p,operation='confirm_general_receipt_ai',request_id=UUID)
    assert replay==grant and t.payload==before and t.writes==1
    assert c.store.load()['audit'][0]['confirmation_digest']==grant['confirmation_digest']
    other,_=page('unknown',authority_revision=2)
    with pytest.raises(StateError):authority.validate_grant(grant,other)
    assert not authority.legacy_migration_allowed({'human_classification':'normal'})

@pytest.mark.parametrize('change',[{'automatic_reason':'conflicting_sensitive_evidence'},
    {'automatic_reason':'known_sensitive_source'},{'clearly_sensitive':True},{'observation_complete':False},
    {'extraction_status':'failed'},{'human_page_kind':'medical'}])
def test_unknown_fail_closed_restrictions(change):
    p,_=page('unknown',**change);c,t=confirmation(p)
    with pytest.raises(StateError):c.confirm(p,operation='confirm_general_receipt_ai',request_id=UUID)
    assert t.writes==0

def test_missing_independently_verified_actor_does_not_save_even_on_replay():
    p,_=page('unknown');c,t=confirmation(p);c.confirm(p,operation='confirm_general_receipt_ai',request_id=UUID)
    denied,_=confirmation(p,t,actor=None)
    with pytest.raises(StateError):denied.confirm(p,operation='confirm_general_receipt_ai',request_id=UUID)
    assert t.writes==1

def test_stale_review_snapshot_cas_conflict_and_corrupt_audit():
    p,_=page('unknown');c,t=confirmation(p)
    nextpage,_=page('unknown',authority_revision=2)
    c.current_page=lambda *args:nextpage
    with pytest.raises(StateError,match='stale'):c.confirm(p,operation='confirm_general_receipt_ai',request_id=UUID)
    assert t.writes==0
    c.current_page=lambda *args:p
    c.confirm(p,operation='confirm_general_receipt_ai',request_id=UUID)
    state=c.store.load();state['audit']=[]
    with pytest.raises(StateError):c.store.save(state)
    old,_=c.store.transport.read_versioned();c.store.load();t.version+=1
    with pytest.raises(StateError,match='changed_since_read'):c.store.save(c.store.load() if False else json.loads(old))
    assert t.writes==2 # one rejected If-Match; no retry or fallback

@pytest.mark.parametrize('kind,count',[('normal',1),('normal',2),('unknown',1),('unknown',3)])
def test_exact_png_only_two_readings_with_separate_unknown_authority(local_ocr,kind,count):
    p,raw=page(kind);grant=authority.authority(p,ACTOR,TIME) if kind=='unknown' else None
    payload=png(kind);seen=[]
    def create(**kwargs):
        import base64
        content=kwargs['input'];assert len(content)==2 and content[1]['mime_type']=='image/png'
        assert base64.b64decode(content[1]['data'])==payload and payload!=raw
        seen.append(content[1]['data']);return SimpleNamespace(output_text=reading(count,len(seen)==2).model_dump_json())
    permission=lambda page,png:ai.authorize_payload(page,png,current_page=lambda *args:p,load_source=lambda _:raw,load_grant=lambda _:grant)
    analyzer=ai.GeminiPageReceipts(SimpleNamespace(interactions=SimpleNamespace(create=create)),'synthetic-model',permission)
    readings,proof=analyzer.analyze(p,payload,CATEGORIES,expected_payload_sha256=sha256(payload).hexdigest())
    assert analyzer.calls==2 and len(readings[0].receipts)==count and len(seen)==2
    assert proof['automatic_classification']==p.automatic_classification
    assert proof['basis']==('human_general_receipt' if kind=='unknown' else 'automatic_normal')

@pytest.mark.parametrize('failure',['source','page','missing_grant','payload','medical','ocr_failure','pii'])
def test_rejected_exact_or_authority_gate_never_calls_gemini(local_ocr,monkeypatch,failure):
    p,raw=page('unknown');payload=png('unknown');grant=authority.authority(p,ACTOR,TIME);latest=p
    if failure=='source':raw+=b'changed'
    if failure=='page':latest,_=page('unknown',authority_revision=2)
    if failure=='missing_grant':grant=None
    if failure=='medical':payload=png('medical')
    if failure=='ocr_failure':payload=png('failed')
    if failure=='pii':
        monkeypatch.setattr(ai,'_extract_receipt_text',lambda *_:SimpleNamespace(status='extracted',observation_complete=True,text='マイナンバー',structured_tokens=[]))
    client=Mock();permission=lambda page,png:ai.authorize_payload(page,png,current_page=lambda *args:latest,load_source=lambda _:raw,load_grant=lambda _:grant)
    analyzer=ai.GeminiPageReceipts(client,'model',permission)
    fingerprint=sha256(payload).hexdigest() if failure!='payload' else 'f'*64
    with pytest.raises((StateError,ReceiptPrivacyBlocked)):
        analyzer.analyze(p,payload,CATEGORIES,expected_payload_sha256=fingerprint)
    client.interactions.create.assert_not_called()

def test_source_pdf_or_png_metadata_rejected(local_ocr):
    p,raw=page();permission=lambda data:ai.authorize_payload(p,data,current_page=lambda *args:p,load_source=lambda _:raw,load_grant=Mock())
    with pytest.raises(ReceiptPrivacyBlocked):permission(raw)
    from PIL.PngImagePlugin import PngInfo
    info=PngInfo();info.add_text('private','hidden');output=BytesIO()
    with Image.new('RGB',(10,10)) as image:image.save(output,format='PNG',pnginfo=info)
    with pytest.raises(ReceiptPrivacyBlocked):permission(output.getvalue())

@pytest.mark.parametrize('kinds',[['normal','normal'],['medical','normal'],['normal','medical','normal'],['unknown','medical'],['normal','unknown','normal']])
def test_mixed_pdf_sequential_without_medical_render_or_unknown_guess(local_ocr,kinds):
    pages={n:page(kind,n,kinds)[0] for n,kind in enumerate(kinds,1)}
    raw=synthetic_pdf(kinds);source=Mock(return_value=raw)
    def permit(p,payload):return ai.authorize_payload(p,payload,current_page=lambda _,n:pages[n],load_source=source,
        load_grant=lambda _:None)
    calls=Mock(return_value=SimpleNamespace(output_text=reading(2).model_dump_json()))
    analyzer=ai.GeminiPageReceipts(SimpleNamespace(interactions=SimpleNamespace(create=calls)),'model',permit)
    runner=ReadonlyPageReceipts(lambda _,n:pages[n],source,lambda _:None,analyzer,CATEGORIES)
    results=[runner.run('synthetic-source-id',n) for n in pages]
    assert analyzer.calls==2*kinds.count('normal') and runner.budget.peak_live_pages==int('normal' in kinds)
    assert all(not r.get('terminal') and not r['accounting_allowed'] and not r['archive_allowed'] for r in results)
    for kind,r in zip(kinds,results):
        if kind=='medical':assert r['status']=='medical_manual_pending' and 'payload_sha256' not in r
        if kind=='unknown':assert r['status']=='human_general_authority_required'

def test_image_source_fresh_rgb_metadata_free_and_stable_replay(local_ocr):
    p,raw=page(source_kind='image');permission=lambda page,payload:ai.authorize_payload(page,payload,
        current_page=lambda *args:p,load_source=lambda _:raw,load_grant=Mock())
    create=Mock(return_value=SimpleNamespace(output_text=reading().model_dump_json()))
    analyzer=ai.GeminiPageReceipts(SimpleNamespace(interactions=SimpleNamespace(create=create)),'model',permission)
    runner=ReadonlyPageReceipts(lambda *args:p,lambda _:raw,Mock(),analyzer,CATEGORIES)
    first=runner.run(p.source.source_file_id,1);second=runner.run(p.source.source_file_id,1,previous=first)
    assert first['status']==second['status']=='would_import'
    assert first['units'][0]['receipt_unit_id']==second['units'][0]['receipt_unit_id']

def test_review_is_existing_vertical_ui_no_receipt_count_and_projection_failure_no_second_authority():
    p,_=page('unknown');c,t=confirmation(p);card=general_review_card(p)
    snapshot={**card,'original_link':'https://drive.google.com/file/d/synthetic-source-id/view#page=1'}
    snapshot['rows']=[list(r) for r in card['rows']]
    next(r for r in snapshot['rows'] if r[0]=='human_general_kind')[2]='一般レシート'
    # synthetic sources deliberately have no original hyperlink
    snapshot['original_link']=''
    sheet=Mock();sheet.request.return_value=(2,snapshot);sheet.read_card.return_value=snapshot
    sheet.finish_card.side_effect=ConnectionError('projection only')
    with pytest.raises(ConnectionError):process_general_request(c,sheet,UUID)
    sheet.finish_card.side_effect=None
    grant=process_general_request(c,sheet,UUID)
    assert grant['confirmed_kind']=='general_receipt' and t.writes==1
    assert all('枚数' not in str(r) for r in card['rows'])
    assert p.automatic_classification=='sensitive_unknown'

def test_durable_metadata_manifest_replay_new_runner_and_stale_partition_no_extra_write():
    from app.page_receipt_manifest import ReceiptManifestStore,empty_state
    p,_=page();a=reading(2);report=model.build_receipt_units(p,a,a,CATEGORIES)
    t=MemoryTransport();t.payload=json.dumps(empty_state(BINDING)).encode()
    one=ReceiptManifestStore(t,BINDING,preflight=lambda:None)
    first=one.remember(p,report)
    second=ReceiptManifestStore(t,BINDING,preflight=lambda:None)
    assert second.current(p)==first and second.remember(p,report)==first and t.writes==1
    serialized=t.payload.decode()
    assert all(term not in serialized for term in ('商品','merchant','parsed','date','total','image','ocr'))
    replay=model.build_receipt_units(p,a,a,CATEGORIES,previous=second.current(p))
    assert [u['receipt_unit_id'] for u in replay['units']]==[u['receipt_unit_id'] for u in report['units']]
    altered=model.build_receipt_units(p,reading(3),reading(3),CATEGORIES)
    with pytest.raises(StateError,match='changed'):second.remember(p,altered)
    assert t.writes==1
    changed,_=page(kinds=['normal','normal'])
    assert second.current(changed) is None

def test_reread_major_change_and_item_total_mismatch_are_per_receipt_review():
    p,_=page();first=reading(2);data=first.model_dump();data['receipts'][1]['receipt']['total']=201
    result=model.build_receipt_units(p,first,model.PageReceiptExtraction.model_validate(data),CATEGORIES)
    assert result['units'][0]['analysis_status']=='would_import'
    assert result['units'][1]['analysis_status']=='would_need_review' and not result['terminal']

def test_explicit_unknown_hold_revokes_general_consent_old_request_cannot_restore_it():
    p,_=page('unknown');c,t=confirmation(p)
    c.confirm(p,operation='confirm_general_receipt_ai',request_id=UUID)
    held=UUID[:-1]+'2';c.hold(p,request_id=held)
    with pytest.raises(StateError,match='missing'):c.current(p)
    c.hold(p,request_id=held);assert t.writes==2
    with pytest.raises(StateError,match='replaced'):c.confirm(p,operation='confirm_general_receipt_ai',request_id=UUID)
    c.confirm(p,operation='confirm_general_receipt_ai',request_id=UUID[:-1]+'3')
    assert t.writes==3 and c.current(p)['confirmed_kind']=='general_receipt'
