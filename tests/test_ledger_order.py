from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.ledger_order import LEDGERS, date_order, order_ledgers
from app.monthly_projection import ProjectionError
from app.projection_store import ProjectionJournal
from test_projection_refresh import initialized, row, PAIRS


class DB:
    sid = "synthetic"

    def __init__(self):
        # Include a formula, note, extra user column and mixed date cell types.
        self.rows = {
            "支出明細": [LEDGERS["支出明細"], ["a", "2026-08-01", 100, "=1+1", "note"],
                        ["b", 46278, 200], ["c", "2026-09-13", -50]],
            "収入明細": [LEDGERS["収入明細"], ["i", "2026-08-01", 500], ["j", "2026-09-01", 700]],
        }
        self.props = [{"sheetId": i, "title": title,
                       "gridProperties": {"rowCount": 5002, "columnCount": 5}}
                      for i, title in enumerate(LEDGERS)]
        self.svc = self
        self.batches = []
        self.before_sort = lambda: None
        self.fail_after_sort = False

    def spreadsheets(self): return self

    def get(self, **kw):
        return SimpleNamespace(execute=lambda: {"sheets": [{"properties": p} for p in self.props]})

    def _execute_sheet_read(self, factory): return factory().execute()

    def get_raw(self, a1):
        title, bounds = a1.split("!")
        first, last = bounds.split(":")
        return [r[:2] for r in self.rows[title.strip("'")][int(first[1:])-1:int(last[1:])]]

    def batchUpdate(self, *, spreadsheetId, body):
        def execute(num_retries):
            assert num_retries == 0
            self.before_sort()
            self.batches.append(body)
            for p in self.props:
                requests = [r for r in body["requests"] if next(iter(r.values())).get("sheetId",
                    next(iter(r.values())).get("range", {}).get("sheetId")) == p["sheetId"]]
                if not requests: continue
                assert [next(iter(r)) for r in requests] == ["appendDimension", "updateCells", "sortRange", "deleteDimension"]
                span = requests[2]["sortRange"]["range"]
                assert span["startRowIndex"] == 1 and span["endColumnIndex"] == 6
                ranks = [r["values"][0]["userEnteredValue"]["numberValue"] for r in requests[1]["updateCells"]["rows"]]
                original = self.rows[p["title"]]
                self.rows[p["title"]] = [original[0], *[original[i+1] for i in sorted(range(len(ranks)), key=ranks.__getitem__)]]
            if self.fail_after_sort: raise RuntimeError("unknown response")
            return {}
        return SimpleNamespace(execute=execute)


def test_mixed_dates_stable_ties_and_blank_rows():
    from datetime import date
    serial = (date(2026, 9, 13) - date(1899, 12, 30)).days
    assert date_order([["a", "2026-8-1"], [], ["b", serial], ["c", "2026/09/13"]]) == [2, 3, 0, 1]


@pytest.mark.parametrize("rows", [[["a", "bad"]], [["a", "2026-09-01"], ["a", "2026-09-02"]], [["", "2026-09-01"]]])
def test_invalid_dates_and_identity_fail_closed(rows):
    with pytest.raises(ProjectionError): date_order(rows)


def test_preview_no_write_then_sort_preserves_entire_rows_and_is_idempotent():
    db = DB()
    before = deepcopy(db.rows)
    assert order_ledgers(db, None)["ledger_order_sheets"] == 2
    assert not db.batches and db.rows == before
    assert order_ledgers(db, None, apply=True)["ledger_order_sheets"] == 2
    for title in LEDGERS:
        assert db.rows[title][0] == before[title][0]
        assert {r[0]: r for r in db.rows[title][1:]} == {r[0]: r for r in before[title][1:]}
    assert order_ledgers(db, None, apply=True)["ledger_order_sheets"] == 0
    assert len(db.batches) == 1
    # A later append becomes the newest row on the next completed update.
    db.rows["支出明細"].append(["later", "2026-10-01", 900])
    order_ledgers(db, None, apply=True)
    assert db.rows["支出明細"][1][0] == "later"


def test_marker_precedes_sort_and_survives_unknown_response():
    db = DB()
    store, _, _ = initialized([row("a")])
    def check(): assert ProjectionJournal(store).read()["rebuild"] is True
    db.before_sort = check
    db.fail_after_sort = True
    with pytest.raises(RuntimeError, match="unknown response"):
        order_ledgers(db, store, apply=True)
    assert ProjectionJournal(store).read()["rebuild"] is True


@pytest.mark.parametrize("fail_key", ["month-2026-08", "index", None])
def test_reordered_addresses_and_history_links_recover_after_partial_rebuild(fail_key):
    store, reader, refresh = initialized([row("a", "2026-08-01"), row("b", "2026-09-01")])
    categories = store.read("catalog")
    ProjectionJournal(store).mark(rebuild=True)
    reader.rows.reverse()
    if fail_key:
        store.fail_key = fail_key
        with pytest.raises(RuntimeError): refresh.refresh(PAIRS)
        assert ProjectionJournal(store).read()["rebuild"]
    refresh.refresh(PAIRS)
    assert store.read("index")["rows"][0][:2] == ["b", 2]
    assert refresh.read_month("2026-09").purchases[0].ledger_row == 2
    assert refresh.read_month("2026-08").purchases[0].ledger_row == 3
    assert refresh.read_month("2026-09").amount == refresh.read_month("2026-08").amount == 100
    assert store.read("catalog") == categories
    assert not ProjectionJournal(store).read().get("rebuild")
