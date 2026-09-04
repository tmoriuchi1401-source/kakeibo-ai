from dataclasses import FrozenInstanceError

import pytest

from app.materialization import (
    MaterializationOperation,
    MaterializationOperationResult,
    MaterializationPlan,
    MaterializationPrecondition,
    MaterializationResult,
    MaterializationSource,
    build_materialization_audit_record,
)


def source() -> MaterializationSource:
    return MaterializationSource(
        identity_kind="source_file_id",
        identity_value="file-1",
        provider="payroll",
        content_hash="hash-1",
    )


def operation(operation_id: str = "append", *, payload: dict[str, object] | None = None):
    return MaterializationOperation(
        operation_id=operation_id,
        kind="append_row",
        target={"resource": "google_sheets", "sheet_key": "safe_target"},
        payload=payload or {"row_count": 1},
        preconditions=(MaterializationPrecondition("schema_ok", True),),
    )


def plan(*, source_value: MaterializationSource | None = None, operations=None,
         blocked: bool = False, blocked_reason: str | None = None, provenance=None):
    return MaterializationPlan(
        domain="payroll",
        plan_version="test-v1",
        source=source() if source_value is None else source_value,
        operations=tuple((operation(),) if operations is None else operations),
        blocked=blocked,
        blocked_reason=blocked_reason,
        provenance={} if provenance is None else provenance,
    )


def result(materialization_plan: MaterializationPlan, *, status: str = "applied",
           external_write: bool = True, operation_result: bool = True,
           occurred_at: str | None = None) -> MaterializationResult:
    operations = ()
    if operation_result:
        operations = (MaterializationOperationResult(
            operation_id=materialization_plan.operations[0].operation_id,
            status="applied" if status == "applied" else "failed",
            external_write=external_write,
            reason="write_failed" if status == "failed" else None,
        ),)
    return MaterializationResult(
        plan_id=materialization_plan.plan_id,
        status=status,
        external_write=external_write,
        action_requested="append",
        actions_performed=("append",) if external_write else (),
        operations=operations,
        reason="write_failed" if status == "failed" else status,
        observed_before={"schema_ok": True},
        observed_after={"row_count": 1} if external_write else None,
        occurred_at=occurred_at,
    )


def test_contract_is_deeply_immutable_and_json_serializable():
    value = plan(provenance={"diagnostic": {"count": 1}})

    with pytest.raises(FrozenInstanceError):
        value.domain = "other"
    with pytest.raises(TypeError):
        value.provenance["other"] = "value"
    with pytest.raises(TypeError):
        value.operations[0].payload["row_count"] = 2
    assert value.to_dict()["operations"][0]["payload"] == {"row_count": 1}


def test_plan_identity_is_deterministic_and_keeps_semantic_boundaries():
    first = plan(provenance={"z": 2, "a": 1})
    second = plan(provenance={"a": 1, "z": 2})
    source_less = MaterializationPlan(
        domain="payroll",
        plan_version="test-v1",
        source=None,
        operations=(operation(),),
    )
    ordered = plan(operations=(operation("header"), operation("items")))
    reordered = plan(operations=(operation("items"), operation("header")))
    blocked = plan(blocked=True, blocked_reason="schema_invalid")

    assert first.plan_id == second.plan_id
    assert first.to_json() == second.to_json()
    assert source_less.plan_id != first.plan_id
    assert ordered.plan_id != reordered.plan_id
    assert blocked.plan_id != first.plan_id


def test_blocked_reason_and_provenance_do_not_change_plan_identity():
    first = plan(
        blocked=True,
        blocked_reason="schema_invalid",
        provenance={"diagnostic": "one"},
    )
    second = plan(
        blocked=True,
        blocked_reason="conflict_detected",
        provenance={"diagnostic": "two"},
    )

    assert first.plan_id == second.plan_id


@pytest.mark.parametrize(
    ("status", "external_write"),
    [
        ("applied", True),
        ("skipped", False),
        ("blocked", False),
        ("failed", False),
        ("failed", True),
    ],
)
def test_result_status_and_external_write_are_independent(status, external_write):
    materialization_plan = plan()
    value = result(
        materialization_plan,
        status=status,
        external_write=external_write,
    )

    assert value.status == status
    assert value.external_write is external_write


def test_audit_builder_is_pure_and_excludes_operation_payload():
    materialization_plan = plan(operations=(operation(
        payload={"raw_ocr": "must-not-enter-audit"},
    ),))
    value = build_materialization_audit_record(
        materialization_plan,
        result(materialization_plan, occurred_at="2026-09-04T00:00:00+00:00"),
    )

    assert value.plan_id == materialization_plan.plan_id
    assert value.source == materialization_plan.source
    assert value.occurred_at == "2026-09-04T00:00:00+00:00"
    assert "must-not-enter-audit" not in value.to_json()
    with pytest.raises(TypeError):
        value.operations[0].target["sheet_key"] = "other"


def test_audit_builder_fails_closed_for_plan_or_operation_mismatch():
    materialization_plan = plan()
    mismatched_plan = plan(operations=(operation("other"),))
    with pytest.raises(ValueError, match="plan_id_mismatch"):
        build_materialization_audit_record(
            materialization_plan,
            result(mismatched_plan),
        )

    unknown_result = MaterializationResult(
        plan_id=materialization_plan.plan_id,
        status="failed",
        external_write=False,
        action_requested="append",
        actions_performed=(),
        operations=(MaterializationOperationResult(
            operation_id="unknown", status="failed", external_write=False,
            reason="write_failed",
        ),),
        reason="write_failed",
    )
    with pytest.raises(ValueError, match="unknown_operation_id"):
        build_materialization_audit_record(materialization_plan, unknown_result)
