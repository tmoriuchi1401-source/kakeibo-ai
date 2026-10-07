"""Authenticated same/different decisions, metadata only, page-local gates.

Reuses the Google signature-verifying actor type. Dropdown contents and email
strings cannot authenticate. Host supplies trusted request/Drive/ledger reads;
no live endpoint, HGA grant, ledger writer, Medical, AI or mover is started here.
"""
from copy import deepcopy
from datetime import datetime, timezone
from .drive_run_state import StateError
from .human_general_auth_transport import VerifiedActor, ISSUER, TTL
from .receipt_audit import (digest, validate_identity, identifier, checked_hash, request_key,
                          event, IDENTITY_FIELDS)
from .receipt_retention import utc

ACTION = 'reconcile_receipt'
DECISIONS = {'同一レシート': 'same', '別のレシート': 'different', '判断できない': 'unknown'}


def binding(identity, *, request_id, candidate_ledger_id, ledger_snapshot_digest, decision, current_state_revision=0):
    validate_identity(identity); request_key(request_id); identifier(candidate_ledger_id)
    checked_hash(ledger_snapshot_digest)
    if decision not in DECISIONS.values(): raise StateError('reconciliation_decision_invalid')
    if type(current_state_revision) is not int or current_state_revision < 0:
        raise StateError('reconciliation_current_revision_invalid')
    return {'schema_version': 1, 'requested_action': ACTION, 'request_id': request_id,
            'identity': deepcopy(identity), 'candidate_ledger_id': candidate_ledger_id,
            'ledger_snapshot_digest': ledger_snapshot_digest, 'selected_decision': decision,
            'current_state_revision': current_state_revision}


class ReconciliationConfirmation:
    def __init__(self, repository, *, trusted_request, verified_actor, current_identity,
                 load_source_hash, ledger_readback, owner_actor_id, clock=None):
        checked_hash(owner_actor_id)
        self.repo, self.request, self.actor = repository, trusted_request, verified_actor
        self.current, self.source, self.ledger = current_identity, load_source_hash, ledger_readback
        self.owner = owner_actor_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def confirm(self, request_id, *, selected_decision, method, explicit_post):
        if method != 'POST' or explicit_post is not True: raise StateError('reconciliation_explicit_post_required')
        record = self.request(request_id)  # Protected server record, never a Sheet snapshot.
        b = record['binding']; ident = b['identity']
        expected = binding(ident, request_id=request_id, candidate_ledger_id=b['candidate_ledger_id'],
                           ledger_snapshot_digest=b['ledger_snapshot_digest'], decision=b['selected_decision'],
                           current_state_revision=b['current_state_revision'])
        if b != expected or record['digest'] != digest(b) or DECISIONS.get(selected_decision) != b['selected_decision']:
            raise StateError('reconciliation_request_tampered')
        now = self.clock(); created = utc(record['created_at']); expires = utc(record['expires_at'])
        if (expires-created).total_seconds() != TTL or not created <= now < expires:
            raise StateError('reconciliation_request_expired')
        actor = self.actor(request_id)  # Existing Google OIDC trusted adapter, not a plain dict.
        if (not isinstance(actor, VerifiedActor) or actor.issuer != ISSUER or actor.actor_id != self.owner
                or actor.method != 'google_oidc_code_pkce_v1' or actor.request_id != request_id
                or actor.request_digest != record['digest'] or actor.verification_revision != 1
                or actor.policy_revision < 1 or not created.timestamp() <= actor.verified_at < expires.timestamp()
                or actor.verified_at > now.timestamp()):
            raise StateError('reconciliation_actor_rejected')
        current = self.current(ident['source_file_id'], ident['page_number'], ident['receipt_unit_id'])
        if current != ident or self.source(ident['source_file_id']) != ident['source_content_hash']:
            raise StateError('reconciliation_source_page_review_stale')
        target = self.ledger(b['candidate_ledger_id'])
        if (set(target) != {'ledger_id','snapshot_digest','active','readback_complete'}
                or target['ledger_id'] != b['candidate_ledger_id'] or target['snapshot_digest'] != b['ledger_snapshot_digest']
                or target['active'] is not True or target['readback_complete'] is not True):
            raise StateError('reconciliation_ledger_stale_or_ambiguous')
        prior = self.repo.lookup(request_id)
        if prior:
            if prior['request_digest'] != record['digest'] or prior['actor_id'] != actor.actor_id:
                raise StateError('reconciliation_request_reuse')
            saved = self.repo.get_event(prior['event_ref'])
            if saved['event_digest'] != prior['event_digest']:
                raise StateError('reconciliation_history_readback_required')
            return {'status': 'already_confirmed', 'replayed': True, 'event': saved, 'accounting_write': 0}
        state = self.repo.get_current(ident)
        if (state['identity'] != ident or state['state_revision'] != b['current_state_revision']
                or state['status'] != 'reconciliation_required'):
            raise StateError('reconciliation_current_stale')
        if b['selected_decision'] == 'unknown':
            return {'status': 'reconciliation_required', 'replayed': False, 'event': None, 'accounting_write': 0}
        # This proves a human-declared relation, NOT equality of different PDF
        # bytes, a privacy authorization or posting permission.
        proof = digest(['authenticated-reconciliation-v1', b, actor.actor_id, actor.verified_at])
        value = event(ident, request_id=request_id, request_digest=record['digest'],
                      event_type='reconciled_existing' if b['selected_decision']=='same' else 'confirmed_distinct',
                      ledger_id=b['candidate_ledger_id'] if b['selected_decision']=='same' else '',
                      candidate_ledger_id=b['candidate_ledger_id'], decision=b['selected_decision'],
                      actor_id=actor.actor_id, confirmed_at=now.isoformat(), authority_digest=proof,
                      reason_code='owner_verified_existing_receipt' if b['selected_decision']=='same' else 'owner_verified_distinct_pair')
        result = self.repo.commit(value, state['state_revision'])
        return {'status': value['event_type'], 'replayed': result['replayed'], 'event': value, 'accounting_write': 0}


def confirmed_distinct_pair(repository, identity, candidate_ledger_id):
    """Pair-scoped evidence only. Does not disable duplicate checks globally."""
    found = repository.get_pair(identity, candidate_ledger_id)
    if not found or found['decision'] != 'different': return False
    saved = repository.get_event(found['event_ref'])
    return (saved['event_type']=='confirmed_distinct' and saved['authority_digest']==found['authority_digest']
            and saved['candidate_ledger_id']==candidate_ledger_id
            and {k:saved[k] for k in IDENTITY_FIELDS}==identity)
