"""Preview-only projection of Payroll storage plans."""

from __future__ import annotations

from .materialization import (
    MaterializationOperation,
    MaterializationPlan,
    MaterializationPrecondition,
    MaterializationSource,
)
from .payroll_storage import PayrollStorageCandidate
from .payroll_storage_preview import PayrollAppendPlan, PayrollSavePlan


PLAN_VERSION = "payroll-storage-preview-v1"


def _source(candidate: PayrollStorageCandidate) -> MaterializationSource:
    statement = candidate.statement
    if statement.source_file_id:
        return MaterializationSource(
            identity_kind="source_file_id",
            identity_value=statement.source_file_id,
            content_hash=statement.content_hash,
        )
    if statement.content_hash:
        # A hash is only a fallback source identity when no provider file ID was
        # supplied; it remains separately labelled as the content hash.
        return MaterializationSource(
            identity_kind="content_hash",
            identity_value=statement.content_hash,
            content_hash=statement.content_hash,
        )
    raise ValueError("payroll_source_identity_required")


def payroll_storage_to_materialization_plan(
    candidate: PayrollStorageCandidate,
    append_plan: PayrollAppendPlan,
    save_plan: PayrollSavePlan,
) -> MaterializationPlan:
    """Project already-decided Payroll preview intent without re-evaluation."""

    if not isinstance(candidate, PayrollStorageCandidate):
        raise TypeError("payroll_storage_candidate_required")
    if not isinstance(append_plan, PayrollAppendPlan):
        raise TypeError("payroll_append_plan_required")
    if not isinstance(save_plan, PayrollSavePlan):
        raise TypeError("payroll_save_plan_required")
    if append_plan.statement_id != candidate.statement.statement_id:
        raise ValueError("payroll_statement_id_mismatch")

    source = _source(candidate)
    blocked = append_plan.action == "blocked_schema"
    preconditions = (
        MaterializationPrecondition("planned_action", append_plan.action),
        MaterializationPrecondition("duplicate_status", save_plan.duplicate_status),
        MaterializationPrecondition(
            "duplicate_reason", save_plan.duplicate_reason or "none",
        ),
    )
    operations: tuple[MaterializationOperation, ...] = ()
    if append_plan.action == "append":
        operations = (
            MaterializationOperation(
                "append_statement_header", "append_row",
                {"resource": "google_sheets", "sheet_key": "payroll_statements"},
                {"statement_id": append_plan.statement_id, "row_count": append_plan.header_rows_to_append},
                preconditions,
            ),
            MaterializationOperation(
                "append_statement_items", "append_row",
                {"resource": "google_sheets", "sheet_key": "payroll_items"},
                {"statement_id": append_plan.statement_id, "row_count": append_plan.item_rows_to_append},
                preconditions,
            ),
        )

    return MaterializationPlan(
        domain="payroll",
        plan_version=PLAN_VERSION,
        source=source,
        operations=operations,
        blocked=blocked,
        blocked_reason="schema_invalid" if blocked else None,
        provenance={
            "existing_action": append_plan.action,
            "statement_id": append_plan.statement_id,
            "statement_type": append_plan.statement_type,
            "pay_period": append_plan.pay_period,
            "review_reason": list(append_plan.review_reason),
            "review_reason_counts": dict(save_plan.review_reason_counts),
            "needs_review_count": save_plan.needs_review_count,
            "duplicate_status": save_plan.duplicate_status,
            "duplicate_reason": save_plan.duplicate_reason or "none",
            "would_create_header": save_plan.would_create_header,
            "would_create_items": save_plan.would_create_items,
        },
    )
