"""Private draft CAS over existing versioned IO. No clients or HTTP writers.

Provisioning, key custody, retention and recovery are operator responsibilities.
The whole projection (including medical inputs) is encrypted in its own domain.
No authentication document, ten-minute ticket or financial request is reused.
"""
from copy import deepcopy
from dataclasses import asdict
import json
import re
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken

from .drive_run_state import StateError
from .iphone_draft_journal import LocalJournal, check_batch, encoded, MAX_REQUESTS
from .pdf_intake_registry import strong_etag
from .unified_receipt_review import digest

SCHEMA = 'iphone-encrypted-draft-domain-v1'
LIMIT = 700_000  # below existing FirestoreConditionalState's 750k byte limit


def fail(reason):
    raise StateError('iphone_persistent_' + reason)


def unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            fail('duplicate_json_key')
        out[key] = value
    return out


class EncryptedDraftIO:
    """Wrap injected FirestoreConditionalState or equivalent conditional IO.

    No create/set/delete/retry. Keys and the expected domain come from trusted
    composition, never Sheet/browser input. Ciphertext binds scope to prevent
    cross-owner, generation or purpose substitution, even with a shared key.
    """
    def __init__(self, io, key, scope):
        self.io, self.cipher, self.scope = io, Fernet(key), deepcopy(scope)

    def encode(self, value):
        raw = self.cipher.encrypt(encoded(dict(domain=SCHEMA, scope=self.scope, value=value)))
        if len(raw) > LIMIT:
            fail('capacity_exceeded')
        return raw

    def decode(self, raw):
        try:
            if type(raw) is not bytes or len(raw) > LIMIT:
                fail('quarantined')
            value = json.loads(self.cipher.decrypt(raw), object_pairs_hook=unique)
            if set(value) != {'domain', 'scope', 'value'} or value['domain'] != SCHEMA or value['scope'] != self.scope:
                fail('quarantined')
            return value['value']
        except (ValueError, TypeError, InvalidToken):
            fail('quarantined')

    def read(self):
        raw, tag = self.io.read_versioned()
        if not strong_etag(tag):
            fail('strong_cas_required')
        return self.decode(raw), raw, tag


class PersistentJournal(LocalJournal):
    """One atomic encrypted projection, shared by all instances.

    Reuses LocalJournal schema/batch validation and exact request fingerprints,
    plus ConditionalRequests' read_versioned/replace_versioned/read-back pattern.
    Old snapshots are retained until an explicitly approved lifecycle operation;
    capacity exhaustion holds rather than silently pruning unsubmitted input.
    """
    def __init__(self, io, key, *, owner, contract_digest, generation, initial_snapshot):
        if (not isinstance(owner, str) or not re.fullmatch('[a-f0-9]{64}', owner)
                or type(generation) is not int or generation < 1
                or any(not isinstance(v, str) or not re.fullmatch('[a-f0-9]{64}', v) for v in (contract_digest, initial_snapshot))):
            fail('scope_invalid')
        self.binding = contract_digest
        self.scope = dict(owner=owner, contract=contract_digest, generation=generation,
                          initial_snapshot=initial_snapshot, purpose='DRAFT_ONLY')
        self.transport = EncryptedDraftIO(io, key, self.scope)

    def initialize(self):
        fail('explicit_offline_migration_required')

    def migration_payload(self, legacy, legacy_tag, *, details=None):
        """Pure conversion. Operator must freeze/check the local source twice,
        provision destination with create-only semantics, read back, then retire
        the old write path. Never invoked by GET or auto-repair.
        """
        self._validate(legacy)
        if details is not None and not isinstance(details, dict):
            fail('migration_invalid')
        if not strong_etag(legacy_tag) or legacy['batch'] is None or legacy['batch']['snapshot_digest'] != self.scope['initial_snapshot']:
            fail('migration_invalid')
        value = dict(legacy=deepcopy(legacy), history=[], details=deepcopy(details or {}), detail_history=[], health='active',
                     migration=dict(legacy_digest=digest(legacy), legacy_tag=legacy_tag))
        return self.transport.encode(value)

    def _state(self):
        value, raw, tag = self.transport.read()
        try:
            if (set(value) != {'legacy', 'history', 'details', 'detail_history', 'health', 'migration'} or value['health'] != 'active'
                    or not isinstance(value['history'], list) or len(value['history']) > MAX_REQUESTS
                    or not isinstance(value['details'], dict) or not isinstance(value['detail_history'], list)
                    or len(value['detail_history']) != len(value['history'])
                    or any(not isinstance(d, dict) for d in value['detail_history'])
                    or set(value['migration']) != {'legacy_digest', 'legacy_tag'}
                    or not strong_etag(value['migration']['legacy_tag'])):
                fail('quarantined')
            self._validate(value['legacy'])
            for old in value['history']:
                self._validate(old)
            chain = value['history'] + [value['legacy']]
            if digest(chain[0]) != value['migration']['legacy_digest']:
                fail('quarantined')
            for previous, current in zip(chain, chain[1:]):
                if (current['revision'] != previous['revision'] + 1
                        or any(current['requests'].get(k) != v for k, v in previous['requests'].items())):
                    fail('quarantined')
            if chain[0]['batch']['snapshot_digest'] != self.scope['initial_snapshot']:
                fail('quarantined')
            encoded(value)
        except (KeyError, TypeError, ValueError):
            fail('quarantined')
        return value, raw, tag

    def read_versioned(self):
        value, _, tag = self._state()
        return deepcopy(value['legacy']), tag

    def read_details(self):
        value, _, tag = self._state()
        return deepcopy(value['details']), tag

    def _request(self, batch, expected_tag, request_id, details):
        value = asdict(batch)
        check_batch(value, self.binding)
        try:
            if str(UUID(request_id)) != request_id:
                fail('request_id_invalid')
        except (ValueError, TypeError, AttributeError):
            fail('request_id_invalid')
        if not strong_etag(expected_tag):
            fail('strong_cas_required')
        if not isinstance(details, dict):
            fail('details_invalid')
        return value, digest(dict(batch=value, expected_tag=expected_tag, details=details))

    @staticmethod
    def _matches(before, receipt, fingerprint, request_id, value, expected_tag, details):
        if receipt['fingerprint'] == fingerprint:
            return True
        # Migrated v1 receipts keep their ORIGINAL fingerprint. Only their
        # original attachment set and exact old payload/tag may be replayed.
        initial = before['history'][0] if before['history'] else before['legacy']
        initial_details = before['detail_history'][0] if before['detail_history'] else before['details']
        return (request_id in initial['requests'] and details == initial_details
                and receipt['fingerprint'] == digest(dict(batch=value, expected_tag=expected_tag)))

    def recover(self, request_id, batch, expected_tag, *, details=None):
        before, _, tag = self._state()
        details = before['details'] if details is None else details
        value, fingerprint = self._request(batch, expected_tag, request_id, details)
        receipt = before['legacy']['requests'].get(request_id)
        if not receipt or not self._matches(before, receipt, fingerprint, request_id, value, expected_tag, details):
            fail('recovery_unresolved')
        return dict(status='draft_save_observed', receipt=deepcopy(receipt), current_tag=tag,
                    is_current=before['legacy']['batch'] == value and before['details'] == details,
                    authorized=False, operation_targets=[])

    def save(self, batch, expected_tag, request_id, *, details=None):
        before, raw, tag = self._state()
        details = before['details'] if details is None else deepcopy(details)
        value, fingerprint = self._request(batch, expected_tag, request_id, details)
        state = before['legacy']
        receipt = state['requests'].get(request_id)
        if receipt:
            if not self._matches(before, receipt, fingerprint, request_id, value, expected_tag, details):
                fail('replay_payload_changed')
            return self.recover(request_id, batch, expected_tag, details=details)
        if tag != expected_tag:
            fail('cas_conflict')
        # The batch validator is the existing eight-case contract; persistent
        # scope additionally fences source/case membership across all saves.
        identities = lambda b: {k: (d['case_id'], d['kind'], d['identity']) for k, d in b['drafts'].items()}
        if identities(value) != identities(state['batch']):
            fail('snapshot_identity_changed')
        if len(state['requests']) >= MAX_REQUESTS:
            fail('capacity_exceeded')
        after = deepcopy(before)
        after['history'].append(deepcopy(state))
        after['detail_history'].append(deepcopy(before['details']))
        after['details'] = details
        updated = after['legacy']
        updated['revision'] += 1
        updated['batch'] = value
        receipt = dict(fingerprint=fingerprint, revision=updated['revision'], batch_digest=digest(value))
        updated['requests'][request_id] = receipt
        self._validate(updated)
        proposed = self.transport.encode(after)
        # Unknown outcomes propagate; no retry or replacement request ID.
        self.transport.io.replace_versioned(raw, tag, proposed)
        observed, _, actual_tag = self._state()
        if observed != after:
            fail('save_outcome_unknown')
        return dict(status='draft_saved', receipt=deepcopy(receipt), current_tag=actual_tag,
                    is_current=True, authorized=False, operation_targets=[])

    def recovery_export(self):
        """Explicit private operator export; not a GET, log or repair action.
        Preserve ciphertext even if corrupt. Restoration needs a new reviewed
        generation and create-only provisioning, never overwrite damaged state.
        """
        return self.transport.io.read_versioned()
