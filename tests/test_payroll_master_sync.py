import sys

import app.cli as cli
from app.payroll_master_sync import (
    apply_master_sync,
    build_master_sync_plan,
    master_sync_preview,
)
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


class FakeRepository:
    def __init__(self, current=None, *, fail_stage=None):
        self.current = current or snapshot()
        self.fail_stage = fail_stage
        self.writes = []

    def snapshot(self):
        return self.current.model_copy(deep=True)

    def append_standard_items(self, items):
        self.writes.append(("standards", [item.standard_item_id for item in items]))
        if self.fail_stage == "standards":
            raise RuntimeError("standard write failed")
        self.current.standard_items.extend(item.model_copy(deep=True) for item in items)

    def append_aliases(self, aliases):
        self.writes.append(("aliases", [alias.alias_id for alias in aliases]))
        if self.fail_stage == "aliases":
            raise RuntimeError("alias write failed")
        self.current.aliases.extend(alias.model_copy(deep=True) for alias in aliases)


def test_safe_apply_writes_standards_before_aliases_and_is_idempotent():
    repository = FakeRepository()
    first = apply_master_sync(build_master_sync_plan(repository.snapshot()), repository,
                              repository, confirmed=True)
    assert first["applied"] is True
    assert len(first["added_standard_items"]) == 24
    assert len(first["added_aliases"]) == 20
    assert [stage for stage, _ in repository.writes] == ["standards", "aliases"]

    repository.writes.clear()
    second = apply_master_sync(build_master_sync_plan(repository.snapshot()), repository,
                               repository, confirmed=True)
    assert second["applied"] is True
    assert second["added_standard_items"] == []
    assert second["added_aliases"] == []
    assert len(second["already_present"]) == 44
    assert repository.writes == []


def test_apply_requires_confirmation_and_does_not_write():
    repository = FakeRepository()
    try:
        apply_master_sync(build_master_sync_plan(repository.snapshot()), repository,
                          repository, confirmed=False)
    except RuntimeError as exc:
        assert "--apply" in str(exc)
    else:
        raise AssertionError("confirmation was not required")
    assert repository.writes == []


def test_conflict_or_schema_mismatch_stops_all_writes():
    inactive = next(item for item in INITIAL_STANDARD_ITEMS
                    if item.standard_item_id == "union_dues").model_copy(
                        update={"active": False})
    for current in (snapshot([inactive]), PayrollSheetsSnapshot(schemas=[
        __import__("app.payroll_sheets", fromlist=["PayrollSheetSchemaResult"])
        .PayrollSheetSchemaResult(
            sheet_key="payroll_standard_items", sheet_title="給与標準項目",
            schema_ok=False, sheet_missing=True,
        )
    ])):
        repository = FakeRepository(current)
        result = apply_master_sync(build_master_sync_plan(current), repository,
                                   repository, confirmed=True)
        assert result["applied"] is False
        assert repository.writes == []
        assert result["conflicts"]


def test_preview_candidate_expansion_during_reread_stops_all_writes():
    repository = FakeRepository()
    old_plan = build_master_sync_plan(snapshot(INITIAL_STANDARD_ITEMS, INITIAL_ALIASES))
    result = apply_master_sync(old_plan, repository, repository, confirmed=True)
    assert result["applied"] is False
    assert repository.writes == []
    assert result["conflicts"][-1]["reason"] == "unexpected_state_change_since_preview"


def test_unsafe_alias_target_is_never_written():
    conflicting = PayrollStandardItemRecord(
        standard_item_id="union_dues", standard_name="別名", section="earning",
        value_type="money",
    )
    repository = FakeRepository(snapshot([conflicting]))
    result = apply_master_sync(build_master_sync_plan(repository.snapshot()), repository,
                               repository, confirmed=True)
    assert result["applied"] is False
    assert repository.writes == []


def test_write_failures_stop_later_stages_and_allow_safe_retry():
    repository = FakeRepository(fail_stage="standards")
    plan = build_master_sync_plan(repository.snapshot())
    result = apply_master_sync(plan, repository, repository, confirmed=True)
    assert result["errors"][0]["stage"] == "standard_items"
    assert [stage for stage, _ in repository.writes] == ["standards"]
    assert repository.current.aliases == []

    repository.fail_stage = "aliases"
    repository.writes.clear()
    result = apply_master_sync(plan, repository, repository, confirmed=True)
    assert result["errors"][0]["stage"] == "aliases"
    assert [stage for stage, _ in repository.writes] == ["standards", "aliases"]
    assert len(repository.current.standard_items) == 24
    assert repository.current.aliases == []

    repository.fail_stage = None
    retry_plan = build_master_sync_plan(repository.snapshot())
    result = apply_master_sync(retry_plan, repository, repository, confirmed=True)
    assert result["added_standard_items"] == []
    assert len(result["added_aliases"]) == 20


def test_cli_without_apply_uses_read_only_preview(monkeypatch, capsys):
    repository = FakeRepository()
    monkeypatch.setattr(cli, "Settings", lambda: type("S", (), {
        "spreadsheet_id": "sheet-id", "validate": lambda self, **kwargs: None,
    })())
    monkeypatch.setattr(cli, "PayrollSheetsReadRepository", lambda _id: repository)
    monkeypatch.setattr(
        cli, "PayrollMasterSyncWriteRepository",
        lambda _id: (_ for _ in ()).throw(AssertionError("writer constructed")),
    )
    monkeypatch.setattr(sys, "argv", ["prog", "payroll-master-sync"])
    cli.main()
    assert '"applied": false' in capsys.readouterr().out
    assert repository.writes == []
