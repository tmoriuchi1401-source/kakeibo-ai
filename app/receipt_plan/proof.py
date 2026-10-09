"""Plan-only subset of PR #91 (1ac98bd); no live authority/writer transport."""
from copy import deepcopy
from dataclasses import dataclass
from .identity import digest
ACTION='confirm_receipt_item_snapshot'
RESOLVABLE={'reread_item_structure_changed','replay_item_structure_changed'}
CHOICES=('未選択','確認済み','保留')
from ..drive_run_state import StateError
def binding(record,snapshot,request_id):
    from .items import validate,check_snapshot,card
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

def covers(proof,record,current):
    return isinstance(proof,ConfirmedItems) and proof.candidate_digest==record['digest'] and \
        proof.input_digest==digest(current) and proof.identity_digest==digest(record['legacy']['identity'])
