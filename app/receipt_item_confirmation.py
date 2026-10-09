"""Per-item evidence and explicit owner confirmation, never cell authority.

Original extraction and item IDs remain immutable. Owner edits are a separate
snapshot. A verified Google OIDC POST can attest only this exact snapshot; it
cannot clear segmentation, transaction-kind or fabricated-adjustment gates.
"""
from copy import deepcopy
from dataclasses import dataclass
import unicodedata
from .drive_run_state import StateError
from .page_receipt_model import digest
from .human_general_auth_transport import VerifiedActor, ISSUER

ACTION='confirm_receipt_item_snapshot'
RESOLVABLE={'reread_item_structure_changed','replay_item_structure_changed'}
CHOICES=('未選択','確認済み','保留')


def annotated(record, readings, categories, *, adjustment_targets=None):
    """Trusted typed readings of the same frozen unit; no global gate removal."""
    from .receipt_item_review import validate
    validate(record)
    if len(readings)<2:raise StateError('item_evidence_independent_readings_required')
    result=deepcopy(record);evidence=[]
    base=record['legacy']['parsed']['items']
    for reading in readings:
        if (len(reading.items)!=len(base) or reading.transaction_kind!='purchase' or
                reading.total!=record['legacy']['parsed']['total']):
            raise StateError('item_evidence_unit_unstable')
    for index,(item,original) in enumerate(zip(result['items'],base)):
        samples=[r.items[index] for r in readings]
        norm=lambda v:unicodedata.normalize('NFKC',v)
        mapped=all(norm(s.name)==norm(original['name']) and s.quantity==original['quantity']
                   and s.amount==original['amount'] for s in samples)
        same_shape=all((s.name,s.quantity,s.amount)==(original['name'],original['quantity'],original['amount']) for s in samples)
        pairs={(s.major_category,s.minor_category) for s in samples}
        stable=mapped and len(pairs)==1 and next(iter(pairs)) in set(categories) and all(s.confidence>=.8 for s in samples)
        # Only the two narrow reread gates may coexist with local prefill.
        safe=not set(record['legacy']['hard_issues'])-RESOLVABLE
        item['category']='｜'.join(next(iter(pairs))) if stable and safe and item['kind']=='product' else ''
        evidence.append({'item_id':item['item_id'],'mapped':mapped,'shape_agrees':same_shape,
            'category_agrees':stable,'category':item['category'],
            'readings':[{'name':s.name,'quantity':s.quantity,'amount':s.amount,
                         'category':'｜'.join((s.major_category,s.minor_category))} for s in samples]})
    if adjustment_targets is not None:result['adjustment_targets']=deepcopy(adjustment_targets)
    result['review_evidence']={'schema':1,'items':evidence,'full_structure_confirmation_required':
                              bool(set(record['legacy']['hard_issues'])&RESOLVABLE)}
    result.pop('digest');result['digest']=digest(result)
    return validate(result)


def binding(record,snapshot,request_id):
    from .receipt_item_review import validate,check_snapshot,card
    validate(record)
    # Snapshot validation is performed against the trusted projection by host.
    if snapshot['identity']['candidate_digest']!=record['digest']:
        raise StateError('item_confirmation_snapshot_stale')
    return {'requested_action':ACTION,'request_id':request_id,
        'identity':deepcopy(record['legacy']['identity']), 'candidate_digest':record['digest'],
        'snapshot_digest':digest(snapshot),'item_ids':[i['item_id'] for i in record['items']],
        'adjustment_targets':deepcopy(record['adjustment_targets'])}


@dataclass(frozen=True)
class ConfirmedItems:
    request_id:str
    candidate_digest:str
    snapshot_digest:str
    input_digest:str
    identity_digest:str
    actor_id:str
    verified_at:int
    authority_digest:str


def attest(record,snapshot,current,actor,*,request_id,owner_actor_id,now,categories):
    from .receipt_item_review import evaluate
    expected=binding(record,snapshot,request_id)
    if (not isinstance(actor,VerifiedActor) or actor.issuer!=ISSUER or actor.actor_id!=owner_actor_id
            or actor.method!='google_oidc_code_pkce_v1' or actor.verification_revision!=1
            or actor.request_id!=request_id or actor.request_digest!=digest(expected)
            or not actor.verified_at<=now<actor.verified_at+600):
        raise StateError('item_confirmation_verified_actor_required')
    if current.get('structure_confirmation')!='確認済み':
        raise StateError('item_confirmation_explicit_review_required')
    proof=ConfirmedItems(request_id,record['digest'],digest(snapshot),digest(current),
        digest(record['legacy']['identity']),actor.actor_id,actor.verified_at,digest([expected,actor.actor_id,actor.verified_at]))
    result=evaluate(record,current,confirmation=proof,categories=categories)
    if result['issues'] or not result['required_fields_complete']:
        raise StateError('item_confirmation_structure_not_resolved')
    return proof


def covers(proof,record,current):
    return isinstance(proof,ConfirmedItems) and proof.candidate_digest==record['digest'] and \
        proof.input_digest==digest(current) and proof.identity_digest==digest(record['legacy']['identity'])
