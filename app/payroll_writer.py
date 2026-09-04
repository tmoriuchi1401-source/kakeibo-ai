from __future__ import annotations

from collections import Counter
from typing import Callable, Iterable, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from .payroll_storage import PAYROLL_ITEM_COLUMNS, PAYROLL_STATEMENT_COLUMNS
from .payroll_storage_preview import PayrollPlannedRow, PayrollWritePlan


class PayrollWriterContractError(RuntimeError):
    """Raised before any write when a plan violates the writer contract."""


class PayrollAppendAdapter(Protocol):
    """Minimal append-only boundary; no update, delete, or row construction."""

    def append_header_rows(self, rows: tuple[PayrollPlannedRow, ...]) -> None: ...

    def append_item_rows(self, rows: tuple[PayrollPlannedRow, ...]) -> None: ...


class PayrollWritePreview(BaseModel):
    model_config = ConfigDict(frozen=True)

    plan_count: int
    ready_count: int
    blocked_count: int
    skipped_duplicate_count: int
    header_rows: int
    item_rows: int


class PayrollPlanApplyResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    statement_id: str
    outcome: Literal[
        "written", "skipped", "header_outcome_unknown", "partial_failure",
    ]
    reason: str
    header_rows_confirmed: int = 0
    item_rows_confirmed: int = 0
    failure_stage: Literal["header", "items"] | None = None
    outcome_unknown: bool = False
    error_type: str | None = None


class PayrollWriteBatchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal[
        "completed", "stale_plan", "header_outcome_unknown", "partial_failure",
    ]
    applied: bool
    results: tuple[PayrollPlanApplyResult, ...] = ()
    not_attempted_statement_ids: tuple[str, ...] = ()


def _contract_error(index: int, detail: str) -> PayrollWriterContractError:
    return PayrollWriterContractError(f"plan[{index}] writer contract violation: {detail}")


def _validate_row_columns(
    rows: tuple[PayrollPlannedRow, ...],
    expected: tuple[str, ...],
    *,
    index: int,
    kind: str,
) -> None:
    for row in rows:
        if row.columns != expected or len(row.values) != len(expected):
            raise _contract_error(index, f"{kind} row schema mismatch")


def _validate_plan(plan: PayrollWritePlan, index: int) -> None:
    if not isinstance(plan, PayrollWritePlan):
        raise _contract_error(index, "not a PayrollWritePlan")

    if plan.status == "ready":
        if plan.eligibility != "eligible":
            raise _contract_error(index, "ready plan is not eligible")
        if plan.reason != "safe_new_statement" or plan.duplicate.status != "new":
            raise _contract_error(index, "ready plan is not authoritative new statement")
        if plan.header_action != "append" or plan.item_action != "append":
            raise _contract_error(index, "ready plan actions are not append")
        if len(plan.planned_header_rows) != 1 or not plan.planned_item_rows:
            raise _contract_error(index, "ready plan rows are incomplete")
    else:
        if plan.eligibility != "ineligible":
            raise _contract_error(index, "non-ready plan is eligible")
        if plan.header_action != "none" or plan.item_action != "none":
            raise _contract_error(index, "non-ready plan contains write action")
        if plan.planned_header_rows or plan.planned_item_rows:
            raise _contract_error(index, "non-ready plan contains rows")
        return

    _validate_row_columns(
        plan.planned_header_rows, PAYROLL_STATEMENT_COLUMNS,
        index=index, kind="header",
    )
    _validate_row_columns(
        plan.planned_item_rows, PAYROLL_ITEM_COLUMNS,
        index=index, kind="item",
    )

    header = plan.planned_header_rows[0].as_dict()
    identity_fields = (
        "statement_id", "employer_id", "statement_type", "source_file_id",
        "content_hash", "pay_period",
    )
    for field in identity_fields:
        if header[field] != getattr(plan.identity, field):
            raise _contract_error(index, f"header identity mismatch: {field}")
    if any(row.as_dict()["statement_id"] != plan.identity.statement_id
           for row in plan.planned_item_rows):
        raise _contract_error(index, "item statement_id mismatch")


def validate_payroll_write_contract(plans: Iterable[PayrollWritePlan]) -> tuple[PayrollWritePlan, ...]:
    """Validate structure only; business eligibility remains plan-authoritative."""
    plans = tuple(plans)
    for index, plan in enumerate(plans):
        _validate_plan(plan, index)
    duplicate_statement_ids = {
        statement_id for statement_id, count in Counter(
            plan.identity.statement_id for plan in plans if plan.status == "ready"
        ).items() if count > 1
    }
    if duplicate_statement_ids:
        raise PayrollWriterContractError(
            "writer contract violation: repeated ready statement_id",
        )
    return plans


def preview_payroll_write(plans: Iterable[PayrollWritePlan]) -> PayrollWritePreview:
    """Validate and summarize without accepting an adapter or performing I/O."""
    plans = validate_payroll_write_contract(plans)
    return PayrollWritePreview(
        plan_count=len(plans),
        ready_count=sum(plan.status == "ready" for plan in plans),
        blocked_count=sum(plan.status == "blocked" for plan in plans),
        skipped_duplicate_count=sum(
            plan.status == "skipped_duplicate" for plan in plans
        ),
        header_rows=sum(len(plan.planned_header_rows) for plan in plans),
        item_rows=sum(len(plan.planned_item_rows) for plan in plans),
    )


def apply_payroll_write_plans(
    preview_plans: Iterable[PayrollWritePlan],
    writer: PayrollAppendAdapter,
    *,
    confirmed: bool,
    latest_plans: Callable[[], Iterable[PayrollWritePlan]],
) -> PayrollWriteBatchResult:
    """Append authoritative rows after a latest-snapshot preflight.

    The callable must rebuild plans from the same candidates and a fresh Sheets
    snapshot. A failed append has an ambiguous remote outcome, so later stages
    stop and no rollback or automatic retry is attempted.
    """
    if not confirmed:
        raise RuntimeError("--apply が必要です")

    preview_plans = validate_payroll_write_contract(preview_plans)
    current_plans = validate_payroll_write_contract(latest_plans())
    if current_plans != preview_plans:
        return PayrollWriteBatchResult(
            status="stale_plan",
            applied=False,
            not_attempted_statement_ids=tuple(
                plan.identity.statement_id for plan in preview_plans
            ),
        )

    results: list[PayrollPlanApplyResult] = []
    for index, plan in enumerate(current_plans):
        if plan.status != "ready":
            results.append(PayrollPlanApplyResult(
                statement_id=plan.identity.statement_id,
                outcome="skipped",
                reason=plan.status,
            ))
            continue

        try:
            writer.append_header_rows(plan.planned_header_rows)
        except Exception as exc:
            return PayrollWriteBatchResult(
                status="header_outcome_unknown",
                applied=False,
                results=tuple([*results, PayrollPlanApplyResult(
                    statement_id=plan.identity.statement_id,
                    outcome="header_outcome_unknown",
                    reason="adapter_failure",
                    failure_stage="header",
                    outcome_unknown=True,
                    error_type=type(exc).__name__,
                )]),
                not_attempted_statement_ids=tuple(
                    remaining.identity.statement_id
                    for remaining in current_plans[index + 1:]
                ),
            )

        try:
            writer.append_item_rows(plan.planned_item_rows)
        except Exception as exc:
            return PayrollWriteBatchResult(
                status="partial_failure",
                applied=False,
                results=tuple([*results, PayrollPlanApplyResult(
                    statement_id=plan.identity.statement_id,
                    outcome="partial_failure",
                    reason="adapter_failure",
                    header_rows_confirmed=len(plan.planned_header_rows),
                    failure_stage="items",
                    outcome_unknown=True,
                    error_type=type(exc).__name__,
                )]),
                not_attempted_statement_ids=tuple(
                    remaining.identity.statement_id
                    for remaining in current_plans[index + 1:]
                ),
            )

        results.append(PayrollPlanApplyResult(
            statement_id=plan.identity.statement_id,
            outcome="written",
            reason="completed",
            header_rows_confirmed=len(plan.planned_header_rows),
            item_rows_confirmed=len(plan.planned_item_rows),
        ))

    return PayrollWriteBatchResult(
        status="completed",
        applied=True,
        results=tuple(results),
    )
