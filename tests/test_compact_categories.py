from copy import deepcopy
import json
import re

import pytest

from app.compact_categories import (CompactCategoryMigration, HEADERS, MARKER, TITLE, VERSION,
    category_rows, compact_helper, controls, logical_rule_row, migration_plan, physical_rule_row)
from app.monthly_projection import Category, CategoryCatalog, ProjectionError
from app.sheets import CATEGORY_WORKFLOW_MARKERS, SheetsDB
from app.sheets_ui import EXPENSE_CATEGORY_HELPER_ID, build_plan


def catalog():
    return CategoryCatalog([Category("CAT-food", "食費", "食料品"),
                            Category("CAT-meal", "食費", "外食"),
                            Category("CAT-old", "古い分類", "", active=False)])


def snapshot():
    def sheet(title, sid, rows, cols):
        return {"properties": {"title": title, "sheetId": sid,
                               "gridProperties": {"rowCount": rows, "columnCount": cols}}}
    helper = sheet(TITLE, EXPENSE_CATEGORY_HELPER_ID, 1001, 1000)
    helper["developerMetadata"] = [{"metadataKey": MARKER, "metadataValue": "1"}]
    markers = list(CATEGORY_WORKFLOW_MARKERS.values())
    return {"metadata": {"sheets": [helper, sheet("支出明細", 0, 7001, 13),
        sheet("カテゴリ操作", 20, 1200, 27), sheet("要確認", 30, 1000, 20)]},
        "workflow_values": [[markers[0]], ["条件", "状態", "大カテゴリ", "小カテゴリ", "今後", "過去"],
            ["店舗", "未保存", "食費", "外食", True, True, "key", "EXP-1", "2026-09-01", "card", "kind", "snapshot"],
            ["古い条件", "保留", "古い分類", "", False, False, "old-key"], [],
            [markers[1]], ["条件", "期間", "開始月", "終了月", "作成"],
            ["期間選択", "", "2025-01", "2025-12", True, "", "request-fixed"], [],
            [markers[2]], ["ID", "内容", "承認"], ["REQ1", "固定内容", True]],
        "metadata_references": False, "audit_complete": True, "review_last_row": 4,
        "references": [["支出明細", 7000, 6, "validation"], ["カテゴリ操作", 2, 3, "validation"]]}


def apply_requests(state, requests):
    """Small native-batch model: updateCells only changes addressed scalars."""
    state = deepcopy(state)
    by_id = {s["properties"]["sheetId"]: s for s in state["metadata"]["sheets"]}
    for request in requests:
        if "updateSheetProperties" in request:
            props = request["updateSheetProperties"]["properties"]
            by_id[props["sheetId"]]["properties"].update(props)
        elif "updateDeveloperMetadata" in request:
            by_id[EXPENSE_CATEGORY_HELPER_ID]["developerMetadata"][0]["metadataValue"] = VERSION
        elif "updateCells" in request:
            body = request["updateCells"]
            grid = body["range"]
            sid = grid["sheetId"]
            if sid not in (20, EXPENSE_CATEGORY_HELPER_ID):
                raise AssertionError("unexpected scalar write")
            key = "workflow_values" if sid == 20 else "helper_values"
            if "rows" not in body:
                state[key] = []
                continue
            values = state.setdefault(key, [])
            while len(values) < grid["endRowIndex"]:
                values.append([])
            for i, row in enumerate(body["rows"], grid["startRowIndex"]):
                while len(values[i]) < grid["endColumnIndex"]:
                    values[i].append("")
                for j, cell in enumerate(row["values"], grid["startColumnIndex"]):
                    values[i][j] = next(iter(cell.get("userEnteredValue", {}).values()), "")
        elif "setDataValidation" in request:
            body = request["setDataValidation"]
            g = body["range"]
            title = by_id[g["sheetId"]]["properties"]["title"]
            def inside(ref):
                return (ref[0] == title and g.get("startRowIndex", 0) <= ref[1] < g.get("endRowIndex", 100000)
                        and g.get("startColumnIndex", 0) <= ref[2] < g.get("endColumnIndex", 10000))
            state["references"] = [ref for ref in state["references"] if not inside(ref)]
            state["validation_sources"] = [ref for ref in state.get("validation_sources", []) if not inside(ref)]
            if TITLE in json.dumps(body, ensure_ascii=False):
                for i in range(g["startRowIndex"], g["endRowIndex"]):
                    state["references"].append([title, i, g["startColumnIndex"], "validation"])
                    state["validation_sources"].append([title, i, g["startColumnIndex"], body["rule"]["condition"]])
    return state


def test_migration_shrinks_million_cell_matrix_without_changing_any_ledger_value_or_approval():
    old = snapshot()
    plan = migration_plan(old, catalog())
    assert plan["helper_cells_before"] == 1_001_000
    assert plan["helper_cells_after"] == 12
    after = apply_requests(old, plan["requests"])
    assert after["helper_values"] == category_rows(catalog())
    assert compact_helper(after["metadata"])
    assert after["workflow_values"][2][2:4] == ["食費｜外食", ""]
    assert after["workflow_values"][2][4:] == old["workflow_values"][2][4:]
    assert after["workflow_values"][3][2:4] == ["古い分類｜", ""]
    assert after["workflow_values"][5:] == old["workflow_values"][5:]
    assert not any(ref[0] == "支出明細" for ref in after["references"])
    assert "formulaValue" not in json.dumps(plan, ensure_ascii=False)
    # Replanning an already migrated workbook never rejoins/splits twice.
    replay = migration_plan(after, catalog())
    again = apply_requests(after, replay["requests"])
    assert again["workflow_values"] == after["workflow_values"]
    assert again["helper_values"] == after["helper_values"]


@pytest.mark.parametrize("ref", [["別の表", 7000, 3, "validation"],
    ["カテゴリ操作", 7, 3, "validation"], ["ホーム", 4, 4, "formula"],
    ["支出明細", 12, 8, "validation"]])
def test_unknown_consumers_stop_before_any_mutation(ref):
    state = snapshot()
    state["references"].append(ref)
    with pytest.raises(ProjectionError, match="reference_requires_mapping"):
        migration_plan(state, catalog())


@pytest.mark.parametrize("change,code", [("metadata_references", "metadata_reference"), ("audit_complete", "reference_audit")])
def test_metadata_and_incomplete_audit_fail_closed(change, code):
    state = snapshot()
    state[change] = change == "metadata_references"
    with pytest.raises(ProjectionError, match=code):
        migration_plan(state, catalog())


def test_stable_ids_and_old_pairs_survive_rename_without_reclassifying_ledger():
    renamed = catalog().rename("CAT-food", "食料", "食材")
    assert category_rows(renamed)[1] == ["食料｜食材", "CAT-food", "食料", "食材"]
    assert renamed.resolve("食費", "食料品") == "CAT-food"
    assert all(row[1] != "CAT-old" for row in category_rows(renamed)[1:])
    for pair in [("食費", "外食"), ("古い分類", ""), ("", ""), ("", "旧小分類")]:
        row = ["店舗", "保留", *pair, True, False, "key", "EXP", "day", "source", "kind", "snapshot"]
        assert logical_rule_row(physical_rule_row(row)) == row
        assert SheetsDB._workflow_logical_row("rule", SheetsDB._workflow_physical_row("rule", row, compact=True), compact=True) == row
    with pytest.raises(ProjectionError, match="input_invalid"):
        logical_rule_row(["", "", "食費｜外食", "食料品"])


def test_controls_are_bounded_and_appending_does_not_regrow_retired_matrix():
    state = apply_requests(snapshot(), migration_plan(snapshot(), catalog())["requests"])
    helper = compact_helper(state["metadata"])
    requests = controls(20, helper, 6002, 2)
    assert len(requests) == 3
    assert requests[0]["setDataValidation"]["rule"]["condition"]["values"][0]["userEnteredValue"].endswith("!$A$2:$A$3")
    assert requests[0]["setDataValidation"]["range"]["endRowIndex"] == 6003
    class NoWrites:
        def spreadsheets(self): return self
        def batchUpdate(self, **kw): raise AssertionError("append must not touch compact helper")
    db = SheetsDB("synthetic", service=NoWrites())
    db._sheet_metadata_cache = state["metadata"]
    db._restore_expense_category_validation_for_append({"updates": {"updatedRange": "'支出明細'!A1002:M1003"}})
    with pytest.raises(ValueError, match="retired"):
        build_plan(state["metadata"])


def test_atomic_transport_rechecks_inputs_and_does_not_retry_unknown_outcomes():
    class Call:
        def __init__(self, action): self.action = action
        def execute(self, **kw): return self.action()
    class DB:
        sid = "synthetic"
        def __init__(self): self.state = snapshot(); self.svc = self; self.writes = 0; self.fail = False
        def spreadsheets(self): return self
        def batchUpdate(self, **kw):
            def write():
                self.writes += 1
                self.state = apply_requests(self.state, kw["body"]["requests"])
                if self.fail: raise TimeoutError("unknown write outcome")
            return Call(write)
        def _invalidate_sheet_metadata(self): pass
    class Migration(CompactCategoryMigration):
        def snapshot(self): return deepcopy(self.db.state)
    db = DB()
    migration = Migration(db)
    plan = migration.plan(catalog())
    db.state["workflow_values"][2][4] = False
    with pytest.raises(ProjectionError, match="snapshot_changed"):
        migration.apply(plan, catalog())
    assert db.writes == 0
    plan = migration.plan(catalog())
    db.fail = True
    with pytest.raises(TimeoutError): migration.apply(plan, catalog())
    assert db.writes == 1
    db.fail = False
    # Explicit recovery reads the committed marker; no duplicate conversion.
    result = migration.apply(migration.plan(catalog()), catalog())
    assert result["category_helper_cells_after"] == 12
    assert db.state["workflow_values"][2][4] is False


def test_audit_reads_beyond_5000_without_medical_or_payroll_scalar_values():
    class Call:
        def __init__(self, value): self.value = value
        def execute(self): return self.value
    class DB:
        sid = "synthetic"
        def __init__(self): self.svc = self; self.reads = []; self.raw = []; self.state = snapshot()
        def spreadsheets(self): return self
        def _execute_sheet_read(self, factory): return factory().execute()
        def get(self, **kw):
            self.reads.append(kw)
            if "ranges" not in kw:
                meta = deepcopy(self.state["metadata"])
                meta["sheets"].append({"properties": {"title": "Medical", "sheetId": 90,
                    "gridProperties": {"rowCount": 9001, "columnCount": 27}}})
                return Call(meta)
            assert "userEnteredValue(formulaValue)" in kw["fields"]
            assert "effectiveValue" not in kw["fields"]
            if "A7001:M7001" in kw["ranges"][0]:
                return Call({"sheets": [{"data": [{"startRow": 7000, "startColumn": 0,
                    "rowData": [{"values": [{}]*6+[{"dataValidation": {"condition": {
                        "type": "ONE_OF_RANGE", "values": [{"userEnteredValue": f"='{TITLE}'!B7001:ZY7001"}]}}}]}]}]}]})
            return Call({})
        def get_raw(self, rng):
            self.raw.append(rng)
            first, last = map(int, re.search(r"!A(\d+):[A-Z]+(\d+)$", rng).groups())
            return self.state["workflow_values"][first-1:last] if "カテゴリ操作" in rng else [["review"]]
    db = DB()
    observed = CompactCategoryMigration(db).snapshot()
    assert ["支出明細", 7000, 6, "validation"] in observed["references"]
    assert all("Medical" not in value for value in db.raw)
    assert any("Medical" in q.get("ranges", [""])[0] and "9001" in q["ranges"][0] for q in db.reads)
    assert not any(TITLE in q.get("ranges", [""])[0] for q in db.reads)
    migration_plan(observed, catalog())


def test_existing_workflow_roundtrip_reads_all_inputs_and_conflicts_preserve_edits():
    state = apply_requests(snapshot(), migration_plan(snapshot(), catalog())["requests"])
    rule = state["workflow_values"][2]
    # More than 1,000 independently selected rule inputs, followed by pending
    # historical preview and confirmation; none can be cut off by the old cap.
    state["workflow_values"][2:5] = [
        [*rule[:6], f"key-{i}", *rule[7:]] for i in range(1050)] + [[]]
    class Call:
        def __init__(self, value): self.value = value
        def execute(self, **kw): return self.value
    class Service:
        def __init__(self): self.requests = []
        def spreadsheets(self): return self
        def batchUpdate(self, **kw): self.requests.extend(kw["body"]["requests"]); return Call({})
    class DB(SheetsDB):
        def get_raw(self, rng):
            first, last = map(int, re.search(r"!A(\d+):L(\d+)$", rng).groups())
            return deepcopy(state["workflow_values"][first-1:last])
    service = Service()
    db = DB("synthetic", service=service)
    db._sheet_metadata_cache = state["metadata"]
    blocks = db._category_workflow_blocks()
    assert len(blocks["rule"][1]) == 1050
    assert blocks["rule"][1][-1][2:6] == ["食費", "外食", True, True]
    assert blocks["confirm"][1][0][2] is True
    state["workflow_values"][1040][2] = "食費｜食料品"
    with pytest.raises(ProjectionError, match="input_changed"):
        db.replace_category_rule_ui_rows(blocks["rule"][1], blocks["rule"][0])
    assert service.requests == []
    assert state["workflow_values"][1040][2] == "食費｜食料品"
    # Rendering controls never writes a row-relative formula into the helper.
    from types import SimpleNamespace
    db.projection_store=lambda:SimpleNamespace(read=lambda key:{"months":{"2025-01":{},"2025-12":{}}})
    db._configure_category_workflow(blocks, db._workflow_positions(blocks))
    assert not any(q.get("updateCells", {}).get("range", {}).get("sheetId") == EXPENSE_CATEGORY_HELPER_ID
                   for q in service.requests)
    category_rule = next(q["setDataValidation"] for q in service.requests
                        if q.get("setDataValidation", {}).get("rule", {}).get("condition", {}).get("type") == "ONE_OF_RANGE")
    assert category_rule["range"]["endRowIndex"] == 1052
    assert category_rule["rule"]["condition"]["values"][0]["userEnteredValue"].endswith("$A$3")
