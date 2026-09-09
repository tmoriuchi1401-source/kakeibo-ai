"""Durable, one-shot production-canary authority for au PAY card ingestion.

There is no standing production flag or CLI route. Authority exists only as a
short-lived exact capability backed by protected approval and SQLite state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Callable
from uuid import UUID, uuid4

from .aupay_card_apply_plan import (
    CanonicalApplyCandidate,
    CanonicalApplyPlan,
    validate_canonical_apply_plan,
)
from .aupay_card_executor import CandidateState, CanonicalIdentityReader
from .aupay_card_writer import (
    AttemptJournal,
    BatchPolicy,
    FixedSourceWindow,
    JournalEvent,
    JournalStage,
    Lease,
    PersistentAuditKey,
    ProductionRunManifest,
    ReadBackPolicy,
    TargetBinding,
    TargetInspector,
    WriteDisposition,
    WriteRequestResult,
    WriterExecutionResult,
    _bounded_readback,
    _journal_event,
    _keyed_ref,
    _load_and_validate_key,
    _target_ref,
    _validate_manifest,
    preflight_writer,
    validate_target_binding,
)


PERSISTENCE_SCHEMA_VERSION = 1
PRODUCTION_CAPABILITY_ENABLED = False
MAX_CANARY_CAPABILITY_TTL_SECONDS = 300
_SAFE_REASON = re.compile(r"[a-z0-9_]{1,64}")
_SAFE_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _storage_path(path: str | os.PathLike, repo_root: str | os.PathLike | None) -> Path:
    requested = Path(path).expanduser()
    if not requested.is_absolute():
        raise RuntimeError("persistence_path_must_be_absolute")
    resolved = requested.resolve()
    if repo_root is None:
        raise RuntimeError("repository_root_required_for_persistence_guard")
    root = Path(repo_root).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        pass
    else:
        raise RuntimeError("persistence_path_must_be_outside_repository")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


class ProtectedAuditKeyProvider:
    """Read key material from a repo-external protected JSON file.

    Expected JSON keys are ``key_id`` and ``key_b64``. Values are never placed
    in exception text or object representations.
    """

    def __init__(self, path: str | os.PathLike, *, repo_root: str | os.PathLike):
        self._path = Path(path).expanduser().resolve()
        root = Path(repo_root).resolve()
        try:
            self._path.relative_to(root)
        except ValueError:
            pass
        else:
            raise RuntimeError("audit_key_file_must_be_outside_repository")

    def __repr__(self) -> str:
        return "ProtectedAuditKeyProvider(path=<redacted>)"

    def load(self) -> PersistentAuditKey | None:
        if not self._path.is_file():
            return None
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            key_id = str(payload.get("key_id", "")).strip()
            encoded = str(payload.get("key_b64", "")).strip()
            if not _SAFE_KEY_ID.fullmatch(key_id):
                raise ValueError("unsafe_key_id")
            secret = base64.b64decode(encoded, validate=True)
            key = PersistentAuditKey(key_id=key_id, secret=secret)
            key.validate()
            return key
        except Exception as exc:
            raise RuntimeError("protected_audit_key_invalid") from exc


def _event_payload(event: JournalEvent) -> str:
    return json.dumps({
        "run_id": event.run_id,
        "canonical_identity": event.canonical_identity,
        "attempt_id": event.attempt_id,
        "batch_id": event.batch_id,
        "stage": event.stage.value,
        "state": event.state.value,
        "timestamp": event.timestamp.isoformat(),
        "reason_code": event.reason_code,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _event_hash(previous_hash: str, payload: str) -> str:
    return hashlib.sha256(f"{previous_hash}\x00{payload}".encode("utf-8")).hexdigest()


def _validate_event_content(event: JournalEvent) -> None:
    try:
        UUID(event.run_id)
        UUID(event.attempt_id)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("journal_identifier_invalid") from exc
    if event.timestamp.tzinfo is None or event.timestamp.utcoffset() is None:
        raise RuntimeError("journal_timestamp_timezone_required")
    if (
        not event.canonical_identity
        or not event.batch_id
        or not _SAFE_REASON.fullmatch(event.reason_code)
    ):
        raise RuntimeError("journal_event_content_invalid")


_ALLOWED_TRANSITIONS = {
    None: {JournalStage.PRE_READ},
    JournalStage.PRE_READ: {JournalStage.WRITE_ATTEMPTED, JournalStage.FINAL},
    JournalStage.WRITE_ATTEMPTED: {JournalStage.WRITE_RESULT},
    JournalStage.WRITE_RESULT: {JournalStage.POST_READ},
    JournalStage.POST_READ: {JournalStage.FINAL},
    JournalStage.FINAL: set(),
}

_ALLOWED_STAGE_STATES = {
    JournalStage.PRE_READ: {
        CandidateState.VERIFIED_NEW,
        CandidateState.ALREADY_PRESENT,
        CandidateState.CONFLICT,
        CandidateState.FAILED,
    },
    JournalStage.WRITE_ATTEMPTED: {CandidateState.WRITE_ATTEMPTED},
    JournalStage.WRITE_RESULT: {
        CandidateState.WRITE_ATTEMPTED,
        CandidateState.OUTCOME_UNKNOWN,
    },
    JournalStage.POST_READ: {
        CandidateState.WRITE_CONFIRMED,
        CandidateState.CONFLICT,
        CandidateState.OUTCOME_UNKNOWN,
    },
    JournalStage.FINAL: {
        CandidateState.WRITE_CONFIRMED,
        CandidateState.ALREADY_PRESENT,
        CandidateState.CONFLICT,
        CandidateState.OUTCOME_UNKNOWN,
        CandidateState.RETRY_ELIGIBLE,
        CandidateState.FAILED,
    },
}


class SqliteAttemptJournal(AttemptJournal):
    """Append-only, hash-chained journal durable across process restarts."""

    persistent = True

    def __init__(self, path: str | os.PathLike, *, repo_root,
                 fail_writes: bool = False):
        self._path = _storage_path(path, repo_root)
        self._fail_writes = fail_writes
        self._initialize()

    def _initialize(self) -> None:
        with _connect(self._path) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS journal_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    canonical_identity TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    state TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE,
                    UNIQUE(run_id, attempt_id, stage)
                );
                CREATE TRIGGER IF NOT EXISTS journal_no_update
                BEFORE UPDATE ON journal_events BEGIN
                    SELECT RAISE(ABORT, 'append_only_journal');
                END;
                CREATE TRIGGER IF NOT EXISTS journal_no_delete
                BEFORE DELETE ON journal_events BEGIN
                    SELECT RAISE(ABORT, 'append_only_journal');
                END;
            """)

    def ready(self, run_id: str) -> bool:
        try:
            UUID(run_id)
            with _connect(self._path) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    return False
                self._validate_rows(connection.execute(
                    "SELECT * FROM journal_events ORDER BY seq"
                ).fetchall())
            return True
        except Exception:
            return False

    def _validate_rows(self, rows) -> None:
        previous_hash = ""
        last_by_attempt: dict[
            tuple[str, str], tuple[JournalStage, datetime, str, str]
        ] = {}
        for row in rows:
            payload = json.dumps({
                "run_id": row["run_id"],
                "canonical_identity": row["canonical_identity"],
                "attempt_id": row["attempt_id"],
                "batch_id": row["batch_id"],
                "stage": row["stage"],
                "state": row["state"],
                "timestamp": row["timestamp"],
                "reason_code": row["reason_code"],
            }, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            if row["prev_hash"] != previous_hash or row["event_hash"] != _event_hash(previous_hash, payload):
                raise RuntimeError("journal_hash_chain_invalid")
            event = _row_to_event(row)
            _validate_event_content(event)
            key = (event.run_id, event.attempt_id)
            prior = last_by_attempt.get(key)
            if event.stage not in _ALLOWED_TRANSITIONS[prior[0] if prior else None]:
                raise RuntimeError("invalid_journal_state_transition")
            if event.state not in _ALLOWED_STAGE_STATES[event.stage]:
                raise RuntimeError("invalid_journal_stage_state")
            if prior and event.timestamp < prior[1]:
                raise RuntimeError("journal_timestamp_regression")
            if prior and (
                event.canonical_identity != prior[2] or event.batch_id != prior[3]
            ):
                raise RuntimeError("journal_attempt_binding_changed")
            last_by_attempt[key] = (
                event.stage, event.timestamp, event.canonical_identity, event.batch_id,
            )
            previous_hash = row["event_hash"]

    def append(self, event: JournalEvent) -> None:
        _validate_event_content(event)
        if self._fail_writes:
            raise RuntimeError("attempt_journal_persist_failed")
        payload = _event_payload(event)
        with _connect(self._path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    "SELECT * FROM journal_events ORDER BY seq"
                ).fetchall()
                self._validate_rows(rows)
                prior_rows = [row for row in rows if (
                    row["run_id"] == event.run_id
                    and row["attempt_id"] == event.attempt_id
                )]
                prior = _row_to_event(prior_rows[-1]) if prior_rows else None
                if event.stage not in _ALLOWED_TRANSITIONS[prior.stage if prior else None]:
                    raise RuntimeError("invalid_journal_state_transition")
                if event.state not in _ALLOWED_STAGE_STATES[event.stage]:
                    raise RuntimeError("invalid_journal_stage_state")
                if prior and event.timestamp < prior.timestamp:
                    raise RuntimeError("journal_timestamp_regression")
                if prior and (
                    event.canonical_identity != prior.canonical_identity
                    or event.batch_id != prior.batch_id
                ):
                    raise RuntimeError("journal_attempt_binding_changed")
                previous_hash = rows[-1]["event_hash"] if rows else ""
                connection.execute(
                    """INSERT INTO journal_events (
                        run_id, canonical_identity, attempt_id, batch_id, stage,
                        state, timestamp, reason_code, prev_hash, event_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event.run_id, event.canonical_identity, event.attempt_id,
                        event.batch_id, event.stage.value, event.state.value,
                        event.timestamp.isoformat(), event.reason_code,
                        previous_hash, _event_hash(previous_hash, payload),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def history(self, run_id: str, attempt_id: str) -> tuple[JournalEvent, ...]:
        if not self.ready(run_id):
            raise RuntimeError("attempt_journal_corrupt_or_unavailable")
        with _connect(self._path) as connection:
            rows = connection.execute(
                """SELECT * FROM journal_events
                   WHERE run_id=? AND attempt_id=? ORDER BY seq""",
                (run_id, attempt_id),
            ).fetchall()
        return tuple(_row_to_event(row) for row in rows)


def _row_to_event(row) -> JournalEvent:
    return JournalEvent(
        run_id=row["run_id"],
        canonical_identity=row["canonical_identity"],
        attempt_id=row["attempt_id"],
        batch_id=row["batch_id"],
        stage=JournalStage(row["stage"]),
        state=CandidateState(row["state"]),
        timestamp=datetime.fromisoformat(row["timestamp"]),
        reason_code=row["reason_code"],
    )


class SqliteLeaseManager:
    """Transactional cross-process lease with expiry and owner-token checks."""

    def __init__(self, path: str | os.PathLike, *, repo_root,
                 clock: Callable[[], datetime] = _utc_now,
                 fail_operations: bool = False):
        self._path = _storage_path(path, repo_root)
        self._clock = clock
        self._fail_operations = fail_operations
        with _connect(self._path) as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS writer_leases (
                    target_ref TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    token TEXT NOT NULL UNIQUE,
                    expires_at TEXT NOT NULL
                )
            """)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("lease_clock_timezone_required")
        return value

    def acquire(self, target_ref: str, owner_id: str, run_id: str,
                lease_seconds: int) -> Lease | None:
        if self._fail_operations:
            raise RuntimeError("lease_backend_unavailable")
        if lease_seconds <= 0:
            raise ValueError("lease_duration_invalid")
        now = self._now()
        with _connect(self._path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM writer_leases WHERE target_ref=?", (target_ref,),
                ).fetchone()
                if row and datetime.fromisoformat(row["expires_at"]) > now:
                    connection.execute("ROLLBACK")
                    return None
                token = hashlib.sha256(os.urandom(32)).hexdigest()
                expires_at = now + timedelta(seconds=lease_seconds)
                connection.execute(
                    """INSERT INTO writer_leases
                       (target_ref, owner_id, run_id, token, expires_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(target_ref) DO UPDATE SET
                         owner_id=excluded.owner_id, run_id=excluded.run_id,
                         token=excluded.token, expires_at=excluded.expires_at""",
                    (target_ref, owner_id, run_id, token, expires_at.isoformat()),
                )
                connection.execute("COMMIT")
                return Lease(target_ref, owner_id, run_id, token, expires_at)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def renew(self, lease: Lease, lease_seconds: int) -> Lease | None:
        if self._fail_operations:
            raise RuntimeError("lease_backend_unavailable")
        now = self._now()
        expires_at = now + timedelta(seconds=lease_seconds)
        with _connect(self._path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE writer_leases SET expires_at=?
                   WHERE target_ref=? AND owner_id=? AND run_id=? AND token=?
                   AND expires_at>?""",
                (
                    expires_at.isoformat(), lease.target_ref, lease.owner_id,
                    lease.run_id, lease.token, now.isoformat(),
                ),
            )
            connection.execute("COMMIT")
        if cursor.rowcount != 1:
            return None
        return Lease(
            lease.target_ref, lease.owner_id, lease.run_id, lease.token, expires_at,
        )

    def release(self, lease: Lease) -> None:
        if self._fail_operations:
            raise RuntimeError("lease_backend_unavailable")
        with _connect(self._path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM writer_leases WHERE target_ref=?",
                (lease.target_ref,),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return
            if (
                row["owner_id"] != lease.owner_id
                or row["run_id"] != lease.run_id
                or row["token"] != lease.token
            ):
                connection.execute("ROLLBACK")
                raise RuntimeError("lease_owner_token_mismatch")
            connection.execute(
                "DELETE FROM writer_leases WHERE target_ref=?", (lease.target_ref,),
            )
            connection.execute("COMMIT")


def _manifest_payload(manifest: ProductionRunManifest) -> str:
    return json.dumps({
        "schema_version": manifest.schema_version,
        "run_id": manifest.run_id,
        "source_window": {
            "start": manifest.source_window.start.isoformat(),
            "end": manifest.source_window.end.isoformat(),
            "timezone_name": manifest.source_window.timezone_name,
            "query_representation": manifest.source_window.query_representation,
        },
        "plan_created_at": manifest.plan_created_at.isoformat(),
        "plan_binding_ref": manifest.plan_binding_ref,
        "audit_key_id": manifest.audit_key_id,
        "target_binding_version": manifest.target_binding_version,
        "canonical_count": manifest.canonical_count,
        "candidate_count": manifest.candidate_count,
        "withheld_count": manifest.withheld_count,
        "authority_mode": manifest.authority_mode,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class SqliteRunManifestStore:
    """Immutable run manifests; a UUID can never be rebound to new content."""

    def __init__(self, path: str | os.PathLike, *, repo_root):
        self._path = _storage_path(path, repo_root)
        with _connect(self._path) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS run_manifests (
                    run_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS manifest_no_update
                BEFORE UPDATE ON run_manifests BEGIN
                    SELECT RAISE(ABORT, 'immutable_manifest');
                END;
                CREATE TRIGGER IF NOT EXISTS manifest_no_delete
                BEFORE DELETE ON run_manifests BEGIN
                    SELECT RAISE(ABORT, 'immutable_manifest');
                END;
            """)

    def save(self, manifest: ProductionRunManifest) -> None:
        manifest.validate()
        payload = _manifest_payload(manifest)
        payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with _connect(self._path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload, payload_hash FROM run_manifests WHERE run_id=?",
                (manifest.run_id,),
            ).fetchone()
            if existing:
                connection.execute("ROLLBACK")
                if existing["payload"] == payload and existing["payload_hash"] == payload_hash:
                    return
                raise RuntimeError("run_manifest_immutable_conflict")
            connection.execute(
                "INSERT INTO run_manifests VALUES (?, ?, ?)",
                (manifest.run_id, payload, payload_hash),
            )
            connection.execute("COMMIT")

    def load(self, run_id: str) -> ProductionRunManifest | None:
        with _connect(self._path) as connection:
            row = connection.execute(
                "SELECT payload, payload_hash FROM run_manifests WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        if hashlib.sha256(row["payload"].encode("utf-8")).hexdigest() != row["payload_hash"]:
            raise RuntimeError("run_manifest_corrupt")
        raw = json.loads(row["payload"])
        window = raw["source_window"]
        manifest = ProductionRunManifest(
            schema_version=raw["schema_version"],
            run_id=raw["run_id"],
            source_window=FixedSourceWindow(
                start=datetime.fromisoformat(window["start"]),
                end=datetime.fromisoformat(window["end"]),
                timezone_name=window["timezone_name"],
                query_representation=window["query_representation"],
            ),
            plan_created_at=datetime.fromisoformat(raw["plan_created_at"]),
            plan_binding_ref=raw["plan_binding_ref"],
            audit_key_id=raw["audit_key_id"],
            target_binding_version=raw["target_binding_version"],
            canonical_count=raw["canonical_count"],
            candidate_count=raw["candidate_count"],
            withheld_count=raw["withheld_count"],
            authority_mode=raw["authority_mode"],
        )
        manifest.validate()
        return manifest


@dataclass(frozen=True)
class ProductionWriteCapability:
    capability_id: str
    run_id: str
    plan_binding_ref: str
    target_ref: str
    candidate_ref: str
    approval_reference: str
    issued_at: datetime
    expires_at: datetime
    token: str = field(repr=False)


@dataclass(frozen=True)
class CanaryApproval:
    """Human-approved, exact one-candidate production envelope."""

    approval_reference: str
    candidate_ref: str
    target_ref: str
    batch_size: int
    expires_at: datetime


class ProtectedCanaryApprovalProvider:
    """Load the exact human-approved canary envelope from outside the repo."""

    def __init__(self, path: str | os.PathLike, *, repo_root: str | os.PathLike):
        self._path = Path(path).expanduser().resolve()
        root = Path(repo_root).resolve()
        try:
            self._path.relative_to(root)
        except ValueError:
            pass
        else:
            raise RuntimeError("canary_approval_file_must_be_outside_repository")

    def __repr__(self) -> str:
        return "ProtectedCanaryApprovalProvider(path=<redacted>)"

    def load(self) -> CanaryApproval:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            approval = CanaryApproval(
                approval_reference=str(payload["approval_reference"]).strip(),
                candidate_ref=str(payload["candidate_ref"]).strip(),
                target_ref=str(payload["target_ref"]).strip(),
                batch_size=int(payload["batch_size"]),
                expires_at=datetime.fromisoformat(str(payload["expires_at"])),
            )
        except Exception as exc:
            raise RuntimeError("protected_canary_approval_invalid") from exc
        if not approval.approval_reference or len(approval.approval_reference) > 256:
            raise RuntimeError("human_approval_reference_required")
        if not re.fullmatch(r"canonical-item-v1:[0-9a-f]{32}", approval.candidate_ref):
            raise RuntimeError("protected_canary_approval_invalid")
        if not re.fullmatch(r"writer-target-v1:[0-9a-f]{32}", approval.target_ref):
            raise RuntimeError("protected_canary_approval_invalid")
        return approval


def canonical_candidate_reference(candidate: CanonicalApplyCandidate,
                                  audit_key: PersistentAuditKey) -> str:
    """Return the approval-safe reference for one exact canonical identity."""
    audit_key.validate()
    return _keyed_ref(audit_key.secret, "canonical-item", candidate.identity)


def target_binding_reference(binding: TargetBinding,
                             audit_key: PersistentAuditKey) -> str:
    """Return the approval-safe reference for one exact target binding."""
    audit_key.validate()
    binding.validate()
    return _target_ref(binding, audit_key.secret)


class SqliteCapabilityStore:
    """Durable one-shot capability registry and atomic dispatch gate."""

    persistent = True

    def __init__(self, path: str | os.PathLike, *, repo_root):
        self._path = _storage_path(path, repo_root)
        with _connect(self._path) as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS production_capabilities (
                    capability_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL UNIQUE,
                    plan_binding_ref TEXT NOT NULL,
                    target_ref TEXT NOT NULL,
                    candidate_ref TEXT NOT NULL,
                    approval_reference TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt_id TEXT UNIQUE,
                    terminal_reason TEXT NOT NULL DEFAULT '',
                    UNIQUE(candidate_ref, target_ref)
                )
            """)

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def ready(self) -> bool:
        try:
            with _connect(self._path) as connection:
                return connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        except Exception:
            return False

    def issue(self, capability: ProductionWriteCapability) -> None:
        if not self.ready():
            raise RuntimeError("capability_store_unavailable")
        try:
            with _connect(self._path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("""INSERT INTO production_capabilities (
                    capability_id, token_hash, run_id, plan_binding_ref,
                    target_ref, candidate_ref, approval_reference, issued_at,
                    expires_at, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued')""", (
                    capability.capability_id,
                    self._token_hash(capability.token), capability.run_id,
                    capability.plan_binding_ref, capability.target_ref,
                    capability.candidate_ref, capability.approval_reference,
                    capability.issued_at.isoformat(), capability.expires_at.isoformat(),
                ))
                connection.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("canary_capability_already_issued") from exc

    def _bound_row(self, capability: ProductionWriteCapability, connection):
        row = connection.execute(
            "SELECT * FROM production_capabilities WHERE capability_id=?",
            (capability.capability_id,),
        ).fetchone()
        if row is None or row["token_hash"] != self._token_hash(capability.token):
            raise RuntimeError("production_capability_invalid")
        expected = (
            capability.run_id, capability.plan_binding_ref, capability.target_ref,
            capability.candidate_ref, capability.approval_reference,
            capability.issued_at.isoformat(), capability.expires_at.isoformat(),
        )
        actual = tuple(row[name] for name in (
            "run_id", "plan_binding_ref", "target_ref", "candidate_ref",
            "approval_reference", "issued_at", "expires_at",
        ))
        if actual != expected:
            raise RuntimeError("production_capability_binding_mismatch")
        return row

    def claim(self, capability: ProductionWriteCapability, attempt_id: str,
              now: datetime) -> None:
        _aware_time(now, "capability_clock_timezone_required")
        with _connect(self._path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._bound_row(capability, connection)
                if datetime.fromisoformat(row["expires_at"]) <= now:
                    raise RuntimeError("production_capability_expired")
                if row["state"] != "issued":
                    raise RuntimeError("production_capability_reused")
                cursor = connection.execute("""UPDATE production_capabilities
                    SET state='claimed', attempt_id=?
                    WHERE capability_id=? AND state='issued'""",
                    (attempt_id, capability.capability_id))
                if cursor.rowcount != 1:
                    raise RuntimeError("production_capability_reused")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def authorize_dispatch(self, capability: ProductionWriteCapability,
                           attempt_id: str, now: datetime) -> None:
        _aware_time(now, "capability_clock_timezone_required")
        with _connect(self._path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._bound_row(capability, connection)
                if datetime.fromisoformat(row["expires_at"]) <= now:
                    raise RuntimeError("production_capability_expired")
                if row["state"] != "claimed" or row["attempt_id"] != attempt_id:
                    raise RuntimeError("production_capability_reused")
                cursor = connection.execute("""UPDATE production_capabilities
                    SET state='dispatching' WHERE capability_id=?
                    AND state='claimed' AND attempt_id=?""",
                    (capability.capability_id, attempt_id))
                if cursor.rowcount != 1:
                    raise RuntimeError("production_capability_reused")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def reseal(self, capability: ProductionWriteCapability, reason: str) -> None:
        safe_reason = reason if _SAFE_REASON.fullmatch(reason) else "execution_ended"
        with _connect(self._path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._bound_row(capability, connection)
                connection.execute("""UPDATE production_capabilities
                    SET state='sealed', terminal_reason=? WHERE capability_id=?""",
                    (safe_reason, capability.capability_id))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def state(self, capability_id: str) -> str | None:
        with _connect(self._path) as connection:
            row = connection.execute(
                "SELECT state FROM production_capabilities WHERE capability_id=?",
                (capability_id,),
            ).fetchone()
        return row["state"] if row else None


def _aware_time(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeError(reason)
    return value


@dataclass(frozen=True)
class ProductionApprovalPreflight:
    """Privacy-safe proof that capability issuance inputs agree, without writes."""

    schema_version: int
    run_id: str
    candidate_ref: str
    target_ref: str
    approval_reference: str = field(repr=False)
    source_window_start: str
    source_window_end: str
    batch_size: int
    expires_at: datetime
    valid: bool = True
    external_write_count: int = 0
    capability_issued_count: int = 0
    lease_acquired_count: int = 0
    journal_write_count: int = 0
    transport_invocation_count: int = 0


def validate_production_approval_preflight(
    plan: CanonicalApplyPlan,
    manifest: ProductionRunManifest,
    *,
    binding: TargetBinding,
    inspector: TargetInspector,
    key_provider: ProtectedAuditKeyProvider,
    approval_provider: ProtectedCanaryApprovalProvider,
    expected_run_id: str,
    expected_source_window: FixedSourceWindow,
    clock: Callable[[], datetime] = _utc_now,
) -> ProductionApprovalPreflight:
    """Validate exact canary authority without issuing or persisting anything."""
    now = _aware_time(clock(), "capability_clock_timezone_required")
    if type(approval_provider) is not ProtectedCanaryApprovalProvider:
        raise RuntimeError("protected_canary_approval_required")
    if type(key_provider) is not ProtectedAuditKeyProvider:
        raise RuntimeError("protected_audit_key_provider_required")
    try:
        UUID(expected_run_id)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("production_preflight_run_id_invalid") from exc
    expected_source_window.validate()
    approval = approval_provider.load()
    validate_canonical_apply_plan(plan)
    key = _load_and_validate_key(key_provider)
    _validate_manifest(plan, manifest, key)
    if manifest.authority_mode != "production_canary":
        raise RuntimeError("production_canary_authority_required")
    if manifest.run_id != expected_run_id:
        raise RuntimeError("production_preflight_run_mismatch")
    if manifest.source_window != expected_source_window:
        raise RuntimeError("production_preflight_source_window_mismatch")
    if approval.batch_size != 1:
        raise RuntimeError("production_canary_batch_size_must_be_one")
    if len(plan.candidates) != 1 or manifest.candidate_count != 1:
        raise RuntimeError("production_canary_requires_exactly_one_candidate")
    if not approval.approval_reference.strip():
        raise RuntimeError("human_approval_reference_required")
    expires_at = _aware_time(approval.expires_at, "capability_expiry_timezone_required")
    ttl = (expires_at - now).total_seconds()
    if ttl <= 0:
        raise RuntimeError("production_capability_expired")
    if ttl > MAX_CANARY_CAPABILITY_TTL_SECONDS:
        raise RuntimeError("production_capability_ttl_too_long")
    validate_target_binding(binding, inspector.inspect(binding.expected_worksheet))
    target_ref = target_binding_reference(binding, key)
    candidate_ref = canonical_candidate_reference(plan.candidates[0], key)
    if approval.target_ref != target_ref:
        raise RuntimeError("approved_target_mismatch")
    if approval.candidate_ref != candidate_ref:
        raise RuntimeError("approved_candidate_mismatch")
    return ProductionApprovalPreflight(
        schema_version=PERSISTENCE_SCHEMA_VERSION,
        run_id=manifest.run_id,
        candidate_ref=candidate_ref,
        target_ref=target_ref,
        approval_reference=approval.approval_reference.strip(),
        source_window_start=manifest.source_window.start.isoformat(),
        source_window_end=manifest.source_window.end.isoformat(),
        batch_size=approval.batch_size,
        expires_at=expires_at,
    )


def issue_production_write_capability(
    plan: CanonicalApplyPlan,
    manifest: ProductionRunManifest,
    *,
    binding: TargetBinding,
    inspector: TargetInspector,
    key_provider,
    journal,
    capability_store: SqliteCapabilityStore,
    approval_provider: ProtectedCanaryApprovalProvider,
    clock: Callable[[], datetime] = _utc_now,
) -> ProductionWriteCapability:
    """Issue one short-lived capability for one exact approved candidate/target."""
    now = _aware_time(clock(), "capability_clock_timezone_required")
    if type(approval_provider) is not ProtectedCanaryApprovalProvider:
        raise RuntimeError("protected_canary_approval_required")
    if type(key_provider) is not ProtectedAuditKeyProvider:
        raise RuntimeError("protected_audit_key_provider_required")
    if type(journal) is not SqliteAttemptJournal:
        raise RuntimeError("durable_attempt_journal_required")
    if type(capability_store) is not SqliteCapabilityStore:
        raise RuntimeError("durable_capability_store_required")
    approval = approval_provider.load()
    validate_canonical_apply_plan(plan)
    key = _load_and_validate_key(key_provider)
    _validate_manifest(plan, manifest, key)
    if manifest.authority_mode != "production_canary":
        raise RuntimeError("production_canary_authority_required")
    if approval.batch_size != 1:
        raise RuntimeError("production_canary_batch_size_must_be_one")
    if len(plan.candidates) != 1 or manifest.candidate_count != 1:
        raise RuntimeError("production_canary_requires_exactly_one_candidate")
    if not approval.approval_reference.strip():
        raise RuntimeError("human_approval_reference_required")
    expires_at = _aware_time(approval.expires_at, "capability_expiry_timezone_required")
    ttl = (expires_at - now).total_seconds()
    if ttl <= 0:
        raise RuntimeError("production_capability_expired")
    if ttl > MAX_CANARY_CAPABILITY_TTL_SECONDS:
        raise RuntimeError("production_capability_ttl_too_long")
    if not getattr(journal, "persistent", False) or not journal.ready(manifest.run_id):
        raise RuntimeError("attempt_journal_unavailable")
    if not capability_store.ready():
        raise RuntimeError("capability_store_unavailable")
    validate_target_binding(binding, inspector.inspect(binding.expected_worksheet))
    target_ref = _target_ref(binding, key.secret)
    candidate_ref = canonical_candidate_reference(plan.candidates[0], key)
    if approval.target_ref != target_ref:
        raise RuntimeError("approved_target_mismatch")
    if approval.candidate_ref != candidate_ref:
        raise RuntimeError("approved_candidate_mismatch")
    capability = ProductionWriteCapability(
        capability_id=str(uuid4()), run_id=manifest.run_id,
        plan_binding_ref=manifest.plan_binding_ref, target_ref=target_ref,
        candidate_ref=candidate_ref,
        approval_reference=approval.approval_reference.strip(),
        issued_at=now, expires_at=expires_at,
        token=hashlib.sha256(os.urandom(32)).hexdigest(),
    )
    capability_store.issue(capability)
    return capability


@dataclass(frozen=True)
class CapabilitySealChecks:
    explicit_capability_flag: bool = False
    persistent_key_available: bool = False
    durable_journal_healthy: bool = False
    lease_acquired: bool = False
    target_binding_verified: bool = False
    fixed_source_window_valid: bool = False
    executor_authority_valid: bool = False
    explicit_apply_authority: bool = False
    human_approval_present: bool = False
    exact_candidate_bound: bool = False
    exact_target_bound: bool = False
    batch_size_one: bool = False
    short_ttl_valid: bool = False
    one_shot_store_ready: bool = False


def evaluate_capability_seal(checks: CapabilitySealChecks) -> tuple[str, ...]:
    """Return privacy-safe blockers for a scoped one-shot issuance request."""
    blockers = [
        name for name, passed in (
            ("explicit_capability_flag_required", checks.explicit_capability_flag),
            ("persistent_audit_key_required", checks.persistent_key_available),
            ("durable_journal_unavailable", checks.durable_journal_healthy),
            ("writer_lock_unavailable", checks.lease_acquired),
            ("target_binding_invalid", checks.target_binding_verified),
            ("fixed_source_window_invalid", checks.fixed_source_window_valid),
            ("executor_authority_invalid", checks.executor_authority_valid),
            ("explicit_apply_authority_required", checks.explicit_apply_authority),
            ("human_approval_reference_required", checks.human_approval_present),
            ("approved_candidate_mismatch", checks.exact_candidate_bound),
            ("approved_target_mismatch", checks.exact_target_bound),
            ("production_canary_batch_size_must_be_one", checks.batch_size_one),
            ("production_capability_ttl_invalid", checks.short_ttl_valid),
            ("capability_store_unavailable", checks.one_shot_store_ready),
        ) if not passed
    ]
    return tuple(blockers)


class SealedSheetsCandidateTransport:
    """Exact Sheets append adapter whose dispatch is capability-store gated."""

    synthetic_only = False

    def __init__(self, db, *, binding: TargetBinding, inspector: TargetInspector,
                 capability: ProductionWriteCapability | None = None,
                 capability_store: SqliteCapabilityStore | None = None,
                 journal: SqliteAttemptJournal | None = None,
                 key_provider=None,
                 clock: Callable[[], datetime] = _utc_now):
        self._db = db
        self._binding = binding
        self._inspector = inspector
        self._capability = capability
        self._capability_store = capability_store
        self._journal = journal
        self._key_provider = key_provider
        self._clock = clock
        self.invocation_count = 0

    def write_once(self, candidate: CanonicalApplyCandidate, *,
                   attempt_id: str | None = None) -> WriteRequestResult:
        capability = self._capability
        if (
            type(capability) is not ProductionWriteCapability
            or type(self._capability_store) is not SqliteCapabilityStore
            or type(self._journal) is not SqliteAttemptJournal
            or type(self._key_provider) is not ProtectedAuditKeyProvider
            or not attempt_id
        ):
            raise RuntimeError("production_capability_required")
        key = _load_and_validate_key(self._key_provider)
        validate_target_binding(
            self._binding, self._inspector.inspect(self._binding.expected_worksheet),
        )
        if _target_ref(self._binding, key.secret) != capability.target_ref:
            raise RuntimeError("capability_target_mismatch")
        candidate_ref = _keyed_ref(key.secret, "canonical-item", candidate.identity)
        if candidate_ref != capability.candidate_ref:
            raise RuntimeError("candidate_not_in_capability")
        if candidate.transaction_kind != "purchase" or candidate.amount_yen <= 0:
            raise RuntimeError("candidate_not_write_eligible")
        history = self._journal.history(capability.run_id, attempt_id)
        if (
            not history
            or history[-1].stage != JournalStage.WRITE_ATTEMPTED
            or history[-1].canonical_identity != candidate.identity
        ):
            raise RuntimeError("write_attempt_journal_required")
        self._capability_store.authorize_dispatch(
            capability, attempt_id, self._clock(),
        )
        self.invocation_count += 1
        # Use the fixed approved range and RAW input so candidate text can never
        # be interpreted as a Sheets formula. No arbitrary range enters this API.
        self._db.svc.spreadsheets().values().append(
            spreadsheetId=self._binding.expected_spreadsheet_id,
            range=f"{self._binding.expected_worksheet}!A:L",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [candidate.to_import_row()]},
        ).execute()
        return WriteRequestResult(WriteDisposition.ACKNOWLEDGED, "sheets_append_ack")


def _execute_one_shot_canary(
    plan: CanonicalApplyPlan,
    manifest: ProductionRunManifest,
    *,
    capability: ProductionWriteCapability,
    capability_store: SqliteCapabilityStore,
    binding: TargetBinding,
    inspector: TargetInspector,
    key_provider,
    journal,
    leases,
    reader: CanonicalIdentityReader,
    transport,
    transport_mode: str,
    readback_policy: ReadBackPolicy,
    owner_id: str,
    lease_seconds: int = 300,
    clock: Callable[[], datetime] = _utc_now,
    sleeper: Callable[[float], None] = lambda _delay: None,
) -> WriterExecutionResult:
    """Shared one-candidate state machine; callers enforce transport authority."""
    lease = None
    terminal_reason = "execution_failed_closed"
    try:
        if transport_mode == "synthetic":
            if not getattr(transport, "synthetic_only", False):
                raise RuntimeError("synthetic_transport_required")
        elif transport_mode == "real":
            if type(transport) is not SealedSheetsCandidateTransport:
                raise RuntimeError("sealed_real_transport_required")
            if type(capability_store) is not SqliteCapabilityStore:
                raise RuntimeError("durable_capability_store_required")
            if type(journal) is not SqliteAttemptJournal:
                raise RuntimeError("durable_attempt_journal_required")
            if type(leases) is not SqliteLeaseManager:
                raise RuntimeError("durable_execution_lease_required")
            if type(key_provider) is not ProtectedAuditKeyProvider:
                raise RuntimeError("protected_audit_key_provider_required")
        else:
            raise RuntimeError("canary_transport_mode_invalid")
        key = _load_and_validate_key(key_provider)
        validate_canonical_apply_plan(plan)
        _validate_manifest(plan, manifest, key)
        readback_policy.validate()
        now = _aware_time(clock(), "capability_clock_timezone_required")
        if now >= capability.expires_at:
            raise RuntimeError("production_capability_expired")
        if manifest.authority_mode != "production_canary":
            raise RuntimeError("production_canary_authority_required")
        if len(plan.candidates) != 1 or manifest.candidate_count != 1:
            raise RuntimeError("production_canary_requires_exactly_one_candidate")
        if capability.run_id != manifest.run_id:
            raise RuntimeError("capability_run_mismatch")
        if capability.plan_binding_ref != manifest.plan_binding_ref:
            raise RuntimeError("capability_plan_mismatch")
        if not getattr(journal, "persistent", False) or not journal.ready(manifest.run_id):
            raise RuntimeError("attempt_journal_unavailable")
        validate_target_binding(binding, inspector.inspect(binding.expected_worksheet))
        target_ref = _target_ref(binding, key.secret)
        if target_ref != capability.target_ref:
            raise RuntimeError("capability_target_mismatch")
        candidate = plan.candidates[0]
        candidate_ref = _keyed_ref(key.secret, "canonical-item", candidate.identity)
        if candidate_ref != capability.candidate_ref:
            raise RuntimeError("candidate_not_in_capability")
        lease = leases.acquire(target_ref, owner_id, manifest.run_id, lease_seconds)
        if lease is None:
            raise RuntimeError("writer_lock_unavailable")
        renewed = leases.renew(lease, lease_seconds)
        if renewed is None:
            raise RuntimeError("writer_lease_lost")
        lease = renewed

        # This is the mandatory fresh pre-read. The one-candidate plan makes
        # selection independent of input order and structurally excludes a second item.
        from .aupay_card_executor import ExecutionSubset, execute_canonical_apply_plan
        preflight = execute_canonical_apply_plan(
            plan, reader, apply=False, audit_key=key.secret,
            audit_ref_scope="persistent_protected_key",
            subset=ExecutionSubset(limit=1),
        )
        if len(preflight.candidate_results) != 1:
            raise RuntimeError("production_canary_preread_incomplete")
        pre_result = preflight.candidate_results[0]
        if pre_result.audit_item_ref != capability.candidate_ref:
            raise RuntimeError("candidate_identity_recheck_failed")
        attempt_id = str(uuid4())
        batch_id = _keyed_ref(key.secret, "writer-batch", manifest.run_id, "canary-1")
        _journal_event(
            journal, manifest=manifest, candidate=candidate,
            attempt_id=attempt_id, batch_id=batch_id,
            stage=JournalStage.PRE_READ, state=pre_result.state,
            reason_code=pre_result.reason_code, clock=clock,
        )
        if pre_result.state != CandidateState.VERIFIED_NEW:
            final_state = pre_result.state
            if final_state not in {
                CandidateState.ALREADY_PRESENT, CandidateState.CONFLICT,
                CandidateState.FAILED,
            }:
                final_state = CandidateState.FAILED
            terminal_reason = pre_result.reason_code
            _journal_event(
                journal, manifest=manifest, candidate=candidate,
                attempt_id=attempt_id, batch_id=batch_id,
                stage=JournalStage.FINAL, state=final_state,
                reason_code=terminal_reason, clock=clock,
            )
            return WriterExecutionResult(
                status="canary_stopped", run_id=manifest.run_id, selected_count=1,
                write_request_count=0, confirmed_count=0,
                already_present_count=int(final_state == CandidateState.ALREADY_PRESENT),
                retry_eligible_count=0, outcome_unknown_count=0,
                conflict_count=int(final_state == CandidateState.CONFLICT),
                failed_count=int(final_state == CandidateState.FAILED),
                readback_count=0, reason_codes=(terminal_reason,),
                final_states=((capability.candidate_ref, final_state),),
            )

        capability_store.claim(capability, attempt_id, clock())
        _journal_event(
            journal, manifest=manifest, candidate=candidate,
            attempt_id=attempt_id, batch_id=batch_id,
            stage=JournalStage.WRITE_ATTEMPTED,
            state=CandidateState.WRITE_ATTEMPTED,
            reason_code="write_request_about_to_send", clock=clock,
        )
        try:
            request_result = transport.write_once(candidate, attempt_id=attempt_id)
        except Exception:
            request_result = WriteRequestResult(
                WriteDisposition.OUTCOME_UNKNOWN, "write_transport_outcome_unknown",
            )
        request_state = (
            CandidateState.OUTCOME_UNKNOWN
            if request_result.disposition == WriteDisposition.OUTCOME_UNKNOWN
            else CandidateState.WRITE_ATTEMPTED
        )
        _journal_event(
            journal, manifest=manifest, candidate=candidate,
            attempt_id=attempt_id, batch_id=batch_id,
            stage=JournalStage.WRITE_RESULT, state=request_state,
            reason_code=request_result.reason_code, clock=clock,
        )
        post_state, reason, reads, saw_absence = _bounded_readback(
            plan, candidate, reader, readback_policy, key.secret, sleeper,
        )
        if post_state == CandidateState.WRITE_CONFIRMED:
            final_state = CandidateState.WRITE_CONFIRMED
        elif post_state == CandidateState.CONFLICT:
            final_state = CandidateState.CONFLICT
        elif saw_absence and request_result.disposition in {
            WriteDisposition.OUTCOME_UNKNOWN, WriteDisposition.DEFINITELY_NOT_SENT,
        }:
            final_state = CandidateState.RETRY_ELIGIBLE
            reason = "write_absence_confirmed_retry_eligible"
        else:
            final_state = CandidateState.OUTCOME_UNKNOWN
        _journal_event(
            journal, manifest=manifest, candidate=candidate,
            attempt_id=attempt_id, batch_id=batch_id,
            stage=JournalStage.POST_READ, state=post_state,
            reason_code=reason, clock=clock,
        )
        _journal_event(
            journal, manifest=manifest, candidate=candidate,
            attempt_id=attempt_id, batch_id=batch_id,
            stage=JournalStage.FINAL, state=final_state,
            reason_code=reason, clock=clock,
        )
        terminal_reason = reason
        return WriterExecutionResult(
            status=("canary_complete" if final_state == CandidateState.WRITE_CONFIRMED
                    else "canary_stopped"),
            run_id=manifest.run_id, selected_count=1, write_request_count=1,
            confirmed_count=int(final_state == CandidateState.WRITE_CONFIRMED),
            already_present_count=0,
            retry_eligible_count=int(final_state == CandidateState.RETRY_ELIGIBLE),
            outcome_unknown_count=int(final_state == CandidateState.OUTCOME_UNKNOWN),
            conflict_count=int(final_state == CandidateState.CONFLICT),
            failed_count=0, readback_count=reads, reason_codes=(reason,),
            final_states=((capability.candidate_ref, final_state),),
        )
    finally:
        try:
            if lease is not None:
                leases.release(lease)
        finally:
            capability_store.reseal(capability, terminal_reason)


def execute_synthetic_one_shot_canary(*args, transport, **kwargs) -> WriterExecutionResult:
    """Exercise the complete capability path without an external write."""
    return _execute_one_shot_canary(
        *args, transport=transport, transport_mode="synthetic", **kwargs,
    )


def execute_production_one_shot_canary(
    *args, transport: SealedSheetsCandidateTransport, **kwargs,
) -> WriterExecutionResult:
    """The sole formal entry to the real Sheets one-shot transport."""
    return _execute_one_shot_canary(
        *args, transport=transport, transport_mode="real", **kwargs,
    )


@dataclass(frozen=True)
class PreCanaryManifest:
    schema_version: int
    run_id: str
    source_window_start: str
    source_window_end: str
    timezone_name: str
    query_representation: str
    target_binding_ref: str
    target_binding_version: int
    selected_candidate_count: int
    selection_audit_ref: str
    pre_read_classification: str
    withheld_count: int
    writer_safety_ready: bool
    missing_blockers: tuple[str, ...]
    external_write_count: int = 0


def build_precanary_manifest(
    plan: CanonicalApplyPlan,
    manifest: ProductionRunManifest,
    *,
    binding: TargetBinding,
    inspector: TargetInspector,
    key_provider,
    journal,
    leases,
    reader: CanonicalIdentityReader,
    owner_id: str,
) -> PreCanaryManifest:
    manifest.validate()
    blockers: list[str] = []
    target_valid = False
    try:
        validate_target_binding(binding, inspector.inspect(binding.expected_worksheet))
        target_valid = True
    except Exception:
        blockers.append("target_binding_invalid")
    key = None
    try:
        key = _load_and_validate_key(key_provider)
        _validate_manifest(plan, manifest, key)
    except Exception:
        blockers.append("persistent_audit_key_required")
    if not getattr(journal, "persistent", False) or not journal.ready(manifest.run_id):
        blockers.append("durable_journal_unavailable")
    if blockers or key is None or not target_valid:
        return PreCanaryManifest(
            PERSISTENCE_SCHEMA_VERSION, manifest.run_id,
            manifest.source_window.start.isoformat(),
            manifest.source_window.end.isoformat(),
            manifest.source_window.timezone_name,
            manifest.source_window.query_representation,
            "", manifest.target_binding_version, 0, "", "not_started",
            manifest.withheld_count, False, tuple(sorted(set(blockers))), 0,
        )
    try:
        result = preflight_writer(
            plan, manifest, binding=binding, inspector=inspector,
            key_provider=key_provider, journal=journal, leases=leases,
            reader=reader, batch_policy=BatchPolicy(requested_size=1),
            owner_id=owner_id,
        )
    except Exception:
        blockers.append("lease_or_preflight_unavailable")
        result = None
    ready = bool(result and result.status == "preflight_ready" and result.selected_count == 1)
    if not ready and not blockers:
        blockers.append("candidate_preflight_not_ready")
    candidate_result = result.dry_run.candidate_results[0] if ready else None
    return PreCanaryManifest(
        PERSISTENCE_SCHEMA_VERSION, manifest.run_id,
        manifest.source_window.start.isoformat(), manifest.source_window.end.isoformat(),
        manifest.source_window.timezone_name,
        manifest.source_window.query_representation,
        _keyed_ref(
            key.secret, "precanary-target", binding.expected_spreadsheet_id,
            binding.expected_worksheet, str(binding.binding_version),
        ),
        manifest.target_binding_version,
        result.selected_count if result else 0,
        candidate_result.audit_item_ref if candidate_result else "",
        candidate_result.state.value if candidate_result else "not_started",
        manifest.withheld_count,
        ready,
        tuple(sorted(set(blockers))),
        0,
    )


@dataclass(frozen=True)
class RestartRecoveryResult:
    run_id: str
    attempt_id: str
    state: CandidateState
    reason_code: str
    readback_count: int
    transport_invocation_count: int = 0


def recover_interrupted_attempt(
    plan: CanonicalApplyPlan,
    manifest: ProductionRunManifest,
    *,
    attempt_id: str,
    journal: SqliteAttemptJournal,
    reader: CanonicalIdentityReader,
    key_provider,
    readback_policy: ReadBackPolicy,
    clock: Callable[[], datetime] = _utc_now,
    sleeper: Callable[[float], None] = lambda _delay: None,
) -> RestartRecoveryResult:
    """Recover a persisted uncertain attempt using reads only; never resend."""
    key = _load_and_validate_key(key_provider)
    _validate_manifest(plan, manifest, key)
    history = journal.history(manifest.run_id, attempt_id)
    if not history:
        raise RuntimeError("recovery_attempt_not_found")
    last = history[-1]
    candidates = [
        candidate for candidate in plan.candidates
        if candidate.identity == last.canonical_identity
    ]
    if len(candidates) != 1:
        raise RuntimeError("recovery_candidate_not_in_plan")
    candidate = candidates[0]
    if last.stage == JournalStage.WRITE_ATTEMPTED:
        journal.append(JournalEvent(
            last.run_id, last.canonical_identity, last.attempt_id, last.batch_id,
            JournalStage.WRITE_RESULT, CandidateState.OUTCOME_UNKNOWN,
            clock(), "restart_write_outcome_unknown",
        ))
    elif last.stage != JournalStage.WRITE_RESULT:
        raise RuntimeError("recovery_state_not_uncertain")
    post_state, reason, reads, saw_absence = _bounded_readback(
        plan, candidate, reader, readback_policy, key.secret, sleeper,
    )
    if post_state == CandidateState.WRITE_CONFIRMED:
        final_state = CandidateState.WRITE_CONFIRMED
    elif post_state == CandidateState.CONFLICT:
        final_state = CandidateState.CONFLICT
    elif saw_absence:
        final_state = CandidateState.RETRY_ELIGIBLE
        reason = "restart_absence_confirmed_retry_eligible"
    else:
        final_state = CandidateState.OUTCOME_UNKNOWN
        reason = "restart_readback_unavailable"
    journal.append(JournalEvent(
        last.run_id, last.canonical_identity, last.attempt_id, last.batch_id,
        JournalStage.POST_READ, post_state, clock(), reason,
    ))
    journal.append(JournalEvent(
        last.run_id, last.canonical_identity, last.attempt_id, last.batch_id,
        JournalStage.FINAL, final_state, clock(), reason,
    ))
    return RestartRecoveryResult(
        manifest.run_id, attempt_id, final_state, reason, reads, 0,
    )
