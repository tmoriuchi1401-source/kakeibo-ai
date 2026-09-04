"""Pure projection of PayPay coverage confirmation apply results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .coverage_confirmation_sheets_apply import (
    CoverageConfirmationApplyResult,
    CoverageConfirmationWriteStatus,
)
from .materialization import (
    MaterializationOperationResult,
    MaterializationPlan,
    MaterializationResult,
)


_ACTION_OPERATION_IDS = {
    "create_sheet": "create_sheet",
    "write_header": "write_header",
    "append_row": "append_confirmation",
    "skip_duplicate": "append_confirmation",
}
_FAILURE_OPERATION_IDS = {
    "sheet_create_failed": "create_sheet",
    "header_write_failed": "write_header",
    "append_write_failed": "append_confirmation",
}
_FAILURE_REASONS = frozenset(
    {
        "sheet_create_failed",
        "header_write_failed",
        "append_write_failed",
        "postwrite_verification_failed",
        "new_sheet_header_state_changed",
    }
)
_SAFE_STATUS_FIELDS = (
    "spreadsheet_matches",
    "sheet_status",
    "schema_status",
    "existing_row_count",
    "invalid_row_count",
    "identity_conflict_count",
    "duplicate_status",
    "current_action",
    "safe_to_write",
    "reason",
)


def _result_mapping(result: CoverageConfirmationApplyResult | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(result, CoverageConfirmationApplyResult):
        return result.to_dict()
    if isinstance(result, Mapping):
        return result
    raise TypeError("coverage_confirmation_apply_result_required")


def _observed_status(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if isinstance(value, CoverageConfirmationWriteStatus):
        values = {
            field: getattr(value, field)
            for field in _SAFE_STATUS_FIELDS
        }
    elif isinstance(value, Mapping):
        values = {field: value.get(field) for field in _SAFE_STATUS_FIELDS}
    else:
        raise TypeError("coverage_confirmation_write_status_required")
    # Headers and rows are deliberately excluded: they are unnecessary for a
    # result audit and may be source-controlled or privacy-sensitive data.
    return values


def _status(values: Mapping[str, Any]) -> str:
    reason = str(values["reason"])
    if reason in _FAILURE_REASONS:
        return "failed"
    if bool(values["blocked"]):
        return "blocked"
    if bool(values["skipped_duplicate"]):
        return "skipped"
    if bool(values["external_write"]):
        return "applied"
    raise ValueError("unclassified_coverage_confirmation_apply_result")


def _operation_results(
    plan: MaterializationPlan,
    values: Mapping[str, Any],
    result_status: str,
) -> tuple[MaterializationOperationResult, ...]:
    plan_operation_ids = {operation.operation_id for operation in plan.operations}
    observed: dict[str, MaterializationOperationResult] = {}
    for action in values["action_performed"]:
        operation_id = _ACTION_OPERATION_IDS.get(action)
        if operation_id not in plan_operation_ids:
            continue
        observed[operation_id] = MaterializationOperationResult(
            operation_id=operation_id,
            status="skipped" if action == "skip_duplicate" else "applied",
            external_write=action != "skip_duplicate",
        )
    failed_operation_id = _FAILURE_OPERATION_IDS.get(str(values["reason"]))
    if failed_operation_id in plan_operation_ids and failed_operation_id not in observed:
        observed[failed_operation_id] = MaterializationOperationResult(
            operation_id=failed_operation_id,
            status="failed",
            external_write=bool(values["external_write"]),
            reason=str(values["reason"]),
        )
    # Preserve planned operation order, rather than the arbitrary order of a
    # mapping populated from observed actions.
    return tuple(
        observed[operation.operation_id]
        for operation in plan.operations
        if operation.operation_id in observed
    )


def coverage_confirmation_result_to_materialization_result(
    result: CoverageConfirmationApplyResult | Mapping[str, Any],
    plan: MaterializationPlan,
) -> MaterializationResult:
    """Project an already-produced PayPay result without invoking apply."""

    if not isinstance(plan, MaterializationPlan) or plan.domain != "paypay":
        raise TypeError("paypay_materialization_plan_required")
    values = _result_mapping(result)
    required = {
        "action_requested", "action_performed", "blocked", "reason",
        "prewrite_status", "postwrite_status", "external_write",
        "skipped_duplicate",
    }
    if not required.issubset(values):
        raise ValueError("incomplete_coverage_confirmation_apply_result")
    action_requested = values["action_requested"]
    reason = values["reason"]
    if not isinstance(action_requested, str) or not isinstance(reason, str):
        raise TypeError("invalid_coverage_confirmation_apply_result")
    actions = tuple(values["action_performed"])
    if any(not isinstance(action, str) for action in actions):
        raise TypeError("invalid_coverage_confirmation_actions")
    result_status = _status(values)
    return MaterializationResult(
        plan_id=plan.plan_id,
        status=result_status,
        external_write=bool(values["external_write"]),
        action_requested=action_requested,
        actions_performed=actions,
        operations=_operation_results(plan, values, result_status),
        reason=reason,
        observed_before=_observed_status(values["prewrite_status"]),
        observed_after=_observed_status(values["postwrite_status"]),
    )
