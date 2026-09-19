"""Small execution boundary around existing source runners.

The production workflow/CLI assembly is intentionally separate. Nothing in this
module constructs credentials, calls Google, or introduces a second writer.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Callable, Mapping
from uuid import uuid4

from .drive_run_state import DurableState, StateError


SAFE_SOURCE_ERRORS = frozenset({
    'confirmation_sheet_header_mismatch', 'confirmation_unknown_ui_identity',
    'confirmation_duplicate_ui_identity', 'confirmation_missing_ui_identity',
    'confirmation_new_row_occupied', 'confirmation_new_row_write_unknown',
    'confirmation_new_row_readback_mismatch', 'source_reconciliation_required',
    'source_command_failed', 'source_result_invalid', 'source_reported_failure',
    'source_command_timed_out', 'receipt_intake_summary_invalid',
    'ledger_invalid', 'ledger_changed_since_read', 'ledger_readback_failed',
    'state_drive_read_failed', 'state_drive_write_unknown', 'state_save_readback_mismatch',
    'receipt_preflight_source_changed', 'receipt_preflight_content_changed',
    'receipt_inbox_collection_incomplete', 'receipt_preflight_required',
    'gemini_auth_rejected', 'gemini_quota_rejected', 'gemini_api_or_model_rejected',
    'gemini_result_invalid', 'gemini_transport_unknown', 'gemini_request_failed_unknown',
    'google_api_auth_rejected', 'google_api_quota_rejected', 'google_api_request_failed',
})


def safe_source_error(error):
    return str(error) if isinstance(error, StateError) and str(error) in SAFE_SOURCE_ERRORS else 'source_execution_failed'


# These are data dependencies, not simply a list of workflows to concatenate.
# Card classification needs the latest Amazon orders; final posting waits for
# all import outcomes, including bank preview, before making global decisions.
DEPENDENCIES = {
    "amazon": (),
    "receipts": (),
    "aupay_card": ("amazon", "receipts"),
    "aupay_balance": (),
    "paypay": (),
    "bank": ("amazon", "receipts", "aupay_card", "aupay_balance", "paypay"),
    "review_apply": ("amazon", "receipts", "aupay_card", "aupay_balance", "paypay", "bank"),
    "reconcile": ("review_apply",),
    "auto_expense": ("reconcile",),
    "review_refresh": ("auto_expense",),
    "expenses_refresh": ("auto_expense",),
}
COUNT_KEYS = frozenset({
    "projection_months", "projection_rows", "projection_sheet_requests", "projection_sheet_cells",
    "projection_drive_reads", "projection_drive_writes",
    "daily_changed_blocks", "daily_write_requests",
    "money_eligible", "money_posted", "money_linked", "money_review", "money_duplicate", "money_supplement", "money_transfer",
    "medical_local_written",
    "found", "fetched", "new", "written", "written_purchases", "new_eligible",
    "already_present", "duplicate", "needs_review", "review", "withheld", "deferred",
    "failure", "errors", "failed_files", "imported_files", "skipped_files",
    "files_seen", "files_new", "files_processed", "write_requests",
    "expenses_created", "expenses_updated", "updated", "unchanged",
    "event_rows_written", "header_rows_written", "import_rows_written", "expense_rows_written",
    "eligible_purchases", "new_event_rows", "new_header_rows",
    "income_created", "deposit_imports_created", "planned_income_writes", "planned_deposit_imports",
    "income_existing", "planned_deposit_reviews", "deposit_reviews_saved", "income_review_pending", "bounded_rows",
    "medical_ai_requests", "medical_ai_reused", "medical_ai_candidates", "medical_ai_held",
    "review_pending", "normal_review_pending", "medical_review_pending", "intake_review_pending",
})


def require_success(result: Mapping) -> None:
    if not isinstance(result, Mapping) or not result:
        raise StateError("source_result_invalid")
    for name in ("failure", "errors", "failed_files"):
        if name in result and (type(result[name]) is not int or result[name] != 0):
            raise StateError("source_reported_failure")
    if "status" in result and result["status"] not in {
        "complete", "noop", "dry_run_ready", "dry_run_noop", "success",
        "imported", "skipped", "needs_review", "privacy_blocked", "unchanged",
    }:
        raise StateError("source_reported_failure")


def run_durable_source(store: DurableState, directory: Path,
                       run: Callable[[Path], Mapping], *, apply: bool) -> Mapping:
    """Preview uses a disposable local copy; apply persists intent then success.

    A failed/unknown source leaves the old remote checkpoint pending. The parent
    must fail the run and skip dependent accounting; recovery is never automatic.
    """
    store.restore(directory)
    if apply:
        store.begin(uuid4().hex)
    result = run(directory)
    require_success(result)
    if apply:
        store.commit(directory)
    return result


def execute_serial(runners: Mapping[str, Callable[[], Mapping]], *, history: Mapping | None = None,
                   preview: bool = False, amazon_canary: bool = False, receipts_only: bool = False) -> dict:
    """Run fixed existing stages serially and expose only count/status metadata.

    Independent sources continue after a failure. Each dependent stage is skipped
    unless all its prerequisites succeeded. A post-processing failure remains a
    failure even if another independent display refresh succeeds.
    """
    if amazon_canary and receipts_only:
        raise StateError('conflicting_source_scopes')
    outcomes = {}
    for source, dependencies in DEPENDENCIES.items():
        if receipts_only and source != 'receipts':
            continue
        if amazon_canary and source != "amazon":
            continue
        started = monotonic()
        previous = (history or {}).get(source, {})
        outcome = {"status": "skipped", "error": "dependency_failed", "counts": {},
                   "last_success": previous.get("last_success"), "duration_seconds": 0.0}
        if all(outcomes[name]["status"] == "success" for name in dependencies):
            try:
                if source not in runners:
                    raise StateError("source_runner_missing")
                result = runners[source]()
                require_success(result)
                counts = {key: value for key, value in result.items()
                          if key in COUNT_KEYS and type(value) is int and value >= 0}
                outcome.update(status="success", error="", counts=counts)
                if not preview:
                    outcome["last_success"] = datetime.now(timezone.utc).isoformat()
            except Exception as error:
                # Exception text may contain a document name, account ID or API
                # response. The safe parent summary deliberately never includes it.
                outcome.update(status="failed", error=safe_source_error(error))
        outcome["duration_seconds"] = round(monotonic() - started, 3)
        outcomes[source] = outcome
    return {"schema": 1, "success": all(item["status"] == "success" for item in outcomes.values()),
            "sources": outcomes}
