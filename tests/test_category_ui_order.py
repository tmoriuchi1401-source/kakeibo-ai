from copy import deepcopy
import json

from app.category_rule_ui import CategoryRuleUIPipeline
from app.category_ui_order import consolidate_blocks, consolidate_rules, member_keys, source_snapshot
from app.category_sheet_requests import merge_results
from app.category_backfill_ui import CategoryBackfillUIPipeline
from test_category_operations import OperationsDB
from test_category_backfill import import_row
from test_category_sheet_requests import blocks


def classified():
    db = OperationsDB()
    db.expenses = {}
    db.imports = []
    for i in range(3):
        key, imported = f"M-{i}", f"p{i}"
        db.expenses[key] = (i+2, [key,"2026-08-10","店","自動計上",100,"食費","外食","","PayPay","",imported,"","active"])
        db.imports.append(import_row(imported,"店",100,key))
    return db


def refresh(db):
    return CategoryRuleUIPipeline(db, ui_enabled=True, save_enabled=True).refresh()


def expanded(db):
    refresh(db)
    template = db.ui[0]
    rows = []
    for key, (_, expense) in db.expenses.items():
        row = deepcopy(template)
        row[6] = row[7] = key
        data = json.loads(source_snapshot(row[11]))
        data.update(expense_id=key, target_id=key, import_id=expense[10])
        row[11] = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        rows.append(row)
    db.ui = rows
    return rows


def test_duplicates_remain_consolidated_and_registration_uses_exact_representative_proof():
    db = classified()
    refresh(db)
    assert len(db.ui) == 1 and len(member_keys(db.ui[0])) == 3
    first = deepcopy(db.ui)
    refresh(db)
    assert db.ui == first
    db.ui[0][4] = True
    refresh(db)
    result = CategoryRuleUIPipeline(db, ui_enabled=True, save_enabled=True).apply_checked()
    assert result["checked"] == 1 and len(db.rule_rows) == 1
    assert not db.category_updates
    refresh(db)
    assert len(db.ui) == 1 and db.ui[0][1].endswith("登録済み")


def test_category_override_and_past_choice_survive_multiple_refreshes():
    db = classified()
    rows = expanded(db)
    for row in rows:
        row[2:4] = ["日用品", "消耗品"]
    rows[1][5] = True
    refresh(db)
    assert len(db.ui) == 1 and db.ui[0][5] is True
    for _ in range(3):
        refresh(db)
        assert len(db.ui) == 1 and db.ui[0][2:6] == ["日用品", "消耗品", False, True]
    assert not db.category_updates


def test_other_categories_sources_and_accounts_are_not_duplicates():
    db = classified()
    rows = expanded(db)
    rows[1][2] = "交通"
    data = json.loads(rows[2][11]); data["account_alias"] = "other-account"
    rows[2][11] = json.dumps(data)
    assert len(consolidate_rules(rows)) == 3
    data["account_alias"] = ""; data["source"] = "OtherSource"
    rows[2][11] = json.dumps(data)
    assert len(consolidate_rules(rows)) == 3


def test_opposing_future_decisions_and_held_rows_are_not_merged():
    db = classified(); rows = expanded(db)
    rows[0][4] = True; rows[1][4] = "登録しない"
    rows[2][1] = "held: source_changed"
    result = consolidate_rules(rows)
    assert len(result) == 3 and result[-1][4] == "登録しない"


def test_processed_candidates_sort_last_without_losing_past_check():
    db = classified(); rows = expanded(db)
    rows[0][2] = "A"; rows[0][1] = "登録済み"; rows[0][5] = True
    rows[1][2] = "B"; rows[1][4] = "登録しない"
    rows[2][2] = "C"
    result = consolidate_rules(rows)
    assert result[0][2] == "C" and len(result) == 3
    assert next(r for r in result if r[2] == "A")[5] is True


def test_historical_controls_migrate_by_member_key_and_distinct_periods_survive():
    db = classified(); rows = expanded(db)
    rows[0][5] = rows[1][5] = True
    past = CategoryBackfillUIPipeline(db, ui_enabled=True, apply_enabled=True)
    past.refresh()
    db.backfill[0][2:4] = ["2026-07", "2026-07"]
    db.backfill[1][2:4] = ["2026-08", "2026-08"]
    refresh(db); past.refresh()
    assert len(db.ui) == 2 and len(db.backfill) == 2
    assert {r[2] for r in db.backfill} == {"2026-07", "2026-08"}
    db.backfill[1][2:4] = ["2026-07", "2026-07"]
    refresh(db); past.refresh()
    assert len(db.ui) == len(db.backfill) == 1
    assert db.backfill[0][2:4] == ["2026-07", "2026-07"]
    db.backfill[0][4] = True
    assert len(past.preview_checked()["results"]) == 1
    assert db.ui[0][5] is False


def test_live_migration_is_idempotent_and_retains_later_edits_to_merged_member():
    db = classified(); expanded(db)
    captured = blocks(db)
    result = consolidate_blocks(captured)
    assert consolidate_blocks(result) == result
    live = deepcopy(captured)
    live["rule"][1][1][2:6] = ["交通", "電車", True, False]
    merged, retained = merge_results(captured, result, live)
    assert retained == 1 and len(merged["rule"][1]) == 2
    changed = next(r for r in merged["rule"][1] if r[6] == "M-1")
    assert changed[2:6] == ["交通", "電車", True, False]
    other = next(r for r in merged["rule"][1] if r[6] != "M-1")
    assert "M-1" not in member_keys(other)
