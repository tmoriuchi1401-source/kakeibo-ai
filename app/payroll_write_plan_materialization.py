"""Preview-only projection of authoritative Payroll statement write plans."""

from __future__ import annotations

from .materialization import (
    MaterializationOperation,
    MaterializationPlan,
    MaterializationPrecondition,
    MaterializationSource,
)
from .payroll_sheets import SHEET_TITLES
from .payroll_storage_preview import PayrollPlannedRow, PayrollWritePlan


PLAN_VERSION = "payroll-statement-write-v1"
_HEADER_SHEET_KEY = "payroll_statements"
_ITEMS_SHEET_KEY = "payroll_items"
_RUNTIME_HEADER_FIELDS = frozenset({"imported_at"})


def _target(sheet_key: str) -> dict[str, str]:
    return {
        "resource": "google_sheets",
        "sheet_key": sheet_key,
        "sheet_title": SHEET_TITLES[sheet_key],
    }


def _semantic_rows(
    rows: tuple[PayrollPlannedRow, ...],
    *,
    excluded_fields: frozenset[str] = frozenset(),
) -> list[dict[str, object]]:
    """Copy existing prospective rows without runtime-only storage metadata."""

    return [
        {
            column: value
            for column, value in row.as_dict().items()
            if column not in excluded_fields
        }
        for row in rows
    ]


def _shared_preconditions(plan: PayrollWritePlan) -> tuple[MaterializationPrecondition, ...]:
    """Preserve preview facts; do not re-read or re-decide them."""

    return (
        MaterializationPrecondition(
            "expected_statement_identity",
            {
                "statement_id": plan.identity.statement_id,
                "source_file_id": plan.identity.source_file_id,
                "content_hash": plan.identity.content_hash,
            },
        ),
        MaterializationPrecondition(
            "expected_duplicate_state",
            {
                "status": plan.duplicate.status,
                "reason": plan.duplicate.reason,
                "matched_statement_id": plan.duplicate.matched_statement_id,
            },
        ),
    )


def _operation_preconditions(
    plan: PayrollWritePlan,
    *,
    action: str,
    row_count: int,
) -> tuple[MaterializationPrecondition, ...]:
    return (
        *_shared_preconditions(plan),
        MaterializationPrecondition("expected_write_action", action),
        MaterializationPrecondition("expected_row_count", row_count),
    )


def payroll_write_plan_to_materialization_plan(
    plan: PayrollWritePlan,
) -> MaterializationPlan:
    """Project one ready ``PayrollWritePlan`` without validation or I/O.

    The write plan is the sole authority for eligibility, source validity,
    duplicate status, row safety, and its two-stage order.  Non-ready plans
    intentionally have no prospective write intent and therefore cannot be
    projected as statement materialization plans.
    """

    if not isinstance(plan, PayrollWritePlan):
        raise TypeError("payroll_write_plan_required")
    if plan.status != "ready":
        raise ValueError("payroll_write_plan_not_ready_for_materialization")
    if not plan.identity.source_file_id or not plan.identity.content_hash:
        raise ValueError("payroll_write_plan_source_identity_required")

    header_rows = _semantic_rows(
        plan.planned_header_rows,
        excluded_fields=_RUNTIME_HEADER_FIELDS,
    )
    item_rows = _semantic_rows(plan.planned_item_rows)
    operations = (
        MaterializationOperation(
            "append_statement_header",
            "append_row",
            _target(_HEADER_SHEET_KEY),
            {"rows": header_rows},
            _operation_preconditions(
                plan,
                action=plan.header_action,
                row_count=len(header_rows),
            ),
        ),
        MaterializationOperation(
            "append_statement_items",
            "append_row",
            _target(_ITEMS_SHEET_KEY),
            {"rows": item_rows},
            _operation_preconditions(
                plan,
                action=plan.item_action,
                row_count=len(item_rows),
            ),
        ),
    )
    return MaterializationPlan(
        domain="payroll",
        plan_version=PLAN_VERSION,
        source=MaterializationSource(
            identity_kind="source_file_id",
            identity_value=plan.identity.source_file_id,
            content_hash=plan.identity.content_hash,
        ),
        operations=operations,
        provenance={
            "statement_id": plan.identity.statement_id,
            "write_plan_status": plan.status,
            "write_plan_reason": plan.reason,
            "write_plan_reasons": list(plan.reasons),
        },
    )
