"""Drive is the sole durable grouping authority; no ledger/AI/Medical clients.

Reuses the private DriveStateTransport. Conditional replacement and read-back
are mandatory. Local grouping confirmations are never imported into this store.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import re

from .drive_run_state import StateError, _aware
from .receipt_pdf_grouping import (
    AdjacentPageGrouping, ConfirmedDocumentUnit, _proposal, _snapshot,
    _valid_proposal, _unit_status, privacy_for,
)
from .receipt_pdf_units import _digest

SCHEMA = 'pdf-grouping-authority-v1'
MAX_STATE_BYTES = 8 * 1024 * 1024
UUID = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
AUTHORITY_FLAGS = {'authority_scope': ['grouping_confirmed', 'rendered_payload_only'],
                   'accounting_allowed': False, 'medical_handoff_allowed': False, 'archive_allowed': False}


def encoded(value):
    # Preserve the established proposal serialization and identity. Reordering
    # nested page metadata would change the existing reviewed proposal digest.
    return json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode()


def empty_state(binding):
    """Operator provisioning only; runtime never initializes missing state."""
    return {'schema': SCHEMA, 'binding': binding, 'generation': 0, 'records': {}, 'audit': []}


def confirmation(proposal, confirmed_at):
    value = {**AUTHORITY_FLAGS,
        'source_file_id': proposal['source_file_id'], 'source_content_hash': proposal['source_content_hash'],
        'page_count': proposal['page_count'], 'proposal_digest': proposal['proposal_digest'],
        'grouping_revision': proposal['grouping_version'], 'confirmed_at': confirmed_at,
        'pages': [{key: page[key] for key in ('page_number', 'page_hash', 'classification', 'extraction_status')}
                  for page in proposal['pages']],
        'confirmed_partition': [g['page_numbers'] for g in proposal['groups']],
        'unit_statuses': [_unit_status(privacy_for(g['page_classifications'])) for g in proposal['groups']]}
    return {**value, 'confirmation_digest': _digest(value)}


def validate(value, binding):
    try:
        if (not isinstance(value, dict) or set(value) != {'schema', 'binding', 'generation', 'records', 'audit'} or
                value['schema'] != SCHEMA or value['binding'] != binding or
                type(value['generation']) is not int or value['generation'] < 0 or
                not isinstance(value['records'], dict) or not isinstance(value['audit'], list)):
            raise ValueError()
        for key, record in value['records'].items():
            if not re.fullmatch(r'[0-9a-f]{64}', key):
                raise ValueError()
            if set(record) != {'proposal', 'confirmation', 'revision', 'status'}:
                raise ValueError()
            proposal = record['proposal']
            if type(record['revision']) is not int or record['revision'] < 1:
                raise ValueError()
            if record['status'] not in {'grouping_required', 'grouping_confirmed', 'rejected', 'held', 'proposal_failed'}:
                raise ValueError()
            if proposal is None:
                if record['confirmation'] is not None or record['status'] not in {'proposal_failed', 'grouping_required'}:
                    raise ValueError()
                continue
            snapshot = {k: proposal[k] for k in ('source_file_id', 'source_content_hash', 'page_count', 'pages')}
            if (key != _digest(proposal['source_file_id']) or
                    not isinstance(proposal['source_file_id'], str) or
                    not re.fullmatch(r'[A-Za-z0-9_-]{1,150}', proposal['source_file_id']) or
                    not re.fullmatch(r'[0-9a-f]{64}', proposal['source_content_hash']) or
                    type(proposal['page_count']) is not int or not 2 <= proposal['page_count'] <= 50 or
                    len(proposal['pages']) != proposal['page_count'] or
                    proposal['grouping_version'] != record['revision'] or
                    not _valid_proposal(proposal, snapshot)):
                raise ValueError()
            for n, page in enumerate(proposal['pages'], 1):
                if (set(page) != {'source_file_id', 'source_content_hash', 'page_number', 'page_hash',
                                  'extraction_status', 'classification', 'reason_code'} or
                        page['source_file_id'] != proposal['source_file_id'] or
                        page['source_content_hash'] != proposal['source_content_hash'] or
                        type(page['page_number']) is not int or page['page_number'] != n or
                        not re.fullmatch(r'[0-9a-f]{64}', page['page_hash']) or
                        page['classification'] not in {'normal', 'medical', 'payroll', 'sensitive_unknown'} or
                        not isinstance(page['extraction_status'], str) or
                        not re.fullmatch(r'[a-z0-9_]{1,80}', page['extraction_status']) or
                        not isinstance(page['reason_code'], str) or
                        not re.fullmatch(r'[a-z0-9_]{1,80}', page['reason_code'])):
                    raise ValueError()
            authority = record['confirmation']
            if authority is not None:
                _aware(authority['confirmed_at'])
                if (record['status'] != 'grouping_confirmed' or proposal['status'] != 'proposed' or
                        any(authority.get(k) is not False for k in
                            ('accounting_allowed', 'medical_handoff_allowed', 'archive_allowed')) or
                        type(authority.get('grouping_revision')) is not int or
                        type(authority.get('page_count')) is not int or
                        authority != confirmation(proposal, authority['confirmed_at'])):
                    raise ValueError()
            elif record['status'] == 'grouping_confirmed':
                raise ValueError()
        for event in value['audit']:
            if (set(event) != {'operation', 'timestamp', 'source_file_id', 'source_content_hash',
                              'before_revision', 'after_revision', 'proposal_digest',
                              'confirmation_digest', 'result', 'request_id', 'request_digest'} or
                    event['operation'] not in {'observe', 'confirm', 'edit', 'reject', 'hold'} or
                    event['result'] not in {'observed', 'source_changed', 'proposal_failed', 'confirmed',
                                           'proposal_updated', 'unchanged_partition', 'rejected', 'held', 'stale_proposal'} or
                    any(type(event[k]) is not int or event[k] < 0 for k in ('before_revision', 'after_revision')) or
                    not re.fullmatch(r'[0-9a-f]{64}', event['source_content_hash']) or
                    any(v and not re.fullmatch(r'[0-9a-f]{64}', v) for v in
                        (event['proposal_digest'], event['confirmation_digest'], event['request_digest'])) or
                    (event['request_id'] and not re.fullmatch(UUID, event['request_id']))):
                raise ValueError()
            _aware(event['timestamp'])
        return value
    except Exception:
        raise StateError('grouping_state_invalid') from None


class DriveGroupingStore:
    def __init__(self, transport, binding, *, preflight=None):
        self.transport, self.binding = transport, binding
        self.preflight = preflight or (lambda: None)
        self.payload = self.tag = self.value = None

    def load(self):
        try:
            self.preflight()
            payload, tag = self.transport.read_versioned()
            if len(payload) > MAX_STATE_BYTES:
                raise ValueError()
            value = validate(json.loads(payload), self.binding)
            self.payload, self.tag, self.value = payload, tag, deepcopy(value)
            return deepcopy(value)
        except StateError:
            raise
        except Exception:
            raise StateError('grouping_state_unavailable') from None

    def save(self, value):
        self.preflight()
        validate(value, self.binding)
        proposed = encoded(value)
        if self.payload is None or len(proposed) > MAX_STATE_BYTES:
            raise StateError('grouping_state_unavailable')
        try:
            self.transport.replace_versioned(self.payload, self.tag, proposed)
        except StateError as error:
            if str(error) == 'state_changed_since_read':
                raise StateError('stale_proposal') from None
            raise
        payload, tag = self.transport.read_versioned()
        if payload != proposed:
            raise StateError('grouping_state_readback_mismatch')
        validate(json.loads(payload), self.binding)
        self.payload, self.tag, self.value = payload, tag, deepcopy(value)


class DurablePdfGrouping:
    def __init__(self, store, load_observations, *, proposer=None, clock=None):
        self.store, self.load_observations = store, load_observations
        self.proposer = proposer or AdjacentPageGrouping()
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat(timespec='seconds'))

    def _current(self, value, source_id):
        record = value['records'].get(_digest(source_id))
        proposal = record['proposal'] if record else None
        # Restricted pages are carried only from the Drive正本, never local JSON.
        observations = self.load_observations(source_id, proposal)
        return observations, record

    def _event(self, value, operation, source_id, source_hash, before, after, result,
               *, request_id='', request_digest=''):
        proposal, authority = after.get('proposal'), after.get('confirmation')
        value['audit'].append({'operation': operation, 'timestamp': self.clock(),
            'source_file_id': source_id, 'source_content_hash': source_hash,
            'before_revision': before['revision'] if before else 0, 'after_revision': after['revision'],
            'proposal_digest': proposal['proposal_digest'] if proposal else '',
            'confirmation_digest': authority['confirmation_digest'] if authority else '',
            'result': result, 'request_id': request_id, 'request_digest': request_digest})
        value['generation'] += 1

    def _sync(self, value, observations, record):
        try:
            snapshot = _snapshot(observations)
        except Exception:
            snapshot = None
        if snapshot is not None and record and record['proposal'] and _valid_proposal(record['proposal'], snapshot):
            return record, False
        revision = record['revision'] + 1 if record else 1
        proposal = None
        if snapshot is not None:
            try:
                proposal = _proposal(snapshot, self.proposer.propose(observations), revision)
            except Exception:
                pass
        after = {'revision': revision, 'proposal': proposal, 'confirmation': None,
                 'status': 'grouping_required'}
        value['records'][_digest(observations.source_file_id)] = after
        result = 'source_changed' if record else 'observed'
        if proposal is None:
            result = 'proposal_failed'
        self._event(value, 'observe', observations.source_file_id, observations.source_content_hash,
                    record, after, result)
        return after, True

    @staticmethod
    def view(record, reason=None):
        confirmed = record['confirmation'] if record else None
        return {'status': record['status'] if record else 'grouping_required',
                'proposal': record['proposal'] if record else None,
                'confirmation': confirmed,
                'reason': reason or ('grouping_proposal_failed' if record and record['proposal'] is None else None),
                **AUTHORITY_FLAGS, 'authority_scope': list(AUTHORITY_FLAGS['authority_scope']) if confirmed else []}

    def display(self, source_id):
        try:
            value = self.store.load()
            observations, before = self._current(value, source_id)
            record, changed = self._sync(value, observations, before)
            if changed:
                self.store.save(value)
            return self.view(record)
        except StateError as error:
            return self.view(None, str(error))
        except Exception:
            return self.view(None, 'grouping_observation_unavailable')

    def review(self, request, *, intent_digest=None):
        """A request is user intent. Re-read source and Drive before authorizing."""
        fields = {'request_id', 'source_file_id', 'source_content_hash', 'proposal_digest',
                  'grouping_revision', 'operation', 'partition'}
        if (set(request) != fields or not re.fullmatch(UUID, request['request_id']) or
                request['operation'] not in {'confirm', 'edit', 'reject', 'hold'}):
            raise StateError('grouping_request_invalid')
        value = self.store.load()
        observations, before = self._current(value, request['source_file_id'])
        record, changed = self._sync(value, observations, before)
        proposal = record['proposal']
        request_digest = intent_digest or _digest(request)
        matching = (not changed and proposal and proposal['source_file_id'] == request['source_file_id'] and
                    proposal['source_content_hash'] == request['source_content_hash'] and
                    proposal['proposal_digest'] == request['proposal_digest'] and
                    type(request['grouping_revision']) is int and record['revision'] == request['grouping_revision'])
        if not matching:
            self._event(value, request['operation'], observations.source_file_id, observations.source_content_hash,
                        before, record, 'stale_proposal', request_id=request['request_id'], request_digest=request_digest)
            self.store.save(value)
            return {'result': 'stale_proposal', **self.view(record)}
        previous = next((e for e in value['audit'] if e['request_id'] == request['request_id']), None)
        if previous:
            if previous['request_digest'] != request_digest:
                raise StateError('grouping_request_replaced')
            current_confirmation = record['confirmation']
            if (record['revision'] == previous['after_revision'] and
                    proposal['proposal_digest'] == previous['proposal_digest'] and
                    (current_confirmation['confirmation_digest'] if current_confirmation else '') == previous['confirmation_digest']):
                return {'result': previous['result'], **self.view(record)}
            # A hold/rejection can revoke an earlier confirmation without changing
            # the partition. Its old request must not reactivate that authority.
            self._event(value, request['operation'], observations.source_file_id, observations.source_content_hash,
                        record, record, 'stale_proposal', request_id=request['request_id'], request_digest=request_digest)
            self.store.save(value)
            return {'result': 'stale_proposal', **self.view(record)}
        after = deepcopy(record)
        operation = request['operation']
        if operation == 'confirm':
            if proposal['status'] != 'proposed' or request['partition'] is not None:
                raise StateError('grouping_request_invalid')
            # New confirmation requests for the same proposal also reuse timestamp,
            # digest and stable Unit IDs. No revision is allocated by confirmation.
            if after['confirmation'] is None:
                after['confirmation'] = confirmation(proposal, self.clock())
            after['status'], result = 'grouping_confirmed', 'confirmed'
        elif operation == 'edit':
            partition = request['partition']
            candidates = [{'page_numbers': n, 'reason': 'human_partition', 'confidence': 1.0} for n in partition or []]
            edited = _proposal(_snapshot(observations), candidates, record['revision'] + 1)
            if partition == [g['page_numbers'] for g in proposal['groups']]:
                result = 'unchanged_partition'
            else:
                after = {'revision': record['revision'] + 1, 'proposal': edited, 'confirmation': None,
                         'status': 'grouping_required'}
                result = 'proposal_updated'
        else:
            if request['partition'] is not None:
                raise StateError('grouping_request_invalid')
            after['confirmation'] = None
            after['status'] = result = 'rejected' if operation == 'reject' else 'held'
            if operation == 'reject':
                candidates = [{'page_numbers': g['page_numbers'], 'reason': g['reason'], 'confidence': g['confidence']}
                              for g in proposal['groups']]
                after['proposal'] = _proposal(_snapshot(observations), candidates, record['revision'], 'rejected')
        value['records'][_digest(observations.source_file_id)] = after
        self._event(value, operation, observations.source_file_id, observations.source_content_hash,
                    record, after, result, request_id=request['request_id'], request_digest=request_digest)
        self.store.save(value)
        return {'result': result, **self.view(after)}

    def replayed(self, request_id, source_id, intent_digest):
        """Recover a saved operation after the Spreadsheet projection failed."""
        value = self.store.load()
        previous = next((e for e in value['audit'] if e['request_id'] == request_id), None)
        if previous is None:
            return None
        if previous['request_digest'] != intent_digest or previous['source_file_id'] != source_id:
            raise StateError('grouping_request_replaced')
        observations, record = self._current(value, source_id)
        proposal = record['proposal'] if record else None
        authority = record['confirmation'] if record else None
        if (proposal and _valid_proposal(proposal, _snapshot(observations)) and
                observations.source_content_hash == previous['source_content_hash'] and
                record['revision'] == previous['after_revision'] and
                proposal['proposal_digest'] == previous['proposal_digest'] and
                (authority['confirmation_digest'] if authority else '') == previous['confirmation_digest']):
            return {'result': previous['result'], **self.view(record)}
        return None

    def note_stale(self, source_id, request_id, operation, intent_digest):
        value = self.store.load()
        observations, before = self._current(value, source_id)
        record, _ = self._sync(value, observations, before)
        self._event(value, operation, source_id, observations.source_content_hash,
                    before, record, 'stale_proposal', request_id=request_id, request_digest=intent_digest)
        self.store.save(value)
        return {'result': 'stale_proposal', **self.view(record)}

    def confirmed_units(self, source_id):
        """Read-only interface; never initializes, regenerates or repairs state."""
        try:
            value = self.store.load()
            observations, record = self._current(value, source_id)
            if (not record or not record['confirmation'] or
                    not _valid_proposal(record['proposal'], _snapshot(observations))):
                return ()
            proposal = record['proposal']
            return tuple(ConfirmedDocumentUnit(proposal['source_file_id'], proposal['source_content_hash'],
                tuple(g['page_numbers']), tuple(g['member_page_hashes']), tuple(g['page_classifications']),
                record['revision'], proposal['proposal_digest']) for g in proposal['groups'])
        except Exception:
            return ()
