"""Protected local-file approval + SQLite one-shot ledger. No Google I/O.

Reuse the repository's SQLite BEGIN IMMEDIATE / FULL durability pattern, not
its financial tables. Provision/enrollment are offline operator operations;
the execution entry NEVER creates, enrolls, resets or restores this database.
This is local-host replay protection, not protection against an administrator
rolling back storage or arbitrary code running as the trusted operator.
"""
from contextlib import contextmanager, closing
from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

from .google_a0g import ApprovedAttempt, fail, valid_deadline

HEX = re.compile(r'[0-9a-f]{64}')
ID = re.compile(r'[A-Za-z0-9_-]{16,128}')
SCHEMA = 'a0g-local-authority-v1'


def protected_path(value, repo_root, *, exists=True):
    p = Path(value)
    if not p.is_absolute() or str(p).startswith(('\\\\', '//')):
        fail('local_protected_path_required')
    if any(part.lower() in {'onedrive', 'dropbox', 'google drive'} for part in p.parts):
        fail('unsynchronized_local_storage_required')
    for part in (p, *p.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            fail('protected_path_link')
    p = p.resolve()
    if p == Path(repo_root).resolve() or Path(repo_root).resolve() in p.parents:
        fail('protected_path_outside_checkout_required')
    if exists and not p.is_file():
        fail('protected_file_missing_no_recreate')
    return p


@dataclass(frozen=True)
class Authority:
    document: dict = field(repr=False)
    digest: str

    @classmethod
    def load(cls, path, *, expected_digest, repo_root, now):
        try:
            p = protected_path(path, repo_root)
            raw = p.read_bytes()
            if len(raw) > 16384 or not HEX.fullmatch(expected_digest):
                fail('authority_pin')
            if sha256(raw).hexdigest() != expected_digest:
                fail('authority_pin')
            def pairs(items):
                d = {}
                for k, v in items:
                    if k in d: fail('authority_duplicate_key')
                    d[k] = v
                return d
            d = json.loads(raw, object_pairs_hook=pairs)
            if set(d) != {'schema','candidate_sha','contract_digest','registry_id','registry_path',
                          'issued_at','authorization','metadata','local_controls_reviewed',
                          'cleanup_plan_ref','browser_executable'}:
                fail('authority_fields')
            if (d['schema'] != SCHEMA or not re.fullmatch(r'[0-9a-f]{40}', d['candidate_sha']) or
                    not HEX.fullmatch(d['contract_digest']) or not HEX.fullmatch(d['registry_id']) or
                    not HEX.fullmatch(d['cleanup_plan_ref']) or d['local_controls_reviewed'] is not True or
                    type(d['issued_at']) not in (int,float) or not 0 <= now-d['issued_at'] <= 600):
                fail('authority_binding')
            protected_path(d['registry_path'], repo_root)
            for phase in ('authorization','metadata'):
                a = d[phase]
                if (set(a) != {'id','approved','expires_at'} or not ID.fullmatch(a['id']) or
                        a['approved'] is not True or not valid_deadline(a['expires_at'], now) or
                        a['expires_at'] > d['issued_at'] + 600):
                    fail('phase_approval_required')
            if d['authorization']['id'] == d['metadata']['id']:
                fail('separate_phase_ids_required')
            return cls(d, expected_digest)
        except (OSError, ValueError, TypeError, KeyError):
            fail('authority_unavailable')

    def handoff(self):
        d = self.document
        return ApprovedAttempt(d['candidate_sha'],d['contract_digest'],
            min(d['authorization']['expires_at'],d['metadata']['expires_at']),
            oauth_authorization=True,token_exchange=True,refresh_token_issuance=True,metadata_get=True)


class LocalLedger:
    def __init__(self, path, *, registry_id, repo_root):
        self.path = protected_path(path, repo_root)
        self.registry_id = registry_id
        self.identity = (self.path.stat().st_dev, self.path.stat().st_ino)
        with self.db() as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                fail('ledger_integrity')

    @contextmanager
    def db(self):
        db = None
        try:
            info = self.path.stat()
            if self.path.is_symlink() or (info.st_dev,info.st_ino) != self.identity:
                fail('ledger_replaced')
            # rw, never implicit creation. Missing/locked/corrupt state fails closed.
            db = sqlite3.connect(self.path.as_uri()+'?mode=rw',uri=True,timeout=2,isolation_level=None)
            db.execute('PRAGMA synchronous=FULL')
            if db.execute('SELECT id FROM registry').fetchall() != [(self.registry_id,)]:
                fail('registry_changed')
            yield db
        except (OSError, sqlite3.Error):
            fail('ledger_unavailable')
        finally:
            if db is not None: db.close()

    @classmethod
    def provision(cls, path, *, repo_root):
        """Explicit OFFLINE provisioning only; no real approval created here."""
        p = protected_path(path,repo_root,exists=False)
        fd = os.open(p,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        os.close(fd)
        registry_id = secrets.token_hex(32)
        with closing(sqlite3.connect(p)) as db, db:
            db.execute('PRAGMA journal_mode=DELETE')
            db.execute('PRAGMA synchronous=FULL')
            db.executescript('''
                CREATE TABLE registry(id TEXT PRIMARY KEY);
                CREATE TABLE approvals(
                  id TEXT PRIMARY KEY, phase TEXT NOT NULL, document TEXT NOT NULL,
                  consumed INTEGER NOT NULL DEFAULT 0 CHECK(consumed IN (0,1)),
                  nonce_hash TEXT, exchange_count INTEGER NOT NULL DEFAULT 0 CHECK(exchange_count BETWEEN 0 AND 1),
                  get_count INTEGER NOT NULL DEFAULT 0 CHECK(get_count BETWEEN 0 AND 6),
                  session_started INTEGER NOT NULL DEFAULT 0 CHECK(session_started IN (0,1)),
                  terminal TEXT NOT NULL DEFAULT 'UNUSED');
                CREATE TRIGGER no_unconsume BEFORE UPDATE ON approvals
                  WHEN OLD.consumed=1 AND (NEW.consumed!=1 OR NEW.document!=OLD.document OR
                  NEW.id!=OLD.id OR NEW.phase!=OLD.phase OR NEW.nonce_hash!=OLD.nonce_hash OR
                  NEW.exchange_count<OLD.exchange_count OR NEW.get_count<OLD.get_count OR
                  NEW.session_started<OLD.session_started OR
                  (OLD.terminal!='CONSUMED_UNKNOWN' AND NEW.terminal!=OLD.terminal))
                  BEGIN SELECT RAISE(ABORT,'irreversible'); END;
                CREATE TRIGGER no_delete BEFORE DELETE ON approvals
                  BEGIN SELECT RAISE(ABORT,'no_delete'); END;
            ''')
            db.execute('INSERT INTO registry VALUES (?)',(registry_id,))
        return cls(p,registry_id=registry_id,repo_root=repo_root)

    def enroll(self, authority):
        """Separate protected operator ceremony. Execution never calls this."""
        d = authority.document
        if d['registry_id'] != self.registry_id or Path(d['registry_path']).resolve() != self.path:
            fail('registry_changed')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                for phase in ('authorization','metadata'):
                    db.execute('INSERT INTO approvals(id,phase,document) VALUES (?,?,?)',
                        (d[phase]['id'],phase,authority.digest))
                db.execute('COMMIT')
            except Exception:
                db.execute('ROLLBACK')
                fail('approval_already_enrolled')

    def consume(self, authority, *, now):
        d = authority.document
        if d['registry_id'] != self.registry_id or Path(d['registry_path']).resolve() != self.path:
            fail('registry_changed')
        for phase in ('authorization','metadata'):
            if not valid_deadline(d[phase]['expires_at'],now): fail('approval_expired')
        nonce = secrets.token_hex(32)
        nh = sha256(nonce.encode()).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                for phase in ('authorization','metadata'):
                    row = db.execute('SELECT phase,document,consumed FROM approvals WHERE id=?',
                                     (d[phase]['id'],)).fetchone()
                    if row != (phase,authority.digest,0): fail('approval_consumed_or_unregistered')
                for phase in ('authorization','metadata'):
                    db.execute("UPDATE approvals SET consumed=1,nonce_hash=?,terminal='CONSUMED_UNKNOWN' WHERE id=?",
                               (nh,d[phase]['id']))
                db.execute('COMMIT')  # Both spent BEFORE browser/OAuth/HTTP. No release path.
            except BaseException:
                if db.in_transaction: db.execute('ROLLBACK')
                raise
        return ConsumedRun(self,authority,nonce,os.getpid())

    def snapshot(self, authority):
        with self.db() as db:
            return {phase:db.execute('SELECT consumed,exchange_count,get_count,session_started,terminal FROM approvals WHERE id=?',
                    (authority.document[phase]['id'],)).fetchone() for phase in ('authorization','metadata')}


class ConsumedRun:
    def __init__(self,ledger,authority,nonce,pid):
        self._ledger,self._authority,self._nonce,self._pid = ledger,authority,nonce,pid
    def __repr__(self): return '<ConsumedRun private>'
    def __reduce__(self): fail('capability_not_serializable')

    def debit(self, operation, *, ordinal=None):
        if not self._nonce or os.getpid() != self._pid: fail('run_ended_or_other_process')
        d = self._authority.document
        phase = 'authorization' if operation=='exchange' else 'metadata'
        if operation not in ('exchange','session','get'): fail('unknown_debit')
        if not valid_deadline(d[phase]['expires_at'],time.time()): fail('approval_expired')
        with self._ledger.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT consumed,nonce_hash,exchange_count,get_count,session_started,terminal FROM approvals WHERE id=?',
                             (d[phase]['id'],)).fetchone()
            if (not row or row[0]!=1 or row[1]!=sha256(self._nonce.encode()).hexdigest() or row[5]!='CONSUMED_UNKNOWN'):
                fail('run_ended_or_unbound')
            if operation=='exchange':
                if row[2]!=0: fail('exchange_consumed')
                db.execute('UPDATE approvals SET exchange_count=1 WHERE id=?',(d[phase]['id'],))
            elif operation=='session':
                if row[4]!=0: fail('metadata_session_consumed')
                auth = db.execute('SELECT exchange_count,terminal FROM approvals WHERE id=?',
                                  (d['authorization']['id'],)).fetchone()
                if auth != (1,'CONSUMED_UNKNOWN'): fail('exchange_required_before_metadata')
                db.execute('UPDATE approvals SET session_started=1 WHERE id=?',(d[phase]['id'],))
            else:
                if row[4]!=1 or ordinal!=row[3] or row[3]>=6: fail('metadata_request_consumed')
                db.execute('UPDATE approvals SET get_count=get_count+1 WHERE id=?',(d[phase]['id'],))
            db.execute('COMMIT')  # request attempt spent even on UNKNOWN delivery

    def close(self, *, completed=False):
        if not self._nonce: return
        try:
            with self._ledger.db() as db:
                db.execute('BEGIN IMMEDIATE')
                for phase in ('authorization','metadata'):
                    db.execute("UPDATE approvals SET terminal=? WHERE id=? AND nonce_hash=? AND terminal='CONSUMED_UNKNOWN'",
                        ('COMPLETED_NO_GO' if completed else 'STOPPED_NO_GO',self._authority.document[phase]['id'],sha256(self._nonce.encode()).hexdigest()))
                db.execute('COMMIT')
        finally:
            self._nonce = None
