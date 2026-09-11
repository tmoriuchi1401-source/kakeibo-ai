"""Windows-local, read-only scheduling boundary for Payroll production scans."""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Callable, Iterator, Literal

from .payroll_production_runner import PayrollProductionRunReport


CONFIG_VERSION = "payroll-scheduled-scan-config-v1"
MAX_CONFIG_BYTES = 64 * 1024
OUTCOMES = (
    "WRITE_READY",
    "EXACT_DUPLICATE",
    "ALTERNATE_SOURCE_DUPLICATE",
    "NEEDS_REVIEW",
    "CONFLICT",
)


@dataclass(frozen=True)
class PayrollScheduledConfig:
    config_version: str
    spreadsheet_id: str = field(repr=False)
    payroll_drive_folder_id: str = field(repr=False)
    google_service_account_file: Path = field(repr=False)
    review_journal_file: Path = field(repr=False)
    review_hmac_key_file: Path = field(repr=False)
    review_source_content_hash: str = field(repr=False)
    reconciliation_journal_file: Path = field(repr=False)
    reconciliation_hmac_key_file: Path = field(repr=False)
    log_directory: Path
    lock_file: Path
    employer_id: str | None = field(default=None, repr=False)
    statement_type: Literal["salary", "bonus", "adjustment"] | None = None


@dataclass(frozen=True)
class PayrollScheduledRunResult:
    summary: dict
    log_path: Path


class PayrollScheduledLockError(RuntimeError):
    pass


def _outside_repository(path, repository_root, reason: str) -> Path:
    target = Path(path).expanduser().resolve()
    root = Path(repository_root).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return target
    raise ValueError(reason)


def _absolute_external_file(value, root, reason: str) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise ValueError(reason)
    target = _outside_repository(raw, root, reason)
    if target.is_symlink() or not target.is_file():
        raise ValueError(reason)
    return target


def _config_dict(config: PayrollScheduledConfig) -> dict:
    return {
        "config_version": config.config_version,
        "spreadsheet_id": config.spreadsheet_id,
        "payroll_drive_folder_id": config.payroll_drive_folder_id,
        "google_service_account_file": str(config.google_service_account_file),
        "review_journal_file": str(config.review_journal_file),
        "review_hmac_key_file": str(config.review_hmac_key_file),
        "review_source_content_hash": config.review_source_content_hash,
        "reconciliation_journal_file": str(config.reconciliation_journal_file),
        "reconciliation_hmac_key_file": str(config.reconciliation_hmac_key_file),
        "log_directory": str(config.log_directory),
        "lock_file": str(config.lock_file),
        "employer_id": config.employer_id,
        "statement_type": config.statement_type,
    }


def serialize_payroll_scheduled_config(config: PayrollScheduledConfig) -> bytes:
    return (json.dumps(
        _config_dict(config), ensure_ascii=True, sort_keys=True,
        separators=(",", ":"),
    ) + "\n").encode("ascii")


def deserialize_payroll_scheduled_config(payload: bytes, *, repository_root):
    if not payload or len(payload) > MAX_CONFIG_BYTES:
        raise ValueError("scheduled_config_size_invalid")
    try:
        values = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("scheduled_config_json_invalid") from exc
    expected = set(_config_dict(PayrollScheduledConfig(
        CONFIG_VERSION, "x", "x", Path("x"), Path("x"), Path("x"),
        "0" * 64, Path("x"), Path("x"), Path("x"), Path("x"),
    )))
    if not isinstance(values, dict) or set(values) != expected:
        raise ValueError("scheduled_config_shape_invalid")
    if values["config_version"] != CONFIG_VERSION:
        raise ValueError("scheduled_config_version_stale")
    if not all(
        isinstance(values[name], str) and values[name].strip()
        for name in ("spreadsheet_id", "payroll_drive_folder_id")
    ):
        raise ValueError("scheduled_config_resource_invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", values["review_source_content_hash"] or ""):
        raise ValueError("scheduled_config_review_source_hash_invalid")
    if values["statement_type"] not in (None, "salary", "bonus", "adjustment"):
        raise ValueError("scheduled_config_statement_type_invalid")
    if values["employer_id"] is not None and not isinstance(values["employer_id"], str):
        raise ValueError("scheduled_config_employer_invalid")
    root = Path(repository_root).resolve()
    files = {
        name: _absolute_external_file(values[name], root, f"scheduled_config_{name}_invalid")
        for name in (
            "google_service_account_file", "review_journal_file",
            "review_hmac_key_file", "reconciliation_journal_file",
            "reconciliation_hmac_key_file",
        )
    }
    log_directory = Path(values["log_directory"]).expanduser()
    lock_file = Path(values["lock_file"]).expanduser()
    if not log_directory.is_absolute() or not lock_file.is_absolute():
        raise ValueError("scheduled_config_output_path_invalid")
    log_directory = _outside_repository(
        log_directory, root, "scheduled_config_log_directory_invalid",
    )
    lock_file = _outside_repository(
        lock_file, root, "scheduled_config_lock_file_invalid",
    )
    if log_directory.is_symlink() or lock_file.is_symlink():
        raise ValueError("scheduled_config_output_symlink_rejected")
    return PayrollScheduledConfig(
        config_version=CONFIG_VERSION,
        spreadsheet_id=values["spreadsheet_id"].strip(),
        payroll_drive_folder_id=values["payroll_drive_folder_id"].strip(),
        google_service_account_file=files["google_service_account_file"],
        review_journal_file=files["review_journal_file"],
        review_hmac_key_file=files["review_hmac_key_file"],
        review_source_content_hash=values["review_source_content_hash"],
        reconciliation_journal_file=files["reconciliation_journal_file"],
        reconciliation_hmac_key_file=files["reconciliation_hmac_key_file"],
        log_directory=log_directory,
        lock_file=lock_file,
        employer_id=values["employer_id"],
        statement_type=values["statement_type"],
    )


def load_payroll_scheduled_config(path, *, repository_root):
    target = _outside_repository(
        path, repository_root, "scheduled_config_must_be_outside_repository",
    )
    if target.is_symlink() or not target.is_file():
        raise ValueError("scheduled_config_unavailable")
    return deserialize_payroll_scheduled_config(
        target.read_bytes(), repository_root=repository_root,
    )


def write_new_payroll_scheduled_config(
    config: PayrollScheduledConfig, path, *, repository_root,
) -> Path:
    target = _outside_repository(
        path, repository_root, "scheduled_config_must_be_outside_repository",
    )
    if target.is_symlink():
        raise ValueError("scheduled_config_symlink_rejected")
    payload = serialize_payroll_scheduled_config(config)
    # Validate the exact persisted form before the exclusive create.
    deserialize_payroll_scheduled_config(payload, repository_root=repository_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise ValueError("scheduled_config_already_exists") from exc
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target


@contextmanager
def single_run_lock(path: Path, *, now: datetime | None = None) -> Iterator[None]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise PayrollScheduledLockError("scheduled_lock_symlink_rejected")
    timestamp = (now or datetime.now(timezone.utc)).isoformat()
    try:
        with path.open("x", encoding="ascii") as handle:
            json.dump({"pid": os.getpid(), "started_at_utc": timestamp}, handle)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise PayrollScheduledLockError("scheduled_run_already_locked") from exc
    try:
        yield
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _source_digest(statement_id: str) -> str:
    return hashlib.sha256(statement_id.encode("utf-8")).hexdigest()


def build_payroll_scheduled_summary(
    report: PayrollProductionRunReport, *, now: datetime | None = None,
) -> dict:
    timestamp = (now or datetime.now(timezone.utc)).isoformat()
    groups: dict[str, list[dict]] = defaultdict(list)
    for result in report.results:
        groups[result.outcome].append({
            "source_identifier_digest": _source_digest(result.statement_id),
            "content_hash": result.content_hash,
            "outcome": result.outcome,
            "reason": result.reason,
            "review_item_count": result.review_item_count,
            "planned_header_rows": result.planned_header_rows,
            "planned_item_rows": result.planned_item_rows,
            "planned_update_rows": result.planned_update_rows,
        })
    return {
        "timestamp_utc": timestamp,
        "mode": "scheduled_read_only_scan",
        "read_only": report.read_only,
        "scanned_source_count": report.statement_count,
        "outcome_counts": {name: report.outcome_counts.get(name, 0) for name in OUTCOMES},
        "write_ready": groups["WRITE_READY"],
        "needs_review": groups["NEEDS_REVIEW"],
        "conflicts": groups["CONFLICT"],
        "exact_duplicates": groups["EXACT_DUPLICATE"],
        "alternate_source_duplicates": groups["ALTERNATE_SOURCE_DUPLICATE"],
        "writer_candidate_count": report.writer_candidate_count,
        "writer_invocation_count": report.writer_invocation_count,
        "actual_header_rows": report.actual_header_rows,
        "actual_item_rows": report.actual_item_rows,
        "actual_update_rows": report.actual_update_rows,
        "reconciliation_source": report.reconciliation_source,
        "reconciliation_reload_reason": report.reconciliation_reload_reason,
    }


def _write_log(log_directory: Path, payload: dict, *, prefix: str) -> Path:
    log_directory.mkdir(parents=True, exist_ok=True)
    if log_directory.is_symlink():
        raise ValueError("scheduled_log_symlink_rejected")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    target = log_directory / f"{prefix}-{stamp}-{os.getpid()}.json"
    encoded = (json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ) + "\n").encode("ascii")
    with target.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target


def run_payroll_scheduled_scan(
    config: PayrollScheduledConfig,
    scan: Callable[[], PayrollProductionRunReport],
) -> PayrollScheduledRunResult:
    with single_run_lock(config.lock_file):
        report = scan()
        if not report.read_only or report.writer_invocation_count or any((
            report.actual_header_rows, report.actual_item_rows,
            report.actual_update_rows,
        )):
            raise RuntimeError("scheduled_scan_write_boundary_violated")
        summary = build_payroll_scheduled_summary(report)
        log_path = _write_log(config.log_directory, summary, prefix="payroll-scan")
        return PayrollScheduledRunResult(summary, log_path)


def write_payroll_scheduled_error_log(
    log_directory: Path, error_category: str,
) -> Path:
    safe_category = re.sub(r"[^a-zA-Z0-9_.-]", "_", error_category)[:120]
    return _write_log(log_directory, {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "scheduled_read_only_scan",
        "status": "failed",
        "error_category": safe_category or "unknown_error",
        "writer_invocation_count": 0,
        "actual_write_count": 0,
    }, prefix="payroll-scan-error")
