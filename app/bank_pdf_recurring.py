"""Bounded recurring ingestion for newly arrived bank PDFs in Drive.

The runner deliberately reuses the existing bank steady-state preview and
production batch transport.  This module only supplies the file-window,
Drive-listing, and durable checkpoint glue; it does not introduce a second
classification or writer implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import subprocess
import tempfile
from typing import Callable, Iterable, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

from .aupay_card_recurring import SqliteRecurringRunState
from .bank_canary_production import (
    _run_bank_recurring_production_batch as run_bank_recurring_production_batch,
)
from .bank_recurring_authority import (
    BANK_RECURRING_SOURCE,
    MAX_AUTHORITY_FILES,
    MAX_AUTHORITY_ROWS,
    MAX_OVERLAP_SECONDS,
    MAX_WINDOW_SECONDS,
    MIN_OVERLAP_SECONDS,
    BankRecurringAuthority,
    ProtectedBankRecurringAuthorityProvider,
    create_bank_recurring_run_context,
)
from .bank_steady_state import build_bank_daily_preview
from .drive_receipts import normalize_folder_id
from .google_clients import download_drive_file


JST = ZoneInfo("Asia/Tokyo")
BANK_PROCESSED_PROPERTY = "kakeiboBankPdfProcessedAt"


@dataclass(frozen=True)
class BankPdfWindow:
    start: datetime
    end: datetime

    def validate(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("bank_recurring_window_timezone_required")
        if self.start >= self.end:
            raise ValueError("bank_recurring_window_invalid")


def build_incremental_window(
    policy: BankRecurringAuthority,
    state: SqliteRecurringRunState,
    now: datetime,
) -> BankPdfWindow:
    policy.validate()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("bank_recurring_now_timezone_required")
    end = now.astimezone(JST).replace(microsecond=0)
    checkpoint = state.successful_window_end()
    boundary = checkpoint.astimezone(JST) if checkpoint else policy.initial_start.astimezone(JST)
    start = boundary - timedelta(seconds=policy.overlap_seconds)
    window = BankPdfWindow(start, end)
    window.validate()
    if (end - start).total_seconds() > policy.max_window_seconds:
        raise RuntimeError("bank_recurring_window_exceeds_authority")
    return window


def _drive_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def list_bounded_bank_pdfs(service, folder_id: str, window: BankPdfWindow, max_files: int) -> list[dict]:
    """List only PDFs in the bounded overlap window; pagination fails closed."""
    window.validate()
    folder = normalize_folder_id(folder_id)
    if not (1 <= max_files <= MAX_AUTHORITY_FILES):
        raise ValueError("bank_recurring_max_files_invalid")
    query = (
        f"'{folder}' in parents and trashed=false and mimeType='application/pdf' "
        f"and modifiedTime >= '{_drive_time(window.start)}' "
        f"and modifiedTime < '{_drive_time(window.end)}'"
    )
    response = service.files().list(
        q=query,
        pageSize=max_files + 1,
        fields="files(id,name,mimeType,modifiedTime,parents,appProperties),nextPageToken",
        orderBy="modifiedTime",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = list(response.get("files", []))
    if response.get("nextPageToken") or len(files) > max_files:
        raise RuntimeError("bank_recurring_file_bound_exceeded")
    return [item for item in files if str(item.get("mimeType", "")) == "application/pdf"]


def _processed(file: Mapping[str, object]) -> bool:
    props = file.get("appProperties") or {}
    return bool(isinstance(props, Mapping) and props.get(BANK_PROCESSED_PROPERTY))


def _temporary_pdf(data: bytes):
    handle = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        handle.write(data)
        handle.close()
        return Path(handle.name)
    except Exception:
        handle.close()
        Path(handle.name).unlink(missing_ok=True)
        raise


def _mark_processed(service, file: Mapping[str, object]) -> None:
    properties = dict(file.get("appProperties") or {})
    properties[BANK_PROCESSED_PROPERTY] = datetime.now(timezone.utc).isoformat()
    service.files().update(
        fileId=str(file["id"]),
        body={"appProperties": properties},
        fields="id,appProperties",
        supportsAllDrives=True,
    ).execute()


def _base_summary(run_id: str, window: BankPdfWindow) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "status": "started",
        "source": BANK_RECURRING_SOURCE,
        "source_window_start": window.start.isoformat(),
        "source_window_end": window.end.isoformat(),
        "source_timezone": "Asia/Tokyo",
        "files_seen": 0,
        "files_new": 0,
        "files_processed": 0,
        "files_withheld": 0,
        "parsed": 0,
        "new_eligible": 0,
        "duplicate": 0,
        "review": 0,
        "income": 0,
        "non_expense": 0,
        "withheld": 0,
        "written": 0,
        "write_requests": 0,
        "write_attempted": 0,
        "failure": 0,
        "planned_expense_writes": 0,
        "planned_import_updates": 0,
        "safe_noop": False,
    }


def run_bank_pdf_recurring(
    *,
    drive_service,
    db,
    state: SqliteRecurringRunState,
    authority_provider: ProtectedBankRecurringAuthorityProvider,
    repo_root: str | Path,
    now: datetime,
    dry_run: bool = True,
    audit_key_file: str | Path | None = None,
    download: Callable[[str], bytes] | None = None,
    sleeper: Callable[[float], None] | None = None,
    confirmed_internal_transfers=frozenset(),
    confirmed_non_own_classifications=frozenset(),
    card_statement_authorities: Iterable = (),
) -> dict[str, object]:
    policy = authority_provider.load()
    if not (policy.valid_from <= now < policy.expires_at):
        raise RuntimeError("bank_recurring_authority_expired_or_not_started")
    if str(db.sid) != policy.expected_spreadsheet_id:
        raise RuntimeError("bank_recurring_target_mismatch")
    window = build_incremental_window(policy, state, now)
    run_id = str(uuid4())
    summary = _base_summary(run_id, window)
    expected_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=Path(repo_root), text=True,
    ).strip()
    files = list_bounded_bank_pdfs(
        drive_service, policy.expected_drive_folder_id, window, policy.max_files,
    )
    summary["files_seen"] = len(files)
    downloader = download or (lambda file_id: download_drive_file(file_id, service=drive_service))
    existing_ids = {
        str(row[0]).strip()
        for row in db.get("取込データ!A2:L")
        if row and str(row[0]).strip()
    }
    candidates: list[tuple[dict, Path, tuple[str, ...], dict]] = []
    temp_paths: list[Path] = []
    try:
        for file in files:
            if _processed(file):
                continue
            summary["files_new"] = int(summary["files_new"]) + 1
            path = _temporary_pdf(downloader(str(file["id"])))
            temp_paths.append(path)
            daily = build_bank_daily_preview(
                db,
                path,
                target_spreadsheet_id=policy.expected_spreadsheet_id,
                expected_git_head=expected_head,
                account_alias=policy.account_alias or None,
                confirmed_internal_transfers=confirmed_internal_transfers,
                confirmed_non_own_classifications=confirmed_non_own_classifications,
                card_statement_authorities=tuple(card_statement_authorities),
            )
            details = daily.summary
            summary["parsed"] = int(summary["parsed"]) + int(details.get("parsed", 0))
            summary["duplicate"] = int(summary["duplicate"]) + int(details.get("existing_duplicate", 0))
            file_review = int(details.get("true_unknown", 0)) + int(
                details.get("operator_confirmed_non_own_review", 0)
            )
            summary["review"] = int(summary["review"]) + file_review
            summary["income"] = int(summary["income"]) + int(details.get("new_income", 0))
            summary["non_expense"] = int(summary["non_expense"]) + sum(
                int(details.get(name, 0))
                for name in (
                    "card_settlement_suppressed",
                    "own_transfer_suppressed",
                    "cash_withdrawal_suppressed",
                    "reimbursement_suppressed",
                )
            )
            summary["withheld"] = int(summary["withheld"]) + int(details.get("withheld_by_classification", 0))
            if file_review or int(details.get("collision", 0)):
                summary["files_withheld"] = int(summary["files_withheld"]) + 1
                path.unlink(missing_ok=True)
                continue
            expense_identities = tuple(daily.expense_candidate_identities)
            new_ids = tuple(identity for identity in expense_identities if identity not in existing_ids)
            existing_ids.update(new_ids)
            summary["new_eligible"] = int(summary["new_eligible"]) + len(new_ids)
            summary["planned_expense_writes"] = int(summary["planned_expense_writes"]) + len(new_ids)
            if new_ids:
                candidates.append((file, path, new_ids, details))
            else:
                path.unlink(missing_ok=True)
                if not details.get("true_unknown") and not details.get("collision"):
                    summary["files_processed"] = int(summary["files_processed"]) + 1
                    if not dry_run:
                        _mark_processed(drive_service, file)
                else:
                    summary["files_withheld"] = int(summary["files_withheld"]) + 1
        if int(summary["new_eligible"]) > policy.max_rows:
            raise RuntimeError("bank_recurring_row_bound_exceeded")
        if not candidates:
            summary.update({"status": "dry_run_noop" if dry_run else "noop", "safe_noop": True})
            state.record(summary, advance_checkpoint=not dry_run)
            return summary
        if dry_run:
            summary["status"] = "dry_run_ready"
            state.record(summary, advance_checkpoint=False)
            return summary
        if not audit_key_file:
            raise RuntimeError("bank_recurring_audit_key_required")
        recurring_context = create_bank_recurring_run_context(
            authority_provider=authority_provider,
            run_id=run_id,
            now=now,
            spreadsheet_id=str(db.sid),
            drive_folder_id=policy.expected_drive_folder_id,
            files_seen=int(summary["files_seen"]),
            candidate_batches=(identities for _, _, identities, _ in candidates),
        )
        for file, path, identities, details in candidates:
            try:
                batch_state_dir = state.path.parent / (
                    "bank-pdf-batch-" + hashlib.sha256(str(file["id"]).encode("utf-8")).hexdigest()[:16]
                )
                batch_state_dir.mkdir(parents=True, exist_ok=True)
                result = run_bank_recurring_production_batch(
                    db,
                    path,
                    selected_source_identities=identities,
                    approved_target_spreadsheet_id=policy.expected_spreadsheet_id,
                    expected_git_head=expected_head,
                    expected_branch=policy.expected_branch,
                    repo_root=repo_root,
                    state_dir=batch_state_dir,
                    audit_key_file=audit_key_file,
                    recurring_context=recurring_context,
                    account_alias=policy.account_alias or None,
                    confirmed_internal_transfers=confirmed_internal_transfers,
                    confirmed_non_own_classifications=confirmed_non_own_classifications,
                    card_statement_authorities=tuple(card_statement_authorities),
                    clock=lambda: datetime.now(timezone.utc),
                    sleeper=sleeper or __import__("time").sleep,
                    steady_state=True,
                )
                summary["written"] = int(summary["written"]) + int(result.get("confirmed_count", 0))
                summary["write_requests"] = int(summary["write_requests"]) + int(result.get("write_request_count", 0))
                summary["recurring_authority_ref"] = result.get("recurring_authority_ref", "")
                summary["files_processed"] = int(summary["files_processed"]) + 1
                _mark_processed(drive_service, file)
            finally:
                path.unlink(missing_ok=True)
        summary.update({"status": "complete", "write_attempted": int(summary["written"])})
        state.record(summary, advance_checkpoint=True)
        return summary
    except Exception as exc:
        for path in temp_paths:
            path.unlink(missing_ok=True)
        summary.update({"status": "failed", "failure": 1, "failure_reason": str(exc)})
        state.record(summary, advance_checkpoint=False)
        return summary
