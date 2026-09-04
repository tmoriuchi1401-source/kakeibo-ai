"""Pure projection of Payroll master-sync apply results.

The master-sync apply path predates ``MaterializationPlan`` and does not own a
common source identity.  This adapter therefore requires a corresponding plan
from its caller, reuses only that plan's ID and operation IDs, and never
creates a source or operation identity from the apply result.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .materialization import (
    MaterializationOperationResult,
    MaterializationPlan,
    MaterializationResult,
)


_STAGE_SHEET_KEYS = {
    "standard_items": "payroll_standard_items",
    "aliases": "payroll_item_aliases",
}
_KIND_STAGE = {
    "standard_item": "standard_items",
    "alias": "aliases",
}
_SAFE_POSTWRITE_ERROR = "post_write_verification_failed"


def _required_list(values: Mapping[str, Any], key: str) -> list[object]:
    value = values.get(key)
    if not isinstance(value, list):
        raise TypeError(f"payroll_master_sync_{key}_list_required")
    return value


def _safe_entries(entries: list[object], *, field_name: str) -> list[dict[str, str]]:
    safe: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise TypeError(f"payroll_master_sync_{field_name}_entry_required")
        kind = entry.get("kind")
        code_id = entry.get("id")
        if kind not in _KIND_STAGE or not isinstance(code_id, str) or not code_id:
            raise ValueError(f"invalid_payroll_master_sync_{field_name}_entry")
        safe.append({"kind": kind, "id": code_id})
    return safe


def _safe_conflicts(entries: list[object]) -> list[dict[str, str]]:
    safe: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise TypeError("payroll_master_sync_conflict_entry_required")
        kind = entry.get("kind")
        code_id = entry.get("code_id")
        reason = entry.get("reason")
        if not all(isinstance(value, str) and value for value in (kind, code_id, reason)):
            raise ValueError("invalid_payroll_master_sync_conflict_entry")
        safe.append({"kind": kind, "code_id": code_id, "reason": reason})
    return safe


def _safe_errors(entries: list[object]) -> list[dict[str, object]]:
    safe: list[dict[str, object]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise TypeError("payroll_master_sync_error_entry_required")
        stage = entry.get("stage")
        raw_error = entry.get("error")
        if stage not in _STAGE_SHEET_KEYS or not isinstance(raw_error, str) or not raw_error:
            raise ValueError("invalid_payroll_master_sync_error_entry")
        item: dict[str, object] = {
            "stage": stage,
            # Writer exception text can contain provider data.  Preserve only
            # the existing controlled verification code; other errors retain
            # their stage without copying the raw exception message.
            "reason": raw_error if raw_error == _SAFE_POSTWRITE_ERROR else "write_failed",
        }
        outcome = entry.get("outcome")
        if outcome is not None:
            if not isinstance(outcome, str) or not outcome:
                raise ValueError("invalid_payroll_master_sync_error_outcome")
            item["outcome"] = outcome
        unconfirmed = entry.get("unconfirmed_ids")
        if unconfirmed is not None:
            if not isinstance(unconfirmed, list) or any(
                not isinstance(item_id, str) or not item_id for item_id in unconfirmed
            ):
                raise ValueError("invalid_payroll_master_sync_unconfirmed_ids")
            item["unconfirmed_ids"] = list(unconfirmed)
        safe.append(item)
    return safe


def _stage_operation_ids(plan: MaterializationPlan) -> dict[str, str]:
    stages: dict[str, str] = {}
    for operation in plan.operations:
        sheet_key = operation.target.get("sheet_key")
        for stage, expected_sheet_key in _STAGE_SHEET_KEYS.items():
            if sheet_key == expected_sheet_key:
                if stage in stages:
                    raise ValueError("duplicate_payroll_master_sync_stage_operation")
                stages[stage] = operation.operation_id
    return stages


def _stage_entries(entries: list[dict[str, str]], stage: str) -> list[dict[str, str]]:
    return [entry for entry in entries if _KIND_STAGE[entry["kind"]] == stage]


def _stage_error(errors: list[dict[str, object]], stage: str) -> dict[str, object] | None:
    return next((entry for entry in errors if entry["stage"] == stage), None)


def _operation_results(
    plan: MaterializationPlan,
    *,
    added_standard_items: list[str],
    added_aliases: list[str],
    already_present: list[dict[str, str]],
    skipped: list[dict[str, str]],
    errors: list[dict[str, object]],
) -> tuple[MaterializationOperationResult, ...]:
    operations_by_stage = _stage_operation_ids(plan)
    added = {
        "standard_items": added_standard_items,
        "aliases": added_aliases,
    }
    results: dict[str, MaterializationOperationResult] = {}
    for stage in _STAGE_SHEET_KEYS:
        error = _stage_error(errors, stage)
        present_or_skipped = _stage_entries(already_present, stage) + _stage_entries(skipped, stage)
        has_outcome = bool(added[stage] or present_or_skipped or error)
        if not has_outcome:
            continue
        operation_id = operations_by_stage.get(stage)
        if operation_id is None:
            # A result cannot safely claim an operation identity absent from
            # its plan.  Do not invent stage IDs to make the join appear valid.
            raise ValueError("payroll_master_sync_result_operation_missing_from_plan")
        if error is not None:
            results[operation_id] = MaterializationOperationResult(
                operation_id=operation_id,
                status="failed",
                external_write=bool(added[stage]),
                reason=str(error["reason"]),
            )
        elif added[stage]:
            results[operation_id] = MaterializationOperationResult(
                operation_id=operation_id,
                status="applied",
                external_write=True,
            )
        else:
            results[operation_id] = MaterializationOperationResult(
                operation_id=operation_id,
                status="skipped",
                external_write=False,
            )
    return tuple(
        results[operation.operation_id]
        for operation in plan.operations
        if operation.operation_id in results
    )


def _result_status(
    *,
    applied: bool,
    added_standard_items: list[str],
    added_aliases: list[str],
    conflicts: list[dict[str, str]],
    errors: list[dict[str, object]],
) -> tuple[str, str]:
    if errors:
        first = errors[0]
        return "failed", str(first["reason"])
    if conflicts:
        return "blocked", "conflicts_detected"
    if added_standard_items or added_aliases:
        return "applied", "master_sync_applied"
    if applied:
        return "skipped", "already_present"
    raise ValueError("unclassified_payroll_master_sync_apply_result")


def payroll_master_sync_result_to_materialization_result(
    result: Mapping[str, Any],
    plan: MaterializationPlan,
) -> MaterializationResult:
    """Project an already-produced master-sync result without invoking apply.

    ``applied_at`` is intentionally not copied: it is an audit timestamp, not
    an observed state, and the current common result contract has no timestamp
    field.  No source identity is inferred because master sync has none in its
    existing apply result.
    """

    if not isinstance(result, Mapping):
        raise TypeError("payroll_master_sync_result_required")
    if not isinstance(plan, MaterializationPlan) or plan.domain != "payroll":
        raise TypeError("payroll_master_sync_materialization_plan_required")
    applied = result.get("applied")
    if not isinstance(applied, bool):
        raise TypeError("payroll_master_sync_applied_bool_required")
    added_standard_items = _required_list(result, "added_standard_items")
    added_aliases = _required_list(result, "added_aliases")
    if any(not isinstance(item, str) or not item for item in added_standard_items + added_aliases):
        raise ValueError("invalid_payroll_master_sync_added_ids")
    already_present = _safe_entries(_required_list(result, "already_present"), field_name="already_present")
    skipped = _safe_entries(_required_list(result, "skipped"), field_name="skipped")
    conflicts = _safe_conflicts(_required_list(result, "conflicts"))
    errors = _safe_errors(_required_list(result, "errors"))
    status, reason = _result_status(
        applied=applied,
        added_standard_items=added_standard_items,
        added_aliases=added_aliases,
        conflicts=conflicts,
        errors=errors,
    )
    operations = _operation_results(
        plan,
        added_standard_items=list(added_standard_items),
        added_aliases=list(added_aliases),
        already_present=already_present,
        skipped=skipped,
        errors=errors,
    )
    external_write = any(item.external_write for item in operations)
    return MaterializationResult(
        plan_id=plan.plan_id,
        status=status,
        external_write=external_write,
        action_requested="payroll_master_sync",
        actions_performed=tuple(
            operation.operation_id for operation in operations if operation.status == "applied"
        ),
        operations=operations,
        reason=reason,
        observed_before={"already_present": already_present},
        observed_after={
            "added_standard_items": list(added_standard_items),
            "added_aliases": list(added_aliases),
            "skipped": skipped,
            "conflicts": conflicts,
            "errors": errors,
        },
    )
