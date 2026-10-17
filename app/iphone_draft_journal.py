"""Single-host private JSON draft journal. No Drive/authority/accounting IO.

CAS and idempotency follow the existing versioned-IO/strong-etag convention.
An OS-owned advisory lock (never a timestamp lease) serializes local processes.
This is not a distributed journal or a transaction with Google Sheets.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
import uuid

from .drive_run_state import StateError
from .pdf_intake_registry import strong_etag
from .unified_receipt_review import digest
from .iphone_sheet_reader import CASE_IDS, CASE_KINDS, HELD, raw_value, source_identity

SCHEMA = 'iphone-private-draft-journal-v1'
MAX_BYTES = 2_000_000
MAX_REQUESTS = 256


def fail(reason):
    raise StateError('iphone_journal_' + reason)


def encoded(value):
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    if len(raw) > MAX_BYTES:
        fail('capacity_exceeded')
    return raw


@contextmanager
def exclusive(path):
    # Lock file persists; OS lock disappears on process exit. Never steal a
    # lock based on elapsed time or remove a possibly still-open lock file.
    with open(path, 'a+b') as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fail('busy')
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class LocalJournal:
    """Caller provisions a private local directory; no implicit initialization.

    No network filesystem/multiple-host support. An unknown save outcome must
    be resolved by recover(), never by constructing a new request ID.
    """
    def __init__(self, path, contract_digest):
        self.path = Path(path)
        self.binding = contract_digest
        self.lock_path = self.path.with_suffix(self.path.suffix + '.lock')

    def _validate(self, value):
        try:
            if (set(value) != {'schema', 'contract_digest', 'revision', 'batch', 'requests'}
                    or value['schema'] != SCHEMA or value['contract_digest'] != self.binding
                    or type(value['revision']) is not int or value['revision'] < 0
                    or not isinstance(value['requests'], dict) or len(value['requests']) > MAX_REQUESTS
                    or value['revision'] != len(value['requests'])):
                fail('corrupt_or_foreign')
            if (value['revision'] == 0) != (value['batch'] is None):
                fail('corrupt_or_foreign')
            if value['batch'] is not None:
                check_batch(value['batch'], self.binding)
            for rid, receipt in value['requests'].items():
                if (str(uuid.UUID(rid)) != rid or set(receipt) != {'fingerprint', 'revision', 'batch_digest'}
                        or type(receipt['revision']) is not int or not 1 <= receipt['revision'] <= value['revision']
                        or any(not isinstance(receipt[k], str) or len(receipt[k]) != 64
                               for k in ('fingerprint', 'batch_digest'))):
                    fail('corrupt_or_foreign')
            if {r['revision'] for r in value['requests'].values()} != set(range(1, value['revision']+1)):
                fail('corrupt_or_foreign')
            if value['batch'] is not None:
                latest = next(r for r in value['requests'].values() if r['revision'] == value['revision'])
                if latest['batch_digest'] != digest(value['batch']):
                    fail('corrupt_or_foreign')
            encoded(value)
        except (KeyError, TypeError, ValueError):
            fail('corrupt_or_foreign')
        return value

    def _read(self):
        try:
            with self.path.open('rb') as handle:
                raw = handle.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                fail('capacity_exceeded')
            return self._validate(json.loads(raw))
        except (OSError, ValueError):
            fail('missing_or_unreadable')

    def _replace(self, value):
        raw = encoded(self._validate(value))
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix='.draft-', delete=False) as handle:
                temp_path = handle.name
                handle.write(raw); handle.flush(); os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            temp_path = None
            if os.name != 'nt':
                fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except OSError:
            # Replace could have committed. Caller gets no success receipt.
            fail('save_outcome_unknown')
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def initialize(self):
        with exclusive(self.lock_path):
            if self.path.exists():
                fail('already_exists')
            self._replace(dict(schema=SCHEMA, contract_digest=self.binding, revision=0, batch=None, requests={}))
        return self.read_versioned()

    def read_versioned(self):
        value = self._read()
        return deepcopy(value), '"' + digest(value) + '"'

    def save(self, batch, expected_tag, request_id):
        value = asdict(batch)
        check_batch(value, self.binding)
        if not strong_etag(expected_tag):
            fail('strong_cas_required')
        try:
            if str(uuid.UUID(request_id)) != request_id:
                fail('request_id_invalid')
        except (ValueError, TypeError, AttributeError):
            fail('request_id_invalid')
        fingerprint = digest(dict(batch=value, expected_tag=expected_tag))
        with exclusive(self.lock_path):
            before, tag = self.read_versioned()
            old = before['requests'].get(request_id)
            if old:
                if old['fingerprint'] != fingerprint:
                    fail('replay_payload_changed')
                return dict(status='draft_saved', replay=True, is_current=before['batch'] == value,
                            receipt=deepcopy(old), current_tag=tag,
                            authorized=False, operation_targets=[])
            if tag != expected_tag:
                fail('cas_conflict')
            if len(before['requests']) >= MAX_REQUESTS:
                fail('capacity_exceeded')
            after = deepcopy(before)
            after['revision'] += 1
            after['batch'] = value
            receipt = dict(fingerprint=fingerprint, revision=after['revision'], batch_digest=digest(value))
            after['requests'][request_id] = receipt
            self._replace(after)
            # Explicit read-back, no retry/repair on mismatch or lost response.
            actual, actual_tag = self.read_versioned()
            if actual != after:
                fail('save_outcome_unknown')
            return dict(status='draft_saved', replay=False, is_current=True, receipt=receipt, current_tag=actual_tag,
                        authorized=False, operation_targets=[])

    def recover(self, request_id, batch, expected_tag):
        """Read-only exact request lookup. Absence/ambiguity does not resubmit."""
        value = asdict(batch)
        check_batch(value, self.binding)
        state, tag = self.read_versioned()
        receipt = state['requests'].get(request_id)
        if not receipt or receipt['fingerprint'] != digest(dict(batch=value, expected_tag=expected_tag)):
            fail('recovery_unresolved')
        return dict(status='draft_save_observed', receipt=deepcopy(receipt), current_tag=tag,
                    is_current=state['batch'] == value, authorized=False, operation_targets=[])


def check_batch(value, binding):
    try:
        if (set(value) != {'contract_digest', 'snapshot_digest', 'drafts'} or value['contract_digest'] != binding
                or not isinstance(value['snapshot_digest'], str) or len(value['snapshot_digest']) != 64
                or not isinstance(value['drafts'], dict) or len(value['drafts']) != 8):
            fail('batch_invalid')
        for key, draft in value['drafts'].items():
            if (draft['authorized'] is not False or draft['operation_targets'] != []
                    or len(draft['cells']) != 17
                    or raw_value(draft['cells'][0]) != draft['case_id']
                    or raw_value(draft['cells'][1]) != draft['kind']
                    or CASE_KINDS.get(draft['case_id']) != draft['kind']
                    or draft['case_id'] in HELD and draft['held'] is not True
                    or digest(dict(case_id=draft['case_id'], kind=draft['kind'], identity=draft['identity'])) != key
                    or digest(draft['cells']) != draft['input_snapshot_digest']):
                fail('batch_invalid')
            identity = draft['identity']
            if draft['case_id'] == 'SYN-H301':
                if identity is not None or raw_value(draft['cells'][15]) or raw_value(draft['cells'][16]):
                    fail('batch_invalid')
            elif (source_identity(identity) != identity
                  or (raw_value(draft['cells'][15]), raw_value(draft['cells'][16])) != (identity['sha256'], identity['source_id'])):
                fail('batch_invalid')
        if {d['case_id'] for d in value['drafts'].values()} != CASE_IDS:
            fail('batch_invalid')
        encoded(value)
    except (KeyError, TypeError, ValueError):
        fail('batch_invalid')


def save_current(reader, journal, expected_snapshot_digest, expected_tag, request_id):
    """Explicit local draft save only, after a fresh GET-only capture.

    The caller's expected Sheet snapshot detects stale client drafts; journal
    CAS detects another local save. Neither is an accounting/POST permission,
    nor does this promise an atomic transaction with concurrent Sheet edits.
    """
    batch = reader.read()
    if batch.snapshot_digest != expected_snapshot_digest:
        fail('stale_sheet_snapshot')
    return journal.save(batch, expected_tag, request_id)


def quarantine(directory, snapshot, reason):
    """Preserve untrusted raw cell metadata privately, outside journal adoption.

    Only pass a Sheets raw snapshot. Never pass source bytes/URLs/credentials.
    The caller must keep this directory out of Git, logs and CI artifacts.
    """
    path = Path(directory) / ('quarantine-' + str(uuid.uuid4()) + '.json')
    raw = encoded(dict(reason=reason, snapshot=snapshot, adopted=False))
    with path.open('xb') as handle:
        handle.write(raw); handle.flush(); os.fsync(handle.fileno())
    return path
