"""Exact, one-shot authority for repairing an existing au PAY card canary row."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Callable
from uuid import UUID, uuid4

from .aupay_card_production import (
    ProtectedAuditKeyProvider,
    SqliteLeaseManager,
)
from .aupay_card_writer import PersistentAuditKey, TargetBinding, validate_target_binding


REPAIR_COLUMNS = ((1, "B"), (7, "H"), (8, "I"), (10, "K"))
REPAIR_SCHEMA_VERSION = 1
MAX_REPAIR_TTL_SECONDS = 300


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _external_path(path, repo_root) -> Path:
    value = Path(path).expanduser().resolve()
    root = Path(repo_root).resolve()
    if not value.is_absolute():
        raise RuntimeError("repair_state_path_must_be_absolute")
    try:
        value.relative_to(root)
    except ValueError:
        pass
    else:
        raise RuntimeError("repair_state_must_be_outside_repository")
    value.parent.mkdir(parents=True, exist_ok=True)
    return value


def _row(values) -> tuple:
    return tuple((list(values) + [""] * 12)[:12])


def row_digest(values) -> str:
    body = json.dumps(_row(values), ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RepairPreview:
    run_id: str
    row_number: int
    identity: str
    reason: str
    candidate_ref: str
    target_ref: str
    before_row: tuple
    after_row: tuple
    before_digest: str
    after_digest: str
    changed_columns: tuple[str, ...]
    schema_version: int = REPAIR_SCHEMA_VERSION

    def validate(self) -> None:
        try:
            UUID(self.run_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("repair_preview_run_id_invalid") from exc
        if self.schema_version != REPAIR_SCHEMA_VERSION or self.row_number < 2:
            raise RuntimeError("repair_preview_schema_invalid")
        if (
            not re.fullmatch(r"[a-z0-9_]{1,64}", self.reason)
            or not re.fullmatch(r"canonical-item-v2:[0-9a-f]{32}", self.candidate_ref)
            or not re.fullmatch(r"writer-target-v1:[0-9a-f]{32}", self.target_ref)
        ):
            raise RuntimeError("repair_preview_binding_invalid")
        if len(self.before_row) != 12 or len(self.after_row) != 12:
            raise RuntimeError("repair_preview_row_width_invalid")
        if self.before_row[0] != self.identity or self.after_row[0] != self.identity:
            raise RuntimeError("repair_preview_identity_mismatch")
        actual = tuple(
            chr(ord("A") + index)
            for index, (before, after) in enumerate(zip(self.before_row, self.after_row))
            if before != after
        )
        if actual != self.changed_columns or actual != tuple(column for _, column in REPAIR_COLUMNS):
            raise RuntimeError("repair_preview_changed_columns_invalid")
        if self.before_digest != row_digest(self.before_row):
            raise RuntimeError("repair_preview_before_digest_invalid")
        if self.after_digest != row_digest(self.after_row):
            raise RuntimeError("repair_preview_after_digest_invalid")
        if self.before_row[1] or not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", str(self.after_row[1])):
            raise RuntimeError("repair_preview_import_timestamp_invalid")
        if self.before_row[7] != "メール通知" or self.after_row[7] != "通常払い":
            raise RuntimeError("repair_preview_payment_invalid")
        if self.before_row[8] != "unclassified_card" or self.after_row[8] != "auto_expense":
            raise RuntimeError("repair_preview_status_invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", str(self.after_row[10])):
            raise RuntimeError("repair_preview_source_hash_invalid")


def create_repair_preview(
    *, run_id: str, row_number: int, identity: str, reason: str,
    candidate_ref: str, target_ref: str, before_row, corrected_timestamp: str,
    corrected_source_hash: str,
) -> RepairPreview:
    before = _row(before_row)
    after = list(before)
    after[1] = corrected_timestamp
    after[7] = "通常払い"
    after[8] = "auto_expense"
    after[10] = corrected_source_hash
    after = tuple(after)
    preview = RepairPreview(
        run_id, row_number, identity, reason, candidate_ref, target_ref,
        before, after, row_digest(before), row_digest(after),
        tuple(column for _, column in REPAIR_COLUMNS),
    )
    preview.validate()
    return preview


def repair_reference(preview: RepairPreview, key: PersistentAuditKey) -> str:
    preview.validate()
    key.validate()
    payload = {
        "schema_version": preview.schema_version,
        "run_id": preview.run_id,
        "row_number": preview.row_number,
        "identity": preview.identity,
        "reason": preview.reason,
        "candidate_ref": preview.candidate_ref,
        "target_ref": preview.target_ref,
        "before_digest": preview.before_digest,
        "after_digest": preview.after_digest,
        "changed_columns": list(preview.changed_columns),
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hmac.new(key.secret, body.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return f"aupay-card-repair-v1:{digest}"


@dataclass(frozen=True)
class RepairApproval:
    approval_reference: str
    repair_ref: str
    target_ref: str
    expires_at: datetime


class ProtectedRepairApprovalProvider:
    def __init__(self, path, *, repo_root):
        self.path = _external_path(path, repo_root)

    def __repr__(self) -> str:
        return "ProtectedRepairApprovalProvider(path=<redacted>)"

    def load(self) -> RepairApproval:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            approval = RepairApproval(
                str(raw["approval_reference"]), str(raw["repair_ref"]),
                str(raw["target_ref"]), datetime.fromisoformat(str(raw["expires_at"])),
            )
        except Exception as exc:
            raise RuntimeError("protected_repair_approval_invalid") from exc
        if approval.expires_at.tzinfo is None or approval.expires_at.utcoffset() is None:
            raise RuntimeError("repair_approval_timezone_required")
        if (
            not approval.approval_reference
            or len(approval.approval_reference) > 256
            or not re.fullmatch(r"aupay-card-repair-v1:[0-9a-f]{32}", approval.repair_ref)
            or not re.fullmatch(r"writer-target-v1:[0-9a-f]{32}", approval.target_ref)
        ):
            raise RuntimeError("protected_repair_approval_invalid")
        return approval


@dataclass(frozen=True)
class RepairCapability:
    capability_id: str
    token: str
    run_id: str
    repair_ref: str
    target_ref: str
    approval_reference: str
    issued_at: datetime
    expires_at: datetime


class SqliteRepairCapabilityStore:
    def __init__(self, path, *, repo_root):
        self.path = _external_path(path, repo_root)
        with sqlite3.connect(self.path) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS repair_capabilities (
                capability_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL UNIQUE, repair_ref TEXT NOT NULL UNIQUE,
                target_ref TEXT NOT NULL, approval_reference TEXT NOT NULL,
                issued_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                state TEXT NOT NULL, attempt_id TEXT UNIQUE,
                terminal_reason TEXT NOT NULL DEFAULT ''
            )""")

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def issue(self, capability: RepairCapability) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute("""INSERT INTO repair_capabilities (
                capability_id,token_hash,run_id,repair_ref,target_ref,
                approval_reference,issued_at,expires_at,state
            ) VALUES (?,?,?,?,?,?,?,?, 'issued')""", (
                capability.capability_id, self._token_hash(capability.token),
                capability.run_id, capability.repair_ref, capability.target_ref,
                capability.approval_reference, capability.issued_at.isoformat(),
                capability.expires_at.isoformat(),
            ))

    def _get(self, capability: RepairCapability, connection):
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM repair_capabilities WHERE capability_id=?",
            (capability.capability_id,),
        ).fetchone()
        if row is None or row["token_hash"] != self._token_hash(capability.token):
            raise RuntimeError("repair_capability_invalid")
        expected = (
            capability.run_id, capability.repair_ref, capability.target_ref,
            capability.approval_reference, capability.issued_at.isoformat(),
            capability.expires_at.isoformat(),
        )
        actual = tuple(row[name] for name in (
            "run_id", "repair_ref", "target_ref", "approval_reference",
            "issued_at", "expires_at",
        ))
        if actual != expected:
            raise RuntimeError("repair_capability_binding_mismatch")
        return row

    def claim(self, capability: RepairCapability, attempt_id: str, now: datetime) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get(capability, connection)
            if row["state"] != "issued":
                raise RuntimeError("repair_capability_reused")
            if datetime.fromisoformat(row["expires_at"]) <= now:
                raise RuntimeError("repair_capability_expired")
            connection.execute(
                "UPDATE repair_capabilities SET state='claimed',attempt_id=? WHERE capability_id=?",
                (attempt_id, capability.capability_id),
            )
            connection.commit()

    def authorize(self, capability: RepairCapability, attempt_id: str, now: datetime) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get(capability, connection)
            if row["state"] != "claimed" or row["attempt_id"] != attempt_id:
                raise RuntimeError("repair_capability_reused")
            if datetime.fromisoformat(row["expires_at"]) <= now:
                raise RuntimeError("repair_capability_expired")
            connection.execute(
                "UPDATE repair_capabilities SET state='dispatching' WHERE capability_id=?",
                (capability.capability_id,),
            )
            connection.commit()

    def seal(self, capability: RepairCapability, reason: str) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE repair_capabilities SET state='sealed',terminal_reason=? WHERE capability_id=?",
                (reason, capability.capability_id),
            )

    def state(self, capability_id: str) -> str:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT state FROM repair_capabilities WHERE capability_id=?", (capability_id,),
            ).fetchone()
        return str(row[0]) if row else ""


class SqliteRepairJournal:
    def __init__(self, path, *, repo_root):
        self.path = _external_path(path, repo_root)
        with sqlite3.connect(self.path) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS repair_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,stage TEXT NOT NULL,state TEXT NOT NULL,
                reason TEXT NOT NULL,before_digest TEXT NOT NULL,
                after_digest TEXT NOT NULL,timestamp TEXT NOT NULL
            )""")

    def append(self, preview: RepairPreview, attempt_id: str, stage: str,
               state: str, reason: str, timestamp: datetime) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute("""INSERT INTO repair_events (
                run_id,attempt_id,stage,state,reason,before_digest,after_digest,timestamp
            ) VALUES (?,?,?,?,?,?,?,?)""", (
                preview.run_id, attempt_id, stage, state, reason,
                preview.before_digest, preview.after_digest, timestamp.isoformat(),
            ))

    def history(self, run_id: str, attempt_id: str) -> tuple[tuple, ...]:
        with sqlite3.connect(self.path) as connection:
            return tuple(connection.execute(
                "SELECT stage,state,reason,before_digest,after_digest FROM repair_events "
                "WHERE run_id=? AND attempt_id=? ORDER BY seq", (run_id, attempt_id),
            ).fetchall())


def issue_repair_capability(
    preview: RepairPreview, *, key_provider: ProtectedAuditKeyProvider,
    approval_provider: ProtectedRepairApprovalProvider,
    store: SqliteRepairCapabilityStore, now: datetime | None = None,
) -> RepairCapability:
    preview.validate()
    now = now or _utc_now()
    key = key_provider.load()
    if key is None:
        raise RuntimeError("persistent_audit_key_required")
    key.validate()
    approval = approval_provider.load()
    expected_ref = repair_reference(preview, key)
    if approval.repair_ref != expected_ref or approval.target_ref != preview.target_ref:
        raise RuntimeError("repair_approval_binding_mismatch")
    ttl = (approval.expires_at - now).total_seconds()
    if ttl <= 0 or ttl > MAX_REPAIR_TTL_SECONDS:
        raise RuntimeError("repair_approval_ttl_invalid")
    capability = RepairCapability(
        str(uuid4()), hashlib.sha256(os.urandom(32)).hexdigest(), preview.run_id,
        expected_ref, preview.target_ref, approval.approval_reference,
        now, approval.expires_at,
    )
    store.issue(capability)
    return capability


@dataclass(frozen=True)
class RepairResult:
    status: str
    attempt_id: str
    mutation_count: int
    row_count: int
    exact: bool


class SealedSheetsRowRepairTransport:
    def __init__(self, db, *, capability: RepairCapability,
                 store: SqliteRepairCapabilityStore,
                 binding: TargetBinding, inspector,
                 key_provider: ProtectedAuditKeyProvider,
                 clock: Callable[[], datetime] = _utc_now):
        self.db = db
        self.capability = capability
        self.store = store
        self.binding = binding
        self.inspector = inspector
        self.key_provider = key_provider
        self.clock = clock
        self.invocation_count = 0

    def write_once(self, preview: RepairPreview, *, attempt_id: str) -> None:
        validate_target_binding(self.binding, self.inspector.inspect(self.binding.expected_worksheet))
        if preview.target_ref != self.capability.target_ref:
            raise RuntimeError("repair_target_mismatch")
        key = self.key_provider.load()
        if key is None or repair_reference(preview, key) != self.capability.repair_ref:
            raise RuntimeError("repair_capability_preview_mismatch")
        self.store.authorize(self.capability, attempt_id, self.clock())
        data = [
            {"range": f"{self.binding.expected_worksheet}!{column}{preview.row_number}",
             "values": [[preview.after_row[index]]]}
            for index, column in REPAIR_COLUMNS
        ]
        self.invocation_count += 1
        self.db.svc.spreadsheets().values().batchUpdate(
            spreadsheetId=self.binding.expected_spreadsheet_id,
            body={"valueInputOption": "RAW", "data": data},
        ).execute()


def execute_repair_once(
    preview: RepairPreview, *, capability: RepairCapability,
    store: SqliteRepairCapabilityStore, journal: SqliteRepairJournal,
    leases: SqliteLeaseManager, reader: Callable[[], list[tuple[int, tuple]]],
    transport: SealedSheetsRowRepairTransport,
    key_provider: ProtectedAuditKeyProvider, owner_id: str,
    clock: Callable[[], datetime] = _utc_now,
) -> RepairResult:
    preview.validate()
    if (
        type(store) is not SqliteRepairCapabilityStore
        or type(journal) is not SqliteRepairJournal
        or type(leases) is not SqliteLeaseManager
        or type(transport) is not SealedSheetsRowRepairTransport
        or type(key_provider) is not ProtectedAuditKeyProvider
    ):
        raise RuntimeError("formal_repair_authority_required")
    key = key_provider.load()
    if key is None or repair_reference(preview, key) != capability.repair_ref:
        raise RuntimeError("repair_capability_preview_mismatch")
    attempt_id = str(uuid4())
    lease = None
    reason = "repair_failed_closed"
    try:
        lease = leases.acquire(preview.target_ref, owner_id, preview.run_id, 300)
        if lease is None:
            raise RuntimeError("repair_lease_unavailable")
        rows = reader()
        if rows != [(preview.row_number, preview.before_row)]:
            raise RuntimeError("repair_fresh_pre_read_mismatch")
        journal.append(preview, attempt_id, "pre_read", "verified", "exact_before", clock())
        store.claim(capability, attempt_id, clock())
        journal.append(preview, attempt_id, "write_attempted", "attempted", "four_cells_about_to_update", clock())
        try:
            transport.write_once(preview, attempt_id=attempt_id)
        except Exception:
            reason = "repair_transport_outcome_unknown"
            journal.append(
                preview, attempt_id, "write_result", "outcome_unknown", reason, clock(),
            )
            raise
        journal.append(preview, attempt_id, "write_result", "attempted", "sheets_batch_update_ack", clock())
        after = reader()
        exact = after == [(preview.row_number, preview.after_row)]
        journal.append(preview, attempt_id, "post_read", "confirmed" if exact else "conflict",
                       "post_repair_exact" if exact else "post_repair_mismatch", clock())
        reason = "post_repair_exact" if exact else "post_repair_mismatch"
        journal.append(preview, attempt_id, "final", "confirmed" if exact else "conflict", reason, clock())
        return RepairResult("repair_complete" if exact else "repair_conflict", attempt_id,
                            transport.invocation_count, len(after), exact)
    finally:
        store.seal(capability, reason)
        if lease is not None:
            leases.release(lease)
