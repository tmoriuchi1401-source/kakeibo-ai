from __future__ import annotations

from typing import Callable, Iterable, Literal

from pydantic import BaseModel, ConfigDict

from .payroll_google_sheets_adapter import PayrollRecoveryReadAdapter
from .payroll_recovery import PayrollRecoveryPreview, build_payroll_recovery_preview
from .payroll_storage_preview import PayrollWritePlan
from .payroll_writer import (
    PayrollAppendAdapter,
    PayrollWriteBatchResult,
    apply_payroll_write_plans,
    validate_payroll_write_contract,
)


class PayrollApplyRecoveryStatementResult(BaseModel):
    """One intended statement after one guarded write invocation and read-back."""

    model_config = ConfigDict(frozen=True)

    statement_id: str
    status: Literal[
        "confirmed",
        "no_write_required",
        "uncertain_requires_readback_or_review",
        "safe_to_create_fresh_plan",
        "not_attempted",
    ]
    writer_outcome: str | None = None
    writer_was_attempted: bool = False
    recovery: PayrollRecoveryPreview | None = None


class PayrollApplyRecoveryResult(BaseModel):
    """Application-level outcome; a write response alone is never confirmed."""

    model_config = ConfigDict(frozen=True)

    status: Literal[
        "confirmed",
        "no_write_required",
        "uncertain_requires_readback_or_review",
        "safe_to_create_fresh_plan",
        "not_attempted",
    ]
    write_result: PayrollWriteBatchResult
    statements: tuple[PayrollApplyRecoveryStatementResult, ...]
    automatic_retry_performed: Literal[False] = False
    external_write_authorized: Literal[False] = False


def _writer_was_attempted(outcome: str | None) -> bool:
    return outcome not in {None, "skipped"}


def _status_from_recovery(
    recovery: PayrollRecoveryPreview,
    *,
    writer_outcome: str | None,
) -> Literal[
    "confirmed",
    "no_write_required",
    "uncertain_requires_readback_or_review",
    "safe_to_create_fresh_plan",
]:
    if recovery.verification == "confirmed":
        return "confirmed" if _writer_was_attempted(writer_outcome) else "no_write_required"
    if recovery.verification == "missing":
        return "safe_to_create_fresh_plan"
    return "uncertain_requires_readback_or_review"


def _batch_status(
    statements: tuple[PayrollApplyRecoveryStatementResult, ...],
) -> Literal[
    "confirmed",
    "no_write_required",
    "uncertain_requires_readback_or_review",
    "safe_to_create_fresh_plan",
    "not_attempted",
]:
    statuses = {statement.status for statement in statements}
    if not statuses or statuses == {"not_attempted"}:
        return "not_attempted"
    if statuses <= {"confirmed", "no_write_required"}:
        return "confirmed" if "confirmed" in statuses else "no_write_required"
    if statuses == {"safe_to_create_fresh_plan"}:
        return "safe_to_create_fresh_plan"
    return "uncertain_requires_readback_or_review"


def apply_payroll_write_with_recovery(
    preview_plans: Iterable[PayrollWritePlan],
    writer: PayrollAppendAdapter,
    recovery_reader: PayrollRecoveryReadAdapter,
    *,
    confirmed: bool,
    latest_plans: Callable[[], Iterable[PayrollWritePlan]],
) -> PayrollApplyRecoveryResult:
    """Run one explicit write batch, then evaluate its remote state read-only.

    ``apply_payroll_write_plans`` remains the sole write authority and is called
    exactly once. This function never retries a write. A stale preflight is
    still read back: that can safely identify an already-complete record without
    writing, while missing data only instructs the caller to build a fresh plan.
    """
    preview_plans = validate_payroll_write_contract(preview_plans)
    write_result = apply_payroll_write_plans(
        preview_plans,
        writer,
        confirmed=confirmed,
        latest_plans=latest_plans,
    )
    writer_results = {
        result.statement_id: result for result in write_result.results
    }
    stale_preflight = write_result.status == "stale_plan"
    statements = []
    for plan in preview_plans:
        if plan.status != "ready":
            statements.append(PayrollApplyRecoveryStatementResult(
                statement_id=plan.identity.statement_id,
                status="not_attempted",
            ))
            continue

        writer_result = writer_results.get(plan.identity.statement_id)
        if writer_result is None and not stale_preflight:
            statements.append(PayrollApplyRecoveryStatementResult(
                statement_id=plan.identity.statement_id,
                status="not_attempted",
            ))
            continue

        writer_outcome = writer_result.outcome if writer_result else None
        recovery = build_payroll_recovery_preview(plan, recovery_reader)
        statements.append(PayrollApplyRecoveryStatementResult(
            statement_id=plan.identity.statement_id,
            status=_status_from_recovery(
                recovery, writer_outcome=writer_outcome,
            ),
            writer_outcome=writer_outcome,
            writer_was_attempted=_writer_was_attempted(writer_outcome),
            recovery=recovery,
        ))

    statement_results = tuple(statements)
    return PayrollApplyRecoveryResult(
        status=_batch_status(statement_results),
        write_result=write_result,
        statements=statement_results,
    )
