from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from app.coverage_confirmation import CoverageConfirmationRecord, coverage_confirmation_to_sheet_row
from app.coverage_confirmation_sheets_apply import (
    CoverageConfirmationApplyResult,
    CoverageConfirmationWritePlan,
    CoverageConfirmationWriteStatus,
)
from app.materialization import MaterializationResult
from app.paypay_materialization import coverage_confirmation_to_materialization_plan
from app.paypay_materialization_result import (
    coverage_confirmation_result_to_materialization_result,
)


def record():
    return CoverageConfirmationRecord(
        schema_version="1", provider="paypay", content_sha256="a" * 64,
        confirmed_start="2026-08-01", confirmed_end="2026-08-31",
        range_source="user_confirmed",
        confirmed_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        confirmation_version="1", source_filename="transactions.csv",
    )


def plan(*, action="append", blocked=False):
    item = record()
    created_at = datetime(2026, 9, 1, 1, tzinfo=timezone.utc)
    write_plan = CoverageConfirmationWritePlan(
        target_spreadsheet_id="spreadsheet-1", record=item, created_at=created_at,
        action_requested="blocked" if blocked else action,
        expected_sheet_status="invalid" if blocked else (
            "sheet_missing" if action == "create_and_append" else "exact_match"
        ),
        expected_duplicate_status=None if blocked else "not_found",
        candidate_row=tuple(coverage_confirmation_to_sheet_row(
            item, created_at=created_at,
        ).to_sheet_row()),
        blocked=blocked,
        reason="schema_mismatch" if blocked else "ready",
    )
    return coverage_confirmation_to_materialization_plan(write_plan)


def status(*, duplicate="not_found", safe=True, reason="prewrite_revalidation_passed"):
    return {
        "spreadsheet_matches": True,
        "sheet_status": "exact_match",
        "schema_status": "exact_match",
        "current_headers": ["raw-header-that-must-not-be-audited"],
        "existing_row_count": 1,
        "invalid_row_count": 0,
        "identity_conflict_count": 0,
        "duplicate_status": duplicate,
        "current_action": "append",
        "safe_to_write": safe,
        "reason": reason,
    }


def result(**overrides):
    values = {
        "action_requested": "append",
        "action_performed": ("append_row",),
        "created_sheet": False,
        "wrote_header": False,
        "appended_row": True,
        "skipped_duplicate": False,
        "blocked": False,
        "reason": "postwrite_verification_passed",
        "prewrite_status": status(),
        "postwrite_status": status(
            duplicate="exact_duplicate", safe=True,
            reason="postwrite_verification_passed",
        ),
        "external_write": True,
    }
    values.update(overrides)
    return values


def test_result_contract_is_immutable_and_deterministically_serialized():
    materialized = coverage_confirmation_result_to_materialization_result(
        result(), plan(),
    )

    with pytest.raises(FrozenInstanceError):
        materialized.reason = "other"
    with pytest.raises(TypeError):
        materialized.observed_before["reason"] = "other"
    assert materialized.to_json() == materialized.to_json()
    assert materialized.plan_id == plan().plan_id
    assert materialized.operations[0].operation_id == "append_confirmation"


def test_successful_apply_preserves_action_and_observed_state_without_headers():
    materialized = coverage_confirmation_result_to_materialization_result(
        result(), plan(),
    )

    assert materialized.status == "applied"
    assert materialized.external_write is True
    assert materialized.actions_performed == ("append_row",)
    assert materialized.operations[0].status == "applied"
    assert materialized.observed_before["duplicate_status"] == "not_found"
    assert materialized.observed_after["duplicate_status"] == "exact_duplicate"
    assert "current_headers" not in materialized.to_json()
    assert "transactions.csv" not in materialized.to_json()


def test_duplicate_no_write_maps_to_skipped_operation():
    materialized = coverage_confirmation_result_to_materialization_result(
        result(
            action_performed=("skip_duplicate",), appended_row=False,
            skipped_duplicate=True, external_write=False,
            reason="postwrite_verification_passed",
            postwrite_status=status(
                duplicate="exact_duplicate", safe=True,
                reason="postwrite_verification_passed",
            ),
        ),
        plan(),
    )

    assert materialized.status == "skipped"
    assert materialized.external_write is False
    assert materialized.operations[0].operation_id == "append_confirmation"
    assert materialized.operations[0].status == "skipped"


def test_blocked_result_keeps_reason_without_recomputing_preconditions():
    materialized = coverage_confirmation_result_to_materialization_result(
        result(
            action_performed=(), appended_row=False, blocked=True,
            external_write=False, reason="identity_range_conflict",
            prewrite_status=status(duplicate="identity_conflict", safe=False,
                                   reason="identity_range_conflict"),
            postwrite_status=None,
        ),
        plan(),
    )

    assert materialized.status == "blocked"
    assert materialized.reason == "identity_range_conflict"
    assert materialized.operations == ()
    assert materialized.observed_before["duplicate_status"] == "identity_conflict"


def test_partial_failure_allows_external_write_true_and_failed_status():
    materialized = coverage_confirmation_result_to_materialization_result(
        result(
            action_requested="create_and_append", action_performed=("create_sheet",),
            created_sheet=True, appended_row=False, blocked=True,
            external_write=True, reason="header_write_failed",
            postwrite_status=status(safe=False, reason="header_write_failed"),
        ),
        plan(action="create_and_append"),
    )

    assert materialized.status == "failed"
    assert materialized.external_write is True
    assert [(item.operation_id, item.status) for item in materialized.operations] == [
        ("create_sheet", "applied"),
        ("write_header", "failed"),
    ]


def test_postwrite_failure_keeps_applied_operation_and_failed_overall_status():
    materialized = coverage_confirmation_result_to_materialization_result(
        result(
            blocked=True, external_write=True,
            reason="postwrite_verification_failed",
            postwrite_status=status(safe=False, reason="postwrite_verification_failed"),
        ),
        plan(),
    )

    assert materialized.status == "failed"
    assert materialized.operations[0].status == "applied"


def test_existing_dataclass_result_is_supported_without_apply():
    existing = CoverageConfirmationApplyResult(
        action_requested="append", action_performed=("append_row",),
        created_sheet=False, wrote_header=False, appended_row=True,
        skipped_duplicate=False, blocked=False,
        reason="postwrite_verification_passed",
        prewrite_status=CoverageConfirmationWriteStatus(**status()),
        postwrite_status=CoverageConfirmationWriteStatus(**status(
            duplicate="exact_duplicate", safe=True,
            reason="postwrite_verification_passed",
        )),
        external_write=True,
    )

    materialized = coverage_confirmation_result_to_materialization_result(existing, plan())

    assert isinstance(materialized, MaterializationResult)
    assert materialized.status == "applied"


def test_result_reason_never_changes_the_referenced_plan_identity():
    materialization_plan = plan()
    blocked = coverage_confirmation_result_to_materialization_result(
        result(
            action_performed=(), appended_row=False, blocked=True,
            external_write=False, reason="schema_mismatch",
            prewrite_status=status(safe=False, reason="schema_mismatch"),
            postwrite_status=None,
        ), materialization_plan,
    )
    conflict = coverage_confirmation_result_to_materialization_result(
        result(
            action_performed=(), appended_row=False, blocked=True,
            external_write=False, reason="identity_range_conflict",
            prewrite_status=status(duplicate="identity_conflict", safe=False,
                                   reason="identity_range_conflict"),
            postwrite_status=None,
        ), materialization_plan,
    )

    assert blocked.plan_id == conflict.plan_id == materialization_plan.plan_id
