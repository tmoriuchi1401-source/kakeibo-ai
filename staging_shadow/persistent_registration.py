"""Injected persistent custody for #160. No credentials, provisioning or CLI.

The existing ConditionalControl/FirestoreConditionalState is the only CAS.
Precreated Drive slots are write-once by protocol under its non-expiring run
claim; Drive read/compare/write is NEVER used as a distributed CAS.
"""
from base64 import b64decode, b64encode
from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import signal
import subprocess

from app.drive_run_state import DriveStateTransport
from app.iphone_persistent_drafts import EncryptedDraftIO, PersistentJournal, unique
from .registration_runner import (ConditionalControl, MAX_STORAGE,
                                  MAX_VERSIONS, atomic, fail, fingerprint)

SCHEMA = 'synthetic-runner-checkpoint-v1'
MAX_CHECKPOINT = 1_600_000
MAX_CONFIG = 500_000
CHUNK = 240_000
MAX_BUNDLE = 3_000_000
MAX_CIPHER = 5_500_000


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def parsed(raw):
    return json.loads(raw, object_pairs_hook=unique)


def journals(checkpoint):
    """Validate the existing encrypted journal schema/history, without writes."""
    cp = parsed(checkpoint); values = []
    if cp.get('schema') != 'live-synthetic-probe-checkpoint-v1' or cp.get('archives') != []:
        fail('checkpoint_schema_or_archive_unsupported')
    for name, key, scope_name in (('journal','key','native_scope'), ('outer','outer_key','outer_scope')):
        scope = cp[scope_name]; raw = b64decode(cp[name]['raw'], validate=True)
        class ReadOnly:
            def read_versioned(self):return raw, '"checkpoint-'+fingerprint(raw)+'"'
        journal = PersistentJournal(ReadOnly(), cp[key].encode(), owner=scope['owner'],
            contract_digest=scope['contract'], generation=scope['generation'], initial_snapshot=scope['initial_snapshot'])
        if journal.scope != scope:fail('checkpoint_journal_scope')
        values.append(journal._state()[0])
    return cp, values


def has_pending(checkpoint):
    cp, values = journals(checkpoint)
    if cp['uncertain'] or cp['projection_unknown']:return True
    for value in values:
        if any(r['status'] in {'ATTEMPTING','UNKNOWN'}
               for r in value.get('confirmation',{}).get('requests',{}).values()):return True
        for slot in ('sheets_registration','sheets_only_review','iphone_sheet_handoff'):
            if any(r['status'] in {'ATTEMPTING','UNKNOWN'}
                   for r in value['details'].get(slot,{}).get('requests',{}).values()):return True
        if any(r['state'] in {'ATTEMPTING','SENDING','UNKNOWN'}
               for r in value['details'].get('live_synthetic_sheet_outbox',{}).values()):return True
    return False


@dataclass(frozen=True)
class Slot:
    file_id: str
    folder_id: str


class DriveCheckpointSlots:
    """Existing Drive transport plus fixed allowlist/private-owner preflight.

    Caller supplies an authenticated service with bounded HTTP timeouts.
    All files and their folder must ALREADY exist. No list-by-name/create,
    sharing, deletion, pruning or write retry is available here.
    """
    def __init__(self, service, slots, *, owner_permission_id):
        self.service, self.slots = service, tuple(slots)
        self.owner = owner_permission_id
        if (not isinstance(self.owner, str) or not self.owner
                or not 2 <= len(self.slots) <= MAX_VERSIONS
                or len({s.file_id for s in self.slots}) != len(self.slots)
                or len({s.folder_id for s in self.slots}) != 1
                or any(not re.fullmatch(r'[\w-]{1,200}', x)
                       for s in self.slots for x in (s.file_id, s.folder_id))
                or self.slots[0].folder_id in {s.file_id for s in self.slots}):
            fail('slot_allowlist_invalid')

    def _private(self, identity, *, folder=False):
        try:
            meta = self.service.files().get(fileId=identity, supportsAllDrives=True,
                fields='id,trashed,mimeType,driveId,shared,size,owners(permissionId)').execute(num_retries=0)
            permissions = self.service.permissions().list(fileId=identity, supportsAllDrives=True,
                fields='nextPageToken,permissions(id,type,role,deleted)').execute(num_retries=0)
            acl = permissions.get('permissions', [])
            if (meta.get('id') != identity or meta.get('trashed') is not False
                    or meta.get('shared') is not False or meta.get('driveId')
                    or meta.get('owners') != [dict(permissionId=self.owner)]
                    or folder and meta.get('mimeType') != 'application/vnd.google-apps.folder'
                    or not folder and not 0 <= int(meta['size']) <= MAX_CIPHER
                    or permissions.get('nextPageToken')
                    or len(acl) != 1 or acl[0].get('deleted', False) is not False
                    or {k:v for k,v in acl[0].items() if k != 'deleted'} != dict(id=self.owner, type='user', role='owner')):
                fail('checkpoint_private_owner_required')
        except Exception:
            fail('checkpoint_private_preflight_failed')

    def _transport(self, generation):
        if type(generation) is not int or not 0 <= generation < len(self.slots):
            fail('checkpoint_slot_capacity')
        slot = self.slots[generation]
        self._private(slot.folder_id, folder=True)
        self._private(slot.file_id)
        return DriveStateTransport(self.service, slot)

    def read(self, generation):
        raw = self._transport(generation).read()
        if type(raw) is not bytes or len(raw) > MAX_CIPHER:
            fail('checkpoint_storage_capacity')
        return raw

    def write(self, generation, raw):
        if type(raw) is not bytes or len(raw) > MAX_CIPHER:
            fail('checkpoint_storage_capacity')
        self._transport(generation).write(raw)


class Checkpoints:
    """Chunk existing encrypted draft IO; no Firestore-sized bundle limit.

    Each chunk binds the trusted scope, generation, config and bundle hash.
    The CAS pointer also fixes the complete ciphertext hash and byte count.
    """
    def __init__(self, slots, key, scope):
        self.slots, self.key, self.scope = slots, key, deepcopy(scope)

    def _cipher(self, generation, digest):
        return EncryptedDraftIO(None, self.key,
            dict(self.scope, storage_schema=SCHEMA, checkpoint_generation=generation, bundle_sha256=digest))

    def payload(self, directory):
        root = Path(directory)
        config = (root/'config.json').read_bytes()
        checkpoint = (root/'checkpoint.json').read_bytes()
        if (not 0 < len(config) <= MAX_CONFIG or not 0 < len(checkpoint) <= MAX_CHECKPOINT
                or fingerprint(config) != self.scope['config_sha256']):
            fail('checkpoint_capacity_or_config_changed')
        journals(checkpoint)
        return encoded(dict(config=b64encode(config).decode(), checkpoint=b64encode(checkpoint).decode()))

    def save(self, directory, generation, *, total_bytes=0):
        plain = self.payload(directory)
        if len(plain) > MAX_BUNDLE:fail('checkpoint_bundle_capacity')
        digest = fingerprint(plain); cipher = self._cipher(generation, digest)
        pieces = [plain[i:i+CHUNK] for i in range(0, len(plain), CHUNK)]
        raw = encoded(dict(schema=SCHEMA, chunks=[cipher.encode(dict(index=i, count=len(pieces),
            data=b64encode(p).decode())).decode() for i, p in enumerate(pieces)]))
        if len(raw) > MAX_CIPHER or total_bytes+len(raw) > MAX_STORAGE:
            fail('checkpoint_storage_capacity')
        # This unused-slot check is NOT CAS. The caller must already own the
        # shared run claim (or be doing an explicit frozen initial migration).
        if self.slots.read(generation) != b'':fail('checkpoint_slot_not_empty')
        self.slots.write(generation, raw)  # exactly once, including lost reply
        if self.slots.read(generation) != raw:fail('checkpoint_readback_unknown')
        return dict(generation=generation, bundle_sha256=digest, cipher_sha256=fingerprint(raw),
            checkpoint_sha256=fingerprint((Path(directory)/'checkpoint.json').read_bytes()),
            size=len(raw), total_bytes=total_bytes+len(raw))

    def load(self, pointer):
        fields = {'generation','bundle_sha256','cipher_sha256','checkpoint_sha256','size','total_bytes'}
        if (type(pointer) is not dict or set(pointer) != fields
                or type(pointer['generation']) is not int or not 0 <= pointer['generation'] < MAX_VERSIONS
                or any(type(pointer[k]) is not int for k in ('size','total_bytes'))
                or not 0 < pointer['size'] <= pointer['total_bytes'] <= MAX_STORAGE
                or any(not isinstance(pointer[k],str) or not re.fullmatch('[a-f0-9]{64}',pointer[k])
                       for k in ('bundle_sha256','cipher_sha256','checkpoint_sha256'))):
            fail('checkpoint_pointer_invalid')
        raw = self.slots.read(pointer['generation'])
        if len(raw) != pointer['size'] or fingerprint(raw) != pointer['cipher_sha256']:
            fail('checkpoint_missing_or_rollback')
        envelope = parsed(raw)
        if (set(envelope) != {'schema','chunks'} or envelope['schema'] != SCHEMA
                or not isinstance(envelope['chunks'],list) or not 1 <= len(envelope['chunks']) <= 13):
            fail('checkpoint_envelope_invalid')
        cipher = self._cipher(pointer['generation'], pointer['bundle_sha256']); pieces = []
        for i, raw_chunk in enumerate(envelope['chunks']):
            piece = cipher.decode(raw_chunk.encode())
            if set(piece) != {'index','count','data'} or piece['index'] != i or piece['count'] != len(envelope['chunks']):
                fail('checkpoint_chunk_invalid')
            pieces.append(b64decode(piece['data'], validate=True))
        plain = b''.join(pieces)
        if len(plain) > MAX_BUNDLE or fingerprint(plain) != pointer['bundle_sha256']:
            fail('checkpoint_bundle_mismatch')
        files = parsed(plain)
        if set(files) != {'config','checkpoint'}:fail('checkpoint_file_allowlist')
        config, checkpoint = (b64decode(files[k], validate=True) for k in ('config','checkpoint'))
        if (not 0 < len(config) <= MAX_CONFIG or not 0 < len(checkpoint) <= MAX_CHECKPOINT
                or fingerprint(config) != self.scope['config_sha256']
                or fingerprint(checkpoint) != pointer['checkpoint_sha256']):
            fail('checkpoint_content_mismatch')
        journals(checkpoint)
        return config, checkpoint


class PersistentControl:
    """Adapter of existing ConditionalControl, small pointer history only.

    read/replace expose #160's checkpoint hash contract to ShortRunner.
    save_checkpoint is called by the port while the CAS is PENDING. Any
    ambiguous persistence/CAS result latches this instance read-only.
    """
    def __init__(self, conditional_io, slots, key, scope, directory):
        self.control = ConditionalControl(conditional_io, key, scope)
        self.checkpoints = Checkpoints(slots, key, scope)
        self.root = Path(directory)
        self.uncertain = False; self.staged = None

    @staticmethod
    def _public(state):
        value = deepcopy(state); value['checkpoint'] = state['checkpoint']['checkpoint_sha256']
        return value

    def _read(self):
        envelope, _, tag = self.control._read()
        previous = None
        for state in envelope['history']+[envelope['current']]:
            pointer = state['checkpoint']
            if previous is not None and pointer != previous:
                if (pointer['generation'] != previous['generation']+1
                        or pointer['total_bytes'] != previous['total_bytes']+pointer['size']):
                    fail('checkpoint_generation_regression')
            elif previous is None and pointer['generation'] != 0:
                fail('checkpoint_generation_origin')
            previous = pointer
        return envelope['current'], tag

    def read(self):
        state, tag = self._read()
        self.checkpoints.load(state['checkpoint'])  # even UNKNOWN: never mask missing custody
        return self._public(state), tag

    def restore(self):
        """Empty ephemeral directory only; no missing-state initialization."""
        if not self.root.is_dir() or any(self.root.iterdir()):fail('empty_restore_directory_required')
        state, tag = self._read()
        config, checkpoint = self.checkpoints.load(state['checkpoint'])
        # Re-read the shared tip after blob IO. A concurrent update invalidates
        # this restore before constructing any port or observing Sheet data.
        if self._read() != (state, tag):fail('restore_tip_changed')
        atomic(self.root/'config.json', config, create=True)
        atomic(self.root/'checkpoint.json', checkpoint, create=True)
        return self._public(state), tag

    def save_checkpoint(self, budget):
        if self.uncertain or budget <= 0:fail('persistence_result_unknown')
        try:
            state, tag = self._read()
            if state['status'] != 'PENDING' or state['run_id'] != self.owner:
                fail('checkpoint_pending_owner_required')
            current = state['checkpoint']
            if fingerprint((self.root/'checkpoint.json').read_bytes()) == current['checkpoint_sha256']:
                self.checkpoints.load(current)
                return current['checkpoint_sha256']
            _, old_cp = self.checkpoints.load(current)
            old, old_journals = journals(old_cp)
            new, new_journals = journals((self.root/'checkpoint.json').read_bytes())
            for before, after in zip(old_journals, new_journals):
                if (after['history'][:len(before['history'])] != before['history']
                        or any(after['legacy']['requests'].get(k) != v
                               for k,v in before['legacy']['requests'].items())):
                    fail('checkpoint_receipt_or_history_regression')
            for name in ('key','outer_key','native_scope','outer_scope'):
                if old[name] != new[name]:fail('checkpoint_journal_domain_changed')
            pointer = self.checkpoints.save(self.root, current['generation']+1, total_bytes=current['total_bytes'])
            self.staged = (tag, pointer)
            return pointer['checkpoint_sha256']
        except Exception:
            self.uncertain = True
            raise

    def replace(self, expected, *, claim=None, **changes):
        if self.uncertain:fail('persistence_result_unknown')
        try:
            state, tag = self._read()
            if tag != expected:fail('control_cas_conflict')
            if changes.get('status') == 'RUNNING' and state['status'] in {'IDLE','HELD'}:
                self.owner = changes['run_id']
            if 'checkpoint' in changes:
                if self.staged is None:
                    if changes['checkpoint'] != state['checkpoint']['checkpoint_sha256']:
                        fail('checkpoint_durable_ack_required')
                    changes['checkpoint'] = state['checkpoint']
                else:
                    staged_tag, pointer = self.staged
                    if staged_tag != tag or changes['checkpoint'] != pointer['checkpoint_sha256']:
                        fail('checkpoint_durable_ack_required')
                    changes['checkpoint'] = pointer
            after, actual_tag = self.control.replace(expected, claim=claim, **changes)
            self.staged = None
            return self._public(after), actual_tag
        except Exception:
            # #160's generic exception handler attempts to mark UNKNOWN. It
            # must not perform a second write after an uncertain first one.
            self.uncertain = True
            raise


def run_persistent(control, port_factory, **bounds):
    """Restore/validate first, then use the unchanged bounded coordinator.

    Host must supervise the ENTIRE process with <=180s hard wall timeout;
    SDK HTTP timeouts must be bounded too. No backend factory is selected.
    """
    from .registration_runner import ShortRunner
    if bounds.get('enabled') is not True:
        return ShortRunner(control.root, port_factory).summary('DISABLED')
    control.restore()
    def factory(directory):
        port = port_factory(directory)
        # Existing port independently decodes native/delivery journals and
        # pending; ShortRunner checks its hash against the CAS before calls.
        port.persist_checkpoint = control.save_checkpoint
        original_pending = port.restored_pending
        port.restored_pending = lambda:has_pending((Path(directory)/'checkpoint.json').read_bytes()) or original_pending()
        return port
    return ShortRunner(control.root, factory, control_factory=lambda _:control).run(**bounds)


def supervise(command, *, max_seconds=120):
    """Trusted host command, entire ephemeral worker bounded to <=180 seconds.

    Timeout kills the child tree; parent never writes/clears the shared claim.
    Child stdout is a fixed ShortRunner summary, never SDK logs or custody.
    No retry or credential discovery. A live command is not supplied here.
    """
    if type(max_seconds) not in {int,float} or not 1 <= max_seconds <= 180:
        fail('bounds_invalid')
    options = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if os.name == 'nt':options['creationflags'] = subprocess.CREATE_NO_WINDOW
    else:options['start_new_session'] = True
    child = subprocess.Popen(command, **options)
    try:
        out, err = child.communicate(timeout=max_seconds)
    except subprocess.TimeoutExpired:
        if os.name == 'nt':
            subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        else:
            try:os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:pass
        if child.poll() is None:child.kill()
        child.communicate(timeout=5)
        return dict(status='UNKNOWN', synthetic_only=True, accounting_writes=0, authority_calls=0)
    if child.returncode or len(out)>4096 or err:fail('worker_stopped')
    result = parsed(out)
    from .registration_runner import ShortRunner
    expected = ShortRunner(Path('.'),None).summary(result.get('status'),result.get('results',()))
    expected['backend_calls'] = result.get('backend_calls',0)
    if result != expected:fail('worker_summary_invalid')
    return result
