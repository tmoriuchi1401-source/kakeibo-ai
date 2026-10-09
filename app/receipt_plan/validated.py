"""Plan-only subset of PR #91 (1ac98bd); no live authority/writer transport."""
import json
from ..drive_run_state import StateError
from .identity import digest
from .items import SCHEMA,check_snapshot,card,evaluate
from .proof import ConfirmedItems,binding
def validate_plan(readers,journal,request_id,snapshot,owner_actor_id):
    candidate,current,categories=readers.read()
    if snapshot!=current:raise StateError('item_runner_snapshot_stale')
    raw,tag=journal.read_versioned();state=json.loads(raw);request=state['requests'].get(request_id)
    if (not request or request['snapshot']!=snapshot or request['binding']!=binding(candidate,snapshot,request_id)
            or request['proof']['actor_id']!=owner_actor_id):
        raise StateError('item_runner_authenticated_request_required')
    proof=ConfirmedItems(**request['proof']);inputs=check_snapshot(snapshot,card(candidate,categories=categories))
    if proof.request_id!=request_id or proof.snapshot_digest!=digest(snapshot):raise StateError('item_runner_proof_stale')
    if proof.authority_digest!=digest([request['binding'],proof.actor_id,proof.verified_at]):raise StateError('item_runner_proof_stale')
    plan=evaluate(candidate,inputs,categories,confirmation=proof)
    if request['plan']!={'input':inputs,'validation':plan}:raise StateError('item_runner_plan_changed')
    if journal.read_versioned()!=(raw,tag):raise StateError('item_runner_journal_changed')
    return candidate,plan

def accounting_plan(candidate,validation,categories,*,timestamp):
    """Same ReceiptPipeline materializer; only an in-memory PlanningDB."""
    from .planning import PlanningDB
    from ..receipt_pipeline import ReceiptPipeline
    from .models import ReceiptResult
    from .validation import validate_receipt_result
    from .fields import original_uri
    if validation['status']!='ready_to_confirm':raise StateError('item_runner_not_ready')
    result=ReceiptResult.model_validate(validation['parsed'])
    if result.transaction_kind!='purchase' or not validate_receipt_result(result,categories)[0]:
        raise StateError('item_runner_validation_failed')
    db=PlanningDB(categories);pipeline=ReceiptPipeline(db,None,clock=lambda:timestamp)
    ident=candidate['legacy']['identity']
    pipeline._materialize_result(result,ident['receipt_unit_id'],original_uri(ident['source_file_id'],ident['page_number']),[])
    return db.plan
