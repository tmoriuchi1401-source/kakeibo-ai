"""Synthetic automatic authority; never uses a production source/crop/value."""
from copy import deepcopy
from hashlib import sha256
import hmac,json
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.drive_run_state import StateError
from app.medical_auto_posting import POLICY,VALIDATION,apply_automatic
from app.medical_candidate_runtime import process_plans
from app.medical_candidate_preparation import prepare
from app.receipt_confirmation import ReceiptConfirmation,TITLE
from test_receipt_confirmation import DB
from test_medical_candidate_runtime import fixture


def automatic(count=1):
    store,plans,args,send=fixture(count)
    store.value.pop('medical_crop_reviews');store.value.pop('medical_image_send_reviews')
    for p in plans:
        m=p['mapping'];m.pop('human_review_digest');m.update(validation=VALIDATION,automatic_policy=POLICY,
            payment_label='領収額',unresolved_candidates=0,verified_payment_cells=1)
        p['local_provenance']={'date_candidates':1,'date_evidence_verified':True,'issuer_status':'SELECTED_ISSUER',
            'document_binding':{k:m[k] for k in ('source_sha256','source_image_sha256','page','unit')}}
        seal(p,args['key'])
    args['automatic_policy']='auto-v1:free'
    db=DB();review=ReceiptConfirmation(store,db,args['verify_source']);review.render()
    return store,plans,args,send,db,review


def seal(plan,key):
    packet={k:v for k,v in plan.items() if k not in {'preparation_tag','review_id','crop_file'}}
    value=json.dumps(packet,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()
    plan['preparation_tag']=hmac.new(key,b'medical-preparation\0'+value,'sha256').hexdigest()


def post(review,args):return apply_automatic(review,identity_key=args['identity_key'],policy=args['automatic_policy'])


@pytest.mark.parametrize('column,value',[(7,'2026-01-01'),(8,'Owner clinic'),(12,'保留'),(14,'Owner note')])
def test_owner_input_and_hold_are_never_automatically_overwritten(column,value):
    store,plans,args,send,db,review=automatic();db.rows[TITLE][0][column]=value;review.capture_inputs()
    before=deepcopy(db.rows[TITLE]);assert process_plans(plans,**args)['medical_ai_requests']==0
    assert post(review,args)==0;review.render()
    assert db.rows[TITLE][0][7:15]==before[0][7:15] and not db.rows['支出明細']


def test_multiple_verified_images_are_not_a_unique_accounting_answer():
    store,plans,args,send,db,review=automatic();plans[0]['mapping']['verified_payment_cells']=2;seal(plans[0],args['key'])
    assert process_plans(plans,**args)['medical_ai_requests']==1
    assert post(review,args)==0 and not db.rows['支出明細']


def test_auto_preparation_ignores_poisoned_manual_record_and_preserves_exact_bytes(monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY',raising=False)
    payload=(Path(__file__).parent/'fixtures/synthetic_medical_payment.png').read_bytes()
    source={'source_id':'synthetic','version':'1','sha256':sha256(payload).hexdigest(),'mime_type':'image/png'}
    packet,crop=prepare(source,payload,b'x'*32,automatic=True,crop_review={'poisoned':'manual'},review_key=None)
    assert packet['status']=='prepared' and packet['mapping']['automatic_policy']==POLICY
    assert 'human_review_digest' not in packet['mapping'] and sha256(crop).hexdigest()==packet['proof']['digest']


@pytest.mark.parametrize('label',['前回入金額','累計入金額','未収額','預り金','釣銭'])
def test_known_nonpayment_cells_can_be_classified_but_never_sent_as_payment(label):
    from test_medical_anonymized_candidate import glyph_fixture
    from app.medical_anonymization import verify_cell_pixels,png
    from app.medical_image_candidate import seal_crop
    image,obs,glyphs=glyph_fixture(label+'123円')
    assert verify_cell_pixels(image,observations=obs,glyphs=glyphs,classify_nonpayment=True)==label
    with pytest.raises(ValueError):seal_crop(png(image),label,b'x'*32)


@pytest.mark.parametrize('status,reason',[('ambiguous','multiple_payment_amounts'),('unreadable','illegible')])
def test_ai_ambiguity_is_not_converted_to_a_payment(status,reason):
    from app.medical_image_candidate import PaymentAnswer
    store,plans,args,send,db,review=automatic()
    send.return_value=PaymentAnswer(status=status,candidates=[],reason=reason)
    process_plans(plans,**args);assert post(review,args)==0 and not db.rows['支出明細']


def test_old_version_owner_hold_cannot_be_bypassed_by_a_new_version():
    from app.medical_auto_posting import owner_blocked
    store,plans,args,send,db,review=automatic();source=plans[0]['source']
    db.rows[TITLE][0][12]='保留';review.capture_inputs()
    newer=dict(source,version='2');review.observe_medical(newer,'synthetic-folder')
    assert owner_blocked(newer,store.value)
    assert post(review,args)==0 and db.rows[TITLE][0][12]=='保留'


def test_valid_saved_result_from_manual_binding_is_held_without_resend_or_global_failure():
    from app.medical_candidate_runtime import response_tag
    store,plans,args,send,db,review=automatic(2)
    process_plans(plans[:1],**args)
    aid,record=next(iter(store.value['medical_image_analyses'].items()))
    # A completed, authenticated record from the separate legacy manual route.
    record['mapping']['validation']='exact_human_reviewed_payment_crop'
    record['mapping'].pop('automatic_policy')
    record['integrity_tag']=response_tag(aid,record,record['result'],args['identity_key'])
    result=process_plans(plans,**args)
    assert result['medical_ai_held']==1 and result['medical_ai_requests']==1
    assert send.call_count==2 and store.value['medical_image_analyses'][aid]==record
