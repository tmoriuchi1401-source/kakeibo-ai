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
import re
from pathlib import Path
import subprocess
import tempfile
from typing import Callable, Iterable, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

from .aupay_card_recurring import SqliteRecurringRunState
from .aupay_card_production import ProtectedAuditKeyProvider
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
from .bank_income_recurring import BankRecurringIncome, require_income_actions
from .drive_processed import move_processed, validate_processed_folder
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


def _mark_processed(service, file: Mapping[str, object], inbox_id="", processed_id="") -> None:
    properties = dict(file.get("appProperties") or {})
    properties[BANK_PROCESSED_PROPERTY] = datetime.now(timezone.utc).isoformat()
    if processed_id:
        move_processed(service, str(file["id"]), inbox_id, processed_id, properties)
        return
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
        "household_income_confirmed": 0,
        "household_income_review": 0,
        "income_write_enabled": False,
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
    income_write_enabled: bool = False,
    processed_folder_id: str = "",
) -> dict[str, object]:
    policy = authority_provider.load()
    if not (policy.valid_from <= now < policy.expires_at):
        raise RuntimeError("bank_recurring_authority_expired_or_not_started")
    if str(db.sid) != policy.expected_spreadsheet_id:
        raise RuntimeError("bank_recurring_target_mismatch")
    if processed_folder_id:
        processed_folder_id = normalize_folder_id(processed_folder_id)
        if processed_folder_id == normalize_folder_id(policy.expected_drive_folder_id):
            raise ValueError("processed_folder_is_inbox")
        if not dry_run:
            validate_processed_folder(drive_service, policy.expected_drive_folder_id, processed_folder_id)
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
    pending_processed = []

    def finish_pending():
        for pending in pending_processed:
            _mark_processed(drive_service, pending, policy.expected_drive_folder_id, processed_folder_id)
            summary["files_processed"] = int(summary["files_processed"]) + 1

    income = None
    try:
        if income_write_enabled:
            if income_write_enabled is not True:
                raise RuntimeError("bank_income_recurring_flag_invalid")
            if not dry_run:
                require_income_actions(repo_root, expected_head)
            income = BankRecurringIncome(db, policy, state, summary, now, dict(
                confirmed_internal_transfers=confirmed_internal_transfers,
                confirmed_non_own_classifications=confirmed_non_own_classifications))
            if not dry_run:
                income.evidence.require_existing_history()
            income.evidence.reconcile(dry_run=dry_run)
            summary["income_write_enabled"] = not dry_run
        for file in files:
            already_processed = _processed(file)
            if already_processed and not income and not processed_folder_id:
                continue
            summary["files_new"] = int(summary["files_new"]) + int(not already_processed)
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
            household = details.get("household_income", {}).get("classification", {})
            summary["household_income_confirmed"] += int(household.get("confirmed_income", {}).get("count", 0))
            summary["household_income_review"] += int(household.get("needs_review", {}).get("count", 0))
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
            if income:
                if int(details.get("collision", 0)):
                    raise RuntimeError("bank_income_recurring_pdf_collision")
                income.collect(daily, held=bool(file_review))
                if already_processed and not processed_folder_id:
                    # Old expense-only runs could mark an income-only PDF.
                    # Revisit deposits within this bounded window; never replay
                    # expenses or rewrite the existing processed marker.
                    path.unlink(missing_ok=True)
                    continue
            if file_review or int(details.get("collision", 0)):
                summary["files_withheld"] = int(summary["files_withheld"]) + 1
                path.unlink(missing_ok=True)
                continue
            # An expense-only legacy marker is not proof that deposits finished.
            # Keep unresolved deposits/empty parses available for processing.
            archive_ready = not (
                int(household.get("needs_review", {}).get("count", 0))
                or (int(details.get("new_income", 0)) and not income)
                or not int(details.get("parsed", 0))
            )
            if processed_folder_id and not archive_ready:
                summary["files_withheld"] = int(summary["files_withheld"]) + 1
            expense_identities = tuple(daily.expense_candidate_identities)
            new_ids = tuple(identity for identity in expense_identities if identity not in existing_ids)
            existing_ids.update(new_ids)
            summary["new_eligible"] = int(summary["new_eligible"]) + len(new_ids)
            summary["planned_expense_writes"] = int(summary["planned_expense_writes"]) + len(new_ids)
            if new_ids:
                candidates.append((file, path, new_ids, {**details, "archive_ready": archive_ready}))
            else:
                path.unlink(missing_ok=True)
                if (not details.get("true_unknown") and not details.get("collision")
                        and (not processed_folder_id or archive_ready)):
                    # A duplicate can overlap a candidate from this same run.
                    # Archive it only after those candidate writes complete.
                    pending_processed.append(file)
                    if dry_run:
                        summary["files_processed"] = int(summary["files_processed"]) + 1
        if int(summary["new_eligible"]) > policy.max_rows:
            raise RuntimeError("bank_recurring_row_bound_exceeded")
        if income:
            summary.update(income.plan(identity for _, _, ids, _ in candidates for identity in ids))
            income_changes = summary["planned_income_writes"] + summary["planned_deposit_imports"]
            if dry_run and (income_changes or candidates):
                summary["status"] = "dry_run_ready"
                state.record(summary, advance_checkpoint=False)
                return summary
            if not dry_run:
                if (income_changes or candidates) and not audit_key_file:
                    raise RuntimeError("bank_recurring_audit_key_required")
                if (income_changes or candidates) and ProtectedAuditKeyProvider(
                        audit_key_file, repo_root=repo_root).load() is None:
                    raise RuntimeError("bank_recurring_audit_key_required")
                # Persist deposits first. Every processed PDF then has a saved-row
                # resume path even when the income append or expense stage fails.
                summary.update(income.apply())
                summary["write_requests"] += int(bool(summary["income_created"])) + int(bool(summary["deposit_imports_created"]))
                if income_changes and not candidates:
                    finish_pending()
                    summary.update(status="complete", write_attempted=summary["income_created"],
                                   written=summary["income_created"])
                    state.record(summary, advance_checkpoint=True)
                    return summary
        if not candidates:
            if not dry_run:
                finish_pending()
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
                if int(result.get("confirmed_count", 0)) != len(identities):
                    raise RuntimeError("bank_recurring_write_unverified")
                if not processed_folder_id or details["archive_ready"]:
                    _mark_processed(drive_service, file, policy.expected_drive_folder_id, processed_folder_id)
                    summary["files_processed"] = int(summary["files_processed"]) + 1
            finally:
                path.unlink(missing_ok=True)
        finish_pending()
        summary["written"] = int(summary["written"]) + int(summary.get("income_created", 0))
        summary.update({"status": "complete", "write_attempted": int(summary["written"])})
        state.record(summary, advance_checkpoint=True)
        return summary
    except Exception as exc:
        for path in temp_paths:
            path.unlink(missing_ok=True)
        reason = str(exc)
        if income_write_enabled and not re.fullmatch(r"(?:bank_|protected_audit_)[a-z_]+", reason):
            reason = type(exc).__name__
        summary.update({"status": "failed", "failure": 1, "failure_reason": reason})
        state.record(summary, advance_checkpoint=False)
        return summary
