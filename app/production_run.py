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
    'source_execution_failed', 'source_timeout', 'source_connection_failed',
    'source_tls_failed', 'source_invalid_data', 'source_internal_error',
    'receipt_response_invalid',
    'projection_drive_read_failed', 'projection_drive_write_unknown',
    'projection_state_changed', 'projection_readback_failed',
    'projection_bootstrap_required', 'projection_range_incomplete',
    'projection_catalog_invalid', 'projection_index_invalid', 'projection_month_invalid',
    'projection_journal_invalid', 'projection_file_invalid',
    'projection_folder_sharing_mismatch', 'projection_file_sharing_mismatch',
    'state_drive_read_failed_401', 'state_drive_read_failed_403', 'state_drive_read_failed_404',
    'state_drive_read_failed_429', 'state_drive_read_failed_500', 'state_drive_read_failed_502',
    'state_drive_read_failed_503', 'state_drive_read_failed_504', 'receipt_audit_state_changed',
    'confirmation_archive_readback_required', 'confirmation_archive_folders_invalid',
    'confirmation_readback_mismatch',
    'confirmation_source_changed', 'confirmation_store_permissions_changed',
    'confirmation_store_invalid',
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
    'bank_recurring_drive_folder_mismatch', 'protected_bank_recurring_authority_invalid',
    'bank_recurring_authority_expired_or_not_started', 'bank_recurring_target_mismatch',
    'bank_recurring_window_exceeds_authority', 'bank_recurring_file_bound_exceeded',
    'processed_folder_is_inbox',
})


def safe_source_error(error):
    return str(error) if isinstance(error, StateError) and str(error) in SAFE_SOURCE_ERRORS else 'source_execution_failed'


# These are data dependencies, not simply a list of workflows to concatenate.
# Card monetary handling follows Amazon supplements and receipt import; final posting waits for
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
    'archived', 'review_eligible', 'review_closable',
    "projection_months", "projection_rows", "projection_sheet_requests", "projection_sheet_cells",
    "projection_drive_reads", "projection_drive_writes",
    "daily_changed_blocks", "daily_write_requests",
    "corrections_submitted", "corrections_applied", "corrections_failed", "corrections_pending",
    "correction_form_ready", "correction_input_changed",
    "money_reviews_submitted", "money_reviews_applied", "money_reviews_failed", "money_reviews_pending",
    "coverage_submitted", "coverage_applied", "coverage_failed", "coverage_pending", "coverage_form_ready", "coverage_input_changed",
    "categories_submitted", "categories_applied", "categories_failed", "categories_pending", "category_form_ready", "category_input_changed",
    "money_review_form_ready", "money_review_input_changed",
    "money_eligible", "money_posted", "money_linked", "money_review", "money_duplicate", "money_supplement", "money_transfer",
    "money_canary_selected", "money_canary_verified", "money_canary_replay",
    "money_notice_review",
    "medical_local_written",
    "found", "fetched", "new", "written", "written_purchases", "new_eligible",
    "already_present", "duplicate", "needs_review", "review", "withheld", "deferred",
    "gemini_quota_deferred", "gemini_unavailable_deferred",
    "failure", "errors", "failed_files", "imported_files", "skipped_files",
    "files_seen", "files_new", "files_processed", "write_requests", "catch_up_pending",
    "expenses_created", "expenses_updated", "updated", "unchanged",
    "event_rows_written", "header_rows_written", "import_rows_written", "expense_rows_written",
    "eligible_purchases", "new_event_rows", "new_header_rows",
    "income_created", "deposit_imports_created", "planned_income_writes", "planned_deposit_imports",
    "income_existing", "planned_deposit_reviews", "deposit_reviews_saved", "income_review_pending", "bounded_rows",
    "medical_ai_requests", "medical_ai_reused", "medical_ai_candidates", "medical_ai_held",
    "review_pending", "normal_review_pending", "medical_review_pending", "intake_review_pending",
})


SOURCE_STAGES = frozenset({'receipt_preflight', 'receipt_processing',
                           'receipt_archive', 'receipt_projection'})


class SourceFailure(StateError):
    """Only fixed codes and completed-result counts may cross the child boundary.

    Counts are a lower bound after a failed/unknown write, never permission to
    replay or clear the durable pending marker.
    """
    def __init__(self, code, stage='', counts=None):
        super().__init__(code if isinstance(code, str) and code in SAFE_SOURCE_ERRORS
                         else 'source_execution_failed')
        self.stage = stage if isinstance(stage, str) and stage in SOURCE_STAGES else ''
        self.counts = {key: value for key, value in (counts or {}).items()
                       if key in COUNT_KEYS and type(value) is int and value >= 0}

    def report(self):
        return {**self.counts, 'failure': 1, 'error': str(self),
                **({'stage': self.stage} if self.stage else {})}


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
                   preview: bool = False, amazon_canary: bool = False, receipts_only: bool = False,
                   canary_source: str = "amazon", selected_sources: frozenset[str] | None = None,
                   require_omitted_ready: bool = False) -> dict:
    """Run fixed existing stages serially and expose only count/status metadata.

    Independent sources continue after a failure. Each dependent stage is skipped
    unless all its prerequisites succeeded. A post-processing failure remains a
    failure even if another independent display refresh succeeds.
    """
    if amazon_canary and receipts_only:
        raise StateError('conflicting_source_scopes')
    if selected_sources is not None and (not selected_sources or not selected_sources <= DEPENDENCIES.keys()
                                         or amazon_canary or receipts_only):
        raise StateError('invalid_source_scope')
    if require_omitted_ready and selected_sources is None:
        raise StateError('invalid_source_scope')
    if canary_source not in {"amazon","aupay_card"} or (canary_source!="amazon" and not amazon_canary):raise StateError("money_canary_source_invalid")
    outcomes = {}
    for source, dependencies in DEPENDENCIES.items():
        if selected_sources is not None and source not in selected_sources:
            continue
        if receipts_only and source != 'receipts':
            continue
        if amazon_canary and source != canary_source:
            continue
        started = monotonic()
        previous = (history or {}).get(source, {})
        outcome = {"status": "skipped", "error": "dependency_failed", "counts": {},
                   "last_success": previous.get("last_success"), "duration_seconds": 0.0}
        if amazon_canary or all(
                outcomes[name]["status"] == "success" if name in outcomes
                else not require_omitted_ready or (
                    (history or {}).get(name, {}).get("phase") == "ready"
                    and (name != "bank" or not (history or {}).get(name, {}).get("counts", {}).get("catch_up_pending"))
                )
                for name in dependencies):
            try:
                if source not in runners:
                    raise StateError("source_runner_missing")
                result = runners[source]()
                require_success(result)
                counts = {key: value for key, value in result.items()
                          if key in COUNT_KEYS and type(value) is int and value >= 0}
                outcome.update(status="success", error="", counts=counts)
                if source == "bank" and counts.get("catch_up_pending"):
                    outcome.update(status="partial", error="bank_catch_up_pending")
                if not preview:
                    outcome["last_success"] = datetime.now(timezone.utc).isoformat()
            except Exception as error:
                # Exception text may contain a document name, account ID or API
                # response. The safe parent summary deliberately never includes it.
                outcome.update(status="failed", error=safe_source_error(error))
                if isinstance(error, SourceFailure) and error.stage:
                    outcome.update(stage=error.stage, counts=error.report())
                    outcome['counts'] = {key: value for key, value in outcome['counts'].items()
                                         if key in COUNT_KEYS}
        outcome["duration_seconds"] = round(monotonic() - started, 3)
        outcomes[source] = outcome
    return {"schema": 1, "success": all(item["status"] == "success" for item in outcomes.values()),
            "sources": outcomes}
