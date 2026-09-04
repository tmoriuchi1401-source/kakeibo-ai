from app.payroll_master_sync import build_master_sync_plan
from app.payroll_master_sync_materialization import (
    payroll_master_sync_to_materialization_plan,
)
from app.payroll_master_sync_materialization_result import (
    payroll_master_sync_result_to_materialization_result,
)
from app.payroll_sheets import PayrollSheetSchemaResult, PayrollSheetsSnapshot
from app.payroll_storage import (
    INITIAL_ALIASES,
    INITIAL_STANDARD_ITEMS,
    PayrollStandardItemRecord,
)


def snapshot(standards=(), aliases=(), *, schema_ok=True):
    schemas = [] if schema_ok else [PayrollSheetSchemaResult(
        sheet_key="payroll_standard_items", sheet_title="給与標準項目",
        schema_ok=False, sheet_missing=True,
    )]
    return PayrollSheetsSnapshot(
        schemas=schemas, standard_items=list(standards), aliases=list(aliases),
    )


def test_full_append_intent_projects_two_batch_operations_without_source_identity():
    materialized = payroll_master_sync_to_materialization_plan(
        build_master_sync_plan(snapshot()),
    )

    assert materialized.source is None
    assert materialized.blocked is False
    assert [operation.operation_id for operation in materialized.operations] == [
        "append_standard_items", "append_aliases",
    ]
    assert materialized.operations[0].target["sheet_key"] == "payroll_standard_items"
    assert materialized.operations[1].target["sheet_key"] == "payroll_item_aliases"
    assert {item.kind for item in materialized.operations[0].preconditions} == {
        "schema_ok", "conflict_free", "expected_candidate_ids",
    }
    assert "created_at" not in materialized.to_json()


def test_standard_only_and_alias_only_intents_preserve_existing_batch_boundaries():
    previous_standards = [item for item in INITIAL_STANDARD_ITEMS
                          if item.standard_item_id != "collective_savings"]
    standard_only = payroll_master_sync_to_materialization_plan(
        build_master_sync_plan(snapshot(previous_standards, INITIAL_ALIASES)),
    )
    previous_aliases = [alias for alias in INITIAL_ALIASES
                        if alias.alias_id != "alias-collective-savings"]
    alias_only = payroll_master_sync_to_materialization_plan(
        build_master_sync_plan(snapshot(INITIAL_STANDARD_ITEMS, previous_aliases)),
    )

    assert [item.operation_id for item in standard_only.operations] == ["append_standard_items"]
    assert [item.operation_id for item in alias_only.operations] == ["append_aliases"]


def test_noop_and_conflict_preserve_existing_plan_semantics_without_operations():
    noop = payroll_master_sync_to_materialization_plan(
        build_master_sync_plan(snapshot(INITIAL_STANDARD_ITEMS, INITIAL_ALIASES)),
    )
    conflicting = PayrollStandardItemRecord(
        standard_item_id="union_dues", standard_name="別の組合費",
        section="earning", value_type="money",
    )
    blocked = payroll_master_sync_to_materialization_plan(
        build_master_sync_plan(snapshot([conflicting])),
    )

    assert noop.blocked is False
    assert noop.operations == ()
    assert blocked.blocked is True
    assert blocked.blocked_reason == "standard_item_id_collision_or_inactive"
    assert blocked.operations == ()


def test_plan_identity_and_serialization_are_deterministic_without_runtime_created_at():
    first = payroll_master_sync_to_materialization_plan(build_master_sync_plan(snapshot()))
    second = payroll_master_sync_to_materialization_plan(build_master_sync_plan(snapshot()))

    assert first.plan_id == second.plan_id
    assert first.to_json() == second.to_json()


def test_plan_and_result_share_existing_stage_operation_ids():
    plan = payroll_master_sync_to_materialization_plan(build_master_sync_plan(snapshot()))
    result = payroll_master_sync_result_to_materialization_result({
        "applied": True,
        "added_standard_items": ["basic_pay"],
        "added_aliases": ["alias-basic-pay-honkyu"],
        "already_present": [], "skipped": [], "conflicts": [], "errors": [],
        "applied_at": "2026-09-04T00:00:00+00:00",
    }, plan)

    assert result.plan_id == plan.plan_id
    assert [item.operation_id for item in result.operations] == [
        "append_standard_items", "append_aliases",
    ]
