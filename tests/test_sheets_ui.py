"""Synthetic fixtures only. These tests never create or access a Google file."""
from copy import deepcopy
import re

import pytest

from app.amazon_review import AMAZON_REVIEW_HEADERS
from app.expense_view import ExpenseViewPipeline
from app.review_pipeline import ReviewPipeline, is_reviewable_status
from app.sheets import HEADERS, SheetsDB
from app.sheets_ui import (
    CATEGORY_UI_ID, CATEGORY_UI_TITLE,
    AUTO_MONTH, CAP, CHART_ID, DAILY, EXPENSE_CATEGORY_HELPER_ID, EXPENSE_CATEGORY_HELPER_TITLE,
    HIDDEN, HOME_COLUMNS, HOME_ID, IDS, MARKER, RIGHT, SPREADSHEET_ID,
    build_plan, home_cells, initial_month_selection, installed, plan_digest, refresh_layout_requests,
)
from app.sheets_ui_cli import capture_restore, execute_backup_read, execute_plan, private_path, read_metadata
from test_amazon_manual_review import MemoryDB, review_row


def metadata():
    return {"spreadsheetId": SPREADSHEET_ID, "sheets": [
        {"properties": {"sheetId": sid, "title": title, "index": i,
                        "gridProperties": {"rowCount": 100, "columnCount": 26}},
         "header": AMAZON_REVIEW_HEADERS if title == "Amazon要確認" else HEADERS.get(title, ["fixture-header"])}
        for i, (title, sid) in enumerate(IDS.items())]}


class Call:
    def __init__(self, result): self.result = result
    def execute(self, **kwargs): return deepcopy(self.result)


class FixtureService:
    """Small metadata/format transport emulator, not a formula engine."""
    def __init__(self, meta):
        self.meta = deepcopy(meta)
        self.requests = []
        self.cells = {}
        self.dimensions = {}
        self.validations = {}

    def spreadsheets(self): return self
    def values(self): return self
    def batchGet(self, *, ranges, **kwargs):
        values = []
        for rng in ranges:
            title = rng.split("!")[0].strip("'")
            s = next(s for s in self.meta["sheets"] if s["properties"]["title"] == title)
            if title == "ホーム" and rng.endswith("B3:B4"):
                assert kwargs["valueRenderOption"] == "FORMULA"
                values.append({"values": [[next(iter(self.cells.get((HOME_ID, i, 1), {}).get("userEnteredValue", {}).values()), "")] for i in [2, 3]]})
                continue
            if s["properties"]["sheetId"] in {HOME_ID, CATEGORY_UI_ID}:
                width = s["properties"]["gridProperties"]["columnCount"]
                header = [next(iter(self.cells.get((s["properties"]["sheetId"], 0, j), {}).get("userEnteredValue", {}).values()), "") for j in range(width)]
                values.append({"values": [header]})
                continue
            values.append({"values": [s["header"]] if s["header"] else []})
        return Call({"valueRanges": values})

    def get(self, **kwargs):
        if kwargs.get("ranges"):
            title = kwargs["ranges"][0].split("!")[0].strip("'")
            s = next(s for s in self.meta["sheets"] if s["properties"]["title"] == title)
            if s["properties"]["sheetId"] in {HOME_ID, CATEGORY_UI_ID}:
                sid = s["properties"]["sheetId"]
                width = s["properties"]["gridProperties"]["columnCount"]
                rows = []
                for i in range(35):
                    row = []
                    for j in range(width):
                        c = deepcopy(self.cells.get((sid, i, j), {}))
                        if self.validations.get((sid, i, j)):
                            c["dataValidation"] = deepcopy(self.validations[sid, i, j])
                        row.append(c)
                    rows.append({"values": row})
                return Call({"sheets": [dict(s, data=[{"rowData": rows,
                    "columnMetadata": [self.dimensions.get((sid, "COLUMNS", j), {}) for j in range(width)],
                    "rowMetadata": [self.dimensions.get((sid, "ROWS", i), {}) for i in range(70)]}])]})
            return Call({"sheets": [dict(properties=s["properties"], data=[{
                "rowData": [{"values": [{"userEnteredFormat": {"textFormat": {"bold": True}}}]}],
                "columnMetadata": [{"pixelSize": 113}], "rowMetadata": [{"pixelSize": 29}],
            }])]})
        return Call(self.meta)

    def batchUpdate(self, *, body, **kwargs):
        for request in body["requests"]:
            self.requests.append(deepcopy(request))
            kind, v = next(iter(request.items()))
            if kind == "addSheet":
                p = deepcopy(v["properties"])
                p["index"] = len(self.meta["sheets"])
                self.meta["sheets"].append({"properties": p, "header": []})
            elif kind == "createDeveloperMetadata":
                m = deepcopy(v["developerMetadata"])
                next(s for s in self.meta["sheets"] if s["properties"]["sheetId"] == m["location"]["sheetId"]).setdefault("developerMetadata", []).append(m)
            elif kind == "updateDeveloperMetadata":
                for s in self.meta["sheets"]:
                    for m in s.get("developerMetadata", []):
                        if m.get("metadataKey") == v["dataFilters"][0]["developerMetadataLookup"]["metadataKey"]:
                            m.update(v["developerMetadata"])
            elif kind == "updateSheetProperties":
                p = v["properties"]
                s = next(s for s in self.meta["sheets"] if s["properties"]["sheetId"] == p["sheetId"])
                if "index" in p:
                    # We only move to index zero or the end, avoiding API's
                    # before-removal index ambiguity for forward moves.
                    self.meta["sheets"].remove(s)
                    self.meta["sheets"].insert(p["index"], s)
                for k, val in p.items():
                    if k == "gridProperties": s["properties"][k].update(val)
                    else: s["properties"][k] = val
                for i, sheet in enumerate(self.meta["sheets"]): sheet["properties"]["index"] = i
            elif kind == "addChart":
                chart = deepcopy(v["chart"])
                assert not any(c["chartId"] == chart["chartId"] for s in self.meta["sheets"] for c in s.get("charts", []))
                next(s for s in self.meta["sheets"] if s["properties"]["sheetId"] == HOME_ID).setdefault("charts", []).append(chart)
            elif kind == "mergeCells":
                s = next(s for s in self.meta["sheets"] if s["properties"]["sheetId"] == v["range"]["sheetId"])
                assert v["range"] not in s.setdefault("merges", [])
                s["merges"].append(v["range"])
            elif kind == "updateCells":
                r = v["range"]
                for i, row in enumerate(v["rows"], r["startRowIndex"]):
                    for j, c in enumerate(row["values"], r["startColumnIndex"]):
                        target = self.cells.setdefault((r["sheetId"], i, j), {})
                        for field in v["fields"].split(","):
                            if field in c: target[field] = deepcopy(c[field])
                            else: target.pop(field, None)
            elif kind in {"repeatCell", "setDataValidation"}:
                r = v["range"]
                target = self.validations if kind == "setDataValidation" else self.cells
                n = next(s["properties"]["gridProperties"]["rowCount"] for s in self.meta["sheets"] if s["properties"]["sheetId"] == r["sheetId"])
                # Only the leading synthetic rows need materialized formatting.
                for i in range(r.get("startRowIndex", 0), min(r.get("endRowIndex", n), 15)):
                    for j in range(r.get("startColumnIndex", 0), r.get("endColumnIndex", 26)):
                        key = r["sheetId"], i, j
                        if kind == "setDataValidation": target[key] = deepcopy(v.get("rule", {}))
                        else:
                            fmt = target.setdefault(key, {}).setdefault("userEnteredFormat", {})
                            fmt.update(v["cell"]["userEnteredFormat"])
            elif kind == "updateDimensionProperties":
                r = v["range"]
                for i in range(r["startIndex"], r["endIndex"]):
                    self.dimensions.setdefault((r["sheetId"], r["dimension"], i), {}).update(v["properties"])
        return Call({})


def test_ui_mutation_scope_and_legacy_headers_are_preserved():
    meta = metadata()
    before = deepcopy(meta)
    plan = build_plan(meta)
    assert meta == before
    assert len([r for r in plan["requests"] if "addSheet" in r]) == 3
    for r in plan["requests"]:
        assert len(r) == 1
        kind, v = next(iter(r.items()))
        assert kind not in {"deleteSheet", "deleteDimension", "moveDimension", "sortRange", "addProtectedRange", "setBasicFilter"}
        if kind in {"updateCells", "setDataValidation", "mergeCells"}:
            assert v["range"]["sheetId"] in {HOME_ID, CATEGORY_UI_ID, EXPENSE_CATEGORY_HELPER_ID, IDS["支出明細"]}
        if kind == "repeatCell":
            assert "userEnteredValue" not in v["fields"] and "dataValidation" not in v["fields"]
        if kind == "updateCells":
            rect = v["range"]
            assert len(v["rows"]) == rect["endRowIndex"]-rect["startRowIndex"]
            assert all(len(row["values"]) == rect["endColumnIndex"]-rect["startColumnIndex"] for row in v["rows"])
        if "fields" in v: assert "*" not in v["fields"]
    assert plan["new_conditional_formats"] == 0
    assert "レシート" in plan["visible_order"] and "取込データ" in plan["visible_order"]
    assert str(IDS["レシート"]) in home_cells()[23, 1]
    assert str(IDS["取込データ"]) in home_cells()[23, 2]


@pytest.mark.parametrize("selected", [AUTO_MONTH, "2026-07"])
def test_replay_keeps_one_home_chart_merges_and_month(selected):
    svc = FixtureService(metadata())
    svc.batchUpdate(body=build_plan(svc.meta))
    assert installed(svc.meta)
    svc.cells[HOME_ID, 3, 1]["userEnteredValue"] = {"stringValue": selected}
    names = [s["properties"]["title"] for s in svc.meta["sheets"]]
    assert names == ["ホーム"] + DAILY + RIGHT + HIDDEN
    before_merges = deepcopy(next(s for s in svc.meta["sheets"] if s["properties"]["sheetId"] == HOME_ID)["merges"])
    replay = build_plan(read_metadata(svc))
    assert not any("addSheet" in r or "addChart" in r or "mergeCells" in r or "createDeveloperMetadata" in r for r in replay["requests"])
    svc.batchUpdate(body=replay)
    assert svc.cells[HOME_ID, 3, 1]["userEnteredValue"] == {"stringValue": selected}
    assert svc.cells[HOME_ID, 2, 1]["userEnteredValue"] == {"formulaValue": home_cells()[3, 2]}
    assert not any(r.get("updateCells", {}).get("range") == {
        "sheetId": HOME_ID, "startRowIndex": 3, "endRowIndex": 4, "startColumnIndex": 1, "endColumnIndex": 2}
        and "userEnteredValue" in r["updateCells"]["fields"] for r in replay["requests"])
    home = next(s for s in svc.meta["sheets"] if s["properties"]["sheetId"] == HOME_ID)
    assert home["merges"] == before_merges
    assert len(home["charts"]) == 1
    assert sum(s["properties"].get("hidden", False) for s in svc.meta["sheets"]) == len(HIDDEN)


def test_dry_run_has_no_service_calls_and_apply_requires_review():
    class NoCalls:
        def spreadsheets(self): raise AssertionError("network access")
    plan = build_plan(metadata())
    assert execute_plan(NoCalls(), plan)["mode"] == "dry-run"
    with pytest.raises(ValueError, match="reviewed plan"):
        execute_plan(NoCalls(), plan, apply=True)


@pytest.mark.parametrize("title", ["支出一覧", "取込データ", "Amazon要確認"])
def test_source_schema_mismatch_fails_without_schema_repair(title):
    meta = metadata()
    next(s for s in meta["sheets"] if s["properties"]["title"] == title)["header"] = ["unexpected"]
    with pytest.raises(ValueError, match="source contract"):
        build_plan(meta)


def test_unknown_sheet_untouched_and_home_collision_rejected():
    meta = metadata()
    unknown = {"properties": {"sheetId": 12345, "title": "別Work", "index": 20,
                              "gridProperties": {"rowCount": 100, "columnCount": 26}}, "header": []}
    meta["sheets"].append(unknown)
    assert "12345" not in str(build_plan(meta)["requests"])
    unknown["properties"]["title"] = "ホーム"
    with pytest.raises(ValueError, match="not owned"):
        build_plan(meta)


def test_review_regex_matches_existing_status_contract_without_double_counts():
    regex = re.search(r'"(\^.*\$)"', home_cells()[12, 2]).group(1)
    statuses = ["", "要確認", "needs_review", "needs_review_refund", "amazon_needs_review",
                "needs_review_needs_review", "amazon_unmatched", "unclassified_aupay", "manual_expense",
                "bank_expense", "bank_income", "auto_expense", "matched_amazon"]
    for state in statuses:
        assert bool(re.fullmatch(regex, state)) == is_reviewable_status(state)
    assert "<>反映済み" in home_cells()[13, 2]


def test_formulas_cover_types_zero_results_and_bounded_growth():
    formulas = home_cells()
    normalization = formulas[2, 4]
    assert all(token in normalization for token in ["ISNUMBER(dates)", "DATEVALUE", "VALUE", "IFERROR", 'ids=""', 'raw_amount=""'])
    assert "Amazon注文" not in normalization and "取込データ" not in normalization
    assert "INDIRECT" in normalization and str(CAP+1) in normalization
    assert 'MAX(0,COUNTIF' in formulas[17, 2]  # Overflow must be visible.
    assert '該当なし' in formulas[34, 1]
    assert "NOW()" not in str(formulas)
    assert all(not isinstance(formulas[pos], (float, int)) for pos in [(6, 1), (8, 2), (9, 2), (12, 2), (13, 2)])


class StyledDB(MemoryDB):
    def __init__(self):
        super().__init__()
        self.svc = FixtureService(metadata())
        self.sid = SPREADSHEET_ID
        self.svc.batchUpdate(body=build_plan(self.svc.meta))
    configure_review_validation = SheetsDB.configure_review_validation
    format_date_column = SheetsDB.format_date_column

    def set_raw_range(self, rng, rows):
        title, region = rng.split('!')
        assert title.strip("'") == '支出一覧' and region.startswith('A2:J')
        self.sheets['支出一覧'] = deepcopy(rows)

    def get(self, rng):
        if rng == "支出明細!A2:M": return deepcopy(self.sheets["支出明細"])
        return super().get(rng)

    def clear(self, rng):
        if rng == "支出一覧!A2:J": self.sheets["支出一覧"] = []
        else: super().clear(rng)

    def append(self, sheet, rows):
        super().append(sheet, rows)
        # Existing INSERT_ROWS writer can increase the grid; formatting hooks
        # must use the fresh metadata returned after the append.
        p = next(s["properties"] for s in self.svc.meta["sheets"] if s["properties"]["title"] == sheet)
        p["gridProperties"]["rowCount"] += len(rows)


def test_real_review_refresh_retains_manual_inputs_and_validation_with_ui():
    db = StyledDB()
    ReviewPipeline(db).refresh()
    row = review_row(db, "card:1")
    label = db.sheets["Amazon照合候補"][0][17]
    row[9:14] = ["Amazon注文と照合", "receipt:1", "日用品｜雑貨", "従来入力", "手入力メモ"]
    row[17] = label
    ReviewPipeline(db).refresh()
    after = review_row(db, "card:1")
    assert after[9:14] == ["Amazon注文と照合", "receipt:1", "日用品｜雑貨", "従来入力", "手入力メモ"]
    assert after[17] == label and after[19] == "選択済み"
    sid = IDS["要確認"]
    assert len(db.svc.validations[sid, 1, 9]["condition"]["values"]) == 5
    assert db.svc.validations[sid, 1, 12] == {}  # Legacy free entry still allowed.
    for col in [9, 10, 11, 12, 13, 17]:
        assert db.svc.cells[sid, 1, col]["userEnteredFormat"]["backgroundColorStyle"] != {"rgbColor": {"red": 1, "green": 1, "blue": 1}}
        assert not db.svc.dimensions.get((sid, "COLUMNS", col), {}).get("hiddenByUser")


def test_real_expense_refresh_retains_ui_and_business_contract():
    db = StyledDB()
    db.sheets["支出明細"] = [
        ["e1", "2026-09-01", "店舗", "商品", 1200, "食費", "食料品", "カード", "fixture", "", "i1", "", "active"],
        ["e2", "2026-09-02", "店舗", "商品", 999, "食費", "食料品", "カード", "fixture", "", "i2", "", "duplicate_excluded"],
    ]
    before = deepcopy(db.sheets["支出明細"])
    result = ExpenseViewPipeline(db).refresh()
    assert result["active_expenses"] == 1 and result["total"] == 1200
    assert db.sheets["支出明細"] == before
    assert len(db.sheets["支出一覧"][0]) == 10 and db.sheets["支出一覧"][0][-1] == "e1"
    sid = IDS["支出一覧"]
    assert db.svc.dimensions[sid, "COLUMNS", 9]["hiddenByUser"]
    assert db.svc.cells[sid, 1, 3]["userEnteredFormat"]["numberFormat"]["type"] == "NUMBER"
    assert db.svc.dimensions[sid, "ROWS", 99]["pixelSize"] == 56
    assert next(s['properties']['gridProperties']['rowCount'] for s in db.svc.meta['sheets'] if s['properties']['sheetId']==sid)==100
    assert refresh_layout_requests(metadata(), "支出一覧") == []


def test_expense_refresh_grows_only_when_needed_and_never_inserts_blank_rows():
    db=StyledDB()
    db.sheets['支出明細']=[[str(i),'2026-09-01','店','品',100,'食費','食料品','','fixture','','','','active'] for i in range(105)]
    ExpenseViewPipeline(db).refresh()
    sheet=next(s['properties'] for s in db.svc.meta['sheets'] if s['properties']['title']=='支出一覧')
    assert sheet['gridProperties']['rowCount']==106 and len(db.sheets['支出一覧'])==105
    before=deepcopy(db.sheets['支出一覧'])
    ExpenseViewPipeline(db).refresh()
    assert sheet['gridProperties']['rowCount']==106 and db.sheets['支出一覧']==before


def test_restore_captures_ui_fields_only_and_disables_hooks_without_deletion():
    svc = FixtureService(metadata())
    plan = build_plan(svc.meta)
    backup = capture_restore(svc, svc.meta, plan)
    assert not any("deleteSheet" in r or "deleteDimension" in r or "deleteDeveloperMetadata" in r for r in backup["requests"])
    for r in backup["requests"]:
        if "repeatCell" in r: assert "userEnteredValue" not in r["repeatCell"]["fields"]
    expense_validation_restore = next(r["updateCells"] for r in backup["requests"]
                                      if "updateCells" in r
                                      and r["updateCells"]["range"]["sheetId"] == IDS["支出明細"])
    assert expense_validation_restore["range"]["endRowIndex"] == CAP + 1
    assert expense_validation_restore["fields"] == "dataValidation"
    svc.batchUpdate(body=plan)
    svc.batchUpdate(body=backup)
    assert not installed(svc.meta)
    assert not any(s["properties"].get("hidden", False) for s in svc.meta["sheets"] if s["properties"]["sheetId"] not in {HOME_ID, CATEGORY_UI_ID, EXPENSE_CATEGORY_HELPER_ID})
    assert [s["properties"]["title"] for s in svc.meta["sheets"] if s["properties"]["sheetId"] not in {HOME_ID, CATEGORY_UI_ID, EXPENSE_CATEGORY_HELPER_ID}] == list(IDS)
    assert next(s for s in svc.meta["sheets"] if s["properties"]["sheetId"] == EXPENSE_CATEGORY_HELPER_ID)["properties"]["hidden"]
    assert not any("addSheet" in r for r in build_plan(read_metadata(svc))["requests"])


def test_backup_reads_disable_compression_only_when_supported():
    class Request:
        headers = {}
        def execute(self): return {"ok": True}
    request = Request()
    assert execute_backup_read(request) == {"ok": True}
    assert request.headers["accept-encoding"] == "identity"
    assert execute_backup_read(Call({"fixture": True})) == {"fixture": True}


def test_approval_digest_changes_when_another_work_changes_metadata():
    meta = metadata()
    first = plan_digest(build_plan(meta))
    meta["sheets"][0]["properties"]["hidden"] = True
    assert plan_digest(build_plan(meta)) != first
    with pytest.raises(ValueError, match="ignored .private"):
        private_path("leaky-backup.json")


def test_independent_monthly_arithmetic_handles_types_empty_dates_and_zero():
    from datetime import date
    from decimal import Decimal
    from app.sheets_ui_verify import source_summary
    serial = (date(2026, 9, 2)-date(1899, 12, 30)).days
    def row(day, amount, major="食費", minor="食料品", identity="id"):
        return [day, "店舗", "商品", amount, major, minor, "", "", "", identity]
    rows = [row("2026-09-01", 1200), row(serial, "￥2,000"), row("2026/9/3", "-300円", minor=""),
            row("2026-08-31", 900), row("", "100"), row("invalid", "bad"), row("2026-09-01", 10, identity=""),
            row("2026-09-04", 0), []]
    regular = [["id", "", "", "", "", "", "", "", "needs_review_needs_review"]]
    amazon = [["a", "未確認"], ["b", "選択済み"], ["c", "反映済み"], ["d", "保留"], ["e", "エラー"], ["f", ""], []]
    result = source_summary(rows, regular, amazon, "2026-09")
    assert result["total"] == Decimal(2900)
    assert result["unclassified_count"] == 1 and result["unclassified_amount"] == -300
    assert result["invalid"] == 2 and result["regular"] == 1 and result["amazon"] == 5
    empty = source_summary([], [], [], "2026-09")
    assert empty["total"] == 0 and empty["regular"] == empty["amazon"] == 0


def test_requests_match_bundled_official_sheets_api_schema():
    import json
    from googleapiclient.discovery_cache import get_static_doc
    schemas = json.loads(get_static_doc("sheets", "v4"))["schemas"]
    def check(value, schema):
        if "$ref" in schema: schema = schemas[schema["$ref"]]
        if isinstance(value, dict):
            props = schema.get("properties", {})
            assert set(value) <= set(props), set(value)-set(props)
            for key, child in value.items(): check(child, props[key])
        elif isinstance(value, list):
            for child in value: check(child, schema["items"])
        elif schema.get("enum"):
            assert value in schema["enum"]
    def check_mask(mask, schema_name):
        for path in mask.split(","):
            schema = schemas[schema_name]
            for part in path.split("."):
                if "$ref" in schema: schema = schemas[schema["$ref"]]
                assert part in schema["properties"], (schema_name, path)
                schema = schema["properties"][part]
    masks = {"repeatCell": "CellData", "updateCells": "CellData", "updateSheetProperties": "SheetProperties",
             "updateDimensionProperties": "DimensionProperties", "updateEmbeddedObjectPosition": "OverlayPosition"}
    svc = FixtureService(metadata())
    initial = build_plan(svc.meta)
    svc.batchUpdate(body=initial)
    for plan in [initial, build_plan(read_metadata(svc))]:
        for request in plan["requests"]:
            check(request, schemas["Request"])
            kind, body = next(iter(request.items()))
            if kind in masks: check_mask(body["fields"], masks[kind])


def test_apply_captures_backup_before_one_write_and_reads_back_headers(monkeypatch):
    import app.sheets_ui_cli as cli
    import app.sheets_ui_verify as verify
    svc = FixtureService(metadata())
    plan = build_plan(svc.meta)
    saved = []
    def backup(path, value):
        assert not svc.requests
        saved.append(value)
    monkeypatch.setattr(cli, "write_private", backup)
    monkeypatch.setattr(verify, "verify_home", lambda service, meta: {"fixture_readback": True})
    result = cli.execute_plan(svc, plan, apply=True, approved_digest=plan_digest(plan), backup=".private/test.json")
    assert len(saved) == 1 and saved[0]["applied_plan_sha256"] == plan_digest(plan)
    assert result["headers_unchanged"] and result["verification"]["fixture_readback"]
    assert len(svc.requests) == len(plan["requests"])


def test_apply_stops_before_write_if_another_work_changes_target(monkeypatch):
    import app.sheets_ui_cli as cli
    svc = FixtureService(metadata())
    plan = build_plan(svc.meta)
    svc.meta["sheets"][0]["properties"]["gridProperties"]["rowCount"] += 1
    with pytest.raises(ValueError, match="state changed"):
        cli.execute_plan(svc, plan, apply=True, approved_digest=plan_digest(plan), backup=".private/test.json")
    assert not svc.requests


def test_month_dropdown_initialization_and_legacy_date_migration():
    from datetime import date
    svc = FixtureService(metadata())
    svc.batchUpdate(body=build_plan(svc.meta))
    home = next(s for s in svc.meta["sheets"] if s["properties"]["sheetId"] == HOME_ID)
    assert home["properties"]["gridProperties"]["columnCount"] == HOME_COLUMNS
    assert home["properties"]["gridProperties"]["frozenRowCount"] == 4
    assert svc.cells[HOME_ID, 3, 1]["userEnteredValue"] == {"stringValue": AUTO_MONTH}
    assert svc.validations[HOME_ID, 2, 1] == {}
    assert svc.validations[HOME_ID, 3, 1]["condition"] == {
        "type": "ONE_OF_RANGE", "values": [{"userEnteredValue": "='ホーム'!$I$2:$I$5001"}]}
    assert svc.dimensions[HOME_ID, "COLUMNS", 8]["hiddenByUser"]
    # A legacy eight-column Home can gain the UI helper without deleting columns.
    home["properties"]["gridProperties"]["columnCount"] = 8
    svc.cells[HOME_ID, 3, 1].pop("userEnteredValue")
    svc.cells[HOME_ID, 2, 1]["userEnteredValue"] = {"numberValue": (date(2025, 7, 1)-date(1899, 12, 30)).days}
    plan = build_plan(read_metadata(svc))
    svc.batchUpdate(body=plan)
    assert svc.cells[HOME_ID, 3, 1]["userEnteredValue"] == {"stringValue": "2025-07"}
    assert home["properties"]["gridProperties"]["columnCount"] == 9
    assert not any("deleteDimension" in r for r in plan["requests"])


def test_month_selection_requires_a_fresh_read_and_guards_concurrent_change(monkeypatch):
    import app.sheets_ui_cli as cli
    svc = FixtureService(metadata())
    svc.batchUpdate(body=build_plan(svc.meta))
    home = next(s for s in svc.meta["sheets"] if s["properties"]["sheetId"] == HOME_ID)
    with pytest.raises(ValueError, match="not read"):
        initial_month_selection(home)
    plan = build_plan(read_metadata(svc))
    svc.cells[HOME_ID, 3, 1]["userEnteredValue"] = {"stringValue": "2026-07"}
    svc.requests.clear()
    with pytest.raises(ValueError, match="state changed"):
        cli.execute_plan(svc, plan, apply=True, approved_digest=plan_digest(plan), backup=".private/test.json")
    assert not svc.requests
    assert plan_digest(build_plan(read_metadata(svc))) != plan_digest(plan)
    home["monthState"] = {"B4": "2026-13"}
    with pytest.raises(ValueError, match="Unexpected Home"):
        initial_month_selection(home)


def test_monthly_unclassified_and_all_period_reviews_remain_separate():
    from app.sheets_ui_verify import source_summary
    rows = [["2026-07-01", "fixture", "fixture", 100, "未分類", "", "", "", "", str(i)] for i in range(53)]
    rows += [["2026-09-01", "fixture", "fixture", 200, "食費", "食料品", "", "", "", "current"]]
    summaries = [source_summary(rows, [], [], m) for m in ["2026-09", "2026-07", "2026-09", "2020-01"]]
    assert [s["unclassified_count"] for s in summaries] == [0, 53, 0, 0]
    assert [s["total"] for s in summaries] == [200, 5300, 200, 0]
    assert all(s["regular"] == s["amazon"] == 0 for s in summaries)
    regular = [["review-id", "", "", "", "", "", "", "", "needs_review"]]
    amazon = [["pending", "未確認"], ["held", "保留"], ["done", "反映済み"]]
    for month in ["2026-09", "2026-07", "2020-01"]:
        with_reviews = source_summary(rows, regular, amazon, month)
        assert with_reviews["regular"] == 1 and with_reviews["amazon"] == 2
    formulas = home_cells()
    assert formulas[7, 1] == "要対応" and "カテゴリ未分類" in formulas[8, 1]
    assert "計上済み" in formulas[10, 1] and "全期間" in formulas[11, 1]
    assert all("$B$3" in formulas[pos] for pos in [(5, 1), (6, 1), (8, 2), (9, 2), (34, 1)])
    assert all("$B$3" not in formulas[pos] and "$B$4" not in formulas[pos] for pos in [(12, 2), (13, 2)])
    assert "TODAY()" in formulas[3, 2] and AUTO_MONTH in formulas[3, 2]
    assert "$B$4" in formulas[3, 9]  # Keep a chosen month even outside the rolling 36-month list.


def test_restore_keeps_dropdown_helper_notes_and_frozen_rows():
    svc = FixtureService(metadata())
    svc.batchUpdate(body=build_plan(svc.meta))
    meta = read_metadata(svc)
    backup = capture_restore(svc, meta, build_plan(meta))
    block = next(r["updateCells"] for r in backup["requests"]
                 if "updateCells" in r and r["updateCells"]["range"]["sheetId"] == HOME_ID)
    assert block["range"]["endColumnIndex"] == 9 and "note" in block["fields"]
    assert block["rows"][2]["values"][8]["userEnteredValue"] == {"formulaValue": home_cells()[3, 9]}
    assert block["rows"][3]["values"][1]["dataValidation"] == svc.validations[HOME_ID, 3, 1]
    assert block["rows"][3]["values"][1]["note"]
    assert any(r.get("updateSheetProperties", {}).get("properties", {}).get("gridProperties", {}).get("frozenRowCount") == 4 for r in backup["requests"])
