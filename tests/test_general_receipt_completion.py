"""Corroborated machine fields and explicit owner completion, no live services."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from app import general_receipt_completion as c, page_receipt_model as model
from app import page_receipt_ai as ai, human_general_authority as hga
from app.page_receipt_manifest import ReceiptManifestStore, empty_state as manifest_empty
from app.drive_run_state import StateError
from app.pdf_page_review import PageReviewSheet
from test_pdf_grouping_transport_ui import FakeSheets
from test_page_receipts import page, reading, MemoryTransport, BINDING, CATEGORIES, UUID, confirmation, png
from test_pdf_page_review import snapshot
from test_receipt_pdf_units import local_ocr
from test_human_general_auth_transport import Rig, keys


def memory(initial):
    transport=MemoryTransport();transport.payload=json.dumps(initial).encode()
    return transport


def setup(count=1,p=None):
    p,raw=page() if p is None else p
    first,second=reading(count),reading(count,reverse=True)
    report=model.build_receipt_units(p,first,second,CATEGORIES)
    mt=memory(manifest_empty(BINDING))
    manifest=ReceiptManifestStore(mt,BINDING,preflight=lambda:None).remember(p,report)
    return p,raw,manifest,first,second


def candidate(**kwargs):
    p,raw,m,a,b=setup()
    r=c.draft(p,m,m['units'][0]['receipt_unit_id'],[a.receipts[0].receipt,b.receipts[0].receipt],categories=CATEGORIES,**kwargs)
    return r,p,raw,m


def form(record,**fields):
    return snapshot(c.card(record,categories=CATEGORIES),{**fields,'manual_action':c.CONFIRM_ACTION})


def backend(record,p,raw,manifest,grant=None):
    transport=memory(c.empty_state(BINDING));owner={}
    svc=c.GeneralCompletion(c.CompletionStore(transport,BINDING,preflight=lambda:None),
        lambda *_:p,lambda _:raw,lambda _:manifest,lambda _:grant,lambda:CATEGORIES,
        lambda rid:owner.get(rid),lambda *_:False)
    svc.remember(record)
    return svc,transport,owner


def test_success_and_missing_optional_share_complete_manual_rows():
    r,p,raw,m=candidate()
    assert r['prefill']==dict(date='2026-10-05',amount=100,category='食費｜食品',merchant='',payment='',memo='')
    result=c.evaluate(r,r['prefill'],CATEGORIES)
    assert result['status']=='ready_to_confirm' and result['provenance']['date']=='gemini'
    blank=c.draft(p,m,m['units'][0]['receipt_unit_id'],categories=CATEGORIES)
    assert [(f,l) for f,l,v in c.card(r,categories=CATEGORIES)['rows']]==[(f,l) for f,l,v in c.card(blank,categories=CATEGORIES)['rows']]
    assert c.evaluate(blank,blank['prefill'],CATEGORIES)['status']=='needs_human_completion'


@pytest.mark.parametrize('missing',[('date',),('amount',),('category',),('date','amount'),('date','category','amount')])
def test_only_untrusted_required_fields_blank_and_owner_completes(missing):
    r,p,raw,m=candidate(ambiguous=missing)
    assert all(r['prefill'][f]=='' for f in missing)
    assert c.evaluate(r,r['prefill'],CATEGORIES)['status']=='needs_human_completion'
    complete={**r['prefill'],'date':'2026/10/05','amount':'100','category':'食費｜食品'}
    result=c.evaluate(r,complete,CATEGORIES)
    assert result['status']=='ready_to_confirm'
    assert all(result['provenance'][f]=='human' for f in missing)
    assert result['parsed']['items']==r['parsed']['items']


@pytest.mark.parametrize('field,value',[('date','2026-02-30'),('total',0),('total',1000)])
def test_invalid_date_or_total_not_prefilled_then_correctable(field,value):
    p,raw,m,a,b=setup()
    for x in (a,b):setattr(x.receipts[0].receipt,field,value)
    r=c.draft(p,m,m['units'][0]['receipt_unit_id'],[a.receipts[0].receipt,b.receipts[0].receipt],categories=CATEGORIES)
    target='amount' if field=='total' else 'date'
    assert r['prefill'][target]==''
    final={**r['prefill'],'date':'2026/10/05','amount':100}
    assert c.evaluate(r,final,CATEGORIES)['status']=='ready_to_confirm'


def test_invalid_category_blanks_and_existing_dropdown_selection_validates():
    p,raw,m,a,b=setup()
    for x in (a,b):x.receipts[0].receipt.items[0].minor_category='Invented'
    r=c.draft(p,m,m['units'][0]['receipt_unit_id'],[a.receipts[0].receipt,b.receipts[0].receipt],categories=CATEGORIES)
    assert r['prefill']['category']==''
    assert c.evaluate(r,{**r['prefill'],'category':'食費｜食品'},CATEGORIES)['status']=='ready_to_confirm'
    assert c.evaluate(r,{**r['prefill'],'category':'Invented｜Invented'},CATEGORIES)['status']=='needs_human_completion'


@pytest.mark.parametrize('confidence',[0.1,None,float('nan'),True])
def test_insufficient_or_invalid_confidence_never_places_guess(confidence):
    r,*_=candidate(confidence={'amount':confidence})
    assert r['prefill']['amount']=='' and r['prefill']['date']=='2026-10-05'


def test_optional_unsupported_payment_cleared_not_inferred():
    p,raw,m,a,b=setup()
    for x in (a,b):x.receipts[0].receipt.payment_method='現金'
    r=c.draft(p,m,m['units'][0]['receipt_unit_id'],[a.receipts[0].receipt,b.receipts[0].receipt],categories=CATEGORIES)
    assert r['prefill']['payment']==''
    assert c.evaluate(r,r['prefill'],CATEGORIES)['status']=='ready_to_confirm'


def test_human_override_and_date_serial_do_not_shift_calendar_day():
    from app.pdf_page_review import sheet_date_value
    r,*_=candidate()
    final=c.evaluate(r,{**r['prefill'],'date':'2026/10/06'},CATEGORIES)
    assert final['parsed']['date']=='2026-10-06' and final['provenance']['date']=='human_override'
    unchanged=c.evaluate(r,{**r['prefill'],'date':sheet_date_value('2026-10-05'),'amount':'100'},CATEGORIES)
    assert unchanged['provenance']['date']=='gemini' and unchanged['status']=='ready_to_confirm'


def test_item_mismatch_is_not_approved_by_arbitrary_human_total():
    r,*_=candidate(ambiguous=['amount'])
    bad=c.evaluate(r,{**r['prefill'],'amount':200},CATEGORIES)
    assert bad['status']=='needs_review' and any('明細合計' in i for i in bad['issues'])
    assert c.evaluate(r,{**r['prefill'],'amount':100},CATEGORIES)['status']=='ready_to_confirm'


@pytest.mark.parametrize('problem',['items','fake_adjustment','kind','buyback','single_read'])
def test_item_structure_and_transaction_holds_survive_complete_required_fields(problem):
    p,raw,m,a,b=setup();one,two=a.receipts[0].receipt,b.receipts[0].receipt
    gate=None
    if problem=='items':two.items[0].name='different item'
    if problem=='fake_adjustment':one.items[0].name=two.items[0].name='差額調整'
    if problem=='kind':one.transaction_kind=two.transaction_kind='unknown'
    if problem=='buyback':gate=SimpleNamespace(buyback_evidence=True)
    readings=[one] if problem=='single_read' else [one,two]
    r=c.draft(p,m,m['units'][0]['receipt_unit_id'],readings,categories=CATEGORIES,gate=gate)
    assert c.evaluate(r,dict(date='2026/10/05',amount=100,category='食費｜食品'),CATEGORIES)['status']=='needs_review'


def test_no_results_or_invalid_schema_uses_same_blank_manual_form_without_fake_items():
    p,raw,m,a,b=setup()
    r=c.draft(p,m,m['units'][0]['receipt_unit_id'],[{'invalid':'schema'}],categories=CATEGORIES)
    assert all(v=='' for v in r['prefill'].values())
    result=c.evaluate(r,dict(date='2026/10/05',amount=100,category='食費｜食品'),CATEGORIES)
    assert result['status']=='ready_to_confirm' and result['parsed'] is None


@pytest.mark.parametrize('count',[1,2,3])
def test_multiple_receipts_use_frozen_spatial_manifest_and_identical_card_component(count):
    p,raw,m,a,b=setup(count)
    first=c.drafts_for_page(p,m,a,b,categories=CATEGORIES)
    again=c.drafts_for_page(p,m,b,a,categories=CATEGORIES)
    assert [r['identity']['receipt_unit_id'] for r in first]==[r['identity']['receipt_unit_id'] for r in again]
    assert [r['prefill']['amount'] for r in first]==[100*(i+1) for i in range(count)]
    assert len({c.card(r,categories=CATEGORIES)['token'] for r in first})==count
    assert all(r['parsed']['items'][0]['name']==f'商品{i+1}' for i,r in enumerate(first))


@pytest.mark.parametrize('problem',['count','overlap','mixed','ownership'])
def test_unstable_page_never_prefills_or_creates_completion_cards(problem):
    p,raw,m,a,b=setup(2)
    if problem=='count':b.receipts.pop()
    if problem=='overlap':b.receipts[1].bbox=b.receipts[0].bbox
    if problem=='mixed':b.mixed_page_kind_suspected=True
    if problem=='ownership':b.receipts[0].receipt,b.receipts[1].receipt=b.receipts[1].receipt,b.receipts[0].receipt
    with pytest.raises(StateError,match='segmentation'):c.drafts_for_page(p,m,a,b,categories=CATEGORIES)


def test_explicit_request_readback_and_replay_are_validation_only():
    r,p,raw,m=candidate();svc,t,owner=backend(r,p,raw,m)
    snap=form(r);owner[UUID]=snap
    plan=svc.confirm(UUID,r['identity']['receipt_unit_id'],snap)
    assert plan['status']=='validated_not_written' and plan['plan']['posting_authority'] is False
    assert plan['plan']['existing_writer_route']=='receipt_itemized'
    before=t.payload, t.version, t.writes
    assert svc.confirm(UUID,r['identity']['receipt_unit_id'],snap)['replayed']
    assert (t.payload,t.version,t.writes)==before
    assert svc.store.load()['requests'][UUID]['plan_digest']==plan['plan_digest']
    for name in ('accounting_allowed','medical_handoff_allowed','archive_allowed'):assert plan['plan'][name] is False


@pytest.mark.parametrize('problem',['source','page','revision','snapshot','digest','duplicate','unknown_duplicate','category','explicit','unit'])
def test_stale_tamper_and_duplicate_rejected_without_saved_intent(problem):
    r,p,raw,m=candidate();svc,t,owner=backend(r,p,raw,m)
    snap=form(r);owner[UUID]=deepcopy(snap);before=t.writes
    if problem=='source':svc.load_source=lambda _:raw+b'changed'
    if problem=='page':svc.current_page=lambda *_:page(kinds=['normal','normal'])[0]
    if problem=='revision':svc.current_page=lambda *_:page(authority_revision=2)[0]
    if problem=='snapshot':snap['rows'][-3][2]='changed'
    if problem=='digest':snap['identity']['candidate_digest']='f'*64
    if problem=='duplicate':svc.duplicates=lambda *_:True
    if problem=='unknown_duplicate':svc.duplicates=lambda *_:None
    if problem=='category':svc.categories=lambda:[]
    if problem=='explicit':snap['rows']=[(f,l,'' if f=='manual_action' else v) for f,l,v in snap['rows']]
    if problem=='unit':snap['identity']['receipt_unit_id']='other'
    with pytest.raises(StateError):svc.confirm(UUID,r['identity']['receipt_unit_id'],snap)
    assert t.writes==before and svc.store.load()['requests']=={}


def test_cas_conflict_no_retry_or_unconditional_fallback():
    r,p,raw,m=candidate();svc,t,owner=backend(r,p,raw,m);snap=form(r);owner[UUID]=snap
    original=t.replace_versioned
    def race(before,tag,after):
        t.version+=1;return original(before,tag,after)
    t.replace_versioned=race;before=t.writes
    with pytest.raises(StateError,match='changed_since_read'):svc.confirm(UUID,r['identity']['receipt_unit_id'],snap)
    assert t.writes==before+1 and svc.store.load()['requests']=={}


def test_projection_failure_after_saved_intent_recovers_without_reapplication():
    r,p,raw,m=candidate();svc,t,owner=backend(r,p,raw,m);snap=form(r);owner[UUID]=snap
    intent=svc.confirm(UUID,r['identity']['receipt_unit_id'],snap);before=t.payload,t.version,t.writes
    with pytest.raises(OSError):raise OSError('synthetic UI failure after validation')
    recovered=svc.confirm(UUID,r['identity']['receipt_unit_id'],snap)
    assert recovered['plan_digest']==intent['plan_digest'] and (t.payload,t.version,t.writes)==before


def test_current_shared_sheet_projection_prefill_editability_dates_amount_categories_and_hold():
    r,p,raw,m=candidate(ambiguous=['amount'])
    svc,t,owner=backend(r,p,raw,m)
    native=FakeSheets();sheet=PageReviewSheet(native,'synthetic-management',CATEGORIES);sheet._rows=lambda:[]
    sheet.publish_cards(svc.projected())
    rows=native.writes[-1][0]['values']
    field_rows={row[5]:(i+1,row) for i,row in enumerate(rows) if len(row)>5 and row[5]}
    assert field_rows['date'][1][1]>40000 and field_rows['amount'][1][1]==''
    assert field_rows['category'][1][1]=='食費｜食品'
    validations={r['setDataValidation']['range']['startRowIndex']:r['setDataValidation']['rule']
        for r in native.formats if 'setDataValidation' in r and 'rule' in r['setDataValidation']}
    assert validations[field_rows['date'][0]]['condition']['type']=='DATE_IS_VALID'
    assert validations[field_rows['date'][0]]['strict'] is True
    assert validations[field_rows['manual_action'][0]]['condition']['values']==[{'userEnteredValue':'保留'}]
    # Editing formatted cells persists as owner input; same token does not restore model values.
    rows[field_rows['date'][0]-1][1]='2026/10/06';rows[field_rows['amount'][0]-1][1]='100'
    sheet._rows=lambda:[['header']]+rows
    sheet.publish_cards(svc.projected())
    changed=native.writes[-1][0]['values']
    assert next(row for row in changed if len(row)>5 and row[5]=='amount')[1]==100
    date_value=next(row for row in changed if len(row)>5 and row[5]=='date')[1]
    from app.pdf_page_review import sheet_date_value
    assert date_value==sheet_date_value('2026/10/06')
    assert all(d['range'].startswith("'PDFページ確認'!") for batch in native.writes for d in batch)
    assert not any('addSheet' in r for r in native.formats)


@pytest.mark.parametrize('value',['100.5','-1',True,'abc'])
def test_amount_not_rounded_or_inferred(value):
    r,*_=candidate()
    assert c.evaluate(r,{**r['prefill'],'amount':value},CATEGORIES)['status']=='needs_human_completion'


def test_extra_raw_data_and_replaced_request_are_rejected():
    r,p,raw,m=candidate()
    changed=deepcopy(r);changed['raw_ocr']='forbidden';changed['candidate_digest']=model.digest({k:v for k,v in changed.items() if k!='candidate_digest'})
    with pytest.raises(StateError):c.validate_draft(changed)
    svc,t,owner=backend(r,p,raw,m);snap=form(r);owner[UUID]=snap
    svc.confirm(UUID,r['identity']['receipt_unit_id'],snap);before=t.writes
    altered=form(r,date='2026/10/06');owner[UUID]=altered
    with pytest.raises(StateError):svc.confirm(UUID,r['identity']['receipt_unit_id'],altered)
    assert t.writes==before


def test_unknown_delivery_readback_recovers_intent_without_retrying_write():
    r,p,raw,m=candidate();svc,t,owner=backend(r,p,raw,m);snap=form(r);owner[UUID]=snap
    replace=t.replace_versioned
    def delivered_then_disconnected(before,tag,after):
        replace(before,tag,after);raise ConnectionError('synthetic unknown delivery')
    t.replace_versioned=delivered_then_disconnected
    with pytest.raises(ConnectionError):svc.confirm(UUID,r['identity']['receipt_unit_id'],snap)
    before=t.payload,t.version,t.writes
    recovered=svc.confirm(UUID,r['identity']['receipt_unit_id'],snap)
    assert recovered['replayed'] and (t.payload,t.version,t.writes)==before


def test_another_request_cannot_reconfirm_same_receipt_and_page_review_change_is_stale():
    r,p,raw,m=candidate();svc,t,owner=backend(r,p,raw,m);snap=form(r);owner[UUID]=snap
    svc.confirm(UUID,r['identity']['receipt_unit_id'],snap);before=t.writes
    other='00000000-0000-4000-8000-000000000002';owner[other]=snap
    with pytest.raises(StateError,match='already_confirmed'):svc.confirm(other,r['identity']['receipt_unit_id'],snap)
    svc.current_page=lambda *_:p.model_copy(update={'review_identity':'f'*64})
    with pytest.raises(StateError,match='stale'):svc.confirm(UUID,r['identity']['receipt_unit_id'],snap)
    assert t.writes==before


@pytest.mark.parametrize('change',[{'automatic_classification':'medical'},{'automatic_classification':'payroll'},
    {'clearly_sensitive':True},{'human_page_kind':'medical'},{'observation_complete':False}])
def test_privacy_unresolved_cannot_override_clear_sensitive(change):
    p,raw=page('unknown',automatic_reason='privacy_unresolved',**change)
    assert not hga.eligible(p)
    core,t=confirmation(p)
    with pytest.raises(StateError):core.confirm(p,operation='confirm_general_receipt_ai',request_id=UUID)
    assert t.writes==0


def test_hga_to_exact_png_mock_gemini_partial_completion_end_to_end(local_ocr,keys):
    p,raw=page('unknown',automatic_reason='privacy_unresolved')
    signed=Rig(keys);signed.p=p;signed.source=raw
    actor=signed.authenticate()
    assert actor.method=='google_oidc_code_pkce_v1' and signed.grants.writes==0
    signed.confirm() # Separate explicit POST after signature/allowlist validation.
    grant=next(iter(json.loads(signed.grants.payload)['grants'].values()))
    payload=png('unknown');calls=[]
    def create(**kwargs):
        import base64
        image=kwargs['input'][1]
        assert image['mime_type']=='image/png' and base64.b64decode(image['data'])==payload
        calls.append(True);result=reading();result.receipts[0].receipt.date=''
        return SimpleNamespace(output_text=json.dumps(ai.wire_value(result)))
    permission=lambda page,png:ai.authorize_payload(page,png,current_page=lambda *_:p,load_source=lambda _:raw,load_grant=lambda _:grant)
    analyzer=ai.GeminiPageReceipts(SimpleNamespace(interactions=SimpleNamespace(create=create)),'synthetic-model',permission)
    (first,second),_=analyzer.analyze(p,payload,CATEGORIES,expected_payload_sha256=ai.sha256(payload).hexdigest(),
        render_proof=ai.fresh_render_proof(p,raw,p.page_number,p.source.page_count,payload))
    report=model.build_receipt_units(p,first,second,CATEGORIES)
    store=ReceiptManifestStore(memory(manifest_empty(BINDING)),BINDING,preflight=lambda:None)
    manifest=store.remember(p,report)
    r=c.drafts_for_page(p,manifest,first,second,categories=CATEGORIES)[0]
    assert len(calls)==2 and r['prefill']['date']=='' and r['prefill']['amount']==100
    assert p.automatic_classification=='sensitive_unknown'
    svc,t,owner=backend(r,p,raw,manifest,grant);snap=form(r,date='2026/10/05');owner[UUID]=snap
    result=svc.confirm(UUID,r['identity']['receipt_unit_id'],snap)
    assert result['plan']['provenance']['date']=='human'
    assert result['plan']['status']=='ready_to_confirm' and result['plan']['posting_authority'] is False
    svc.grant=lambda _:None
    with pytest.raises(StateError):svc.confirm(UUID,r['identity']['receipt_unit_id'],snap)


def test_single_image_source_uses_same_form_and_stable_receipt_ids():
    p,raw=page(source_kind='image');p,raw,m,a,b=setup(2,(p,raw))
    results=c.drafts_for_page(p,m,a,b,categories=CATEGORIES)
    assert len(results)==2 and all(r['identity']['source_kind']=='image' for r in results)
    assert [r['prefill']['amount'] for r in results]==[100,200]


def test_reread_disagreement_blank_only_affected_date_and_optional_merchant():
    p,raw,m,a,b=setup();b.receipts[0].receipt.date='2026-10-06'
    a.receipts[0].receipt.merchant='one';b.receipts[0].receipt.merchant='two'
    r=c.draft(p,m,m['units'][0]['receipt_unit_id'],[a.receipts[0].receipt,b.receipts[0].receipt],categories=CATEGORIES)
    assert r['prefill']['date']==r['prefill']['merchant']==''
    assert r['prefill']['amount']==100 and r['prefill']['category']=='食費｜食品'
    assert c.evaluate(r,{**r['prefill'],'date':'2026/10/05'},CATEGORIES)['status']=='ready_to_confirm'
