"""Pure projection of existing Payroll writer outcomes into common results.

This adapter deliberately consumes the writer's already-produced result.  It
does not call the writer, an executor, or a Sheets client, and it does not make
any decision about whether a statement should be written.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .materialization import (
    MaterializationAuditRecord,
    MaterializationOperationResult,
    MaterializationPlan,
    MaterializationResult,
    build_materialization_audit_record,
)
from .payroll_storage_preview import PayrollWritePlan
from .payroll_write_plan_materialization import (
    payroll_write_plan_to_materialization_plan,
)
from .payroll_writer import PayrollPlanApplyResult, PayrollWriteBatchResult


_HEADER_OPERATION_ID = "append_statement_header"
_ITEMS_OPERATION_ID = "append_statement_items"
_EXPECTED_OPERATION_IDS = (_HEADER_OPERATION_ID, _ITEMS_OPERATION_ID)


def _require_corresponding_plan(
    payroll_plan: PayrollWritePlan,
    materialization_plan: MaterializationPlan,
    apply_result: PayrollPlanApplyResult,
) -> None:
    """Fail closed unless all identities point to the same previewed intent."""

    if not isinstance(payroll_plan, PayrollWritePlan):
        raise TypeError("payroll_write_plan_required")
    if not isinstance(materialization_plan, MaterializationPlan):
        raise TypeError("materialization_plan_required")
    if not isinstance(apply_result, PayrollPlanApplyResult):
        raise TypeError("payroll_plan_apply_result_required")

    if tuple(operation.operation_id for operation in materialization_plan.operations) != _EXPECTED_OPERATION_IDS:
        raise ValueError("payroll_materialization_expected_operations_missing")
    if materialization_plan.provenance.get("statement_id") != payroll_plan.identity.statement_id:
        raise ValueError("payroll_materialization_plan_statement_id_mismatch")
    if apply_result.statement_id != payroll_plan.identity.statement_id:
        raise ValueError("payroll_materialization_result_statement_id_mismatch")
    # Rebuilding this *pure preview projection* is an identity check, not a
    # write-plan re-decision.  It checks the statement, source, rows, targets,
    # preconditions, operation order, and deterministic plan ID together.
    expected_plan = payroll_write_plan_to_materialization_plan(payroll_plan)
    if materialization_plan.plan_id != expected_plan.plan_id:
        raise ValueError("payroll_materialization_plan_mismatch")


def _observed_state(
    apply_result: PayrollPlanApplyResult,
    *,
    operation_id: str | None = None,
) -> dict[str, object]:
    """Keep only writer-owned, privacy-safe semantic facts.

    ``MaterializationOperationResult`` intentionally has no observed-state
    field.  Operation-specific unknown facts therefore live under the common
    result's observed state, keyed by the existing operation ID.
    """

    state: dict[str, object] = {
        "statement_id": apply_result.statement_id,
        "header_rows_confirmed": apply_result.header_rows_confirmed,
        "item_rows_confirmed": apply_result.item_rows_confirmed,
    }
    if apply_result.failure_stage is not None:
        state["failure_stage"] = apply_result.failure_stage
    if apply_result.outcome_unknown:
        state["outcome_unknown"] = True
        state["writer_reason"] = apply_result.reason
        if operation_id is not None:
            state["operations"] = {
                operation_id: {
                    "outcome_unknown": True,
                    "stage": apply_result.failure_stage,
                    "writer_reason": apply_result.reason,
                },
            }
    return state


def _require_counts(
    apply_result: PayrollPlanApplyResult,
    *,
    header_rows: int,
    item_rows: int,
) -> None:
    if (apply_result.header_rows_confirmed != header_rows
            or apply_result.item_rows_confirmed != item_rows):
        raise ValueError("payroll_materialization_confirmed_row_count_mismatch")


def _failed_operation(
    operation_id: str,
    reason: str,
) -> MaterializationOperationResult:
    return MaterializationOperationResult(
        operation_id=operation_id,
        status="failed",
        external_write=False,
        reason=reason,
    )


def build_payroll_statement_materialization_result(
    payroll_plan: PayrollWritePlan,
    materialization_plan: MaterializationPlan,
    apply_result: PayrollPlanApplyResult,
) -> MaterializationResult:
    """Project one attempted statement result without doing I/O.

    The common ``external_write`` flag means a write is confirmed, never that a
    write definitely did not occur.  In particular, an unknown remote outcome
    remains ``failed``/``False`` while retaining its ambiguity in observed
    state.
    """

    _require_corresponding_plan(payroll_plan, materialization_plan, apply_result)
    header_rows = len(payroll_plan.planned_header_rows)
    item_rows = len(payroll_plan.planned_item_rows)
    requested_action = "payroll_statement_write"

    if apply_result.outcome == "written":
        if apply_result.failure_stage is not None or apply_result.outcome_unknown:
            raise ValueError("payroll_materialization_written_result_inconsistent")
        _require_counts(apply_result, header_rows=header_rows, item_rows=item_rows)
        return MaterializationResult(
            plan_id=materialization_plan.plan_id,
            status="applied",
            external_write=True,
            action_requested=requested_action,
            actions_performed=_EXPECTED_OPERATION_IDS,
            operations=(
                MaterializationOperationResult(_HEADER_OPERATION_ID, "applied", True),
                MaterializationOperationResult(_ITEMS_OPERATION_ID, "applied", True),
            ),
            reason=apply_result.reason,
            observed_after=_observed_state(apply_result),
            occurred_at=None,
        )

    if apply_result.outcome == "skipped":
        if (apply_result.failure_stage is not None or apply_result.outcome_unknown
                or apply_result.header_rows_confirmed != 0
                or apply_result.item_rows_confirmed != 0):
            raise ValueError("payroll_materialization_skipped_result_inconsistent")
        # The writer result tells us the statement was skipped, but it does not
        # report per-stage execution.  Do not invent operation outcomes.
        return MaterializationResult(
            plan_id=materialization_plan.plan_id,
            status="skipped",
            external_write=False,
            action_requested=requested_action,
            actions_performed=(),
            operations=(),
            reason=apply_result.reason,
            observed_after=_observed_state(apply_result),
            occurred_at=None,
        )

    if apply_result.outcome == "confirmed_failure":
        if (apply_result.failure_stage != "header" or apply_result.outcome_unknown):
            raise ValueError("payroll_materialization_confirmed_failure_inconsistent")
        _require_counts(apply_result, header_rows=0, item_rows=0)
        return MaterializationResult(
            plan_id=materialization_plan.plan_id,
            status="failed",
            external_write=False,
            action_requested=requested_action,
            actions_performed=(),
            operations=(_failed_operation(_HEADER_OPERATION_ID, apply_result.reason),),
            reason=apply_result.reason,
            observed_after=_observed_state(apply_result),
            occurred_at=None,
        )

    if apply_result.outcome == "header_outcome_unknown":
        if apply_result.failure_stage != "header" or not apply_result.outcome_unknown:
            raise ValueError("payroll_materialization_header_unknown_inconsistent")
        _require_counts(apply_result, header_rows=0, item_rows=0)
        return MaterializationResult(
            plan_id=materialization_plan.plan_id,
            status="failed",
            external_write=False,
            action_requested=requested_action,
            actions_performed=(),
            operations=(_failed_operation(_HEADER_OPERATION_ID, "header_outcome_unknown"),),
            reason="header_outcome_unknown",
            observed_after=_observed_state(
                apply_result, operation_id=_HEADER_OPERATION_ID,
            ),
            occurred_at=None,
        )

    if apply_result.outcome == "partial_failure":
        if apply_result.failure_stage != "items":
            raise ValueError("payroll_materialization_partial_failure_inconsistent")
        _require_counts(apply_result, header_rows=header_rows, item_rows=0)
        item_reason = "item_outcome_unknown" if apply_result.outcome_unknown else apply_result.reason
        overall_reason = item_reason
        return MaterializationResult(
            plan_id=materialization_plan.plan_id,
            status="failed",
            external_write=True,
            action_requested=requested_action,
            actions_performed=(_HEADER_OPERATION_ID,),
            operations=(
                MaterializationOperationResult(_HEADER_OPERATION_ID, "applied", True),
                _failed_operation(_ITEMS_OPERATION_ID, item_reason),
            ),
            reason=overall_reason,
            observed_after=_observed_state(
                apply_result,
                operation_id=(_ITEMS_OPERATION_ID if apply_result.outcome_unknown else None),
            ),
            occurred_at=None,
        )

    raise ValueError("unsupported_payroll_plan_apply_outcome")


def build_payroll_statement_materialization_audit_record(
    payroll_plan: PayrollWritePlan,
    materialization_plan: MaterializationPlan,
    apply_result: PayrollPlanApplyResult,
) -> MaterializationAuditRecord:
    """Build the common audit record from this pure statement projection."""

    result = build_payroll_statement_materialization_result(
        payroll_plan, materialization_plan, apply_result,
    )
    return build_materialization_audit_record(materialization_plan, result)


def build_payroll_batch_materialization_results(
    payroll_plans: Iterable[PayrollWritePlan],
    materialization_plans: Mapping[str, MaterializationPlan],
    batch_result: PayrollWriteBatchResult,
) -> tuple[MaterializationResult, ...]:
    """Project only writer-attempted statements from an existing batch result.

    Batch status and ``not_attempted_statement_ids`` never manufacture a
    statement result.  Every returned result is still derived independently
    from its corresponding writer statement result.
    """

    if not isinstance(batch_result, PayrollWriteBatchResult):
        raise TypeError("payroll_write_batch_result_required")
    plans_by_statement: dict[str, PayrollWritePlan] = {}
    for plan in payroll_plans:
        if not isinstance(plan, PayrollWritePlan):
            raise TypeError("payroll_write_plan_required")
        statement_id = plan.identity.statement_id
        if statement_id in plans_by_statement:
            raise ValueError("payroll_materialization_duplicate_statement_plan")
        plans_by_statement[statement_id] = plan

    result_ids = [result.statement_id for result in batch_result.results]
    if len(result_ids) != len(set(result_ids)):
        raise ValueError("payroll_materialization_duplicate_statement_result")
    not_attempted = set(batch_result.not_attempted_statement_ids)
    if not_attempted.intersection(result_ids):
        raise ValueError("payroll_materialization_attempted_not_attempted_overlap")

    projected: list[MaterializationResult] = []
    for apply_result in batch_result.results:
        statement_id = apply_result.statement_id
        try:
            payroll_plan = plans_by_statement[statement_id]
            materialization_plan = materialization_plans[statement_id]
        except KeyError as exc:
            raise ValueError("payroll_materialization_statement_plan_missing") from exc
        projected.append(build_payroll_statement_materialization_result(
            payroll_plan, materialization_plan, apply_result,
        ))
    return tuple(projected)
