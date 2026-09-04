"""Application boundary for an existing Payroll statement writer apply.

The service owns only orchestration: it validates preview-time plan pairings,
calls the existing writer unchanged, then projects its already-produced result
into in-memory materialization and audit values.  It has no CLI, persistence,
Sheets, or Drive dependency.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from .materialization import (
    MaterializationAuditRecord,
    MaterializationPlan,
    MaterializationResult,
    build_materialization_audit_record,
)
from .payroll_storage_preview import PayrollWritePlan
from .payroll_write_plan_materialization import (
    payroll_write_plan_to_materialization_plan,
)
from .payroll_write_result_materialization import (
    build_payroll_batch_materialization_results,
)
from .payroll_writer import (
    PayrollAppendAdapter,
    PayrollWriteBatchResult,
    apply_payroll_write_plans,
    validate_payroll_write_contract,
)


@dataclass(frozen=True)
class PayrollMaterializationProjectionFailure:
    """A post-write projection problem, kept separate from the writer result.

    ``reason`` is deliberately a fixed safe code.  Raw projection exceptions
    can contain arbitrary data and must not turn an already-returned writer
    outcome into an apparent Payroll apply failure.
    """

    stage: Literal["materialization_result", "audit_record"]
    reason: Literal[
        "payroll_materialization_projection_failed",
        "payroll_audit_projection_failed",
    ]
    statement_id: str | None = None


@dataclass(frozen=True)
class PayrollWriteApplicationResult:
    """In-memory apply observation; ``writer_result`` remains authoritative."""

    writer_result: PayrollWriteBatchResult
    materialization_results: tuple[MaterializationResult, ...]
    audit_records: tuple[MaterializationAuditRecord, ...]
    projection_failures: tuple[PayrollMaterializationProjectionFailure, ...]


def _validate_preview_materialization_inputs(
    payroll_plans: Iterable[PayrollWritePlan],
    materialization_plans: Mapping[str, MaterializationPlan],
) -> tuple[PayrollWritePlan, ...]:
    """Validate pre-write pairings without deriving any plan after apply.

    A MaterializationPlan exists only for a ready statement because the
    preview adapter intentionally rejects non-ready PayrollWritePlans.  The
    writer can still report such a statement as skipped; that writer result is
    preserved but has no materialization intent to project.
    """

    if not isinstance(materialization_plans, Mapping):
        raise TypeError("payroll_materialization_plans_mapping_required")
    plans = validate_payroll_write_contract(payroll_plans)
    statement_ids: set[str] = set()
    ready_ids: set[str] = set()
    for payroll_plan in plans:
        statement_id = payroll_plan.identity.statement_id
        if statement_id in statement_ids:
            raise ValueError("payroll_application_duplicate_statement_id")
        statement_ids.add(statement_id)
        if payroll_plan.status != "ready":
            continue
        ready_ids.add(statement_id)
        try:
            materialization_plan = materialization_plans[statement_id]
        except KeyError as exc:
            raise ValueError("payroll_application_materialization_plan_missing") from exc
        if not isinstance(materialization_plan, MaterializationPlan):
            raise TypeError("payroll_application_materialization_plan_required")
        # This is a pure identity comparison with the preview-time projection.
        # It catches plan ID, source, operation, payload, precondition, and
        # provenance drift before the writer can make an external call.
        expected_plan = payroll_write_plan_to_materialization_plan(payroll_plan)
        if materialization_plan != expected_plan:
            raise ValueError("payroll_application_materialization_plan_mismatch")

    unexpected = set(materialization_plans).difference(ready_ids)
    if unexpected:
        raise ValueError("payroll_application_unexpected_materialization_plan")
    return plans


def apply_payroll_write_application(
    payroll_plans: Iterable[PayrollWritePlan],
    materialization_plans: Mapping[str, MaterializationPlan],
    writer: PayrollAppendAdapter,
    *,
    confirmed: bool,
    latest_plans: Callable[[], Iterable[PayrollWritePlan]],
) -> PayrollWriteApplicationResult:
    """Apply existing writer intent and return observation values in memory.

    Pre-write plan mismatch is a fail-closed input error.  Once the writer has
    returned, materialization/audit errors are never raised or folded into its
    result: callers can distinguish a Payroll write outcome from a later
    observation failure, including when a confirmed external write occurred.
    """

    plans = _validate_preview_materialization_inputs(
        payroll_plans, materialization_plans,
    )
    writer_result = apply_payroll_write_plans(
        plans, writer, confirmed=confirmed, latest_plans=latest_plans,
    )
    if not writer_result.results:
        return PayrollWriteApplicationResult(writer_result, (), (), ())

    plans_by_statement = {plan.identity.statement_id: plan for plan in plans}
    # Do not create an intent for writer-reported non-ready skips.  The
    # pre-apply adapter intentionally supplied no MaterializationPlan for them.
    projectable: list = []
    unprojectable_result_ids: list[str] = []
    for apply_result in writer_result.results:
        payroll_plan = plans_by_statement.get(apply_result.statement_id)
        if payroll_plan is None:
            unprojectable_result_ids.append(apply_result.statement_id)
        elif payroll_plan.status == "ready":
            projectable.append(apply_result)
        elif apply_result.outcome != "skipped":
            # A non-ready preview plan cannot have a MaterializationPlan.  A
            # different writer outcome would be an inconsistent post-write
            # result, so do not quietly discard it.
            unprojectable_result_ids.append(apply_result.statement_id)
    if unprojectable_result_ids:
        return PayrollWriteApplicationResult(
            writer_result,
            (),
            (),
            tuple(PayrollMaterializationProjectionFailure(
                stage="materialization_result",
                reason="payroll_materialization_projection_failed",
                statement_id=statement_id,
            ) for statement_id in unprojectable_result_ids),
        )
    projectable_results = tuple(projectable)
    if not projectable_results:
        return PayrollWriteApplicationResult(writer_result, (), (), ())

    projection_batch = writer_result.model_copy(update={"results": projectable_results})
    try:
        materialization_results = build_payroll_batch_materialization_results(
            plans, materialization_plans, projection_batch,
        )
        if len(materialization_results) != len(projectable_results):
            raise ValueError("payroll_application_materialization_result_count_mismatch")
        for apply_result, materialization_result in zip(
            projectable_results, materialization_results,
        ):
            if materialization_result.plan_id != materialization_plans[
                apply_result.statement_id
            ].plan_id:
                raise ValueError("payroll_application_materialization_result_plan_mismatch")
    except Exception:
        return PayrollWriteApplicationResult(
            writer_result,
            (),
            (),
            (PayrollMaterializationProjectionFailure(
                stage="materialization_result",
                reason="payroll_materialization_projection_failed",
            ),),
        )

    audit_records: list[MaterializationAuditRecord] = []
    projection_failures: list[PayrollMaterializationProjectionFailure] = []
    for apply_result, materialization_result in zip(
        projectable_results, materialization_results,
    ):
        try:
            audit_records.append(build_materialization_audit_record(
                materialization_plans[apply_result.statement_id],
                materialization_result,
            ))
        except Exception:
            projection_failures.append(PayrollMaterializationProjectionFailure(
                stage="audit_record",
                reason="payroll_audit_projection_failed",
                statement_id=apply_result.statement_id,
            ))
    return PayrollWriteApplicationResult(
        writer_result,
        materialization_results,
        tuple(audit_records),
        tuple(projection_failures),
    )
