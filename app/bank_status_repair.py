"""Exact one-cell authority for a bank import-status repair.

The stable import identity is the authority.  A Sheets row number is resolved
from a fresh read only after the authority has been validated and is used only
as the transport coordinate for the single-cell compare-and-set operation.
"""
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

from .aupay_card_production import ProtectedAuditKeyProvider, SqliteLeaseManager
from .aupay_card_writer import (
    PersistentAuditKey,
    TargetBinding,
    validate_target_binding,
)


STATUS_COLUMN = "I"
STATUS_INDEX = 8
SCHEMA_VERSION = 1
MAX_TTL_SECONDS = 300


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _external_path(path, repo_root) -> Path:
    value = Path(path).expanduser().resolve()
    root = Path(repo_root).resolve()
    try:
        value.relative_to(root)
    except ValueError:
        pass
    else:
        raise RuntimeError("cell_repair_state_must_be_outside_repository")
    value.parent.mkdir(parents=True, exist_ok=True)
    return value


def _row(values) -> tuple:
    return tuple((list(values) + [""] * 12)[:12])


def _digest(values) -> str:
    body = json.dumps(_row(values), ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BankStatusRepairPreview:
    run_id: str
    identity: str
    target_ref: str
    before_row: tuple
    after_row: tuple
    before_digest: str
    after_digest: str
    expected_old_value: str
    new_value: str
    worksheet: str = "取込データ"
    column: str = STATUS_COLUMN
    maximum_mutations: int = 1
    schema_version: int = SCHEMA_VERSION

    def validate(self) -> None:
        try:
            UUID(self.run_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("cell_repair_run_id_invalid") from exc
        if (
            self.schema_version != SCHEMA_VERSION
            or self.worksheet != "取込データ"
            or self.column != STATUS_COLUMN
            or self.maximum_mutations != 1
        ):
            raise RuntimeError("cell_repair_boundary_invalid")
        if not self.identity or not re.fullmatch(r"writer-target-v1:[0-9a-f]{32}", self.target_ref):
            raise RuntimeError("cell_repair_binding_invalid")
        if len(self.before_row) != 12 or len(self.after_row) != 12:
            raise RuntimeError("cell_repair_row_width_invalid")
        if self.before_row[0] != self.identity or self.after_row[0] != self.identity:
            raise RuntimeError("cell_repair_identity_mismatch")
        changed = tuple(i for i, pair in enumerate(zip(self.before_row, self.after_row)) if pair[0] != pair[1])
        if changed != (STATUS_INDEX,):
            raise RuntimeError("cell_repair_must_change_exactly_status")
        if (
            self.before_row[STATUS_INDEX] != self.expected_old_value
            or self.after_row[STATUS_INDEX] != self.new_value
            or self.expected_old_value == self.new_value
        ):
            raise RuntimeError("cell_repair_compare_and_set_invalid")
        if self.before_digest != _digest(self.before_row) or self.after_digest != _digest(self.after_row):
            raise RuntimeError("cell_repair_digest_invalid")


def create_bank_status_repair_preview(
    *, run_id: str, identity: str, target_ref: str, before_row,
    expected_old_value: str, new_value: str,
) -> BankStatusRepairPreview:
    before = _row(before_row)
    after = list(before)
    after[STATUS_INDEX] = new_value
    preview = BankStatusRepairPreview(
        run_id=run_id,
        identity=identity,
        target_ref=target_ref,
        before_row=before,
        after_row=tuple(after),
        before_digest=_digest(before),
        after_digest=_digest(after),
        expected_old_value=expected_old_value,
        new_value=new_value,
    )
    preview.validate()
    return preview


def repair_reference(preview: BankStatusRepairPreview, key: PersistentAuditKey) -> str:
    preview.validate()
    key.validate()
    payload = {
        "schema_version": preview.schema_version,
        "run_id": preview.run_id,
        "identity": preview.identity,
        "target_ref": preview.target_ref,
        "worksheet": preview.worksheet,
        "column": preview.column,
        "before_digest": preview.before_digest,
        "after_digest": preview.after_digest,
        "expected_old_value": preview.expected_old_value,
        "new_value": preview.new_value,
        "maximum_mutations": preview.maximum_mutations,
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    value = hmac.new(key.secret, body.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return f"bank-status-repair-v1:{value}"


@dataclass(frozen=True)
class BankStatusRepairApproval:
    approval_reference: str
    repair_ref: str
    target_ref: str
    expires_at: datetime


class ProtectedBankStatusRepairApprovalProvider:
    def __init__(self, path, *, repo_root):
        self.path = _external_path(path, repo_root)

    def __repr__(self) -> str:
        return "ProtectedBankStatusRepairApprovalProvider(path=<redacted>)"

    def load(self) -> BankStatusRepairApproval:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            approval = BankStatusRepairApproval(
                approval_reference=str(raw["approval_reference"]),
                repair_ref=str(raw["repair_ref"]),
                target_ref=str(raw["target_ref"]),
                expires_at=datetime.fromisoformat(str(raw["expires_at"])),
            )
        except Exception as exc:
            raise RuntimeError("protected_cell_repair_approval_invalid") from exc
        if approval.expires_at.tzinfo is None or approval.expires_at.utcoffset() is None:
            raise RuntimeError("cell_repair_approval_timezone_required")
        if (
            not approval.approval_reference
            or not re.fullmatch(r"bank-status-repair-v1:[0-9a-f]{32}", approval.repair_ref)
            or not re.fullmatch(r"writer-target-v1:[0-9a-f]{32}", approval.target_ref)
        ):
            raise RuntimeError("protected_cell_repair_approval_invalid")
        return approval


@dataclass(frozen=True)
class BankStatusRepairCapability:
    capability_id: str
    token: str
    run_id: str
    repair_ref: str
    target_ref: str
    approval_reference: str
    issued_at: datetime
    expires_at: datetime


class SqliteBankStatusRepairState:
    def __init__(self, path, *, repo_root):
        self.path = _external_path(path, repo_root)
        with sqlite3.connect(self.path) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS capabilities (
                    capability_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL UNIQUE, repair_ref TEXT NOT NULL UNIQUE,
                    target_ref TEXT NOT NULL, approval_reference TEXT NOT NULL,
                    issued_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    state TEXT NOT NULL, attempt_id TEXT UNIQUE,
                    terminal_reason TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL, stage TEXT NOT NULL, state TEXT NOT NULL,
                    reason TEXT NOT NULL, before_digest TEXT NOT NULL,
                    after_digest TEXT NOT NULL, timestamp TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS events_no_update
                BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append_only_journal'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete
                BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append_only_journal'); END;
            """)

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def issue(self, capability: BankStatusRepairCapability) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO capabilities VALUES (?,?,?,?,?,?,?,?, 'issued', NULL, '')",
                (
                    capability.capability_id, self._token_hash(capability.token),
                    capability.run_id, capability.repair_ref, capability.target_ref,
                    capability.approval_reference, capability.issued_at.isoformat(),
                    capability.expires_at.isoformat(),
                ),
            )

    def _get(self, connection, capability: BankStatusRepairCapability):
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM capabilities WHERE capability_id=?", (capability.capability_id,),
        ).fetchone()
        if row is None or row["token_hash"] != self._token_hash(capability.token):
            raise RuntimeError("cell_repair_capability_invalid")
        expected = (
            capability.run_id, capability.repair_ref, capability.target_ref,
            capability.approval_reference, capability.issued_at.isoformat(),
            capability.expires_at.isoformat(),
        )
        actual = tuple(row[name] for name in (
            "run_id", "repair_ref", "target_ref", "approval_reference", "issued_at", "expires_at",
        ))
        if actual != expected:
            raise RuntimeError("cell_repair_capability_binding_mismatch")
        return row

    def claim(self, capability: BankStatusRepairCapability, attempt_id: str, now: datetime) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get(connection, capability)
            if row["state"] != "issued":
                raise RuntimeError("cell_repair_capability_reused")
            if datetime.fromisoformat(row["expires_at"]) <= now:
                raise RuntimeError("cell_repair_capability_expired")
            connection.execute(
                "UPDATE capabilities SET state='claimed',attempt_id=? WHERE capability_id=?",
                (attempt_id, capability.capability_id),
            )
            connection.commit()

    def authorize(self, capability: BankStatusRepairCapability, attempt_id: str, now: datetime) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get(connection, capability)
            if row["state"] != "claimed" or row["attempt_id"] != attempt_id:
                raise RuntimeError("cell_repair_capability_reused")
            if datetime.fromisoformat(row["expires_at"]) <= now:
                raise RuntimeError("cell_repair_capability_expired")
            connection.execute(
                "UPDATE capabilities SET state='dispatching' WHERE capability_id=?",
                (capability.capability_id,),
            )
            connection.commit()

    def seal(self, capability: BankStatusRepairCapability, reason: str) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE capabilities SET state='sealed',terminal_reason=? WHERE capability_id=?",
                (reason, capability.capability_id),
            )

    def state(self, capability_id: str) -> str:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT state FROM capabilities WHERE capability_id=?", (capability_id,),
            ).fetchone()
        return str(row[0]) if row else ""

    def append(self, preview: BankStatusRepairPreview, attempt_id: str, stage: str,
               state: str, reason: str, timestamp: datetime) -> None:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise RuntimeError("cell_repair_journal_timezone_required")
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO events (run_id,attempt_id,stage,state,reason,before_digest,after_digest,timestamp) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    preview.run_id, attempt_id, stage, state, reason,
                    preview.before_digest, preview.after_digest, timestamp.isoformat(),
                ),
            )

    def history(self, run_id: str, attempt_id: str) -> tuple[tuple, ...]:
        with sqlite3.connect(self.path) as connection:
            return tuple(connection.execute(
                "SELECT stage,state,reason FROM events WHERE run_id=? AND attempt_id=? ORDER BY seq",
                (run_id, attempt_id),
            ).fetchall())


def issue_bank_status_repair_capability(
    preview: BankStatusRepairPreview, *, key_provider: ProtectedAuditKeyProvider,
    approval_provider: ProtectedBankStatusRepairApprovalProvider,
    store: SqliteBankStatusRepairState, now: datetime | None = None,
) -> BankStatusRepairCapability:
    preview.validate()
    now = now or _utc_now()
    key = key_provider.load()
    if key is None:
        raise RuntimeError("persistent_audit_key_required")
    approval = approval_provider.load()
    expected_ref = repair_reference(preview, key)
    if approval.repair_ref != expected_ref or approval.target_ref != preview.target_ref:
        raise RuntimeError("cell_repair_approval_binding_mismatch")
    ttl = (approval.expires_at - now).total_seconds()
    if ttl <= 0 or ttl > MAX_TTL_SECONDS:
        raise RuntimeError("cell_repair_approval_ttl_invalid")
    capability = BankStatusRepairCapability(
        capability_id=str(uuid4()), token=hashlib.sha256(os.urandom(32)).hexdigest(),
        run_id=preview.run_id, repair_ref=expected_ref, target_ref=preview.target_ref,
        approval_reference=approval.approval_reference, issued_at=now,
        expires_at=approval.expires_at,
    )
    store.issue(capability)
    return capability


def _matching_rows(reader, identity: str) -> list[tuple[int, tuple]]:
    return [(number, _row(row)) for number, row in reader() if _row(row)[0] == identity]


class SealedBankStatusCellTransport:
    def __init__(
        self, db, *, capability: BankStatusRepairCapability,
        store: SqliteBankStatusRepairState, binding: TargetBinding, inspector,
        key_provider: ProtectedAuditKeyProvider,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self.db = db
        self.capability = capability
        self.store = store
        self.binding = binding
        self.inspector = inspector
        self.key_provider = key_provider
        self.clock = clock
        self.invocation_count = 0

    def write_once(self, preview: BankStatusRepairPreview, *, attempt_id: str, row_number: int) -> None:
        validate_target_binding(self.binding, self.inspector.inspect(preview.worksheet))
        key = self.key_provider.load()
        if key is None or repair_reference(preview, key) != self.capability.repair_ref:
            raise RuntimeError("cell_repair_capability_preview_mismatch")
        self.store.authorize(self.capability, attempt_id, self.clock())
        self.invocation_count += 1
        self.db.svc.spreadsheets().values().update(
            spreadsheetId=self.binding.expected_spreadsheet_id,
            range=f"{preview.worksheet}!{preview.column}{row_number}",
            valueInputOption="RAW", body={"values": [[preview.new_value]]},
        ).execute()


@dataclass(frozen=True)
class BankStatusRepairResult:
    status: str
    attempt_id: str
    requested_mutations: int
    confirmed_changed_cells: int
    exact: bool


def execute_bank_status_repair_once(
    preview: BankStatusRepairPreview, *, capability: BankStatusRepairCapability,
    store: SqliteBankStatusRepairState, leases: SqliteLeaseManager,
    reader: Callable[[], list[tuple[int, tuple]]],
    transport: SealedBankStatusCellTransport,
    key_provider: ProtectedAuditKeyProvider, owner_id: str,
    clock: Callable[[], datetime] = _utc_now,
) -> BankStatusRepairResult:
    preview.validate()
    if (
        type(store) is not SqliteBankStatusRepairState
        or type(leases) is not SqliteLeaseManager
        or type(transport) is not SealedBankStatusCellTransport
        or type(key_provider) is not ProtectedAuditKeyProvider
    ):
        raise RuntimeError("formal_cell_repair_authority_required")
    key = key_provider.load()
    if key is None or repair_reference(preview, key) != capability.repair_ref:
        raise RuntimeError("cell_repair_capability_preview_mismatch")
    attempt_id = str(uuid4())
    lease = None
    reason = "cell_repair_failed_closed"
    try:
        lease = leases.acquire(preview.target_ref, owner_id, preview.run_id, 300)
        if lease is None:
            raise RuntimeError("cell_repair_lease_unavailable")
        before = _matching_rows(reader, preview.identity)
        if len(before) != 1 or before[0][1] != preview.before_row:
            raise RuntimeError("cell_repair_fresh_pre_read_mismatch")
        row_number = before[0][0]
        store.append(preview, attempt_id, "pre_read", "verified", "exact_before", clock())
        store.claim(capability, attempt_id, clock())
        store.append(preview, attempt_id, "write_attempted", "attempted", "one_cell_about_to_update", clock())
        try:
            transport.write_once(preview, attempt_id=attempt_id, row_number=row_number)
        except Exception:
            after_error = _matching_rows(reader, preview.identity)
            if len(after_error) == 1 and after_error[0][1] == preview.after_row:
                status, exact, reason = "success_recovered", True, "post_repair_exact_recovered"
                changed = 1
            elif len(after_error) == 1 and after_error[0][1] == preview.before_row:
                status, exact, reason = "repair_not_applied", False, "post_repair_still_old"
                changed = 0
            else:
                status, exact, reason = "unknown_repair_outcome", False, "post_repair_unknown"
                changed = 0
            store.append(preview, attempt_id, "write_result", "outcome_unknown", "transport_error_no_retry", clock())
            store.append(preview, attempt_id, "post_read", "confirmed" if exact else "conflict", reason, clock())
            store.append(preview, attempt_id, "final", "confirmed" if exact else "conflict", reason, clock())
            return BankStatusRepairResult(status, attempt_id, 1, changed, exact)
        store.append(preview, attempt_id, "write_result", "acknowledged", "sheets_update_ack", clock())
        after = _matching_rows(reader, preview.identity)
        exact = len(after) == 1 and after[0][1] == preview.after_row
        reason = "post_repair_exact" if exact else "post_repair_mismatch"
        store.append(preview, attempt_id, "post_read", "confirmed" if exact else "conflict", reason, clock())
        store.append(preview, attempt_id, "final", "confirmed" if exact else "conflict", reason, clock())
        return BankStatusRepairResult(
            "repair_complete" if exact else "repair_conflict", attempt_id, 1,
            1 if exact else 0, exact,
        )
    finally:
        store.seal(capability, reason)
        if lease is not None:
            leases.release(lease)
