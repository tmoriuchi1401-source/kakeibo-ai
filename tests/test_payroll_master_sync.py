from app.payroll_master_sync import build_master_sync_plan, master_sync_preview
from app.payroll_sheets import PayrollSheetsSnapshot
from app.payroll_storage import (
    INITIAL_ALIASES,
    INITIAL_STANDARD_ITEMS,
    PayrollItemAliasRecord,
    PayrollStandardItemRecord,
)


def snapshot(standards=(), aliases=()):
    return PayrollSheetsSnapshot(schemas=[], standard_items=list(standards), aliases=list(aliases))


def test_empty_sheet_previews_all_code_masters_in_dependency_order():
    plan = build_master_sync_plan(snapshot())
    output = master_sync_preview(plan)
    assert output["code_standard_item_count"] == 24
    assert output["schema_ok"] is True
    assert output["sheets_active_standard_item_count"] == 0
    assert output["code_exact_alias_count"] == 20
    assert output["sheets_active_alias_count"] == 0
    assert len(output["would_add_standard_items"]) == 24
    assert len(output["would_add_aliases"]) == 20
    assert output["already_present"] == []
    assert output["conflict_unsafe"] == []


def test_identical_active_rows_are_already_present_not_duplicates():
    plan = build_master_sync_plan(snapshot(INITIAL_STANDARD_ITEMS, INITIAL_ALIASES))
    assert plan.would_add_standard_items == []
    assert plan.would_add_aliases == []
    assert len(plan.already_present) == 44
    assert plan.conflict_unsafe == []


def test_standard_collision_blocks_it_and_dependent_alias():
    conflicting = PayrollStandardItemRecord(
        standard_item_id="union_dues", standard_name="別の組合費",
        section="earning", value_type="money",
    )
    plan = build_master_sync_plan(snapshot([conflicting]))
    reasons = {(issue.code_id, issue.reason) for issue in plan.conflict_unsafe}
    assert ("union_dues", "standard_item_id_collision_or_inactive") in reasons
    assert ("alias-union-dues", "standard_item_dependency_not_safe") in reasons
    assert "union_dues" not in {item.standard_item_id for item in plan.would_add_standard_items}
    assert "alias-union-dues" not in {alias.alias_id for alias in plan.would_add_aliases}


def test_exact_alias_conflict_is_unsafe_and_inactive_rows_are_not_overwritten():
    inactive = PayrollStandardItemRecord(
        standard_item_id="union_dues", standard_name="組合費",
        section="deduction", value_type="money", active=False,
    )
    conflicting_alias = PayrollItemAliasRecord(
        alias_id="other-id", raw_item_name="課税対象額",
        standard_item_id="basic_pay",
    )
    plan = build_master_sync_plan(snapshot([inactive], [conflicting_alias]))
    reasons = {(issue.code_id, issue.reason) for issue in plan.conflict_unsafe}
    assert ("union_dues", "standard_item_id_collision_or_inactive") in reasons
    assert ("alias-taxable-amount", "exact_alias_collision_or_inactive") in reasons


def test_same_exact_mapping_with_different_alias_id_is_already_present():
    standard = next(item for item in INITIAL_STANDARD_ITEMS
                    if item.standard_item_id == "union_dues")
    alias = PayrollItemAliasRecord(
        alias_id="existing-id", raw_item_name="組合費", standard_item_id="union_dues",
    )
    plan = build_master_sync_plan(snapshot([standard], [alias]))
    assert {entry["id"] for entry in plan.already_present} >= {
        "union_dues", "alias-union-dues",
    }
    assert "alias-union-dues" not in {item.alias_id for item in plan.would_add_aliases}


def test_schema_mismatch_blocks_every_candidate():
    from app.payroll_sheets import PayrollSheetSchemaResult

    target = snapshot()
    target.schemas = [PayrollSheetSchemaResult(
        sheet_key="payroll_standard_items", sheet_title="給与標準項目",
        schema_ok=False, sheet_missing=True,
    )]
    plan = build_master_sync_plan(target)
    assert plan.schema_ok is False
    assert plan.would_add_standard_items == []
    assert plan.would_add_aliases == []
    assert plan.conflict_unsafe[0].reason == "sheet_schema_not_safe"
