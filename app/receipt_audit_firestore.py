"""Opt-in Firestore atomic metadata adapter; no provisioning or TTL on history.

New records only. Event + replay receipt + pair decision + current pointer are
one transaction. No transaction retry, event replacement or delete interface.
"""
from .drive_run_state import StateError
from .receipt_audit import (validate_event, entity_key, request_key, pair_key, partition,
    pending, current_after, needs_attention, receipt, IDENTITY_FIELDS, checked_hash, validate_current)


class FirestoreAuditRepository:
    def __init__(self, client, scope, *, write_enabled=False):
        checked_hash(scope)
        self.client, self.enabled = client, write_enabled
        self.root = client.collection('kakeibo_receipt_audit').document(scope)

    def _current(self, identity): return self.root.collection('current').document(entity_key(identity))
    def _receipt(self, key): return self.root.collection('receipts').document(key)
    def _event(self, ref):
        if set(ref) != {'year','event_id'} or len(ref['year']) != 4 or not ref['year'].isdigit():
            raise StateError('audit_history_reference_invalid')
        checked_hash(ref['event_id'])
        return self.root.collection('years').document(ref['year']).collection('events').document(ref['event_id'])
    def _pair(self, identity, ledger_id): return self.root.collection('pairs').document(pair_key(identity, ledger_id))

    @staticmethod
    def _read(document):
        result = document.get(retry=None, timeout=10)
        return result.to_dict() if result.exists else None

    def get_current(self, identity): return validate_current(self._read(self._current(identity)) or pending(identity))
    def lookup(self, rid): return self._read(self._receipt(request_key(rid)))
    def get_pair(self, identity, ledger_id): return self._read(self._pair(identity, ledger_id))
    def get_event(self, ref):
        value = self._read(self._event(ref))
        if value is None: raise StateError('audit_history_missing')
        return validate_event(value)

    def commit(self, value, expected_revision):
        if self.enabled is not True: raise StateError('audit_live_write_disabled')
        validate_event(value)
        if type(expected_revision) is not int or expected_revision < 0: raise StateError('audit_current_revision_invalid')
        from google.cloud import firestore
        identity = {k: value[k] for k in IDENTITY_FIELDS}
        current_ref = self._current(identity); receipt_ref = self._receipt(value['event_id'])
        event_ref = self._event({'year': partition(value), 'event_id': value['event_id']})
        pair_ref = self._pair(identity, value['candidate_ledger_id']) if value['event_type'] in {'confirmed_distinct','reconciled_existing'} else None
        marker = receipt(value)

        @firestore.transactional
        def apply(transaction):
            prior = receipt_ref.get(transaction=transaction, retry=None, timeout=10)
            if prior.exists:
                if prior.to_dict() != marker: raise StateError('audit_request_reuse')
                return True
            current_snap = current_ref.get(transaction=transaction, retry=None, timeout=10)
            current = current_snap.to_dict() if current_snap.exists else pending(identity)
            validate_current(current)
            if current['state_revision'] != expected_revision or current['identity'] != identity:
                raise StateError('HTTP_412')
            if not needs_attention(current): raise StateError('audit_terminal_operation_forbidden')
            pair = pair_ref.get(transaction=transaction, retry=None, timeout=10) if pair_ref else None
            if pair and pair.exists: raise StateError('audit_pair_already_confirmed')
            after = current_after(value, expected_revision)
            transaction.create(event_ref, value)
            transaction.create(receipt_ref, marker)
            transaction.set(current_ref, after)
            if pair_ref:
                transaction.create(pair_ref, {'decision': value['decision'], 'event_ref': after['last_event'],
                                             'authority_digest': value['authority_digest']})
            return False
        try:
            replayed = apply(self.client.transaction(max_attempts=1))
        except StateError: raise
        except Exception:
            # Unknown commit may already have completed. Inspect the receipt,
            # do not call commit again or overwrite current/history automatically.
            raise StateError('audit_commit_unknown_readback_required') from None
        if self._read(receipt_ref) != marker or self.get_event(marker['event_ref']) != value:
            raise StateError('audit_exact_readback_required')
        if not replayed and self.get_current(identity) != current_after(value, expected_revision):
            raise StateError('audit_current_readback_required')
        return {'replayed': replayed, 'receipt': marker}
