from copy import deepcopy
import pytest
from app.drive_run_state import StateError
from app.receipt_plan.models import ReceiptResult
from app.receipt_settlement import printed_facts, add_external_tax, normalize_readings, settlement_issues
from app.receipt_settlement_review import repair_candidate
from app.receipt_item_review import fields, snapshot, check_snapshot, card, evaluate
from app.receipt_item_confirmation import annotated
from app.page_receipt_model import digest, PageUnit, PageReceiptExtraction, build_receipt_units
from app.general_receipt_completion import draft
from test_pdf_intake_runner import rig, CATEGORIES

STAR = '本体合計(2点) 1,015\n8%対象 1,015\n消費税 81\n総合計 1,096\nスターバックスカード895\nauPAY201'
BELC = '小計6点 3,285\n税込金額合計 3,547\n買物袋持参割 -2\nポイントお買物券 -500\n合計 3,045\n8%税抜対象額 2,820\n8%税額225\n獲得ポイント46'
CAT = [('食費','外食'), ('食費','食料品'), ('日用品','洗剤')]


def receipt(amounts=(469,546), total=1096, pairs=None):
    return ReceiptResult.model_validate({'date':'2026-10-01','merchant':'synthetic店',
        'transaction_kind':'purchase','total':total,'payment_method':'','note':'',
        'items':[{'name':'商品'+str(i), 'quantity':1, 'amount':v,
            'major_category':(pairs or [('食費','外食')]*len(amounts))[i][0],
            'minor_category':(pairs or [('食費','外食')]*len(amounts))[i][1], 'note':'','confidence':.99}
            for i,v in enumerate(amounts)]})


def test_printed_external_tax_and_prepaid_tender_not_discount():
    raw=receipt();facts=printed_facts(STAR);fixed=add_external_tax(raw,facts,CAT)
    assert [i.amount for i in fixed.items]==[469,546,81]
    assert fixed.total==sum(i.amount for i in fixed.items)==1096
    assert fixed.items[-1].minor_category=='外食' and raw.items==receipt().items
    assert add_external_tax(fixed,facts,CAT)==fixed


def test_coupon_tax_bridge_not_final_tax_added_twice():
    with pytest.raises(StateError,match='confirmation_required'):printed_facts(BELC)
    facts=printed_facts(BELC,voucher_decision='discount')
    assert facts.tax==262 and facts.method=='printed_subtotal_tax_bridge'
    raw=receipt((396,99,2790,-2,-500),3045)
    fixed=add_external_tax(raw,facts,CAT)
    assert sum(i.amount for i in fixed.items)==3045 and fixed.items[-1].amount!=225
    with pytest.raises(StateError,match='tender_model_required'):printed_facts(BELC,voucher_decision='tender')


@pytest.mark.parametrize('text',[
    STAR.replace('消費税 81','消費税 82'),
    STAR.replace('本体合計(2点) 1,015',''),
    STAR.replace('総合計 1,096','合計 1,096'),
    STAR+'\n消費税81',
    STAR+'\n内税81',
    BELC.replace('8%税抜対象額 2,820','8%税抜対象額 2,821'),
    BELC+'\n10%税額0',
])
def test_incomplete_ambiguous_or_included_tax_never_balanced(text):
    with pytest.raises(StateError):printed_facts(text,voucher_decision='discount')


def test_mixed_categories_and_bad_totals_hold_without_mutation():
    facts=printed_facts(STAR)
    for raw in (receipt(pairs=[('食費','外食'),('日用品','洗剤')]), receipt(total=1095), receipt((468,546))):
        before=raw.model_dump()
        with pytest.raises(StateError):add_external_tax(raw,facts,CAT)
        assert raw.model_dump()==before


def test_earned_points_not_discount_or_authority():
    assert settlement_issues('獲得ポイント46\n累計ポイント84')==[]
    assert settlement_issues('ポイントお買物券 -500')
    assert settlement_issues('商品券充当500') and settlement_issues('クーポン500')


def test_real_discount_and_external_tax_keep_printed_sign_and_category():
    text='本体合計730\n消費税58\n税込金額合計788\n値引き-13\n合計775'
    facts=printed_facts(text);raw=receipt((250,480,-13),775)
    fixed=add_external_tax(raw,facts,CAT)
    assert [i.amount for i in fixed.items]==[250,480,-13,58]
    assert sum(i.amount for i in fixed.items)==775
    assert [(i.major_category,i.minor_category) for i in fixed.items]==[('食費','外食')]*4


def taxed_rig():
    runner,key,uid,db=rig();value=runner.store.load();u=value['pages'][key]['units'][uid]
    record=u['candidate']['record'];legacy=record['legacy'];parsed=legacy['parsed']
    parsed['items'][0]['amount']=250;parsed['items'][1].update(amount=480,major_category='食費',minor_category='食料品')
    parsed['total']=788
    legacy['item_category_evidence']=[{'item_index':i+1,'category':'食費｜食料品','corroborated':True} for i in range(2)]
    legacy['prefill'].update(amount='');legacy.pop('candidate_digest');legacy['candidate_digest']=digest(legacy)
    from app.receipt_item_review import prepare
    record=prepare(legacy,CATEGORIES);reading=ReceiptResult.model_validate(parsed)
    record=annotated(record,[reading,reading],CATEGORIES)
    u['candidate'].update(record=record,view=card(record,categories=CATEGORIES),ui_projected=True)
    value['generation']+=1;runner.store.save(value)
    current=fields(record);current.update(amount=788,date=46296,merchant='本人修正',action='記帳する',structure_confirmation='確認済み')
    current['items'][0]['category']='食費｜食料品';current['items'][1]['category']='食費｜食料品'
    return runner,key,uid,db,current


def test_unsigned_revision_preserves_edits_stales_old_snapshot_and_cannot_post():
    runner,key,uid,db,current=taxed_rig();v=runner.store.load();u=v['pages'][key]['units'][uid]
    before=deepcopy(u);old_snapshot=snapshot(u['candidate']['view'],current)
    facts=printed_facts('本体合計730\n消費税58\n総合計788')
    updated,inputs=repair_candidate(u,current,facts,CATEGORIES)
    assert u==before and inputs['items'][:2]==[
        {**n, **{k:o[k] for k in ('name','amount','category')}} for o,n in zip(current['items'],updated['record']['items'])]
    assert inputs['date']==46296 and inputs['merchant']=='本人修正' and inputs['amount']==788
    assert inputs['action']==inputs['structure_confirmation']=='未選択'
    with pytest.raises(StateError,match='snapshot_stale'):check_snapshot(old_snapshot,updated['view'])
    u['candidate']=updated;v['generation']+=1;runner.store.save(v)
    assert runner.post_normal(key,uid,apply=True)['status']=='posting_authority_required'
    assert runner.post_manual(key,uid,snapshot(updated['view'],inputs),apply=True)['status']=='posting_authority_required'
    assert db.writes==[]
    assert evaluate(updated['record'],inputs,CATEGORIES)['item_sum']==788
    with pytest.raises(StateError,match='immutable'):repair_candidate(u,inputs,facts,CATEGORIES)


@pytest.mark.parametrize('field',['posting_authority','intent','readback'])
def test_authenticated_or_started_candidate_is_not_repaired(field):
    runner,key,uid,db,current=taxed_rig();u=runner.store.load()['pages'][key]['units'][uid];u[field]={}
    with pytest.raises(StateError,match='immutable'):repair_candidate(u,current,printed_facts('本体合計730\n消費税58\n総合計788'),CATEGORIES)


def test_future_page_normalization_preserves_unit_identity_and_replay():
    from test_pdf_intake_authority import page
    p=PageUnit.model_validate(page())
    reading=PageReceiptExtraction.model_validate({'receipts':[{'bbox':{'left':0,'top':0,'right':1,'bottom':1},
        'receipt':receipt().model_dump(),'item_boxes':[]}], 'separation_complete':True,
        'mixed_page_kind_suspected':False,'cross_page_continuation_suspected':False})
    initial=build_receipt_units(p,reading,reading,CAT)
    normalized=normalize_readings([reading,reading],{1:STAR},CAT)
    final=build_receipt_units(p,*normalized,CAT)
    assert final['units'][0]['receipt_unit_id']==initial['units'][0]['receipt_unit_id']
    assert normalized==normalize_readings(normalized,{1:STAR},CAT)
    assert normalized[0].receipts[0].receipt.total==1096
    assert len(reading.receipts[0].receipt.items)==2


def test_voucher_classification_is_hard_not_cell_completion_bypass():
    from test_pdf_intake_authority import page
    runner,key,uid,db=rig();p=PageUnit.model_validate(runner.store.load()['pages'][key]['page'])
    c=runner.store.load()['pages'][key]['units'][uid]['candidate']
    # A model can produce apparently balanced negatives; policy must still hold.
    raw=receipt((1000,-500),500)
    value=draft(p,c['manifest'],uid,[raw,raw],categories=CAT,text='ポイントお買物券 -500')
    assert 'settlement_classification_required' in value['hard_issues']


def test_repair_plan_uses_existing_writer_with_explicit_tax_category():
    from app.pdf_intake_runner import plan
    from app.receipt_item_confirmation import ConfirmedItems
    from uuid import uuid4
    runner,key,uid,db,current=taxed_rig();u=runner.store.load()['pages'][key]['units'][uid]
    fixed,inputs=repair_candidate(u,current,printed_facts('本体合計730\n消費税58\n総合計788'),CATEGORIES)
    inputs.update(action='記帳する',structure_confirmation='確認済み')
    snap=snapshot(fixed['view'],inputs)
    proof=ConfirmedItems(str(uuid4()),fixed['record']['digest'],digest(snap),digest(inputs),
        digest(fixed['record']['legacy']['identity']),'a'*64,100,'b'*64)
    evaluation=evaluate(fixed['record'],inputs,CATEGORIES,confirmation=proof)
    assert evaluation['status']=='ready_to_confirm'
    first=plan(evaluation['parsed'],uid,runner.store.load()['pages'][key]['page'],CATEGORIES,fixed['prepared_at'])
    assert first==plan(evaluation['parsed'],uid,runner.store.load()['pages'][key]['page'],CATEGORIES,fixed['prepared_at'])
    assert [len(op['rows']) for op in first['operations']]==[1,3,1]
    assert sum(r[4] for r in first['operations'][1]['rows'])==788
    assert first['operations'][1]['rows'][-1][5:7]==['食費','食料品'] and db.writes==[]


def test_pending_projection_cannot_prepare_or_append_a_second_card():
    from app.pdf_intake_production import Projection
    from types import SimpleNamespace
    runner,key,uid,db=rig();v=runner.store.load();v['pages'][key]['units'][uid]['status']='settlement_projection_pending'
    v['generation']+=1;runner.store.save(v)
    assert runner.prepare_manual(key,uid,{})['status']=='settlement_projection_pending'
    projection=Projection(SimpleNamespace(store=runner.store,db=db))
    projection.rows=lambda:[]
    projection.batch=lambda _:pytest.fail('pending revision must not append a card')
    projection.unit(key,uid)
    assert db.writes==[]
