from dataclasses import FrozenInstanceError

import pytest

from app.materialization import (
    MaterializationOperation,
    MaterializationPlan,
    MaterializationPrecondition,
    MaterializationSource,
)
from app.payroll_master_sync_materialization_result import (
    payroll_master_sync_result_to_materialization_result,
)


def master_sync_plan(*, include_standard=True, include_alias=True):
    """A caller-supplied common plan; Phase 3 adds no master-sync plan adapter."""

    operations = []
    if include_standard:
        operations.append(MaterializationOperation(
            "sync_standard_items", "append_row",
            {"resource": "google_sheets", "sheet_key": "payroll_standard_items"},
            {"batch": "standard_items"},
            (MaterializationPrecondition("preview", "approved"),),
        ))
    if include_alias:
        operations.append(MaterializationOperation(
            "sync_aliases", "append_row",
            {"resource": "google_sheets", "sheet_key": "payroll_item_aliases"},
            {"batch": "aliases"},
            (MaterializationPrecondition("preview", "approved"),),
        ))
    return MaterializationPlan(
        domain="payroll",
        plan_version="master-sync-example-v1",
        # Master sync has no existing source identity.  This generic source is
        # test-only plumbing for the already-required common plan contract; the
        # production adapter neither reads nor derives it.
        source=MaterializationSource("test_master_data", "test-master-v1"),
        operations=tuple(operations),
    )


def result(**overrides):
    value = {
        "applied": True,
        "added_standard_items": [],
        "added_aliases": [],
        "already_present": [],
        "skipped": [],
        "conflicts": [],
        "errors": [],
        "applied_at": "2026-09-04T00:00:00+00:00",
    }
    value.update(overrides)
    return value


def test_full_success_projects_two_existing_apply_stages_without_apply():
    plan = master_sync_plan()
    materialized = payroll_master_sync_result_to_materialization_result(result(
        added_standard_items=["basic_pay"],
        added_aliases=["alias-basic-pay-honkyu"],
    ), plan)

    assert materialized.plan_id == plan.plan_id
    assert materialized.status == "applied"
    assert materialized.external_write is True
    assert materialized.actions_performed == ("sync_standard_items", "sync_aliases")
    assert [(item.operation_id, item.status) for item in materialized.operations] == [
        ("sync_standard_items", "applied"),
        ("sync_aliases", "applied"),
    ]
    assert "2026-09-04" not in materialized.to_json()


def test_all_already_present_is_skipped_without_external_write():
    materialized = payroll_master_sync_result_to_materialization_result(result(
        already_present=[
            {"kind": "standard_item", "id": "basic_pay"},
            {"kind": "alias", "id": "alias-basic-pay-honkyu"},
        ],
    ), master_sync_plan())

    assert materialized.status == "skipped"
    assert materialized.external_write is False
    assert {item.status for item in materialized.operations} == {"skipped"}


def test_conflict_is_blocked_without_recomputing_or_writing():
    materialized = payroll_master_sync_result_to_materialization_result(result(
        applied=False,
        skipped=[{"kind": "standard_item", "id": "basic_pay"}],
        conflicts=[{
            "kind": "standard_item", "code_id": "basic_pay",
            "reason": "standard_item_id_collision_or_inactive",
        }],
    ), master_sync_plan(include_alias=False))

    assert materialized.status == "blocked"
    assert materialized.external_write is False
    assert materialized.reason == "conflicts_detected"
    assert materialized.observed_after["conflicts"][0]["reason"] == (
        "standard_item_id_collision_or_inactive"
    )


def test_failure_before_verified_write_omits_raw_writer_error():
    materialized = payroll_master_sync_result_to_materialization_result(result(
        applied=False,
        errors=[{
            "stage": "standard_items", "error": "provider response with raw details",
            "outcome": "read_back_reconciled", "unconfirmed_ids": ["basic_pay"],
        }],
        skipped=[{"kind": "alias", "id": "alias-basic-pay-honkyu"}],
    ), master_sync_plan())

    assert materialized.status == "failed"
    assert materialized.external_write is False
    assert [(item.operation_id, item.status) for item in materialized.operations] == [
        ("sync_standard_items", "failed"),
        ("sync_aliases", "skipped"),
    ]
    assert materialized.reason == "write_failed"
    assert "raw details" not in materialized.to_json()


def test_partial_success_keeps_applied_standard_and_failed_alias_stage():
    materialized = payroll_master_sync_result_to_materialization_result(result(
        applied=False,
        added_standard_items=["basic_pay"],
        errors=[{
            "stage": "aliases", "error": "alias writer failed",
            "outcome": "read_back_reconciled", "unconfirmed_ids": ["alias-basic-pay-honkyu"],
        }],
    ), master_sync_plan())

    assert materialized.status == "failed"
    assert materialized.external_write is True
    assert [(item.operation_id, item.status, item.external_write) for item in materialized.operations] == [
        ("sync_standard_items", "applied", True),
        ("sync_aliases", "failed", False),
    ]


def test_postwrite_failure_preserves_existing_verification_reason():
    materialized = payroll_master_sync_result_to_materialization_result(result(
        applied=False,
        added_standard_items=["basic_pay"],
        errors=[{
            "stage": "standard_items", "error": "post_write_verification_failed",
        }],
    ), master_sync_plan(include_alias=False))

    assert materialized.status == "failed"
    assert materialized.external_write is True
    assert materialized.reason == "post_write_verification_failed"
    assert materialized.operations[0].reason == "post_write_verification_failed"


def test_result_is_deeply_immutable_and_requires_real_plan_operation_identity():
    materialized = payroll_master_sync_result_to_materialization_result(result(
        added_standard_items=["basic_pay"],
    ), master_sync_plan(include_alias=False))

    with pytest.raises(FrozenInstanceError):
        materialized.reason = "other"
    with pytest.raises(TypeError):
        materialized.observed_after["errors"] = []
    with pytest.raises(ValueError, match="operation_missing_from_plan"):
        payroll_master_sync_result_to_materialization_result(result(
            added_aliases=["alias-basic-pay-honkyu"],
        ), master_sync_plan(include_standard=True, include_alias=False))


def test_inconsistent_unapplied_result_fails_closed_instead_of_becoming_skipped():
    with pytest.raises(ValueError, match="unclassified_payroll_master_sync_apply_result"):
        payroll_master_sync_result_to_materialization_result(
            result(applied=False), master_sync_plan(),
        )
