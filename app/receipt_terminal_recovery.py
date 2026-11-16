"""Offline, injected-port terminal-only recovery. NO live factory or CLI.

The host must supply authenticated GET/control ports and a protected durable
CAS authority store. This module cannot issue grants, create that store, load
credentials, dispatch, enable normal work or construct a financial writer.
An empty cache is never evidence that an append was not attempted.
"""
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import logging
import re
from secrets import token_hex
from threading import Lock
from time import time

from .drive_run_state import StateError
from .page_receipt_model import digest
from .receipt_private_adapter import ReceiptPrivateAdapter, FIELDS, _bytes
from .receipt_sheet_connection import CACHE_RANGE
from .receipt_sheet_validation import (
    FINAL, PURPOSE, ValidationJournal, _check_duplicates, cached_result,
    checked_registration_result, request_uuid, result_cache_entry, strong_etag,
)

LOG = logging.getLogger(__name__)
AUTH_SCHEMA = 'receipt-terminal-recovery-authority-v1'
SCOPE_KEYS = {
    'binding', 'request_id', 'snapshot', 'generation', 'from_state', 'to_state',
    'etag', 'version', 'metadata_digest', 'body_sha256', 'terminal_sha256',
    'result', 'cache_key', 'execution_sha', 'original_run',
}


def _require(condition):
    if not condition:
        raise StateError('receipt_recovery_precondition_failed')


def _sha(raw):
    return sha256(raw).hexdigest()


def _version(value):
    _require(type(value) is str and 0 < len(value) <= 20 and value.isascii() and value.isdecimal())
    return int(value)


def _incident(scope):
    # One grant per incident, even when a caller tries a different approval ID.
    return digest([scope['binding'], scope['request_id'], scope['snapshot']['digest'], scope['generation']])


@dataclass(frozen=True, repr=False)
class RecoveryProposal:
    scope: dict
    checkpoint: dict
    terminal_journal: dict
    terminal_raw: bytes
    entry: dict


@dataclass(frozen=True)
class RecoveryOutcome:
    code: str
    terminal_verified: bool = False
    cache_verified: bool = False


class _Use:
    def __init__(self, owner, approval_id, scope, token):
        self.owner, self.approval_id, self.scope, self.token = owner, approval_id, deepcopy(scope), token
        self.append_reserved = False
        self.lock = Lock()


class DurableRecoveryAuthority:
    """Consume pre-issued grants and append intent through an atomic CAS port.

    transport.read_versioned()/replace_versioned(value, tag) MUST enforce a
    pinned store, ACLs and server If-Match with no retries. preflight MUST
    authenticate that binding on each operation. There is no issue/reset API.
    The complete publisher history is an issuer-certified prerequisite, not
    synthesized from cache reads. No live authority transport is supplied.
    """
    def __init__(self, transport, *, preflight, clock=time):
        self.transport, self.preflight, self.clock = transport, preflight, clock
        self._stopped = False

    def _load(self):
        self.preflight()
        value, tag = self.transport.read_versioned()
        _require(strong_etag(tag) and type(value) is dict
                 and set(value) == {'schema', 'generation', 'grants'}
                 and value['schema'] == AUTH_SCHEMA and type(value['generation']) is int
                 and value['generation'] >= 0 and type(value['grants']) is dict
                 and len(value['grants']) <= 1000)
        incidents = set()
        for approval_id, grant in value['grants'].items():
            _require(type(approval_id) is str and 0 < len(approval_id) <= 100)
            _require(type(grant) is dict and set(grant) == {'scope', 'expires', 'consumed', 'token', 'append_history'})
            scope, history = grant['scope'], grant['append_history']
            _require(type(scope) is dict and set(scope) == SCOPE_KEYS)
            request_uuid(scope['request_id'])
            _require(scope['from_state'] == '検証中' and scope['to_state'] in FINAL
                     and type(scope['generation']) is int and scope['generation'] > 0
                     and strong_etag(scope['etag']) and type(grant['expires']) is int
                     and type(grant['consumed']) is bool)
            _version(scope['version'])
            _require(type(history) is dict and set(history) == {'key', 'complete', 'attempts'}
                     and history['key'] == scope['cache_key'] and history['complete'] is True
                     and type(history['attempts']) is int and history['attempts'] in (0, 1))
            _require((not grant['consumed'] and grant['token'] is None and history['attempts'] == 0)
                     or grant['consumed'] and type(grant['token']) is str
                     and bool(re.fullmatch('[a-f0-9]{32}', grant['token'])))
            incident = _incident(scope)
            _require(incident not in incidents)
            incidents.add(incident)
        return deepcopy(value), tag

    def _save(self, value, tag):
        _require(not self._stopped)
        self.preflight()
        self._stopped = True
        self.transport.replace_versioned(deepcopy(value), tag)  # Once; never retry on ambiguity.
        actual, new_tag = self._load()
        _require(actual == value and new_tag != tag)
        self._stopped = False

    def peek(self, approval_id):
        try:
            _require(not self._stopped)
            value, _ = self._load()
            grant = value['grants'][approval_id]
            _require(not grant['consumed'] and grant['expires'] > self.clock()
                     and grant['append_history']['attempts'] == 0)
            return deepcopy(grant['scope'])
        except Exception:
            raise StateError('receipt_recovery_authority_unavailable') from None

    def consume(self, approval_id, scope):
        try:
            _require(not self._stopped)
            value, tag = self._load()
            grant = value['grants'][approval_id]
            _require(not grant['consumed'] and grant['scope'] == scope
                     and grant['expires'] > self.clock() and grant['append_history']['attempts'] == 0)
            token = token_hex(16)
            grant.update(consumed=True, token=token)
            value['generation'] += 1
            self._save(value, tag)
            return _Use(self, approval_id, scope, token)
        except Exception:
            # Consumed-but-unacknowledged stays consumed; no new capability.
            raise StateError('receipt_recovery_authority_consume_unknown') from None

    def check_use(self, use):
        _require(not self._stopped and type(use) is _Use and use.owner is self)
        value, _ = self._load()
        grant = value['grants'][use.approval_id]
        _require(grant['consumed'] and grant['token'] == use.token and grant['scope'] == use.scope
                 and grant['expires'] > self.clock() and grant['append_history']['attempts'] == 0)

    def reserve_append(self, use):
        try:
            _require(not self._stopped)
            _require(type(use) is _Use and use.owner is self)
            with use.lock:
                _require(not use.append_reserved)
                use.append_reserved = True  # Even failure cannot reuse this capability.
            value, tag = self._load()
            grant = value['grants'][use.approval_id]
            _require(grant['consumed'] and grant['token'] == use.token and grant['scope'] == use.scope
                     and grant['expires'] > self.clock() and grant['append_history']['attempts'] == 0)
            grant['append_history']['attempts'] = 1
            value['generation'] += 1
            self._save(value, tag)  # Persist intent BEFORE sending an append.
        except Exception:
            raise StateError('receipt_recovery_append_reservation_unknown') from None

    def check_append(self, use):
        _require(not self._stopped and type(use) is _Use and use.owner is self and use.append_reserved)
        value, _ = self._load()
        grant = value['grants'][use.approval_id]
        _require(grant['consumed'] and grant['token'] == use.token and grant['scope'] == use.scope
                 and grant['expires'] > self.clock() and grant['append_history']['attempts'] == 1)


class _TerminalOnlyIO:
    """One terminal payload only, after durable authority consumption.

    Wrap existing receipt IO; all original CAS/readback/ACK checks still run.
    An unrelated payload, fresh ETag, changed metadata or second CAS is denied.
    """
    def __init__(self, base, proposal):
        self.base, self.proposal = base, proposal
        self.config = deepcopy(base.config)
        self.used, self.lock = False, Lock()

    def metadata(self, fid, fields):
        value = self.base.metadata(fid, fields)
        if not self.used and fields == FIELDS:
            actual = deepcopy(value)
            actual['permissions'] = sorted(actual['permissions'], key=_bytes)
            expected = next(m for m in self.proposal.checkpoint['metadata'] if m['id'] == fid)
            _require(actual == expected)
        return value

    def read_versioned(self):
        return self.base.read_versioned()

    def replace_versioned(self, before, tag, after):
        with self.lock:
            _require(not self.used and before == self.proposal.checkpoint['raw']
                     and tag == self.proposal.scope['etag'] and after == self.proposal.terminal_raw)
            self.used = True
        self.base.replace_versioned(before, tag, after)  # Exactly one existing terminal CAS call.


class TerminalRecovery:
    """Explicit offline service; not reachable from normal run/result or Actions.

    connection uses the existing pinned reader/projection. read_controls MUST
    independently GET authenticated run/config and complete publisher/exclusion
    evidence. There is deliberately no permissive default or live implementation.
    """
    def __init__(self, connection, authority, *, read_controls):
        self.connection, self.authority, self.read_controls = connection, authority, read_controls

    def _controls(self, request_id):
        controls = deepcopy(self.read_controls())
        _require(set(controls) == {'original_run', 'execution_sha', 'validation_enabled',
                 'competing_runs', 'competing_recoveries', 'writers_quiescent', 'cache_read_complete'})
        run = controls['original_run']
        _require(set(run) == {'id', 'request_id', 'sha', 'status', 'attempt', 'stop_stage'})
        _require(type(run['id']) is str and run['id'].isdigit() and run['request_id'] == request_id
                 and run['status'] == 'completed' and type(run['attempt']) is int and run['attempt'] == 1
                 and run['stop_stage'] == 'claim_readback_failed'
                 and bool(re.fullmatch('[a-f0-9]{40}', run['sha']))
                 and bool(re.fullmatch('[a-f0-9]{40}', controls['execution_sha']))
                 and controls['validation_enabled'] is False
                 and type(controls['competing_runs']) is int and controls['competing_runs'] == 0
                 and type(controls['competing_recoveries']) is int and controls['competing_recoveries'] == 0
                 and controls['writers_quiescent'] is True and controls['cache_read_complete'] is True)
        return controls

    def _binding(self):
        private, projection = self.connection.private, self.connection.projection
        return dict(private_id=private.fid, folder_id=private.folder, owner=private.owner,
                    service=private.service, catalog_digest=private.catalog_digest,
                    spreadsheet_id=projection.spreadsheet_id, cache_range=CACHE_RANGE)

    def _read(self):
        first = self.connection.private.read_checkpoint()
        entries = deepcopy(self.connection.projection.read_results())
        last = self.connection.private.read_checkpoint()
        _require(first == last)
        return first, entries

    def _proposal(self, request_id, generation, controls):
        request_uuid(request_id)
        checkpoint, entries = self._read()
        journal = checkpoint['envelope']['journal']
        record = journal['requests'][request_id]
        _require(type(generation) is int and generation > 0 and journal['generation'] == generation
                 and record['purpose'] == PURPOSE and record['state'] == '検証中' and record['result'] is None
                 and record['snapshot']['case_id'] not in journal['active_prepared'])
        others = deepcopy(journal)
        del others['requests'][request_id]
        _check_duplicates(others, record['snapshot'])
        _require(not any(e.get('request_id') == request_id for e in entries))
        LOG.warning('receipt_recovery_source_check_begin')
        def begin_phase(phase):
            codes = {'receipt_worker_registration_plan':'receipt_recovery_plan_begin',
                     'receipt_worker_source_recheck':'receipt_recovery_source_recheck_begin'}
            LOG.warning(codes[phase])
        result = checked_registration_result(record['snapshot'], reader=self.connection.reader, begin_phase=begin_phase)
        terminal = deepcopy(journal)
        terminal['requests'][request_id].update(state=result['state'], result=result)
        terminal['generation'] += 1
        entry = result_cache_entry(terminal['requests'][request_id])  # Validates result; no financial authority.
        _require(not any(e.get('key') == entry['key'] for e in entries))
        terminal_raw = _bytes(dict(checkpoint['envelope'], journal=terminal))
        _require(self._read() == (checkpoint, entries) and self._controls(request_id) == controls)
        version = checkpoint['metadata'][1]['version']
        _version(version)
        scope = dict(binding=self._binding(), request_id=request_id, snapshot=deepcopy(record['snapshot']),
                     generation=generation, from_state='検証中', to_state=result['state'],
                     etag=checkpoint['etag'], version=version, metadata_digest=digest(checkpoint['metadata']),
                     body_sha256=_sha(checkpoint['raw']), terminal_sha256=_sha(terminal_raw),
                     result=result, cache_key=entry['key'], execution_sha=controls['execution_sha'],
                     original_run=controls['original_run'])
        return RecoveryProposal(scope, checkpoint, terminal, terminal_raw, entry)

    def inspect(self, request_id, *, generation):
        """GET-only eligibility/proposal; does NOT issue or consume authority."""
        try:
            controls = self._controls(request_id)
            return self._proposal(request_id, generation, controls)
        except Exception:
            raise StateError('receipt_recovery_inspection_failed') from None

    def recover(self, approval_id):
        """Each ambiguity stops. A replay cannot even repeat the terminal CAS."""
        verified = False
        try:
            scope = self.authority.peek(approval_id)
            controls = self._controls(scope['request_id'])
            proposal = self._proposal(scope['request_id'], scope['generation'], controls)
            _require(proposal.scope == scope)
            use = self.authority.consume(approval_id, scope)
            # Authorization IO is not a freshness proof for the original/input.
            # Reuse the same checks again immediately before the terminal path.
            LOG.warning('receipt_recovery_source_check_begin')
            checked = checked_registration_result(scope['snapshot'], reader=self.connection.reader,
                                                   begin_phase=lambda phase: None)
            _require(checked == scope['result'])
            # Re-read after authority consumption, before acquiring a write port.
            checkpoint, entries = self._read()
            _require(checkpoint == proposal.checkpoint and self._controls(scope['request_id']) == controls
                     and not any(e.get('key') == scope['cache_key'] or e.get('request_id') == scope['request_id'] for e in entries))
            self.authority.check_use(use)
            old = self.connection.private
            port = _TerminalOnlyIO(old.io, proposal)
            private = ReceiptPrivateAdapter(port, file_id=old.fid, folder_id=old.folder,
                owner_email=old.owner, service_email=old.service, catalog_digest=old.catalog_digest)
            journal = ValidationJournal(private, preflight=lambda: None)
            journal.transition(scope['request_id'], scope['to_state'], scope['result'])
            # transition/save retains the existing adapter readback + core ACK.
            final = private.read_checkpoint()
            _require(final['raw'] == proposal.terminal_raw
                     and final['envelope']['journal'] == proposal.terminal_journal
                     and final['etag'] != scope['etag']
                     and _version(final['metadata'][1]['version']) > _version(scope['version']))
            verified = True
            _require(self._controls(scope['request_id']) == controls)
            entries = self.connection.projection.read_results()
            _require(not any(e.get('key') == scope['cache_key'] or e.get('request_id') == scope['request_id'] for e in entries))
            self.authority.reserve_append(use)  # Complete history + attempts=0; not cache absence alone.
            _require(self._controls(scope['request_id']) == controls and private.read_checkpoint() == final)
            entries = self.connection.projection.read_results()
            _require(not any(e.get('key') == scope['cache_key'] or e.get('request_id') == scope['request_id'] for e in entries))
            self.authority.check_append(use)
            # A consumed authority/append intent is never restored after a crash.
            self.connection.projection.append_result(deepcopy(proposal.entry))
            entries = self.connection.projection.read_results()
            related = [e for e in entries if e.get('key') == scope['cache_key'] or e.get('request_id') == scope['request_id']]
            _require(related == [proposal.entry])
            _require(private.read_checkpoint() == final and self._controls(scope['request_id']) == controls)
            cached_result(entries, proposal.terminal_journal['requests'][scope['request_id']])
            LOG.warning('receipt_recovery_complete')
            return RecoveryOutcome('receipt_recovery_complete', True, True)
        except Exception:
            LOG.warning('receipt_recovery_stopped')
            return RecoveryOutcome('receipt_recovery_stopped', verified, False)
