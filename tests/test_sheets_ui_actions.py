"""Category navigation fixtures: never apply classifications or touch Google."""
from copy import deepcopy
import pytest

from app.sheets import (
    EXPENSE_CATEGORY_HELPER_LEDGER_MINOR_END_A1,
    EXPENSE_CATEGORY_HELPER_LEDGER_MINOR_START_A1,
    SheetsDB,
)
from app.sheets_ui import (
    CAP, CATEGORY_UI_ID, CATEGORY_UI_TITLE, EXPENSE_CATEGORY_HELPER_ID,
    EXPENSE_CATEGORY_HELPER_MARKER, HOME_ID, IDS, VERSION, build_plan, home_cells,
)
from app.sheets_ui_actions import (
    category_ui_cells,
    expense_category_validation_requests,
    expense_minor_category_validation_requests,
    ledger_input_requests,
)
from app.sheets_ui_cli import capture_restore, read_metadata
from app.sheets_ui_verify import category_queue_expected, verify_category_ui
from test_sheets_ui import Call, FixtureService, metadata


def example():
    view = [["2026-07-01", "同じ店", "品名", 100, "その他", "未分類", "", "", "", "expense:a"]]
    ledger = [["expense:a", "2026-07-01", "同じ店", "品名", 100, "その他", "未分類", "", "", "", "", "", "active"]]
    return view, ledger


@pytest.mark.parametrize("variant,state", [("active", "修正可"), ("missing", "元データ確認"),
    ("duplicate", "元データ確認"), ("inactive", "一覧更新待ち"),
    ("different_month", "一覧更新待ち"), ("classified", "分類済・更新待ち")])
def test_edit_link_only_targets_one_active_unclassified_record_in_selected_month(variant, state):
    view, ledger = example()
    if variant == "missing": ledger = []
    if variant == "duplicate": ledger += deepcopy(ledger)
    if variant == "inactive": ledger[0][12] = "duplicate_excluded"
    if variant == "different_month": ledger[0][1] = "2026-09-01"
    if variant == "classified": ledger[0][5:7] = ["食費", "食料品"]
    result = category_queue_expected(view, ledger, [], [], "2026-07")
    assert len(result) == 1 and result[0]["state"] == state
    assert result[0]["row"] == (0 if variant in {"missing", "duplicate"} else 2)
    assert category_queue_expected(view, ledger, [], [], "2026-09") == []


def test_suggestions_require_one_catalog_valid_pair_and_never_modify_inputs():
    view, ledger = example()
    categories = [["食費", "食料品"], ["日用品", "消耗品"]]
    products = [["p", "品名", "食費", "食料品"]]
    before = deepcopy((view, ledger, products, categories))
    result = category_queue_expected(view, ledger, products, categories, "2026-07")[0]
    assert result["suggestion"] == "食費｜食料品\n（商品マスタ）"
    assert (view, ledger, products, categories) == before
    products += [["q", "品名", "日用品", "消耗品"]]
    assert category_queue_expected(view, ledger, products, categories, "2026-07")[0]["suggestion"] == "候補なし"
    assert category_queue_expected(view, ledger, [["p", "品名", "未知", "カテゴリ"]], categories, "2026-07")[0]["suggestion"] == "候補なし"
    view += [["2026-06-01", "同じ店", "別の商品", 50, "食費", "食料品", "", "", "", "history"]]
    assert category_queue_expected(view, ledger, [], categories, "2026-07")[0]["suggestion"] == "食費｜食料品\n（同じ店舗）"
    view += [["2026-05-01", "同じ店", "さらに別の商品", 50, "日用品", "消耗品", "", "", "", "history2"]]
    assert category_queue_expected(view, ledger, [], categories, "2026-07")[0]["suggestion"] == "候補なし"


def test_home_actions_keep_numeric_counts_and_existing_review_destinations():
    plan = build_plan(metadata())
    targets = [(7, CATEGORY_UI_ID, "A1"), (11, IDS["要確認"], "K1"), (12, IDS["Amazon要確認"], "H1")]
    for row, sid, address in targets:
        links = [r["repeatCell"] for r in plan["requests"] if "repeatCell" in r
            and r["repeatCell"]["range"]["sheetId"] == HOME_ID
            and r["repeatCell"]["range"]["startRowIndex"] == row
            and r["repeatCell"]["cell"]["userEnteredFormat"].get("textFormat", {}).get("underline")]
        assert len(links) == 1
        fmt = links[0]["cell"]["userEnteredFormat"]
        assert home_cells()[row+1, 2].startswith(f'=HYPERLINK("#gid={sid}&range={address}",')
        assert fmt["numberFormat"]["pattern"].startswith('0"件') and "対応する" in fmt["numberFormat"]["pattern"]
    formulas = category_ui_cells()
    assert "この月の未分類はありません" in formulas[4, 1]
    assert "'ホーム'!$B$3" in formulas[2, 4] and "'ホーム'!F2:F5001" in formulas[2, 4]
    assert "COUNTIF" in formulas[2, 14] and '"*","~*"' in formulas[2, 14]
    assert 'status="修正可"' in formulas[6, 3] and 'gid=0&range=F' in formulas[6, 3]
    ledger = next(s for s in metadata()["sheets"] if s["properties"]["title"] == "支出明細")
    assert all(not any(k in r for k in ["updateCells", "setDataValidation", "sortRange"]) for r in ledger_input_requests(ledger))


def test_expense_category_rules_are_master_derived_and_row_relative():
    requests = expense_category_validation_requests()
    helper_write = next(r["updateCells"] for r in requests if "updateCells" in r)
    assert helper_write["range"]["endRowIndex"] == CAP + 1
    assert "SORT(UNIQUE(FILTER(カテゴリ!$A$2:$A" in helper_write["rows"][1]["values"][0]["userEnteredValue"]["formulaValue"]
    assert "FILTER(カテゴリ!$B$2:$B" in helper_write["rows"][1]["values"][1]["userEnteredValue"]["formulaValue"]
    assert "INDEX('支出明細'!F:F,ROW())" in helper_write["rows"][1]["values"][1]["userEnteredValue"]["formulaValue"]
    assert 'IF(major="","",' in helper_write["rows"][2]["values"][1]["userEnteredValue"]["formulaValue"]
    rules = [r["setDataValidation"] for r in requests if "setDataValidation" in r]
    major, *minors = rules
    assert len(minors) == CAP
    minor = minors[0]
    assert major["range"] == {"sheetId": IDS["支出明細"], "startRowIndex": 1,
                               "endRowIndex": CAP+1, "startColumnIndex": 5, "endColumnIndex": 6}
    assert minor["range"] == {"sheetId": IDS["支出明細"], "startRowIndex": 1,
                               "endRowIndex": 2, "startColumnIndex": 6, "endColumnIndex": 7}
    assert minors[-1]["range"] == {"sheetId": IDS["支出明細"], "startRowIndex": CAP,
                                    "endRowIndex": CAP+1, "startColumnIndex": 6, "endColumnIndex": 7}
    assert major["rule"]["condition"]["type"] == minor["rule"]["condition"]["type"] == "ONE_OF_RANGE"
    assert major["rule"]["condition"]["values"][0]["userEnteredValue"].endswith("!$A$2:$A")
    # Each rule explicitly names its own helper row.  Sheets does not reliably
    # adjust a range-backed validation source when the rule itself is filled.
    assert minor["rule"]["condition"]["values"][0]["userEnteredValue"].endswith(
        f"!{EXPENSE_CATEGORY_HELPER_LEDGER_MINOR_START_A1}2:{EXPENSE_CATEGORY_HELPER_LEDGER_MINOR_END_A1}2"
    )
    assert minors[1]["rule"]["condition"]["values"][0]["userEnteredValue"].endswith(
        f"!{EXPENSE_CATEGORY_HELPER_LEDGER_MINOR_START_A1}3:{EXPENSE_CATEGORY_HELPER_LEDGER_MINOR_END_A1}3"
    )
    assert all(rule["rule"]["strict"] for rule in [major, *minors])


def test_new_ledger_rows_receive_only_their_own_minor_rule():
    requests = expense_minor_category_validation_requests(1430, 1432)
    assert [request["setDataValidation"]["range"] for request in requests] == [
        {"sheetId": IDS["支出明細"], "startRowIndex": row-1, "endRowIndex": row,
         "startColumnIndex": 6, "endColumnIndex": 7}
        for row in range(1430, 1433)
    ]
    assert requests[2]["setDataValidation"]["rule"]["condition"]["values"][0]["userEnteredValue"].endswith(
        f"!{EXPENSE_CATEGORY_HELPER_LEDGER_MINOR_START_A1}1432:{EXPENSE_CATEGORY_HELPER_LEDGER_MINOR_END_A1}1432"
    )


def test_expense_append_reinstalls_only_new_row_rules_after_ui_installation():
    class AppendService:
        def __init__(self): self.requests = []
        def spreadsheets(self): return self
        def values(self): return self
        def append(self, **kwargs):
            updated_range = "支出明細!A1444:M1445" if kwargs["range"] == "支出明細!A:A" else "支出一覧!A2:J2"
            return Call({"updates": {"updatedRange": updated_range}})
        def get(self, **kwargs):
            return Call({"sheets": [{"properties": {"sheetId": EXPENSE_CATEGORY_HELPER_ID},
                "developerMetadata": [{"metadataKey": EXPENSE_CATEGORY_HELPER_MARKER,
                                        "metadataValue": VERSION}]}]})
        def batchUpdate(self, **kwargs):
            self.requests.extend(kwargs["body"]["requests"])
            return Call({})
    db = SheetsDB("synthetic", service=AppendService())
    db.append("支出明細", [["new-a"], ["new-b"]])
    assert [request["setDataValidation"]["range"]["startRowIndex"] for request in db.svc.requests] == [1443, 1444]
    db.append("支出一覧", [["view-only"]])
    assert len(db.svc.requests) == 2


def test_category_ui_replay_restore_and_collision_preserve_business_surface():
    svc = FixtureService(metadata())
    svc.batchUpdate(body=build_plan(svc.meta))
    fresh = read_metadata(svc)
    plan = build_plan(fresh)
    assert not any("addSheet" in r or "mergeCells" in r for r in plan["requests"])
    backup = capture_restore(svc, fresh, plan)
    writes = [r["updateCells"] for r in backup["requests"] if "updateCells" in r]
    assert all(r["range"]["sheetId"] in {HOME_ID, CATEGORY_UI_ID, IDS["支出明細"]} for r in writes)
    assert any(r["range"]["sheetId"] == CATEGORY_UI_ID and r["range"]["endColumnIndex"] == 17 for r in writes)
    category = next(s for s in fresh["sheets"] if s["properties"]["title"] == CATEGORY_UI_TITLE)
    category["developerMetadata"][0]["metadataValue"] = "restored:1"
    svc.meta = fresh
    svc.batchUpdate(body=build_plan(fresh))
    assert category["developerMetadata"][0]["metadataValue"] == "1"
    category["developerMetadata"] = []
    with pytest.raises(ValueError, match="not owned"):
        build_plan(fresh)


def test_native_verifier_rejects_wrong_or_stale_edit_links_and_handles_empty_queue():
    view, ledger = example()
    class Readback:
        def __init__(self):
            self.queue = [["expense:a", 2, "その他｜未分類", "修正可", "候補なし"]]
            self.links = [{"hyperlink": "#gid=0&range=F2:G2"}]
        def spreadsheets(self): return self
        def values(self): return self
        def get(self, **kwargs):
            if "ranges" in kwargs:
                return Call({"sheets": [{"data": [{"startRow": 5, "startColumn": 2,
                    "rowData": [{"values": [c]} for c in self.links]}]}]})
            title = kwargs["range"].split("!")[0].strip("'")
            return Call({"values": {"ホーム": [["2026-07-01"]], "支出明細": ledger,
                "商品マスタ": [], "カテゴリ": [], "カテゴリ対応": self.queue}[title]})
    svc = Readback()
    check = lambda: verify_category_ui(svc, metadata(), view)
    assert all(v == 0 if k.endswith("errors") else v for k, v in check().items())
    svc.links[0]["hyperlink"] = "#gid=0&range=F3:G3"
    assert check()["category_queue_matches"] and not check()["category_edit_links_match"]
    ledger[0][5:7] = ["食費", "食料品"]
    svc.queue[0][2:4] = ["食費｜食料品", "分類済・更新待ち"]
    svc.links[0]["hyperlink"] = "#gid=0&range=F2:G2"
    assert not check()["category_edit_links_match"]
    svc.links = []
    assert check()["category_edit_links_match"]
    view.clear()
    svc.queue = []
    assert check()["category_queue_matches"] and check()["category_edit_links_match"]
