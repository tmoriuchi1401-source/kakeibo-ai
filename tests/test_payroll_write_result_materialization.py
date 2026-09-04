import pytest

from app.materialization import MaterializationOperation, MaterializationPlan
from app.payroll_models import PayrollItem, PayrollPreview
from app.payroll_sheets import PayrollSheetsSnapshot, validate_sheet_schema
from app.payroll_storage import (
    PAYROLL_SCHEMAS,
    PayrollStandardItemRecord,
    phase_a_to_storage_candidate,
)
from app.payroll_storage_preview import build_write_plan
from app.payroll_write_plan_materialization import payroll_write_plan_to_materialization_plan
from app.payroll_write_result_materialization import (
    build_payroll_batch_materialization_results,
    build_payroll_statement_materialization_audit_record,
    build_payroll_statement_materialization_result,
)
from app.payroll_writer import PayrollPlanApplyResult, PayrollWriteBatchResult


def snapshot():
    return PayrollSheetsSnapshot(
        schemas=[validate_sheet_schema(key, columns) for key, columns in PAYROLL_SCHEMAS.items()],
        standard_items=[PayrollStandardItemRecord(
            standard_item_id="basic_pay", standard_name="basic", section="earning", value_type="money",
        )],
    )


def payroll_plan(index=1):
    preview = PayrollPreview(
        file_type="pdf", extraction_method="pdf_text", pay_period="2026-08", parse_status="success",
        items=[PayrollItem(
            raw_item_name="basic", section="earnings", raw_value="300,000", value=300000,
            standard_item_candidate="basic_pay",
        )],
    )
    candidate = phase_a_to_storage_candidate(
        preview, employer_id="employer-1", statement_type="salary", source_type="drive",
        source_file_id=f"file-{index}", content_hash=f"hash-{index}",
    )
    return build_write_plan([candidate], snapshot())[0]


def materialization(plan):
    return payroll_write_plan_to_materialization_plan(plan)


def apply_result(plan, outcome="written", **overrides):
    values = {
        "statement_id": plan.identity.statement_id,
        "outcome": outcome,
        "reason": "completed",
    }
    values.update(overrides)
    return PayrollPlanApplyResult(**values)


def test_success_projects_existing_confirmations_and_never_calls_writer_or_executor():
    plan = payroll_plan()
    result = build_payroll_statement_materialization_result(
        plan, materialization(plan), apply_result(
            plan, header_rows_confirmed=1, item_rows_confirmed=len(plan.planned_item_rows),
        ),
    )

    assert result.status == "applied"
    assert result.external_write is True
    assert [(item.operation_id, item.status, item.external_write) for item in result.operations] == [
        ("append_statement_header", "applied", True),
        ("append_statement_items", "applied", True),
    ]
    assert dict(result.observed_after) == {
        "statement_id": plan.identity.statement_id,
        "header_rows_confirmed": 1,
        "item_rows_confirmed": len(plan.planned_item_rows),
    }
    assert result.occurred_at is None


@pytest.mark.parametrize("kind", ["statement", "plan", "operation"])
def test_identity_gate_fails_closed_for_mismatched_statement_plan_or_operations(kind):
    plan = payroll_plan()
    actual_plan = materialization(plan)
    actual_result = apply_result(plan, header_rows_confirmed=1, item_rows_confirmed=1)
    if kind == "statement":
        actual_result = actual_result.model_copy(update={"statement_id": "wrong"})
    elif kind == "plan":
        other = payroll_plan(2)
        actual_plan = materialization(other)
    else:
        actual_plan = MaterializationPlan(
            domain=actual_plan.domain,
            plan_version=actual_plan.plan_version,
            source=actual_plan.source,
            operations=(
                MaterializationOperation(
                    "append_statement_header", "append_row", {"resource": "google_sheets"},
                    {"rows": []},
                ),
            ),
            provenance={"statement_id": plan.identity.statement_id},
        )

    with pytest.raises(ValueError, match="payroll_materialization_.*(mismatch|missing)"):
        build_payroll_statement_materialization_result(plan, actual_plan, actual_result)


def test_header_confirmed_failure_is_failed_without_external_write():
    plan = payroll_plan()
    result = build_payroll_statement_materialization_result(
        plan, materialization(plan), apply_result(
            plan, "confirmed_failure", reason="request_not_sent", failure_stage="header",
        ),
    )

    assert result.status == "failed"
    assert result.external_write is False
    assert [(item.operation_id, item.status, item.external_write) for item in result.operations] == [
        ("append_statement_header", "failed", False),
    ]
    assert result.reason == "request_not_sent"


def test_header_unknown_retains_ambiguity_without_claiming_no_write_or_item_execution():
    plan = payroll_plan()
    result = build_payroll_statement_materialization_result(
        plan, materialization(plan), apply_result(
            plan, "header_outcome_unknown", reason="transport_outcome_unknown",
            failure_stage="header", outcome_unknown=True,
        ),
    )

    assert result.status == "failed"
    assert result.external_write is False
    assert [(item.operation_id, item.status, item.reason) for item in result.operations] == [
        ("append_statement_header", "failed", "header_outcome_unknown"),
    ]
    assert result.reason == "header_outcome_unknown"
    assert result.observed_after["outcome_unknown"] is True
    assert result.observed_after["operations"]["append_statement_header"] == {
        "outcome_unknown": True, "stage": "header", "writer_reason": "transport_outcome_unknown",
    }


def test_partial_confirmed_failure_keeps_header_write_and_safe_failure_reason():
    plan = payroll_plan()
    result = build_payroll_statement_materialization_result(
        plan, materialization(plan), apply_result(
            plan, "partial_failure", reason="http_request_rejected", failure_stage="items",
            header_rows_confirmed=1,
        ),
    )

    assert result.status == "failed"
    assert result.external_write is True
    assert [(item.operation_id, item.status, item.external_write, item.reason) for item in result.operations] == [
        ("append_statement_header", "applied", True, None),
        ("append_statement_items", "failed", False, "http_request_rejected"),
    ]


def test_item_unknown_keeps_header_write_and_preserves_unknown_reason_without_payload():
    plan = payroll_plan()
    result = build_payroll_statement_materialization_result(
        plan, materialization(plan), apply_result(
            plan, "partial_failure", reason="transport_outcome_unknown", failure_stage="items",
            header_rows_confirmed=1, outcome_unknown=True,
        ),
    )

    assert result.status == "failed"
    assert result.external_write is True
    assert result.operations[0].status == "applied"
    assert result.operations[1].reason == result.reason == "item_outcome_unknown"
    assert result.observed_after["operations"]["append_statement_items"]["outcome_unknown"] is True
    assert "300000" not in result.to_json()


def test_writer_returned_skip_is_projected_but_not_inferred_for_not_attempted_statement():
    plan = payroll_plan()
    skipped = build_payroll_statement_materialization_result(
        plan, materialization(plan), apply_result(plan, "skipped", reason="skipped_duplicate"),
    )
    batch = PayrollWriteBatchResult(
        status="header_outcome_unknown", applied=False,
        results=(apply_result(plan, "header_outcome_unknown", reason="adapter_failure",
                              failure_stage="header", outcome_unknown=True),),
        not_attempted_statement_ids=("not-attempted",),
    )
    projected = build_payroll_batch_materialization_results(
        [plan], {plan.identity.statement_id: materialization(plan)}, batch,
    )

    assert skipped.status == "skipped"
    assert skipped.external_write is False
    assert skipped.operations == ()
    assert len(projected) == 1
    assert all(value.observed_after["statement_id"] != "not-attempted" for value in projected)


def test_batch_projects_each_attempted_statement_without_overwriting_with_batch_status():
    first = payroll_plan(1)
    second = payroll_plan(2)
    third = payroll_plan(3)
    batch = PayrollWriteBatchResult(
        status="header_outcome_unknown",
        applied=False,
        results=(
            apply_result(
                first, header_rows_confirmed=1, item_rows_confirmed=len(first.planned_item_rows),
            ),
            apply_result(
                second, "header_outcome_unknown", reason="transport_outcome_unknown",
                failure_stage="header", outcome_unknown=True,
            ),
        ),
        not_attempted_statement_ids=(third.identity.statement_id,),
    )

    projected = build_payroll_batch_materialization_results(
        [first, second, third],
        {
            first.identity.statement_id: materialization(first),
            second.identity.statement_id: materialization(second),
        },
        batch,
    )

    assert [(value.status, value.external_write) for value in projected] == [
        ("applied", True), ("failed", False),
    ]
    assert [value.observed_after["statement_id"] for value in projected] == [
        first.identity.statement_id, second.identity.statement_id,
    ]


def test_audit_uses_common_builder_excludes_payload_and_has_no_occurrence_time():
    plan = payroll_plan()
    audit = build_payroll_statement_materialization_audit_record(
        plan, materialization(plan), apply_result(
            plan, "partial_failure", reason="transport_outcome_unknown", failure_stage="items",
            header_rows_confirmed=1, outcome_unknown=True,
        ),
    )

    assert audit.result_status == "failed"
    assert audit.external_write is True
    assert audit.reason == "item_outcome_unknown"
    assert audit.occurred_at is None
    assert "300000" not in audit.to_json()


def test_inconsistent_counts_fail_closed():
    plan = payroll_plan()
    with pytest.raises(ValueError, match="confirmed_row_count_mismatch"):
        build_payroll_statement_materialization_result(
            plan, materialization(plan), apply_result(
                plan, header_rows_confirmed=0, item_rows_confirmed=1,
            ),
        )
