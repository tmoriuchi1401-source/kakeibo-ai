"""Offline-testable permit client; deliberately no PAT/Google live transport.

Only non-secret authority metadata crosses the injected transport. A live
PAT-free, independently authenticated GitHub gate reader is still required.
"""
from copy import deepcopy
import json
from pathlib import Path
import re
import sqlite3
import time

FIELDS = {'schema', 'permit_id', 'issued_at_ms', 'expires_at_ms', 'worker_sha',
          'script_id', 'review_spreadsheet_id'}
ACTIONS = {'status', 'pending', 'lookup', 'read', 'intake', 'claim', 'finish', 'close'}
MAX_TTL_MS = 300_000


def require_daily_transport():
    # Do not expose an activation flag that could turn unverified PAT relay on.
    raise RuntimeError('daily_pat_free_gate_reader_required')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def validate_command(command):
    fields = dict(status=set(), pending=set(), close=set(), lookup={'source_id','sha256'},
        read={'request_id'}, intake={'entry','row'}, claim={'request_id','before'}, finish={'request_id','before','result'})
    if type(command) is not dict or command.get('action') not in fields or set(command) != fields[command['action']] | {'action'}:
        raise ValueError('daily_command_invalid')
    def clean(value):
        if isinstance(value, str) and ('github_pat_' in value or 'ghp_' in value):
            raise ValueError('daily_secret_forbidden')
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in {'pat','token','authorization','access_token','refresh_token'}:
                    raise ValueError('daily_secret_forbidden')
                clean(item)
        elif isinstance(value, list):
            for item in value:
                clean(item)
    clean(command)


def validate_permit(value, binding, now_ms):
    if (type(value) is not dict or set(value) != FIELDS
            or value.get('schema') != 'receipt-daily-permit-v1'
            or not re.fullmatch(r'[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}', str(value.get('permit_id', '')))
            or not re.fullmatch('[a-f0-9]{40}', str(value.get('worker_sha', '')))
            or any(type(value[k]) is not int or not 0 < value[k] < 2**53 for k in ('issued_at_ms', 'expires_at_ms'))
            or not 0 < value['expires_at_ms'] - value['issued_at_ms'] <= MAX_TTL_MS
            or any(type(value[k]) is not str or not re.fullmatch('[A-Za-z0-9_-]{10,150}', value[k])
                   for k in ('script_id', 'review_spreadsheet_id'))):
        raise ValueError('daily_gate_invalid')
    if any(value[k] != binding[k] for k in ('worker_sha', 'script_id', 'review_spreadsheet_id')):
        raise ValueError('daily_binding_mismatch')
    if type(now_ms) is not int or now_ms < value['issued_at_ms']:
        raise ValueError('daily_clock_invalid')
    if now_ms >= value['expires_at_ms']:
        raise ValueError('daily_expired')
    return deepcopy(value)


class PermitClient:
    """Injected offline transport only; no credential loader or network import.

    Both the permanent DB and RUNNING survive process death. A caller cannot
    create another client after an unknown outcome to get a fresh sequence.
    """
    def __init__(self, permit, *, binding, state_dir, attempt_db, transport,
                 wall_ms=lambda: int(time.time()*1000), monotonic=time.monotonic):
        self.state = Path(state_dir)
        self.state.mkdir(parents=True, exist_ok=True)
        self.stop, self.running = self.state/'STOP.json', self.state/'RUNNING.json'
        self.db, self.transport = Path(attempt_db), transport
        self.wall_ms, self.monotonic = wall_ms, monotonic
        self.sequence = 0
        self.closed = False
        self.blocked = False
        if self.stop.exists() or self.running.exists():
            raise RuntimeError('daily_stop_or_running_preserved')
        self.permit = validate_permit(permit, binding, wall_ms())
        if not self.db.is_file():
            raise ValueError('existing_permanent_attempt_db_required')
        self.start_wall, self.start_monotonic = wall_ms(), monotonic()
        self.last_wall, self.last_monotonic = self.start_wall, self.start_monotonic
        try:
            self._save(self.running, {'permit_id': self.permit['permit_id'], 'retry': False})
            with sqlite3.connect(self.db) as db:
                db.execute('CREATE TABLE IF NOT EXISTS daily_permits (permit_id TEXT PRIMARY KEY)')
                db.execute('CREATE TABLE IF NOT EXISTS daily_rpc_attempts (permit_id TEXT, sequence INTEGER, PRIMARY KEY(permit_id,sequence))')
                db.execute('CREATE TABLE IF NOT EXISTS daily_ai_attempts (permit_id TEXT PRIMARY KEY)')
                db.execute('INSERT INTO daily_permits VALUES (?)', (self.permit['permit_id'],))
        except Exception:
            self._halt()
            raise RuntimeError('daily_start_unknown_no_retry') from None

    @staticmethod
    def _save(path, value):
        import os
        with path.open('x', encoding='utf-8') as out:
            out.write(canonical(value))
            out.flush()
            os.fsync(out.fileno())

    def _halt(self):
        self.blocked = True
        try:
            self._save(self.stop, {'reason':'daily_unknown_or_expired', 'retry':False})
        except OSError:
            pass  # RUNNING and permanent fences still block a restarted client.

    def remaining(self, reserve_ms):
        if self.closed or self.blocked or self.stop.exists() or not self.running.is_file():
            raise RuntimeError('daily_stop_or_running_preserved')
        now = self.wall_ms()
        mono = self.monotonic()
        elapsed = (mono-self.start_monotonic)*1000
        if (type(now) is not int or now < self.last_wall or mono < self.last_monotonic or elapsed < 0
                or abs(now-self.start_wall-elapsed) > 2000):
            raise RuntimeError('daily_clock_invalid')
        left = self.permit['expires_at_ms'] - now
        if left <= 0 or left < reserve_ms:
            raise RuntimeError('daily_insufficient_time')
        self.last_wall, self.last_monotonic = now, mono
        return left

    def before_ai(self):
        try:
            remaining = self.remaining(180_000)
            with sqlite3.connect(self.db) as db:
                db.execute('INSERT INTO daily_ai_attempts VALUES (?)', (self.permit['permit_id'],))
            return remaining
        except Exception:
            self._halt()
            raise RuntimeError('daily_no_new_ai') from None

    def rpc(self, action, **data):
        try:
            if action not in ACTIONS or self.sequence >= 32:
                raise ValueError('daily_action_invalid')
            reserve = 120_000 if action == 'intake' else 60_000 if action in {'claim','finish'} else 15_000
            command = dict(action=action, **data)
            validate_command(command)
            self.remaining(reserve)
            self.sequence += 1
            # Commit BEFORE transport, including reads. Never repeat a lost RPC.
            with sqlite3.connect(self.db) as db:
                db.execute('INSERT INTO daily_rpc_attempts VALUES (?,?)', (self.permit['permit_id'], self.sequence))
            envelope = dict(permit=deepcopy(self.permit), sequence=self.sequence, command=command)
            reply = self.transport(envelope)
            if (type(reply) is not dict or set(reply) != {'permit_id','sequence','result'}
                    or reply['permit_id'] != self.permit['permit_id'] or reply['sequence'] != self.sequence):
                raise ValueError('daily_reply_unknown')
            self.remaining(0)
            result = reply['result']
            if action == 'close':
                if result != {'closed': True, 'financial_write_count': 0}:
                    raise ValueError('daily_close_unknown')
                self.closed = True
                self.running.unlink()  # Permanent used-ID/RPC fences remain.
            return result
        except Exception:
            self._halt()
            raise RuntimeError('daily_rpc_unknown_no_retry') from None
