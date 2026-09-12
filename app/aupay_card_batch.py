"""Exact, one-shot multi-item production batches for au PAY card Gmail data."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Callable, Mapping
from uuid import UUID, uuid4

from .aupay_card_apply_plan import (
    CanonicalApplyCandidate,
    CanonicalApplyPlan,
    project_candidate_batch_plan,
    validate_canonical_apply_plan,
)
from .aupay_card_production import (
    ProtectedAuditKeyProvider,
    SqliteLeaseManager,
    target_binding_reference,
)
from .aupay_card_writer import (
    FixedSourceWindow,
    PersistentAuditKey,
    ReadOnlySheetsTargetInspector,
    TargetBinding,
    canonical_candidate_reference_v2,
    full_plan_binding_reference_v2,
    validate_target_binding,
)


BATCH_SCHEMA_VERSION = 1
MAX_BATCH_SIZE = 100
MAX_CAPABILITY_TTL_SECONDS = 300
ALLOWED_STATUSES = {
    "auto_expense", "matched_receipt", "transfer_aupay_charge", "matched_amazon",
}
_SAFE_REASON = re.compile(r"[a-z0-9_]{1,96}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _external(path, repo_root) -> Path:
    value = Path(path).expanduser().resolve()
    root = Path(repo_root).resolve()
    try:
        value.relative_to(root)
    except ValueError:
        pass
    else:
        raise RuntimeError("batch_state_must_be_outside_repository")
    value.parent.mkdir(parents=True, exist_ok=True)
    return value


def _payload_ref(key: bytes, namespace: str, payload: object) -> str:
    body = json.dumps(
        {"namespace": namespace, "schema_version": BATCH_SCHEMA_VERSION, "payload": payload},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return f"{namespace}-v1:{hmac.new(key, body, hashlib.sha256).hexdigest()[:32]}"


def _digest(value: object) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BatchProjection:
    source_plan: CanonicalApplyPlan = field(repr=False)
    plan: CanonicalApplyPlan = field(repr=False)
    candidate_identities: tuple[str, ...]
    candidate_refs: tuple[str, ...]
    statuses: tuple[str, ...]
    target_ref: str
    full_plan_binding_ref: str
    projected_plan_binding_ref: str
    batch_ref: str
    batch_size: int
    schema_version: int = BATCH_SCHEMA_VERSION


def create_batch_projection(
    full_plan: CanonicalApplyPlan, *, candidate_identities: tuple[str, ...],
    statuses: Mapping[str, str], binding: TargetBinding, audit_key: PersistentAuditKey,
) -> BatchProjection:
    validate_canonical_apply_plan(full_plan)
    binding.validate()
    audit_key.validate()
    plan = project_candidate_batch_plan(full_plan, candidate_identities)
    bound_statuses = tuple(str(statuses.get(identity, "")) for identity in candidate_identities)
    if any(status not in ALLOWED_STATUSES for status in bound_statuses):
        raise RuntimeError("batch_candidate_status_not_production_eligible")
    refs = tuple(canonical_candidate_reference_v2(candidate, audit_key) for candidate in plan.candidates)
    target_ref = target_binding_reference(binding, audit_key)
    full_ref = full_plan_binding_reference_v2(full_plan, audit_key)
    projected_ref = full_plan_binding_reference_v2(plan, audit_key)
    batch_ref = _payload_ref(audit_key.secret, "writer-batch", {
        "candidate_refs": list(refs), "statuses": list(bound_statuses),
        "target_ref": target_ref, "full_plan_binding_ref": full_ref,
        "projected_plan_binding_ref": projected_ref, "batch_size": len(refs),
    })
    return BatchProjection(
        full_plan, plan, candidate_identities, refs, bound_statuses, target_ref,
        full_ref, projected_ref, batch_ref, len(refs),
    )


def validate_batch_projection(
    value: object, *, binding: TargetBinding, audit_key: PersistentAuditKey,
) -> BatchProjection:
    if type(value) is not BatchProjection:
        raise TypeError("batch_projection_required")
    if value.schema_version != BATCH_SCHEMA_VERSION or not (1 <= value.batch_size <= MAX_BATCH_SIZE):
        raise RuntimeError("batch_projection_schema_invalid")
    expected = create_batch_projection(
        value.source_plan, candidate_identities=value.candidate_identities,
        statuses=dict(zip(value.candidate_identities, value.statuses)),
        binding=binding, audit_key=audit_key,
    )
    if value != expected:
        raise RuntimeError("batch_projection_binding_mismatch")
    return value


@dataclass(frozen=True)
class BatchManifest:
    run_id: str
    source_window: FixedSourceWindow
    created_at: datetime
    audit_key_id: str
    batch_ref: str
    target_ref: str
    full_plan_binding_ref: str
    projected_plan_binding_ref: str
    candidate_refs: tuple[str, ...]
    statuses: tuple[str, ...]
    batch_size: int
    schema_version: int = BATCH_SCHEMA_VERSION

    def validate(self) -> None:
        try:
            UUID(self.run_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("batch_manifest_run_id_invalid") from exc
        self.source_window.validate()
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise RuntimeError("batch_manifest_timezone_required")
        if self.schema_version != BATCH_SCHEMA_VERSION or not (1 <= self.batch_size <= MAX_BATCH_SIZE):
            raise RuntimeError("batch_manifest_schema_invalid")
        if len(self.candidate_refs) != self.batch_size or len(self.statuses) != self.batch_size:
            raise RuntimeError("batch_manifest_count_invalid")
        if any(not re.fullmatch(r"canonical-item-v2:[0-9a-f]{32}", ref) for ref in self.candidate_refs):
            raise RuntimeError("batch_manifest_candidate_ref_invalid")
        if not re.fullmatch(r"writer-target-v1:[0-9a-f]{32}", self.target_ref):
            raise RuntimeError("batch_manifest_target_ref_invalid")
        if not re.fullmatch(r"writer-batch-v1:[0-9a-f]{32}", self.batch_ref):
            raise RuntimeError("batch_manifest_batch_ref_invalid")


def create_batch_manifest(
    projection: BatchProjection, *, source_window: FixedSourceWindow,
    created_at: datetime, run_id: str, audit_key: PersistentAuditKey,
    binding: TargetBinding,
) -> BatchManifest:
    validate_batch_projection(projection, binding=binding, audit_key=audit_key)
    manifest = BatchManifest(
        run_id, source_window, created_at, audit_key.key_id, projection.batch_ref,
        projection.target_ref, projection.full_plan_binding_ref,
        projection.projected_plan_binding_ref, projection.candidate_refs,
        projection.statuses, projection.batch_size,
    )
    manifest.validate()
    return manifest


def validate_batch_manifest(
    projection: BatchProjection, manifest: BatchManifest, *,
    binding: TargetBinding, audit_key: PersistentAuditKey,
) -> None:
    validate_batch_projection(projection, binding=binding, audit_key=audit_key)
    manifest.validate()
    expected = create_batch_manifest(
        projection, source_window=manifest.source_window, created_at=manifest.created_at,
        run_id=manifest.run_id, audit_key=audit_key, binding=binding,
    )
    if manifest != expected:
        raise RuntimeError("batch_manifest_binding_mismatch")


class SqliteBatchManifestStore:
    def __init__(self, path, *, repo_root):
        self.path = _external(path, repo_root)
        with sqlite3.connect(self.path) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS batch_manifests (
                run_id TEXT PRIMARY KEY, payload TEXT NOT NULL, payload_hash TEXT NOT NULL
            )""")
            connection.execute("""CREATE TRIGGER IF NOT EXISTS batch_manifest_no_update
                BEFORE UPDATE ON batch_manifests BEGIN SELECT RAISE(ABORT,'immutable_manifest'); END""")
            connection.execute("""CREATE TRIGGER IF NOT EXISTS batch_manifest_no_delete
                BEFORE DELETE ON batch_manifests BEGIN SELECT RAISE(ABORT,'immutable_manifest'); END""")

    @staticmethod
    def _serialize(value: BatchManifest) -> str:
        return json.dumps({
            "schema_version": value.schema_version, "run_id": value.run_id,
            "source_window": {
                "start": value.source_window.start.isoformat(),
                "end": value.source_window.end.isoformat(),
                "timezone_name": value.source_window.timezone_name,
                "query_representation": value.source_window.query_representation,
            },
            "created_at": value.created_at.isoformat(), "audit_key_id": value.audit_key_id,
            "batch_ref": value.batch_ref, "target_ref": value.target_ref,
            "full_plan_binding_ref": value.full_plan_binding_ref,
            "projected_plan_binding_ref": value.projected_plan_binding_ref,
            "candidate_refs": list(value.candidate_refs), "statuses": list(value.statuses),
            "batch_size": value.batch_size,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def save(self, value: BatchManifest) -> None:
        value.validate()
        payload = self._serialize(value)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload,payload_hash FROM batch_manifests WHERE run_id=?", (value.run_id,),
            ).fetchone()
            if row:
                if row != (payload, digest):
                    raise RuntimeError("batch_manifest_immutable_conflict")
                return
            connection.execute("INSERT INTO batch_manifests VALUES (?,?,?)", (value.run_id, payload, digest))

    def load(self, run_id: str) -> BatchManifest:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload,payload_hash FROM batch_manifests WHERE run_id=?", (run_id,),
            ).fetchone()
        if not row or hashlib.sha256(row[0].encode("utf-8")).hexdigest() != row[1]:
            raise RuntimeError("batch_manifest_corrupt_or_missing")
        raw = json.loads(row[0])
        window = raw["source_window"]
        value = BatchManifest(
            run_id=raw["run_id"],
            source_window=FixedSourceWindow(
                datetime.fromisoformat(window["start"]), datetime.fromisoformat(window["end"]),
                window["timezone_name"], window["query_representation"],
            ),
            created_at=datetime.fromisoformat(raw["created_at"]),
            audit_key_id=raw["audit_key_id"], batch_ref=raw["batch_ref"],
            target_ref=raw["target_ref"], full_plan_binding_ref=raw["full_plan_binding_ref"],
            projected_plan_binding_ref=raw["projected_plan_binding_ref"],
            candidate_refs=tuple(raw["candidate_refs"]), statuses=tuple(raw["statuses"]),
            batch_size=int(raw["batch_size"]), schema_version=int(raw["schema_version"]),
        )
        value.validate()
        return value


@dataclass(frozen=True)
class BatchApproval:
    approval_reference: str
    batch_ref: str
    target_ref: str
    batch_size: int
    expires_at: datetime


class ProtectedBatchApprovalProvider:
    def __init__(self, path, *, repo_root):
        self.path = _external(path, repo_root)

    def __repr__(self) -> str:
        return "ProtectedBatchApprovalProvider(path=<redacted>)"

    def load(self) -> BatchApproval:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            value = BatchApproval(
                str(raw["approval_reference"]), str(raw["batch_ref"]),
                str(raw["target_ref"]), int(raw["batch_size"]),
                datetime.fromisoformat(str(raw["expires_at"])),
            )
        except Exception as exc:
            raise RuntimeError("protected_batch_approval_invalid") from exc
        if (
            not value.approval_reference or len(value.approval_reference) > 256
            or not re.fullmatch(r"writer-batch-v1:[0-9a-f]{32}", value.batch_ref)
            or not re.fullmatch(r"writer-target-v1:[0-9a-f]{32}", value.target_ref)
            or not (1 <= value.batch_size <= MAX_BATCH_SIZE)
            or value.expires_at.tzinfo is None or value.expires_at.utcoffset() is None
        ):
            raise RuntimeError("protected_batch_approval_invalid")
        return value


@dataclass(frozen=True)
class RecurringAuthorityPolicy:
    """Narrow standing authority for ordinary au PAY card Gmail arrivals."""

    policy_id: str
    source: str
    expected_spreadsheet_id: str = field(repr=False)
    expected_worksheet: str = "取込データ"
    allowed_statuses: tuple[str, ...] = ()
    max_batch_size: int = 0
    max_messages: int = 0
    overlap_seconds: int = 0
    max_window_seconds: int = 0
    initial_start: datetime = field(default_factory=_now)
    valid_from: datetime = field(default_factory=_now)
    expires_at: datetime = field(default_factory=_now)
    schema_version: int = BATCH_SCHEMA_VERSION

    def validate(self) -> None:
        times = (self.initial_start, self.valid_from, self.expires_at)
        if any(value.tzinfo is None or value.utcoffset() is None for value in times):
            raise RuntimeError("recurring_authority_timezone_required")
        if (
            self.schema_version != BATCH_SCHEMA_VERSION
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", self.policy_id)
            or self.source != "au_pay_card_gmail"
            or not self.expected_spreadsheet_id
            or self.expected_worksheet != "取込データ"
            or not self.allowed_statuses
            or not set(self.allowed_statuses).issubset(ALLOWED_STATUSES)
            or len(self.allowed_statuses) != len(set(self.allowed_statuses))
            or not (1 <= self.max_batch_size <= MAX_BATCH_SIZE)
            or not (self.max_batch_size <= self.max_messages <= 1000)
            or not (3600 <= self.overlap_seconds <= 7 * 86400)
            or not (self.overlap_seconds < self.max_window_seconds <= 31 * 86400)
            or self.valid_from >= self.expires_at
        ):
            raise RuntimeError("recurring_authority_invalid")


class ProtectedRecurringAuthorityProvider:
    """Load a repo-external, source/target/status/bounds-limited policy."""

    def __init__(self, path, *, repo_root):
        self.path = _external(path, repo_root)

    def __repr__(self) -> str:
        return "ProtectedRecurringAuthorityProvider(path=<redacted>)"

    def load(self) -> RecurringAuthorityPolicy:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            value = RecurringAuthorityPolicy(
                policy_id=str(raw["policy_id"]), source=str(raw["source"]),
                expected_spreadsheet_id=str(raw["expected_spreadsheet_id"]),
                expected_worksheet=str(raw.get("expected_worksheet", "取込データ")),
                allowed_statuses=tuple(str(item) for item in raw["allowed_statuses"]),
                max_batch_size=int(raw["max_batch_size"]),
                max_messages=int(raw["max_messages"]),
                overlap_seconds=int(raw["overlap_seconds"]),
                max_window_seconds=int(raw["max_window_seconds"]),
                initial_start=datetime.fromisoformat(str(raw["initial_start"])),
                valid_from=datetime.fromisoformat(str(raw["valid_from"])),
                expires_at=datetime.fromisoformat(str(raw["expires_at"])),
                schema_version=int(raw.get("schema_version", BATCH_SCHEMA_VERSION)),
            )
            value.validate()
            return value
        except Exception as exc:
            raise RuntimeError("protected_recurring_authority_invalid") from exc


@dataclass(frozen=True)
class BatchCapability:
    capability_id: str
    token: str = field(repr=False)
    run_id: str = ""
    batch_ref: str = ""
    target_ref: str = ""
    approval_reference: str = field(default="", repr=False)
    issued_at: datetime = field(default_factory=_now)
    expires_at: datetime = field(default_factory=_now)


class SqliteBatchCapabilityStore:
    def __init__(self, path, *, repo_root):
        self.path = _external(path, repo_root)
        with sqlite3.connect(self.path) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS batch_capabilities (
                capability_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL UNIQUE, batch_ref TEXT NOT NULL UNIQUE,
                target_ref TEXT NOT NULL, approval_reference TEXT NOT NULL,
                issued_at TEXT NOT NULL, expires_at TEXT NOT NULL, state TEXT NOT NULL,
                attempt_id TEXT UNIQUE, terminal_reason TEXT NOT NULL DEFAULT ''
            )""")

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def issue(self, value: BatchCapability) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO batch_capabilities VALUES (?,?,?,?,?,?,?,?, 'issued',NULL,'')",
                (value.capability_id, self._token_hash(value.token), value.run_id,
                 value.batch_ref, value.target_ref, value.approval_reference,
                 value.issued_at.isoformat(), value.expires_at.isoformat()),
            )

    def _row(self, value: BatchCapability, connection):
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM batch_capabilities WHERE capability_id=?", (value.capability_id,),
        ).fetchone()
        if row is None or row["token_hash"] != self._token_hash(value.token):
            raise RuntimeError("batch_capability_invalid")
        expected = (value.run_id, value.batch_ref, value.target_ref, value.approval_reference,
                    value.issued_at.isoformat(), value.expires_at.isoformat())
        actual = tuple(row[name] for name in (
            "run_id", "batch_ref", "target_ref", "approval_reference", "issued_at", "expires_at",
        ))
        if actual != expected:
            raise RuntimeError("batch_capability_binding_mismatch")
        return row

    def claim(self, value: BatchCapability, attempt_id: str, now: datetime) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(value, connection)
            if row["state"] != "issued" or datetime.fromisoformat(row["expires_at"]) <= now:
                raise RuntimeError("batch_capability_reused_or_expired")
            connection.execute(
                "UPDATE batch_capabilities SET state='claimed',attempt_id=? WHERE capability_id=?",
                (attempt_id, value.capability_id),
            )
            connection.commit()

    def authorize(self, value: BatchCapability, attempt_id: str, now: datetime) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(value, connection)
            if (
                row["state"] != "claimed" or row["attempt_id"] != attempt_id
                or datetime.fromisoformat(row["expires_at"]) <= now
            ):
                raise RuntimeError("batch_capability_reused_or_expired")
            connection.execute(
                "UPDATE batch_capabilities SET state='dispatching' WHERE capability_id=?",
                (value.capability_id,),
            )
            connection.commit()

    def seal(self, value: BatchCapability, reason: str) -> None:
        safe = reason if _SAFE_REASON.fullmatch(reason) else "batch_execution_ended"
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE batch_capabilities SET state='sealed',terminal_reason=? WHERE capability_id=?",
                (safe, value.capability_id),
            )

    def state(self, capability_id: str) -> str:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT state FROM batch_capabilities WHERE capability_id=?", (capability_id,),
            ).fetchone()
        return str(row[0]) if row else ""


def issue_batch_capability(
    projection: BatchProjection, manifest: BatchManifest, *, binding: TargetBinding,
    inspector: ReadOnlySheetsTargetInspector, key_provider: ProtectedAuditKeyProvider,
    approval_provider: ProtectedBatchApprovalProvider,
    store: SqliteBatchCapabilityStore, now: datetime | None = None,
) -> BatchCapability:
    now = now or _now()
    if (
        type(key_provider) is not ProtectedAuditKeyProvider
        or type(approval_provider) is not ProtectedBatchApprovalProvider
        or type(store) is not SqliteBatchCapabilityStore
    ):
        raise RuntimeError("formal_batch_authority_required")
    key = key_provider.load()
    if key is None:
        raise RuntimeError("persistent_audit_key_required")
    validate_batch_manifest(projection, manifest, binding=binding, audit_key=key)
    validate_target_binding(binding, inspector.inspect(binding.expected_worksheet))
    approval = approval_provider.load()
    ttl = (approval.expires_at - now).total_seconds()
    if (
        approval.batch_ref != projection.batch_ref
        or approval.target_ref != projection.target_ref
        or approval.batch_size != projection.batch_size
        or ttl <= 0 or ttl > MAX_CAPABILITY_TTL_SECONDS
    ):
        raise RuntimeError("batch_approval_binding_or_ttl_invalid")
    capability = BatchCapability(
        str(uuid4()), hashlib.sha256(os.urandom(32)).hexdigest(), manifest.run_id,
        projection.batch_ref, projection.target_ref, approval.approval_reference,
        now, approval.expires_at,
    )
    store.issue(capability)
    return capability


def issue_recurring_batch_capability(
    projection: BatchProjection, manifest: BatchManifest, *, binding: TargetBinding,
    inspector: ReadOnlySheetsTargetInspector,
    key_provider: ProtectedAuditKeyProvider,
    authority_provider: ProtectedRecurringAuthorityProvider,
    store: SqliteBatchCapabilityStore, now: datetime | None = None,
) -> BatchCapability:
    """Issue one short-lived capability under a bounded recurring policy."""
    now = now or _now()
    if (
        type(key_provider) is not ProtectedAuditKeyProvider
        or type(authority_provider) is not ProtectedRecurringAuthorityProvider
        or type(store) is not SqliteBatchCapabilityStore
    ):
        raise RuntimeError("formal_recurring_authority_required")
    key = key_provider.load()
    if key is None:
        raise RuntimeError("persistent_audit_key_required")
    policy = authority_provider.load()
    validate_batch_manifest(projection, manifest, binding=binding, audit_key=key)
    validate_target_binding(binding, inspector.inspect(binding.expected_worksheet))
    window_seconds = (manifest.source_window.end - manifest.source_window.start).total_seconds()
    if (
        not (policy.valid_from <= now < policy.expires_at)
        or binding.expected_spreadsheet_id != policy.expected_spreadsheet_id
        or binding.expected_worksheet != policy.expected_worksheet
        or projection.batch_size > policy.max_batch_size
        or not set(projection.statuses).issubset(policy.allowed_statuses)
        or manifest.source_window.timezone_name != "Asia/Tokyo"
        or window_seconds > policy.max_window_seconds
        or 'from:kddi-fs.com' not in manifest.source_window.query_representation
        or 'subject:"【ご利用詳細】au PAY カード"' not in manifest.source_window.query_representation
    ):
        raise RuntimeError("recurring_authority_scope_mismatch")
    capability = BatchCapability(
        str(uuid4()), hashlib.sha256(os.urandom(32)).hexdigest(), manifest.run_id,
        projection.batch_ref, projection.target_ref,
        f"recurring:{policy.policy_id}", now,
        now + timedelta(seconds=min(120, MAX_CAPABILITY_TTL_SECONDS)),
    )
    store.issue(capability)
    return capability


class SqliteBatchJournal:
    def __init__(self, path, *, repo_root):
        self.path = _external(path, repo_root)
        with sqlite3.connect(self.path) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS batch_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL, batch_ref TEXT NOT NULL, stage TEXT NOT NULL,
                state TEXT NOT NULL, reason TEXT NOT NULL, identity_count INTEGER NOT NULL,
                candidate_digest TEXT NOT NULL, timestamp TEXT NOT NULL,
                UNIQUE(run_id,attempt_id,stage)
            )""")
            connection.execute("""CREATE TRIGGER IF NOT EXISTS batch_journal_no_update
                BEFORE UPDATE ON batch_events BEGIN SELECT RAISE(ABORT,'append_only_journal'); END""")
            connection.execute("""CREATE TRIGGER IF NOT EXISTS batch_journal_no_delete
                BEFORE DELETE ON batch_events BEGIN SELECT RAISE(ABORT,'append_only_journal'); END""")

    def append(self, manifest: BatchManifest, attempt_id: str, stage: str,
               state: str, reason: str, timestamp: datetime) -> None:
        if stage not in {"pre_read", "write_attempted", "write_result", "post_read", "final"}:
            raise RuntimeError("batch_journal_stage_invalid")
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO batch_events (run_id,attempt_id,batch_ref,stage,state,reason,"
                "identity_count,candidate_digest,timestamp) VALUES (?,?,?,?,?,?,?,?,?)",
                (manifest.run_id, attempt_id, manifest.batch_ref, stage, state, reason,
                 manifest.batch_size, _digest(manifest.candidate_refs), timestamp.isoformat()),
            )

    def history(self, run_id: str, attempt_id: str) -> tuple[tuple, ...]:
        with sqlite3.connect(self.path) as connection:
            return tuple(connection.execute(
                "SELECT stage,state,reason,identity_count,candidate_digest FROM batch_events "
                "WHERE run_id=? AND attempt_id=? ORDER BY seq", (run_id, attempt_id),
            ).fetchall())


class SealedSheetsBatchTransport:
    def __init__(self, db, *, projection: BatchProjection, manifest: BatchManifest,
                 capability: BatchCapability, store: SqliteBatchCapabilityStore,
                 journal: SqliteBatchJournal, binding: TargetBinding,
                 inspector: ReadOnlySheetsTargetInspector,
                 key_provider: ProtectedAuditKeyProvider,
                 clock: Callable[[], datetime] = _now):
        self.db = db
        self.projection = projection
        self.manifest = manifest
        self.capability = capability
        self.store = store
        self.journal = journal
        self.binding = binding
        self.inspector = inspector
        self.key_provider = key_provider
        self.clock = clock
        self.invocation_count = 0
        self.dispatched_rows: tuple[tuple, ...] = ()

    def write_once(self, *, attempt_id: str) -> None:
        key = self.key_provider.load()
        if key is None:
            raise RuntimeError("persistent_audit_key_required")
        validate_batch_manifest(
            self.projection, self.manifest, binding=self.binding, audit_key=key,
        )
        validate_target_binding(self.binding, self.inspector.inspect(self.binding.expected_worksheet))
        if self.capability.batch_ref != self.projection.batch_ref:
            raise RuntimeError("batch_capability_projection_mismatch")
        history = self.journal.history(self.manifest.run_id, attempt_id)
        if not history or history[-1][0] != "write_attempted":
            raise RuntimeError("batch_write_attempt_journal_required")
        dispatch_time = self.clock()
        self.store.authorize(self.capability, attempt_id, dispatch_time)
        rows = tuple(
            tuple(candidate.to_import_row(imported_at=dispatch_time, status=status))
            for candidate, status in zip(self.projection.plan.candidates, self.projection.statuses)
        )
        self.dispatched_rows = rows
        self.invocation_count += 1
        self.db.svc.spreadsheets().values().append(
            spreadsheetId=self.binding.expected_spreadsheet_id,
            range=f"{self.binding.expected_worksheet}!A:L",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [list(row) for row in rows]},
        ).execute()


@dataclass(frozen=True)
class BatchExecutionResult:
    status: str
    run_id: str
    attempt_id: str
    selected_count: int
    confirmed_count: int
    write_request_count: int
    readback_count: int
    exact: bool


def _normalize_row(raw) -> tuple[str, ...]:
    return tuple(str(value) for value in (list(raw) + [""] * 12)[:12])


def execute_production_batch_once(
    projection: BatchProjection, manifest: BatchManifest, *,
    capability: BatchCapability, store: SqliteBatchCapabilityStore,
    journal: SqliteBatchJournal, leases: SqliteLeaseManager,
    reader: Callable[[tuple[str, ...]], Mapping[str, tuple[tuple, ...]]],
    transport: SealedSheetsBatchTransport, binding: TargetBinding,
    inspector: ReadOnlySheetsTargetInspector,
    key_provider: ProtectedAuditKeyProvider, owner_id: str,
    readback_attempts: int = 3, readback_delays: tuple[float, ...] = (1.0, 2.0),
    clock: Callable[[], datetime] = _now,
    sleeper: Callable[[float], None] = time.sleep,
) -> BatchExecutionResult:
    if (
        type(store) is not SqliteBatchCapabilityStore
        or type(journal) is not SqliteBatchJournal
        or type(leases) is not SqliteLeaseManager
        or type(transport) is not SealedSheetsBatchTransport
        or type(key_provider) is not ProtectedAuditKeyProvider
    ):
        raise RuntimeError("formal_batch_authority_required")
    if readback_attempts < 1 or len(readback_delays) != readback_attempts - 1:
        raise ValueError("batch_readback_policy_invalid")
    key = key_provider.load()
    if key is None:
        raise RuntimeError("persistent_audit_key_required")
    validate_batch_manifest(projection, manifest, binding=binding, audit_key=key)
    validate_target_binding(binding, inspector.inspect(binding.expected_worksheet))
    if capability.run_id != manifest.run_id or capability.batch_ref != projection.batch_ref:
        raise RuntimeError("batch_capability_binding_mismatch")
    identities = projection.candidate_identities
    attempt_id = str(uuid4())
    lease = None
    terminal_reason = "batch_execution_failed_closed"
    writes = reads = 0
    try:
        lease = leases.acquire(projection.target_ref, owner_id, manifest.run_id, 300)
        if lease is None:
            raise RuntimeError("writer_lock_unavailable")
        before = reader(identities)
        reads += 1
        if set(before) != set(identities) or any(before[identity] for identity in identities):
            raise RuntimeError("batch_fresh_preread_not_all_absent")
        journal.append(manifest, attempt_id, "pre_read", "verified", "all_identities_absent", clock())
        store.claim(capability, attempt_id, clock())
        journal.append(manifest, attempt_id, "write_attempted", "attempted", "batch_about_to_append", clock())
        try:
            transport.write_once(attempt_id=attempt_id)
            writes = 1
            journal.append(manifest, attempt_id, "write_result", "acknowledged", "sheets_append_ack", clock())
        except Exception:
            writes = transport.invocation_count
            journal.append(manifest, attempt_id, "write_result", "outcome_unknown", "batch_transport_outcome_unknown", clock())
        exact = False
        for read_index in range(readback_attempts):
            reads += 1
            try:
                observed = reader(identities)
            except Exception:
                observed = {}
            exact = (
                len(transport.dispatched_rows) == projection.batch_size
                and set(observed) == set(identities)
                and all(
                len(observed[identity]) == 1
                and _normalize_row(observed[identity][0]) == _normalize_row(expected)
                for identity, expected in zip(identities, transport.dispatched_rows)
                )
            )
            if exact:
                break
            if read_index < len(readback_delays):
                sleeper(readback_delays[read_index])
        journal.append(
            manifest, attempt_id, "post_read", "confirmed" if exact else "outcome_unknown",
            "batch_readback_exact" if exact else "batch_readback_unresolved", clock(),
        )
        terminal_reason = "batch_readback_exact" if exact else "batch_readback_unresolved"
        journal.append(
            manifest, attempt_id, "final", "confirmed" if exact else "outcome_unknown",
            terminal_reason, clock(),
        )
        return BatchExecutionResult(
            "batch_complete" if exact else "batch_stopped", manifest.run_id,
            attempt_id, projection.batch_size, projection.batch_size if exact else 0,
            writes, reads, exact,
        )
    except Exception:
        history = journal.history(manifest.run_id, attempt_id)
        if not history or history[-1][0] != "final":
            journal.append(
                manifest, attempt_id, "final", "failed",
                "batch_execution_failed_closed", clock(),
            )
        raise
    finally:
        store.seal(capability, terminal_reason)
        if lease is not None:
            leases.release(lease)
