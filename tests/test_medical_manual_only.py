"""Medical retirement boundaries, using synthetic stores only; no network/PII."""
from copy import deepcopy
from unittest.mock import Mock
import pytest

from app.drive_run_state import StateError
from app.medical_auto_posting import apply_automatic
from app.medical_local_reading import apply_local
from app.medical_candidate_runtime import run_prepared
from app.receipt_confirmation import TITLE,CHOICES
from test_receipt_confirmation import medical,confirm
from test_medical_auto_posting import automatic
from test_medical_candidate_runtime import process_plans
from test_medical_local_reading import parsed,proof


@pytest.mark.parametrize('policy',['off','prepare-only','reviewed-v1:paid','auto-v1:free','auto-v1:paid'])
@pytest.mark.parametrize('status',['waiting','pending','applied','closed_user','superseded'])
def test_saved_authority_and_old_policies_cannot_mutate_review_or_accounting(policy,status):
    store,plans,args,send,db,review=automatic()
    process_plans(plans,**args)  # Historical synthetic admitted evidence, with a mock sender.
    key=next(iter(review.items));review.items[key]['status']=status
    before=deepcopy((store.value,db.rows,db.writes))
    assert apply_automatic(review,identity_key=args['identity_key'],policy=policy)==0
    assert apply_local(review,plans[0]['source'],'synthetic-folder',parsed(),proof()) is False
    assert (store.value,db.rows,db.writes)==before


@pytest.mark.parametrize('policy',['off','prepare-only','auto-v1:free','reviewed-v1:paid'])
def test_retired_coordinator_rejects_before_reading_private_state_or_model(policy,tmp_path,monkeypatch):
    import app.medical_candidate_runtime as runtime
    subprocess=Mock(side_effect=AssertionError('No model/AI subprocess'))
    monkeypatch.setattr(runtime.subprocess,'run',subprocess)
    before=list(tmp_path.iterdir())
    with pytest.raises(StateError,match='medical_human_confirmation_required'):
        run_prepared({'GEMINI_API_KEY':'synthetic','MEDICAL_DERIVED_AI_POLICY':policy},str(tmp_path))
    assert list(tmp_path.iterdir())==before
    subprocess.assert_not_called()


@pytest.mark.parametrize('origin,action,hash_present',[
    ('automatic','医療費を確定',True),('', '候補で医療費を確定',True),('', '医療費を確定',False),
])
def test_direct_medical_writer_requires_explicit_durable_manual_confirmation(origin,action,hash_present):
    from app.medical_candidate_state import digest
    review,store,db,verify,source=medical();confirm(db);review.capture_inputs()
    key=next(iter(review.items));item=deepcopy(review.items[key])
    item.update(status='pending',plan=[['支出明細',['synthetic-do-not-write']]],decision_origin=origin)
    item['inputs'][5]=action
    if hash_present:item['confirmation_hash']=digest(item['inputs'])
    before=deepcopy((store.value,db.rows,db.writes))
    with pytest.raises(StateError,match='medical_manual_input_required'):
        review._write_accounting_plan(key,item)
    assert (store.value,db.rows,db.writes)==before


@pytest.mark.parametrize('unit,amount',[(10,300),(11,300),(22,3050),(23,2140),(29,2140),(30,1100),(31,630),(32,2890),(33,300),(34,6600)])
def test_saved_sample_amounts_do_not_authorize_blank_manual_input(unit,amount):
    review,store,db,verify,source=medical()
    item=next(iter(review.items.values()))
    item['medical_candidates']={'date':'2026-09-01','issuer':'Synthetic clinic','amount_yen':amount,'category':'医療・保険｜病院'}
    review.render();review.capture_inputs()
    assert review.apply_confirmations()==0
    assert db.rows[TITLE][0][7:15]==['']*8 and not db.rows['支出明細']
    assert '候補で医療費を確定' not in CHOICES


def test_historical_pending_automatic_intent_is_not_retried_or_discarded():
    review,store,db,verify,source=medical();key=next(iter(review.items))
    review.items[key].update(status='pending',decision_origin='automatic',plan=[['支出明細',['synthetic-missing']]])
    before=deepcopy((store.value,db.rows))
    with pytest.raises(StateError,match='confirmation_write_reconciliation_required'):
        review.apply_confirmations()
    assert (store.value,db.rows)==before and not db.rows['支出明細']
