"""Synthetic roles and immutable saved-response reuse; no real receipts."""
from copy import deepcopy
from PIL import Image,ImageDraw
import pytest

from app.medical_anonymization import anchors
from app.medical_accounting_roles import evaluate_roles
from app.medical_candidate_runtime import process_plans
from app.medical_auto_posting import decide
from app.drive_run_state import StateError
from test_medical_auto_posting import automatic,seal,post


def token(text,x,y,*,confidence=95):
    return {'text':text,'box':(x,y,x+14*len(text),y+20),'confidence':confidence,'line':(1,1,y)}


def role_fixture(labels,*,selected='今回入金額',ruled=False):
    stream=[token(label,30,y) for label,y in labels]+[token(selected,30,240)]
    mapping={'source_sha256':'a'*64,'source_image_sha256':'b'*64,'crop_sha256':'c'*64,'page':1,'unit':1,
        'crop_coordinates_original':[25,235,350,265],'retained_regions_original':[[25,235,105,265],[245,235,350,265]],
        'verified_payment_cells':1}
    image=None
    if ruled:
        image=Image.new('RGB',(500,450),'white');ImageDraw.Draw(image).line((220,20,220,300),fill='black',width=2)
    cues=anchors(stream);unresolved=[(box,'numeric_region_not_verified') for _,box in cues if box[1]<235 or box[3]>265]
    mapping['unresolved_candidates']=len(unresolved)
    return evaluate_roles((stream,),cues,unresolved,mapping,selected,image=image),mapping,stream


def test_nonpayment_roles_do_not_require_readable_values_or_transmissible_crops():
    e,m,_=role_fixture([('領収書',20),('負担割合',70),('保険者負担額',110),('前回入金額',150),('領収印',340)])
    assert e['original_cutout_unresolved']>0 and e['unresolved_payment_conflicts']==0
    assert e['independent_payment_fields']==1
    assert e['cutout_classifications']=={'nonpayment':e['original_cutout_unresolved']}


def test_current_charge_and_declared_component_need_a_common_value_column():
    labels=[('今回請求額',100),('保険分負担金額',180)]
    linked,_,_=role_fixture(labels,ruled=True)
    assert linked['unresolved_payment_conflicts']==0
    assert {f['role'] for f in linked['field_evidence']}=={'payment','current_charge_linked','billing_component_linked'}
    unlinked,_,_=role_fixture(labels)
    assert unlinked['unresolved_payment_conflicts']>=1


@pytest.mark.parametrize('label',['請求額','未収額','預り金','返金','累計入金額','一部入金額','合計金額'])
def test_name_only_or_unknown_partial_settlement_relationship_is_a_hold(label):
    e,_,_=role_fixture([(label,100)])
    assert e['unresolved_payment_conflicts']>0


def test_second_payment_and_multiple_receipt_units_remain_conflicts():
    e,_,_=role_fixture([('今回入金額',100)])
    assert e['independent_payment_fields']==2 and e['unresolved_payment_conflicts']>=1
    e,_,_=role_fixture([('領収書',20),('領収書',320)])
    assert e['unresolved_payment_conflicts']>=1


def test_split_and_repeated_discovery_of_one_field_is_not_another_payment():
    from app.medical_anonymization import _contains
    e,m,stream=role_fixture([])
    stream=[token('今回',30,240),token('入金',58,240),token('額',86,240)]
    cues=anchors(stream);assert len(cues)>1
    e=evaluate_roles((stream,stream),cues,[],m,'今回入金額')
    assert e['independent_payment_fields']==1 and e['unresolved_payment_conflicts']==0
    assert sum(c['kind']=='duplicate' for c in e['candidates'])>=1


def test_low_confidence_or_distance_is_not_a_nonpayment_reason():
    e,m,stream=role_fixture([('負担割合',20)])
    stream[0]['confidence']=1;cues=anchors(stream)
    e=evaluate_roles((stream,),cues,[(cues[0][1],'numeric_region_not_verified')],m,'今回入金額')
    assert e['unresolved_payment_conflicts']>0
    assert e['candidates'][0]['underlying_kind']=='unknown'


def test_nested_prefix_variants_keep_one_unknown_physical_cue_and_hold():
    e,m,stream=role_fixture([])
    stream += [token('不明',30,100),token('請',58,100),token('求',72,100),token('額',86,100)]
    # Literal observations of the same cue, with and without its prefix.
    # Other fuzzy fragments remain separately subject to the unknown-role gate.
    cues=[c for c in anchors(stream) if c[1][1]!=100 or '請求' in c[0]]
    assert len([c for c in cues if c[1][1]==100])>1
    e=evaluate_roles((stream,),cues,[],m,'今回入金額')
    assert e['unresolved_payment_conflicts']==1
    unresolved=[c for c in e['candidates'] if c['underlying_kind']=='unknown']
    assert len({c['field'] for c in unresolved})==1
    assert sum(c['kind']=='unknown' for c in unresolved)==1
    assert sum(c['kind']=='duplicate' for c in unresolved)>=1


def test_unlabelled_currency_is_held_even_without_a_money_label_cue():
    e,m,stream=role_fixture([])
    stream.append(token('¥???',250,80,confidence=1))
    cues=anchors(stream)
    e=evaluate_roles((stream,),cues,[],m,'今回入金額')
    assert e['additional_unassigned_currency_fields']==1 and e['unresolved_payment_conflicts']==1


def test_unreadable_nonpayment_value_in_its_ruled_cell_is_not_a_new_payment():
    e,m,stream=role_fixture([('保険者負担額',100)])
    stream.append(token('???円',160,100,confidence=1))
    image=Image.new('RGB',(500,450),'white');ImageDraw.Draw(image).rectangle((15,90,300,140),outline='black')
    cues=anchors(stream);unresolved=[(cues[0][1],'numeric_region_not_verified')]
    e=evaluate_roles((stream,),cues,unresolved,m,'今回入金額',image=image)
    assert e['additional_unassigned_currency_fields']==0 and e['unresolved_payment_conflicts']==0


def test_shared_table_edge_can_bind_a_merged_header_without_an_inner_divider():
    e,m,stream=role_fixture([('今回請求額',100)])
    image=Image.new('RGB',(500,450),'white');ImageDraw.Draw(image).line((15,80,15,280),fill='black')
    cues=anchors(stream);unresolved=[(cues[0][1],'text_region_boundary_unknown')]
    e=evaluate_roles((stream,),cues,unresolved,m,'今回入金額',image=image)
    assert e['unresolved_payment_conflicts']==0


def evidence_for_plan(plan):
    e,m,_=role_fixture([('負担割合',20),('領収印',320)])
    actual=plan['mapping'];actual['unresolved_candidates']=e['original_cutout_unresolved']
    e['binding']={k:actual[k] for k in e['binding']}
    return e


def test_policy_evaluation_reuses_signed_response_without_rewriting_mapping_or_provenance():
    store,plans,args,send,db,review=automatic();plan=plans[0]
    evidence=evidence_for_plan(plan);seal(plan,args['key'])
    process_plans(plans,**args);assert post(review,args)==0
    old=deepcopy(store.value['medical_image_analyses'])
    plan['accounting_evaluation']=evidence;seal(plan,args['key'])
    counts=process_plans(plans,**args)
    assert counts['medical_ai_requests']==0 and counts['medical_ai_reused']==1 and send.call_count==1
    assert store.value['medical_image_analyses']==old
    assert post(review,args)==1
    writes=db.writes
    assert process_plans(plans,**args)['medical_ai_requests']==0 and post(review,args)==0 and db.writes==writes
    assert len(store.value['medical_accounting_evaluations'])==1
    assert store.value['medical_image_analyses']==old


@pytest.mark.parametrize('kind',['source','crop','legacy_count'])
def test_new_evaluation_cannot_be_attached_to_different_saved_input(kind):
    store,plans,args,send,db,review=automatic();plan=plans[0]
    e=evidence_for_plan(plan);seal(plan,args['key']);process_plans(plans,**args)
    if kind=='legacy_count':e['original_cutout_unresolved']+=1
    else:e['binding']['source_sha256' if kind=='source' else 'crop_sha256']='f'*64
    plan['accounting_evaluation']=e;seal(plan,args['key'])
    with pytest.raises(StateError,match='evidence_binding_changed'):process_plans(plans,**args)
    assert send.call_count==1 and not db.rows['支出明細']


def test_evaluation_tampering_does_not_relax_the_accounting_veto():
    store,plans,args,send,db,review=automatic();plan=plans[0]
    plan['accounting_evaluation']=evidence_for_plan(plan);seal(plan,args['key']);process_plans(plans,**args)
    next(iter(store.value['medical_accounting_evaluations'].values()))['evidence']['unresolved_payment_conflicts']=0
    next(iter(store.value['medical_accounting_evaluations'].values()))['evidence']['independent_payment_fields']=2
    with pytest.raises(StateError,match='evaluation_integrity_failed'):post(review,args)
    assert not db.rows['支出明細']
