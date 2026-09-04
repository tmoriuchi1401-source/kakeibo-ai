from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from app.coverage_confirmation import CoverageConfirmationRecord, coverage_confirmation_to_sheet_row
from app.coverage_confirmation_sheets_apply import CoverageConfirmationWritePlan
from app.materialization import (
    MaterializationOperation,
    MaterializationPlan,
    MaterializationPrecondition,
    MaterializationSource,
)
from app.paypay_materialization import coverage_confirmation_to_materialization_plan
from app.payroll_materialization import payroll_storage_to_materialization_plan
from app.payroll_storage import (
    PayrollStatementItemRecord,
    PayrollStatementRecord,
    PayrollStorageCandidate,
)
from app.payroll_storage_preview import PayrollAppendPlan, PayrollSavePlan


def source():
    return MaterializationSource(
        identity_kind="drive_file_id",
        identity_value="source-1",
        provider="payroll",
        content_hash="content-1",
    )


def operation(operation_id="append", payload=None):
    return MaterializationOperation(
        operation_id,
        "append_row",
        {"resource": "sheets", "sheet": "safe"},
        payload or {"count": 1},
        (MaterializationPrecondition("schema", "exact_match"),),
    )


def plan(*, operations=None, blocked=False):
    return MaterializationPlan(
        domain="example",
        plan_version="v1",
        source=source(),
        operations=tuple((operation(),) if operations is None else operations),
        blocked=blocked,
        blocked_reason="schema_invalid" if blocked else None,
        provenance={"existing_action": "append"},
    )


def test_contract_is_deeply_immutable_and_json_serializable():
    result = plan()

    with pytest.raises(FrozenInstanceError):
        result.domain = "other"
    with pytest.raises(TypeError):
        result.provenance["other"] = "value"
    with pytest.raises(TypeError):
        result.operations[0].payload["count"] = 2
    assert result.to_dict()["operations"][0]["payload"] == {"count": 1}


def test_plan_serialization_and_identity_are_deterministic():
    first = MaterializationPlan(
        domain="example", plan_version="v1", source=source(),
        operations=(operation(payload={"b": 2, "a": 1}),),
        provenance={"z": 2, "a": 1},
    )
    second = MaterializationPlan(
        domain="example", plan_version="v1", source=source(),
        operations=(operation(payload={"a": 1, "b": 2}),),
        provenance={"a": 1, "z": 2},
    )

    assert first.plan_id == second.plan_id
    assert first.to_json() == second.to_json()


def test_diagnostic_provenance_does_not_change_plan_identity():
    first = MaterializationPlan(
        domain="example", plan_version="v1", source=source(),
        operations=(operation(),), provenance={"review_reason_count": 1},
    )
    second = MaterializationPlan(
        domain="example", plan_version="v1", source=source(),
        operations=(operation(),), provenance={"review_reason_count": 2},
    )

    assert first.plan_id == second.plan_id
    assert first.to_json() != second.to_json()


def test_plan_identity_changes_for_semantic_operation_or_order_change():
    first = plan(operations=(operation("first"), operation("second", {"count": 2})))
    changed = plan(operations=(operation("first"), operation("second", {"count": 3})))
    reordered = plan(operations=(operation("second", {"count": 2}), operation("first")))

    assert first.plan_id != changed.plan_id
    assert first.plan_id != reordered.plan_id
    assert first.plan_id != first.source.identity_value


def test_blocked_state_changes_plan_identity_but_reason_does_not():
    ready = plan()
    blocked = MaterializationPlan(
        domain="example", plan_version="v1", source=source(),
        operations=(operation(),), blocked=True, blocked_reason="schema_invalid",
    )
    blocked_with_other_reason = MaterializationPlan(
        domain="example", plan_version="v1", source=source(),
        operations=(operation(),), blocked=True, blocked_reason="target_changed",
    )

    assert ready.plan_id != blocked.plan_id
    assert blocked.plan_id == blocked_with_other_reason.plan_id


def test_blocked_plan_is_explicitly_representable():
    result = plan(operations=(), blocked=True)

    assert result.blocked
    assert result.blocked_reason == "schema_invalid"
    assert result.operations == ()


def test_source_less_plan_is_explicit_without_changing_source_backed_identity_rules():
    source_less = MaterializationPlan(
        domain="example", plan_version="v1", source=None,
        operations=(operation(),),
    )

    assert source_less.source is None
    assert source_less.to_dict()["source"] is None
    assert source_less.plan_id != plan().plan_id


def test_source_less_plan_identity_is_deterministic_and_keeps_semantic_changes_distinct():
    first = MaterializationPlan(
        domain="example", plan_version="v1", source=None,
        operations=(operation(payload={"count": 1}),),
    )
    same = MaterializationPlan(
        domain="example", plan_version="v1", source=None,
        operations=(operation(payload={"count": 1}),),
    )
    changed = MaterializationPlan(
        domain="example", plan_version="v1", source=None,
        operations=(operation(payload={"count": 2}),),
    )

    assert first.plan_id == same.plan_id
    assert first.plan_id != changed.plan_id


def confirmation_record():
    return CoverageConfirmationRecord(
        schema_version="1", provider="paypay", content_sha256="a" * 64,
        confirmed_start="2026-08-01", confirmed_end="2026-08-31",
        range_source="user_confirmed",
        confirmed_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        confirmation_version="1", source_filename="transactions.csv",
    )


def paypay_write_plan(*, blocked=False, action="append"):
    record = confirmation_record()
    created_at = datetime(2026, 9, 1, 1, tzinfo=timezone.utc)
    return CoverageConfirmationWritePlan(
        target_spreadsheet_id="spreadsheet-1", record=record, created_at=created_at,
        action_requested="blocked" if blocked else action,
        expected_sheet_status="invalid" if blocked else "exact_match",
        expected_duplicate_status=None if blocked else "not_found",
        candidate_row=tuple(coverage_confirmation_to_sheet_row(
            record, created_at=created_at,
        ).to_sheet_row()),
        blocked=blocked,
        reason="schema_mismatch" if blocked else "ready_to_append",
    )


def test_paypay_adapter_projects_existing_plan_without_apply_or_external_write():
    result = coverage_confirmation_to_materialization_plan(paypay_write_plan())

    assert result.domain == "paypay"
    assert result.source.identity_kind == "provider_content_sha256"
    assert result.source.content_hash == "a" * 64
    assert result.blocked is False
    assert result.provenance["coverage_status"] == "user_confirmed"
    assert result.provenance["coverage_reason"] == (
        "explicit_user_confirmation_not_provider_completeness"
    )
    assert len(result.operations) == 1
    assert result.operations[0].kind == "append_row"
    assert result.operations[0].target["spreadsheet_id"] == "spreadsheet-1"
    assert {item.kind for item in result.operations[0].preconditions} == {
        "target_spreadsheet_id", "sheet_status", "duplicate_status",
    }
    assert result.plan_id == "MP-0ae092de7bb918c7ac2950ebf930db07"


def test_paypay_adapter_preserves_blocked_plan_without_operations():
    result = coverage_confirmation_to_materialization_plan(paypay_write_plan(blocked=True))

    assert result.blocked
    assert result.blocked_reason == "schema_mismatch"
    assert result.operations == ()
    assert result.provenance["existing_action"] == "blocked"


def payroll_candidate():
    return PayrollStorageCandidate(
        statement=PayrollStatementRecord(
            statement_id="statement-1", employer_id="employer-1",
            statement_type="salary", pay_period="2026-08", parse_status="success",
            needs_review=False, source_type="drive", source_file_id="drive-file-1",
            content_hash="content-1",
        ),
        items=[PayrollStatementItemRecord(
            item_id="item-1", statement_id="statement-1", raw_item_name="basic_pay",
            raw_value="300,000", value=300000,
        )],
        parse_method="pdf_text",
    )


def payroll_plans(*, action="append", duplicate_status="new", review_reason=None):
    append = PayrollAppendPlan(
        action=action, statement_id="statement-1", statement_type="salary",
        pay_period="2026-08", header_rows_to_append=1 if action == "append" else 0,
        item_rows_to_append=1 if action == "append" else 0,
        item_count=1, duplicate_reason=("statement_key" if duplicate_status == "needs_review" else None),
        review_reason=review_reason or [],
    )
    save = PayrollSavePlan(
        header_action=action, item_count=1, recognized_item_count=1,
        recognized_without_value_count=0, unknown_item_count=0, needs_review_count=0,
        duplicate_status=duplicate_status,
        duplicate_reason=("statement_key" if duplicate_status == "needs_review" else None),
        would_create_header=action == "append", would_create_items=1 if action == "append" else 0,
        planned_header={"statement_id": "statement-1"},
    )
    return append, save


def test_payroll_adapter_projects_append_intent_without_reinterpreting_values():
    append, save = payroll_plans()
    result = payroll_storage_to_materialization_plan(payroll_candidate(), append, save)

    assert result.domain == "payroll"
    assert result.source.identity_kind == "source_file_id"
    assert result.source.identity_value == "drive-file-1"
    assert result.source.content_hash == "content-1"
    assert [item.operation_id for item in result.operations] == [
        "append_statement_header", "append_statement_items",
    ]
    assert result.operations[0].payload == {"statement_id": "statement-1", "row_count": 1}
    assert "300,000" not in result.to_json()


def test_payroll_storage_adapter_keeps_existing_content_hash_fallback_identity():
    candidate = payroll_candidate()
    candidate.statement.source_file_id = None
    append, save = payroll_plans()

    result = payroll_storage_to_materialization_plan(candidate, append, save)

    assert result.source is not None
    assert result.source.identity_kind == "content_hash"
    assert result.source.identity_value == "content-1"
    assert result.source.content_hash == "content-1"


def test_payroll_adapter_preserves_review_and_blocked_semantics():
    review_append, review_save = payroll_plans(
        action="needs_review", duplicate_status="needs_review",
        review_reason=["possible_reissue_or_revision"],
    )
    review = payroll_storage_to_materialization_plan(
        payroll_candidate(), review_append, review_save,
    )
    blocked_append, blocked_save = payroll_plans(action="blocked_schema")
    blocked = payroll_storage_to_materialization_plan(
        payroll_candidate(), blocked_append, blocked_save,
    )

    assert not review.blocked
    assert review.operations == ()
    assert review.provenance["existing_action"] == "needs_review"
    assert review.provenance["duplicate_status"] == "needs_review"
    assert blocked.blocked
    assert blocked.blocked_reason == "schema_invalid"
    assert blocked.operations == ()
