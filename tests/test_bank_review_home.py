from copy import deepcopy

import pytest

from app.bank_review_home import home_cells, update_home
from app.sheets_ui import HOME_ID, SPREADSHEET_ID
from test_ledger_operations_home import metadata


class DB:
    sid = SPREADSHEET_ID
    def __init__(self):
        self.meta = metadata()
        sid = next(sheet["properties"]["sheetId"] for sheet in self.meta["sheets"]
                   if sheet["properties"]["title"] == "カテゴリ操作")
        self.rows = [["整理・改善"], ["daily link"],
                     [f'=HYPERLINK("#gid={sid}&range=A1","分類ルールを登録　"&C8&"件 →")'],
                     ["ルール登録だけでは過去の支出は変わりません。"], ["preview link"],
                     ["confirm link"], ["対象月は「カテゴリ操作」画面上部で選べます。"]]
        self.svc = self
        self.requests = []
        self.corrupt = False
    def _invalidate_sheet_metadata(self): pass
    def _sheet_metadata(self): return deepcopy(self.meta)
    def _execute_sheet_read(self, factory): return factory().execute()
    def values(self): return self
    def get(self, **kwargs):
        assert kwargs == {"spreadsheetId": self.sid, "range": "'ホーム'!A11:B17", "valueRenderOption": "FORMULA"}
        db = self
        class Read:
            def execute(self): return {"values": deepcopy(db.rows)}
        return Read()
    def spreadsheets(self): return self
    def batchUpdate(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        return self
    def execute(self, *, num_retries):
        assert num_retries == 0
        for request in self.requests[-1]["body"]["requests"]:
            if "updateCells" not in request:
                continue
            value = request["updateCells"]
            assert value["start"]["sheetId"] == HOME_ID and value["start"]["columnIndex"] == 0
            row = value["start"]["rowIndex"] + 1
            assert row in {11, 13, 17}
            self.rows[row - 11] = [next(iter(value["rows"][0]["values"][0]["userEnteredValue"].values()))]
        if self.corrupt:
            self.rows[0] = ["wrong"]
        return {}


def test_home_link_uses_moving_bank_marker_and_is_quiet_when_all_counts_are_zero():
    values = home_cells(metadata())
    formula = values[17]["formulaValue"]
    assert "■ 4. 銀行取引をまとめて確認" in formula and "MATCH(" in formula
    assert '銀行確認 ' in formula and '銀行の記帳状況を確認' in formula
    assert formula.endswith(',""))') and "A1:A10000" in formula
    assert "支出明細" not in formula and "QUERY(" not in formula


def test_home_updates_only_owned_display_and_is_idempotent():
    db = DB()
    before = deepcopy(db.rows)
    assert update_home(db) == {"bank_home_write_requests": 1}
    assert [db.rows[i] for i in (1, 3, 4, 5)] == [before[i] for i in (1, 3, 4, 5)]
    assert 'C8&"件 →"' in db.rows[2][0]
    assert update_home(db) == {"bank_home_write_requests": 0}
    assert len(db.requests) == 1


def test_home_owned_content_change_stops_before_overwrite():
    db = DB()
    db.rows[0] = ["user customized home"]
    assert update_home(db) == {"bank_home_write_requests": 0, "bank_home_held": 1}
    assert not db.requests


@pytest.mark.parametrize("row", [2, 6])
def test_customized_navigation_is_preserved_even_with_the_known_heading(row):
    db = DB()
    db.rows[row] = ["user customized navigation"]
    assert update_home(db) == {"bank_home_write_requests": 0, "bank_home_held": 1}
    assert not db.requests


def test_home_readback_failure_is_not_retried():
    db = DB()
    db.corrupt = True
    with pytest.raises(ValueError, match="readback_failed"):
        update_home(db)
    assert len(db.requests) == 1


def test_unowned_home_is_not_changed_and_wrong_workbook_is_rejected():
    meta = metadata()
    meta["sheets"][0]["developerMetadata"] = []
    assert home_cells(meta) == {}
    with pytest.raises(ValueError, match="binding_invalid"):
        home_cells({**metadata(), "spreadsheetId": "other"})
