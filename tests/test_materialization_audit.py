from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from app.coverage_confirmation import (
    CoverageConfirmationRecord,
    coverage_confirmation_to_sheet_row,
)
from app.coverage_confirmation_sheets_apply import CoverageConfirmationWritePlan
from app.materialization import (
    MaterializationOperation,
    MaterializationOperationResult,
    MaterializationPlan,
    MaterializationPrecondition,
    MaterializationResult,
    MaterializationSource,
    build_materialization_audit_record,
)
from app.paypay_materialization import coverage_confirmation_to_materialization_plan
from app.paypay_materialization_result import (
    coverage_confirmation_result_to_materialization_result,
)
from app.payroll_master_sync import build_master_sync_plan
from app.payroll_master_sync_materialization import (
    payroll_master_sync_to_materialization_plan,
)
from app.payroll_master_sync_materialization_result import (
    payroll_master_sync_result_to_materialization_result,
)
from app.payroll_sheets import PayrollSheetsSnapshot
from app.payroll_storage import INITIAL_ALIASES, INITIAL_STANDARD_ITEMS


def common_plan(*, blocked=False, source=True):
    return MaterializationPlan(
        domain="example", plan_version="v1",
        source=(MaterializationSource("file_id", "source-1") if source else None),
        operations=() if blocked else (MaterializationOperation(
            "append", "append_row",
            {"resource": "sheets", "sheet_key": "safe"},
            {"raw_ocr": "must-not-enter-common-audit"},
            (MaterializationPrecondition("schema_ok", True),),
        ),),
        blocked=blocked,
        blocked_reason="schema_mismatch" if blocked else None,
    )


def common_result(plan, *, status="applied", external_write=True, operation=True,
                  occurred_at=None):
    return MaterializationResult(
        plan_id=plan.plan_id, status=status, external_write=external_write,
        action_requested="append", actions_performed=("append",) if external_write else (),
        operations=((MaterializationOperationResult(
            "append", "applied" if status == "applied" else "failed",
            external_write, "write_failed" if status == "failed" else None,
        ),) if operation else ()),
        reason="write_failed" if status == "failed" else status,
        observed_before={"schema_ok": True},
        observed_after={"row_count": 1} if external_write else None,
        occurred_at=occurred_at,
    )


def test_common_audit_is_immutable_deterministic_and_excludes_operation_payload():
    plan = common_plan()
    first = build_materialization_audit_record(plan, common_result(plan))
    second = build_materialization_audit_record(plan, common_result(plan))

    with pytest.raises(FrozenInstanceError):
        first.reason = "other"
    with pytest.raises(TypeError):
        first.observed_before["schema_ok"] = False
    with pytest.raises(TypeError):
        first.operations[0].target["sheet_key"] = "other"
    assert first.to_json() == second.to_json()
    assert first.source == plan.source
    assert first.occurred_at is None
    assert "must-not-enter-common-audit" not in first.to_json()


def test_audit_consistency_gate_rejects_plan_and_unknown_operation_mismatches():
    plan = common_plan()
    other = common_plan(source=False)
    with pytest.raises(ValueError, match="plan_id_mismatch"):
        build_materialization_audit_record(plan, common_result(other))

    unknown = MaterializationResult(
        plan_id=plan.plan_id, status="failed", external_write=False,
        action_requested="append", actions_performed=(),
        operations=(MaterializationOperationResult(
            "unknown", "failed", False, "unknown_operation",
        ),),
        reason="unknown_operation",
    )
    with pytest.raises(ValueError, match="unknown_operation_id"):
        build_materialization_audit_record(plan, unknown)


def test_plan_blocked_and_result_status_remain_separate_from_runtime_failure():
    blocked_plan = common_plan(blocked=True, source=False)
    blocked = build_materialization_audit_record(blocked_plan, MaterializationResult(
        plan_id=blocked_plan.plan_id, status="blocked", external_write=False,
        action_requested="blocked", actions_performed=(), operations=(),
        reason="schema_mismatch",
    ))
    ready_plan = common_plan()
    failed = build_materialization_audit_record(
        ready_plan, common_result(ready_plan, status="failed", external_write=True),
    )

    assert blocked.plan_blocked is True
    assert blocked.result_status == "blocked"
    assert blocked.source is None
    assert failed.plan_blocked is False
    assert failed.result_status == "failed"
    assert failed.external_write is True


@pytest.mark.parametrize(("status", "external_write", "operation"), [
    ("applied", True, True),
    ("skipped", False, False),
    ("blocked", False, False),
    ("failed", False, True),
    ("failed", True, True),
])
def test_audit_copies_external_write_fact_without_inferring_from_status(
    status, external_write, operation,
):
    plan = common_plan(blocked=status == "blocked")
    result = common_result(
        plan, status=status, external_write=external_write,
        operation=operation and not plan.blocked,
    )

    audit = build_materialization_audit_record(plan, result)

    assert audit.result_status == status
    assert audit.external_write is external_write


def paypay_plan(*, action="append", blocked=False):
    record = CoverageConfirmationRecord(
        schema_version="1", provider="paypay", content_sha256="a" * 64,
        confirmed_start="2026-08-01", confirmed_end="2026-08-31",
        range_source="user_confirmed",
        confirmed_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        confirmation_version="1", source_filename="transactions.csv",
    )
    created_at = datetime(2026, 9, 1, 1, tzinfo=timezone.utc)
    existing = CoverageConfirmationWritePlan(
        target_spreadsheet_id="spreadsheet-1", record=record, created_at=created_at,
        action_requested="blocked" if blocked else action,
        expected_sheet_status="invalid" if blocked else (
            "sheet_missing" if action == "create_and_append" else "exact_match"
        ),
        expected_duplicate_status=None if blocked else "not_found",
        candidate_row=tuple(coverage_confirmation_to_sheet_row(
            record, created_at=created_at,
        ).to_sheet_row()),
        blocked=blocked, reason="schema_mismatch" if blocked else "ready",
    )
    return coverage_confirmation_to_materialization_plan(existing)


def paypay_status(*, reason="prewrite_revalidation_passed"):
    return {
        "spreadsheet_matches": True, "sheet_status": "exact_match",
        "schema_status": "exact_match", "current_headers": [],
        "existing_row_count": 0, "invalid_row_count": 0,
        "identity_conflict_count": 0, "duplicate_status": "not_found",
        "current_action": "append", "safe_to_write": True, "reason": reason,
    }


def paypay_result(**overrides):
    value = {
        "action_requested": "append", "action_performed": ("append_row",),
        "created_sheet": False, "wrote_header": False, "appended_row": True,
        "skipped_duplicate": False, "blocked": False,
        "reason": "postwrite_verification_passed",
        "prewrite_status": paypay_status(),
        "postwrite_status": paypay_status(reason="postwrite_verification_passed"),
        "external_write": True,
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(("action", "blocked", "overrides", "expected"), [
    ("append", False, {}, ("applied", True)),
    ("skip_duplicate", False, {
        "action_requested": "skip_duplicate", "action_performed": ("skip_duplicate",),
        "appended_row": False, "skipped_duplicate": True, "external_write": False,
    }, ("skipped", False)),
    ("append", True, {
        "action_requested": "blocked", "action_performed": (), "appended_row": False,
        "blocked": True, "external_write": False, "reason": "schema_mismatch",
        "postwrite_status": None,
    }, ("blocked", False)),
    ("create_and_append", False, {
        "action_requested": "create_and_append", "action_performed": (),
        "appended_row": False, "blocked": True, "external_write": False,
        "reason": "sheet_create_failed", "postwrite_status": None,
    }, ("failed", False)),
    ("create_and_append", False, {
        "action_requested": "create_and_append", "action_performed": ("create_sheet",),
        "created_sheet": True, "appended_row": False, "blocked": True,
        "external_write": True, "reason": "header_write_failed",
    }, ("failed", True)),
])
def test_paypay_plan_and_result_build_privacy_safe_audit_without_timestamp(
    action, blocked, overrides, expected,
):
    plan = paypay_plan(action=action, blocked=blocked)
    result = coverage_confirmation_result_to_materialization_result(
        paypay_result(**overrides), plan,
    )

    audit = build_materialization_audit_record(plan, result)

    assert (audit.result_status, audit.external_write) == expected
    assert audit.plan_id == plan.plan_id
    assert audit.domain == "paypay"
    assert audit.source == plan.source
    assert audit.occurred_at is None
    assert "transactions.csv" not in audit.to_json()
    assert "provider_completeness" not in audit.to_json()


def payroll_plan(standards=(), aliases=()):
    existing = build_master_sync_plan(PayrollSheetsSnapshot(
        schemas=[], standard_items=list(standards), aliases=list(aliases),
    ))
    return payroll_master_sync_to_materialization_plan(existing)


def payroll_result(**overrides):
    value = {
        "applied": True, "added_standard_items": [], "added_aliases": [],
        "already_present": [], "skipped": [], "conflicts": [], "errors": [],
        "applied_at": "2026-09-04T00:00:00+00:00",
    }
    value.update(overrides)
    return value


def test_payroll_success_audit_keeps_source_none_timestamp_and_stage_results():
    plan = payroll_plan()
    result = payroll_master_sync_result_to_materialization_result(payroll_result(
        added_standard_items=["basic_pay"],
        added_aliases=["alias-basic-pay-honkyu"],
    ), plan)

    audit = build_materialization_audit_record(plan, result)

    assert audit.source is None
    assert audit.occurred_at == "2026-09-04T00:00:00+00:00"
    assert [(item.operation_id, item.result_status) for item in audit.operations] == [
        ("append_standard_items", "applied"),
        ("append_aliases", "applied"),
    ]


def test_payroll_existing_only_and_conflict_audits_need_no_fake_operations():
    noop_plan = payroll_plan(INITIAL_STANDARD_ITEMS, INITIAL_ALIASES)
    noop_result = payroll_master_sync_result_to_materialization_result(payroll_result(
        already_present=[
            {"kind": "standard_item", "id": "basic_pay"},
            {"kind": "alias", "id": "alias-basic-pay-honkyu"},
        ],
    ), noop_plan)
    noop = build_materialization_audit_record(noop_plan, noop_result)

    blocked_plan = payroll_master_sync_to_materialization_plan(build_master_sync_plan(
        PayrollSheetsSnapshot(schemas=[], standard_items=[
            INITIAL_STANDARD_ITEMS[0].model_copy(update={"active": False}),
        ])
    ))
    blocked_result = payroll_master_sync_result_to_materialization_result(payroll_result(
        applied=False,
        skipped=[{"kind": "standard_item", "id": "overtime_pay"}],
        conflicts=[{
            "kind": "standard_item", "code_id": "basic_pay",
            "reason": "standard_item_id_collision_or_inactive",
        }],
    ), blocked_plan)
    blocked = build_materialization_audit_record(blocked_plan, blocked_result)

    assert noop.result_status == "skipped"
    assert noop.operations == ()
    assert blocked.plan_blocked is True
    assert blocked.result_status == "blocked"
    assert blocked.operations == ()


def test_payroll_partial_failure_audit_keeps_non_atomic_stage_outcomes():
    plan = payroll_plan()
    result = payroll_master_sync_result_to_materialization_result(payroll_result(
        applied=False,
        added_standard_items=["basic_pay"],
        errors=[{
            "stage": "aliases", "error": "provider detail must be redacted",
            "outcome": "read_back_reconciled",
            "unconfirmed_ids": ["alias-basic-pay-honkyu"],
        }],
    ), plan)

    audit = build_materialization_audit_record(plan, result)

    assert audit.result_status == "failed"
    assert audit.external_write is True
    assert [(item.result_status, item.external_write) for item in audit.operations] == [
        ("applied", True), ("failed", False),
    ]
    assert "provider detail" not in audit.to_json()
