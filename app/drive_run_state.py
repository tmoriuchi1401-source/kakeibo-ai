"""Private Drive transport for existing recurring state, with no new database.

Callers hold the top-level kakeibo-production workflow concurrency lock. This
module is not a distributed lock. A durable pending marker is written BEFORE a
source can write Sheets. A crash or an ambiguous Drive response requires an
operator to reconcile the existing stable IDs before explicitly releasing it.
No operation retries, rolls back, creates folders, or silently initializes state.
"""
from __future__ import annotations

import base64
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Protocol


MAX_BYTES = 64 * 1024 * 1024
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT_COLUMNS = {
    "recurring_checkpoint": ("singleton", "successful_window_end", "run_id"),
    "recurring_runs": ("run_id", "recorded_at", "status", "window_start", "window_end", "summary_json"),
}
TABLE_COLUMNS = {
    **CHECKPOINT_COLUMNS,
    "batch_manifests": ("run_id", "payload", "payload_hash"),
    "batch_capabilities": ("capability_id", "token_hash", "run_id", "batch_ref", "target_ref", "approval_reference", "issued_at", "expires_at", "state", "attempt_id", "terminal_reason"),
    "batch_events": ("seq", "run_id", "attempt_id", "batch_ref", "stage", "state", "reason", "identity_count", "candidate_digest", "timestamp"),
    "writer_leases": ("target_ref", "owner_id", "run_id", "token", "expires_at"),
    "journal_events": ("seq", "run_id", "canonical_identity", "attempt_id", "batch_id", "stage", "state", "timestamp", "reason_code", "prev_hash", "event_hash"),
    "production_capabilities": ("capability_id", "token_hash", "run_id", "plan_binding_ref", "target_ref", "candidate_ref", "approval_reference", "issued_at", "expires_at", "state", "attempt_id", "terminal_reason"),
}
CARD_FILES = {
    "recurring.sqlite3": set(CHECKPOINT_COLUMNS),
    "manifests.sqlite3": {"batch_manifests"},
    "capabilities.sqlite3": {"batch_capabilities"},
    "journal.sqlite3": {"batch_events"},
    "leases.sqlite3": {"writer_leases"},
}
BANK_MANIFEST_FIELDS = {
    "schema_version", "run_id", "created_at", "candidate_refs", "batch_ref",
    "target_ref", "plan_binding_ref", "expected_git_head", "expected_branch",
    "income_count", "expense_count", "authority_provenance", "min_rows", "max_rows",
}


class StateError(RuntimeError):
    """Fixed operational code only; never include payload, paths or API errors."""


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _aware(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise StateError("state_timestamp_invalid")
    return stamp


def _external_directory(directory: Path) -> Path:
    requested = Path(directory)
    if not requested.is_absolute() or requested.is_symlink():
        raise StateError("state_directory_must_be_absolute_and_external")
    resolved = requested.resolve()
    if resolved.is_relative_to(REPOSITORY_ROOT):
        raise StateError("state_directory_must_be_absolute_and_external")
    return resolved


@dataclass(frozen=True)
class StateBinding:
    source: str
    spreadsheet_id: str = field(repr=False)
    folder_id: str = field(repr=False)
    file_id: str = field(repr=False)
    schema: int = 1

    def __post_init__(self):
        if (self.source not in {"amazon_gmail", "au_pay_card_gmail", "bank_pdf_drive"}
                or not all((self.spreadsheet_id, self.folder_id, self.file_id))
                or self.schema != 1):
            raise StateError("state_binding_invalid")

    @property
    def reference(self) -> str:
        return _digest(_json([self.source, self.spreadsheet_id, self.folder_id, self.file_id, self.schema]))

    @property
    def checkpoint_name(self) -> str:
        return "bank-recurring.sqlite3" if self.source == "bank_pdf_drive" else "recurring.sqlite3"


def _expected_tables(source: str, name: str) -> set[str] | None:
    if source == "amazon_gmail" and name == "recurring.sqlite3":
        return set(CHECKPOINT_COLUMNS)
    if source == "au_pay_card_gmail":
        return CARD_FILES.get(name)
    if source == "bank_pdf_drive":
        if name == "bank-recurring.sqlite3":
            return set(CHECKPOINT_COLUMNS)
        if re.fullmatch(r"bank-pdf-batch-[0-9a-f]{16}/bank-steady-state\.sqlite3", name):
            return {"journal_events", "production_capabilities", "writer_leases"}
    return None


def _validate_file(source: str, name: str, data: bytes) -> str | None:
    """Validate native schemas in memory; return a checkpoint when present."""
    if len(data) > MAX_BYTES:
        raise StateError("state_size_limit")
    tables = _expected_tables(source, name)
    if tables is None:
        if source == "bank_pdf_drive" and re.fullmatch(
            r"bank-pdf-batch-[0-9a-f]{16}/exact-steady-state-manifest\.json", name,
        ):
            raw = json.loads(data)
            if not isinstance(raw, dict) or set(raw) != BANK_MANIFEST_FIELDS or raw["schema_version"] != 1:
                raise StateError("state_schema_mismatch")
            return None
        raise StateError("state_file_not_allowed")
    with closing(sqlite3.connect(":memory:")) as db:
        db.deserialize(data)
        db.execute("PRAGMA trusted_schema=OFF")
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise StateError("state_corrupt")
        actual = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if actual != tables:
            raise StateError("state_schema_mismatch")
        for table in tables:
            columns = tuple(row[1] for row in db.execute(f'PRAGMA table_info("{table}")'))
            if columns != TABLE_COLUMNS[table]:
                raise StateError("state_schema_mismatch")
        if "recurring_checkpoint" in tables:
            rows = db.execute("SELECT singleton,successful_window_end FROM recurring_checkpoint").fetchall()
            if len(rows) > 1 or (rows and rows[0][0] != 1):
                raise StateError("state_checkpoint_invalid")
            if rows:
                _aware(rows[0][1])
                return rows[0][1]
    return None


def snapshot(directory: Path, binding: StateBinding) -> dict[str, dict[str, str]]:
    """Snapshot closed source stores using SQLite backup (includes committed WAL).

    Writers must have exited. Sidecars are represented by their database snapshot,
    never restored independently. Unexpected files fail instead of leaking keys.
    """
    result = {}
    root = _external_directory(directory)
    if not root.is_dir():
        raise StateError("state_local_missing")
    try:
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise StateError("state_symlink_forbidden")
            if path.is_dir():
                continue
            name = path.relative_to(root).as_posix()
            if name.endswith(("-wal", "-shm")) and _expected_tables(binding.source, name[:-4]):
                if not path.with_name(path.name[:-4]).is_file():
                    raise StateError("state_orphan_sidecar")
                continue
            if _expected_tables(binding.source, name) is not None:
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as src:
                    with closing(sqlite3.connect(":memory:")) as dest:
                        src.backup(dest)
                        # WAL snapshots must be standalone rollback-mode images.
                        dest.execute("PRAGMA journal_mode=DELETE")
                        data = dest.serialize()
                        if data[18:20] == b"\x02\x02":
                            data = data[:18] + b"\x01\x01" + data[20:]
            else:
                data = path.read_bytes()
            _validate_file(binding.source, name, data)
            result[name] = {"sha256": _digest(data), "data": base64.b64encode(data).decode("ascii")}
        if binding.checkpoint_name not in result:
            raise StateError("state_checkpoint_missing")
        if len(_json(result)) > MAX_BYTES:
            raise StateError("state_size_limit")
        return result
    except StateError:
        raise
    except Exception:
        raise StateError("state_snapshot_invalid") from None


def envelope(binding: StateBinding, files: dict, *, generation: int = 0,
             phase: str = "ready", attempt: str = "") -> bytes:
    return _json({"schema": 1, "source": binding.source, "binding": binding.reference,
                  "generation": generation, "phase": phase, "attempt": attempt, "files": files})


def validate(payload: bytes, binding: StateBinding) -> dict:
    try:
        if len(payload) > MAX_BYTES:
            raise StateError("state_size_limit")
        value = json.loads(payload)
        if (set(value) != {"schema", "source", "binding", "generation", "phase", "attempt", "files"}
                or value["schema"] != binding.schema or value["source"] != binding.source
                or value["binding"] != binding.reference):
            raise StateError("state_binding_mismatch")
        if (type(value["generation"]) is not int or value["generation"] < 0
                or value["phase"] not in {"ready", "pending"}
                or not isinstance(value["files"], dict)
                or not isinstance(value["attempt"], str)
                or (value["phase"] == "pending" and not re.fullmatch(r"[0-9a-f]{32}", value["attempt"]))):
            raise StateError("state_envelope_invalid")
        if binding.checkpoint_name not in value["files"]:
            raise StateError("state_checkpoint_missing")
        for name, item in value["files"].items():
            if set(item) != {"sha256", "data"}:
                raise StateError("state_envelope_invalid")
            data = base64.b64decode(item["data"], validate=True)
            if _digest(data) != item["sha256"]:
                raise StateError("state_checksum_mismatch")
            _validate_file(binding.source, name, data)
        return value
    except StateError:
        raise
    except Exception:
        raise StateError("state_corrupt") from None


class Transport(Protocol):
    def read(self) -> bytes: ...
    def write(self, payload: bytes) -> None: ...


class DriveStateTransport:
    """Use the caller's existing authenticated Drive service and precreated file.

    No create/delete/list-by-name, no OAuth changes, no automatic retries.
    Folder permission/ownership checks are a separate cutover preflight.
    """
    def __init__(self, service, binding: StateBinding):
        self.service, self.binding = service, binding

    def _metadata(self):
        meta = self.service.files().get(
            fileId=self.binding.file_id, supportsAllDrives=True,
            fields="id,parents,trashed,mimeType,capabilities(canEdit)",
        ).execute(num_retries=0)
        if (meta.get("trashed") or meta.get("parents") != [self.binding.folder_id]
                or meta.get("mimeType") != "application/json"):
            raise StateError("state_drive_target_mismatch")
        return meta

    def read(self) -> bytes:
        try:
            self._metadata()
            return self.service.files().get_media(
                fileId=self.binding.file_id, supportsAllDrives=True,
            ).execute(num_retries=0)
        except StateError:
            raise
        except Exception:
            raise StateError("state_drive_read_failed") from None

    def write(self, payload: bytes) -> None:
        from googleapiclient.http import MediaIoBaseUpload
        try:
            meta = self._metadata()
            if not meta.get("capabilities", {}).get("canEdit"):
                raise StateError("state_drive_not_editable")
            self.service.files().update(
                fileId=self.binding.file_id, supportsAllDrives=True,
                media_body=MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/json", resumable=False),
                fields="id",
            ).execute(num_retries=0)
        except StateError:
            raise
        except Exception:
            raise StateError("state_drive_write_unknown") from None


class DurableState:
    def __init__(self, transport: Transport, binding: StateBinding):
        self.transport, self.binding = transport, binding
        self._loaded: bytes | None = None
        self._pending: bytes | None = None

    def inspect(self) -> dict:
        """Read-only inspection; does not release an unresolved attempt."""
        return validate(self.transport.read(), self.binding)

    def restore(self, directory: Path) -> None:
        payload = self.transport.read()
        value = validate(payload, self.binding)
        if value["phase"] != "ready":
            raise StateError("state_reconciliation_required")
        destination = _external_directory(directory)
        if destination.exists():
            raise StateError("state_restore_requires_new_directory")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="kakeibo-restore-", dir=destination.parent) as temp:
            stage = Path(temp) / "state"
            stage.mkdir()
            for name, item in value["files"].items():
                target = stage / name  # strict allowlist validated before any filesystem write
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(base64.b64decode(item["data"], validate=True))
                    stream.flush()
                    os.fsync(stream.fileno())
            stage.rename(destination)
        self._loaded = payload

    def _replace(self, expected: bytes, proposed: bytes) -> None:
        validate(proposed, self.binding)
        if self.transport.read() != expected:
            raise StateError("state_changed_since_restore")
        self.transport.write(proposed)
        if self.transport.read() != proposed:
            raise StateError("state_save_readback_mismatch")

    def begin(self, attempt: str) -> None:
        """Must complete before invoking any Sheets or source write."""
        if self._loaded is None or self._pending is not None:
            raise StateError("state_restore_required")
        value = validate(self._loaded, self.binding)
        pending = envelope(self.binding, value["files"], generation=value["generation"] + 1,
                           phase="pending", attempt=attempt)
        self._replace(self._loaded, pending)
        self._pending = pending

    def commit(self, directory: Path) -> None:
        """Only after source success and its existing exact Sheets read-back."""
        if self._pending is None:
            raise StateError("state_begin_required")
        old = validate(self._pending, self.binding)
        files = snapshot(directory, self.binding)
        previous = _validate_file(self.binding.source, self.binding.checkpoint_name,
                                  base64.b64decode(old["files"][self.binding.checkpoint_name]["data"]))
        current = _validate_file(self.binding.source, self.binding.checkpoint_name,
                                 base64.b64decode(files[self.binding.checkpoint_name]["data"]))
        if previous and (not current or _aware(current) < _aware(previous)):
            raise StateError("state_checkpoint_regression")
        proposed = envelope(self.binding, files, generation=old["generation"] + 1)
        self._replace(self._pending, proposed)
        self._loaded, self._pending = proposed, None

    def release_after_reconciliation(self, *, observed_digest: str, evidence_reference: str) -> None:
        """Operator-only recovery; keep the OLD checkpoint for ID/read-back replay.

        Never called by the daily runner. Approval must bind this observed digest
        after reviewing Sheets and Drive results; this method cannot prove them.
        """
        current = self.transport.read()
        value = validate(current, self.binding)
        if (value["phase"] != "pending" or _digest(current) != observed_digest
                or not re.fullmatch(r"[0-9a-f]{64}", evidence_reference)):
            raise StateError("state_recovery_evidence_required")
        proposed = envelope(self.binding, value["files"], generation=value["generation"] + 1)
        self._replace(current, proposed)
