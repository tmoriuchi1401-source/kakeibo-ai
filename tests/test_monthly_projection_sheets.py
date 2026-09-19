from types import SimpleNamespace

from app.monthly_projection import Category, CategoryCatalog, LedgerIndex, rebuild_month
from app.monthly_projection_sheets import SheetsLedgerReader


class FakeSheets:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, **kwargs):
        return SimpleNamespace(execute=lambda: {"sheets": [{"properties": {
            "title": "支出明細", "gridProperties": {"rowCount": len(self.rows) + 1}}}]})

    def batchGet(self, **kwargs):
        self.calls.append(kwargs)
        blocks = []
        for a1 in kwargs["ranges"]:
            first, last = a1.split("!A")[1].split(":M")
            rows = self.rows[int(first) - 2:int(last) - 1]
            while rows and not rows[-1]:
                rows = rows[:-1]
            blocks.append({"values": rows})
        return SimpleNamespace(execute=lambda: {"valueRanges": blocks})


def test_bootstrap_past_old_cap_then_month_batch_reads_only_affected_rows():
    rows = [[str(i), "2026-09-01" if i % 3 else "2020-01-01", "store", "item",
             10, "food", "food", "card", "synthetic", "", str(i), "", "active"]
            for i in range(7001)] + [[]] * 10
    svc = FakeSheets(rows)
    db = SimpleNamespace(svc=svc, sid="synthetic", _execute_sheet_read=lambda request: request().execute())
    reader = SheetsLedgerReader(db)
    catalog = CategoryCatalog([Category("food", "food", "food")])
    index = LedgerIndex.bootstrap(reader.bootstrap_pages(), catalog)
    assert len(index.by_id) == 7001
    assert reader.metrics.requests == 5  # Metadata + four bounded pages.
    svc.calls.clear()
    month = rebuild_month("2020-01", index, catalog, reader)
    assert month.amount == 2334 * 10
    assert len(svc.calls) == 24  # 2,334 discontiguous rows in batches of 100.
    assert all(len(c["ranges"]) <= 100 for c in svc.calls)
    assert reader.metrics.returned_rows == 7001 + 2334
    assert all(c["valueRenderOption"] == "UNFORMATTED_VALUE" for c in svc.calls)
