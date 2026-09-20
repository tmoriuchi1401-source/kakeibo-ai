from copy import deepcopy
from hashlib import sha256

import pytest

from app.ledger_operations_home import build_plan
from app.daily_edit_cutover import MARKER
from app.sheets_ui import HOME_ID, CHART_ID, SPREADSHEET_ID, MARKER as HOME_MARKER
from app.compact_categories import MARKER as COMPACT_MARKER, VERSION as COMPACT_VERSION


def metadata():
    titles=["ホーム","カテゴリ操作","要確認","領収書確認","支出一覧","カテゴリ自動分類ルール","レシート","取込データ"]
    sheets=[{"properties":{"title":t,"sheetId":HOME_ID if i==0 else i,"index":i},
             "developerMetadata":[{"metadataKey":HOME_MARKER,"metadataValue":"1"}]} for i,t in enumerate(titles)]
    sheets[0]["charts"]=[{"chartId":CHART_ID}]
    # Use the exact compact helper contract used by the canonical cutover.
    from app.sheets_ui import EXPENSE_CATEGORY_HELPER_ID, EXPENSE_CATEGORY_HELPER_TITLE
    sheets.append({"properties":{"sheetId":EXPENSE_CATEGORY_HELPER_ID,"title":EXPENSE_CATEGORY_HELPER_TITLE,
                                  "gridProperties":{"rowCount":57,"columnCount":4}},
                   "developerMetadata":[{"metadataKey":COMPACT_MARKER,"metadataValue":COMPACT_VERSION}]})
    return {"spreadsheetId":SPREADSHEET_ID,"sheets":sheets,
            "developerMetadata":[{"metadataKey":MARKER,"metadataValue":sha256(b"daily").hexdigest()}]}


def test_operations_menu_preserves_inputs_dependencies_and_other_tabs():
    meta=metadata();before=deepcopy(meta)
    plan=build_plan(meta,"daily")
    assert meta==before
    for request in plan["requests"]:
        assert len(request)==1
        if "updateCells" in request:
            target=request["updateCells"]["range"]
            assert target["sheetId"]==HOME_ID and target["endColumnIndex"]<=2 and target["endRowIndex"]<=34
            assert not (target["startRowIndex"]<4 and target["endRowIndex"]>2 and target["endColumnIndex"]>1)
        if "repeatCell" in request:
            target=request["repeatCell"]["range"]
            assert target["sheetId"]==HOME_ID
            assert not (target["startRowIndex"]<4 and target["endRowIndex"]>2)
    assert [r for r in plan["requests"] if "deleteEmbeddedObject" in r]==[{"deleteEmbeddedObject":{"objectId":CHART_ID}}]
    formulas=[c.get("userEnteredValue",{}).get("formulaValue","") for r in plan["requests"] if "updateCells" in r
              for row in r["updateCells"].get("rows",[]) for c in row["values"]]
    assert sum('MATCH(' in f for f in formulas)==2
    assert not any("支出明細'!" in f or "QUERY(" in f for f in formulas)
    meta["sheets"][0]["charts"]=[]
    assert not any("deleteEmbeddedObject" in r for r in build_plan(meta,"daily")["requests"])


def test_wrong_binding_or_unowned_chart_stops_before_requests():
    with pytest.raises(ValueError,match="cutover_required"):build_plan(metadata(),"another-daily")
    meta=metadata();meta["sheets"][0]["charts"].append({"chartId":999})
    with pytest.raises(ValueError,match="unexpected_chart"):build_plan(meta,"daily")
