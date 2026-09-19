from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.monthly_projection import ProjectionError, history_page
from app.projection_refresh import ProjectionRefresh, load_catalog
from app.projection_store import ProjectionJournal, empty_journal
from app.sheets import SheetsDB


PAIRS = [("食費", "食料品")]


def row(key, day="2026-09-01", amount=100):
    return [key, day, "合成店舗", "商品", amount, "食費", "食料品", "card", "synthetic", "", key, "", "active"]


class Store:
    def __init__(self):
        self.data = {}
        self.writes = []
        self.reads = []
        self.fail_key = None
        self.after_save = False

    def read(self, key):
        self.reads.append(key)
        return deepcopy(self.data.get(key))

    def write(self, key, value):
        fail = key == self.fail_key
        if not fail or self.after_save:
            self.data[key] = deepcopy(value)
            self.writes.append(key)
        if fail:
            self.fail_key = None
            raise RuntimeError("synthetic_write_failure")


class Reader:
    page_size = 1000

    def __init__(self, rows):
        self.rows = rows
        self.reads = []

    def row_count(self):
        return len(self.rows) + 1

    def __call__(self, first, last):
        self.reads.append((first, last))
        return deepcopy(self.rows[first - 2:last - 1])

    def read_ranges(self, ranges):
        return (self(first, last) for first, last in ranges)

    def bootstrap_pages(self):
        for first in range(2, len(self.rows) + 2, self.page_size):
            yield first, self(first, min(first + self.page_size - 1, len(self.rows) + 1))


def initialized(rows):
    store, reader = Store(), Reader(rows)
    refresh = ProjectionRefresh(store, reader)
    refresh.bootstrap(PAIRS)
    store.writes.clear()
    store.reads.clear()
    reader.reads.clear()
    return store, reader, refresh


def test_changed_month_only_values_persist_and_idle_refresh_reads_no_ledger():
    store, reader, refresh = initialized([row("old", "2010-01-01"), row("new")])
    refresh.journal.mark([(3, 3)])
    reader.rows[1][4] = 120
    result = refresh.refresh(PAIRS)
    assert result["projection_months"] == 1
    assert set(reader.reads) == {(3, 3)}
    assert "month-2010-01" not in store.writes
    assert store.data["summary"]["months"]["2026-09"]["amount"] == 120
    reader.reads.clear()
    store.reads.clear()
    assert refresh.refresh(PAIRS)["projection_months"] == 0
    assert reader.reads == []
    assert store.reads == ["journal"]


@pytest.mark.parametrize("fail_key", ["month-2025-12", "month-2026-01", "index", "summary"])
@pytest.mark.parametrize("after_save", [False, True])
def test_date_move_survives_failure_at_each_save_without_accounting_replay(fail_key, after_save):
    store, reader, refresh = initialized([row("a", "2025-12-31")])
    refresh.journal.mark([(2, 2)])
    reader.rows[0][1] = "2026-01-01"
    store.fail_key, store.after_save = fail_key, after_save
    with pytest.raises(RuntimeError):
        refresh.refresh(PAIRS)
    assert store.data["journal"]["months"] == ["2025-12", "2026-01"]
    # Simulate a fresh Actions process; no in-memory state is used for recovery.
    restarted = ProjectionRefresh(store, reader)
    restarted.refresh(PAIRS)
    assert restarted.read_month("2025-12").amount == 0
    assert restarted.read_month("2026-01").amount == 100
    assert len(reader.rows) == 1
    assert restarted.refresh(PAIRS)["projection_months"] == 0


def test_append_marked_before_unknown_accounting_response_is_discovered_from_tail():
    store, reader, refresh = initialized([row("a")])
    refresh.journal.mark(append=True)
    # Ledger append succeeded but process died before inspecting its response.
    reader.rows.append(row("b", "2026-08-01", -20))
    restarted = ProjectionRefresh(store, reader)
    restarted.refresh(PAIRS)
    assert restarted.read_month("2026-08").amount == -20
    assert restarted.read_month("2026-09").amount == 100
    assert set(reader.reads) == {(3, 3)}
    assert restarted.refresh(PAIRS)["projection_months"] == 0


def test_append_intent_without_accounting_write_does_not_invent_expense():
    store, reader, refresh = initialized([row("a")])
    refresh.journal.mark(append=True)
    assert refresh.refresh(PAIRS)["projection_months"] == 0
    assert refresh.read_month("2026-09").amount == 100


def test_rebuild_preserves_category_ids_and_coverage_inputs():
    store, reader, refresh = initialized([row("a")])
    before = load_catalog(store.read("catalog")).categories
    store.data["summary"]["coverage"] = {"2026-09": {"card:one": "partial"}}
    refresh.bootstrap(PAIRS)
    assert load_catalog(store.read("catalog")).categories == before
    assert store.data["summary"]["coverage"] == {"2026-09": {"card:one": "partial"}}


def test_daily_history_never_reads_all_years_or_index():
    store, reader, refresh = initialized([row("old", "2010-01-01"), row("new")])
    result = history_page(refresh.read_month, current_month="2026-09", selected_month="2026-09")
    assert result.total == 1
    assert store.reads == ["month-2026-09"]
    assert reader.reads == []


class WriteService:
    def __init__(self, store):
        self.store = store
        self.calls = []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def request(self, **kwargs):
        def execute(**_):
            self.calls.append(kwargs)
            assert self.store.data["journal"]["ranges"] or self.store.data["journal"]["append"]
            return {}
        return SimpleNamespace(execute=execute)

    append = update = batchUpdate = request


@pytest.mark.parametrize("write", [
    lambda db: db.append("支出明細", [row("a")]),
    lambda db: db.append_raw("支出明細", [row("a")]),
    lambda db: db.update_row("支出明細", 2, row("a")),
    lambda db: db.update_row_raw("支出明細", 2, row("a")),
    lambda db: db.update_rows("支出明細", [(2, row("a"))]),
    lambda db: db.set_raw_range("'支出明細'!F2:G2", [["食費", "食料品"]]),
    lambda db: db.update_expense_categories([(2, "食費", "食料品")]),
])
def test_every_supported_ledger_writer_persists_invalidation_before_write(write):
    store = Store()
    store.data["journal"] = empty_journal()
    service = WriteService(store)
    db = SheetsDB("synthetic", service=service, projection_journal=ProjectionJournal(store))
    write(db)
    assert len(service.calls) == 1
    assert store.data["journal"]["generation"] == 1


def test_failed_intent_prevents_any_accounting_request():
    store = Store()
    store.data["journal"] = empty_journal()
    store.fail_key = "journal"
    service = WriteService(store)
    db = SheetsDB("synthetic", service=service, projection_journal=ProjectionJournal(store))
    with pytest.raises(RuntimeError):
        db.append_raw("支出明細", [row("a")])
    assert service.calls == []


def test_uninitialized_journal_and_stale_id_fail_without_silent_full_scan():
    with pytest.raises(ProjectionError, match="journal_invalid"):
        ProjectionJournal(Store()).mark(append=True)
    store, reader, refresh = initialized([row("a")])
    refresh.journal.mark([(2, 2)])
    reader.rows[0][0] = "different"
    with pytest.raises(ProjectionError, match="rebuild_required"):
        refresh.refresh(PAIRS)
    assert store.data["journal"]["ranges"] == [[2, 2]]


def test_new_invalidation_during_refresh_is_not_cleared():
    store, reader, refresh = initialized([row("a")])
    refresh.journal.mark([(2, 2)])
    reader.rows[0][4] = 200
    original = store.write

    def concurrent(key, value):
        original(key, value)
        if key == "index":
            ProjectionJournal(store).mark(append=True)

    store.write = concurrent
    with pytest.raises(ProjectionError, match="state_changed"):
        refresh.refresh(PAIRS)
    assert store.data["journal"]["append"]


def test_durable_pipeline_scales_to_100k_purchases_without_reading_old_history_for_daily_view():
    rows=[]
    for number in range(100_000):
        month=f"{2017+number%120//12:04d}-{number%12+1:02d}"
        for part,amount in [(1,70),(2,30)]:
            item=row(f"p-{number}-{part}",month+"-01",amount)
            item[10]=f"p-{number}"
            rows.append(item)
    store,reader,refresh=initialized(rows)
    summaries=store.data["summary"]["months"]
    assert len(summaries)==120
    assert sum(m["amount"] for m in summaries.values())==10_000_000
    assert sum(m["purchase_count"] for m in summaries.values())==100_000
    # A historical correction reads its row and its month only.
    refresh.journal.mark([(2,2)])
    rows[0][4]=65
    refreshed=refresh.refresh(PAIRS)
    assert refreshed["projection_months"]==1
    assert sum(last-first+1 for first,last in reader.reads)==1+2*834
    assert store.data["summary"]["months"]["2017-01"]["amount"]==83_395
    store.reads.clear()
    reader.reads.clear()
    page=history_page(refresh.read_month,current_month="2026-12",page_size=100)
    assert page.total==10_829
    assert len(page.rows)==100
    assert len(store.reads)==13 and all(key.startswith("month-") for key in store.reads)
    assert reader.reads==[]
