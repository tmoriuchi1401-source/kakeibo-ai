"""Preview-only projection of PayPay coverage confirmation plans."""

from __future__ import annotations

from .coverage_confirmation import (
    COVERAGE_CONFIRMATION_HEADERS,
    COVERAGE_CONFIRMATION_SHEET,
    COVERAGE_REASON_OPERATIONAL_ONLY,
    COVERAGE_STATUS_USER_CONFIRMED,
    coverage_confirmation_id,
)
from .coverage_confirmation_sheets_apply import CoverageConfirmationWritePlan
from .materialization import (
    MaterializationOperation,
    MaterializationPlan,
    MaterializationPrecondition,
    MaterializationSource,
)


PLAN_VERSION = "paypay-coverage-confirmation-v1"


def _preconditions(plan: CoverageConfirmationWritePlan) -> tuple[MaterializationPrecondition, ...]:
    return (
        MaterializationPrecondition("target_spreadsheet_id", plan.target_spreadsheet_id),
        MaterializationPrecondition("sheet_status", plan.expected_sheet_status),
        MaterializationPrecondition(
            "duplicate_status", plan.expected_duplicate_status or "not_applicable",
        ),
    )


def coverage_confirmation_to_materialization_plan(
    plan: CoverageConfirmationWritePlan,
) -> MaterializationPlan:
    """Project an existing PayPay write-free plan without invoking apply."""

    if not isinstance(plan, CoverageConfirmationWritePlan):
        raise TypeError("coverage_confirmation_write_plan_required")
    record = plan.record
    source = MaterializationSource(
        identity_kind="provider_content_sha256",
        identity_value=f"{record.provider}:{record.content_sha256}",
        provider=record.provider,
        content_hash=record.content_sha256,
    )
    target = {
        "resource": "google_sheets",
        "spreadsheet_id": plan.target_spreadsheet_id,
        "sheet_name": COVERAGE_CONFIRMATION_SHEET,
    }
    preconditions = _preconditions(plan)
    operations: tuple[MaterializationOperation, ...] = ()
    if not plan.blocked and plan.action_requested == "create_and_append":
        operations = (
            MaterializationOperation(
                "create_sheet", "create_sheet", target,
                {"sheet_name": COVERAGE_CONFIRMATION_SHEET}, preconditions,
            ),
            MaterializationOperation(
                "write_header", "update_cells",
                {**target, "range": f"{COVERAGE_CONFIRMATION_SHEET}!A1:N1"},
                {"values": list(COVERAGE_CONFIRMATION_HEADERS)}, preconditions,
            ),
            MaterializationOperation(
                "append_confirmation", "append_row",
                {**target, "range": f"{COVERAGE_CONFIRMATION_SHEET}!A:A"},
                {"values": list(plan.candidate_row)}, preconditions,
            ),
        )
    elif not plan.blocked and plan.action_requested == "append":
        operations = (
            MaterializationOperation(
                "append_confirmation", "append_row",
                {**target, "range": f"{COVERAGE_CONFIRMATION_SHEET}!A:A"},
                {"values": list(plan.candidate_row)}, preconditions,
            ),
        )
    elif plan.action_requested not in {"blocked", "skip_duplicate"}:
        raise ValueError("unsupported_coverage_confirmation_action")

    return MaterializationPlan(
        domain="paypay",
        plan_version=PLAN_VERSION,
        source=source,
        operations=operations,
        blocked=plan.blocked,
        blocked_reason=plan.reason if plan.blocked else None,
        provenance={
            "existing_action": plan.action_requested,
            "existing_duplicate_status": plan.expected_duplicate_status or "not_applicable",
            "confirmation_id": coverage_confirmation_id(record.identity),
            "coverage_status": COVERAGE_STATUS_USER_CONFIRMED,
            "coverage_reason": COVERAGE_REASON_OPERATIONAL_ONLY,
        },
    )
