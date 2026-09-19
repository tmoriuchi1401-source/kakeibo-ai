"""Synthetic accounting, replay, and bounded-read acceptance cases."""
from dataclasses import replace

import pytest

from app.monthly_projection import (
    Category, CategoryCatalog, LedgerIndex, ProjectionError, compare_years,
    history_page, parse_line, rebuild_month, refresh_months,
)


@pytest.fixture
def catalog():
    return CategoryCatalog([Category("food", "食費", "食料品"),
                            Category("other", "その他", "未分類")])


def row(key, day="2026-09-01", amount=100, purchase="", status="active"):
    return [key, day, "合成店舗", "合成商品", amount, "食費", "食料品", "card",
            "synthetic", "", purchase, "", status]


def setup(rows, catalog):
    index = LedgerIndex.bootstrap([(2, rows)], catalog)
    reads = []

    def read(first, last):
        reads.append((first, last))
        return rows[first - 2:last - 1]

    return index, read, reads


def test_items_discount_and_retired_header_are_not_double_counted(catalog):
    rows = [row("old-total", amount=250, purchase="p", status="superseded_amazon_items"),
            row("a", amount=200, purchase="p"), row("b", amount=100, purchase="p"),
            row("discount", amount=-50, purchase="p"),
            row("partial-refund-1", amount=-20, purchase="r1"),
            row("partial-refund-2", amount=-30, purchase="r2")]
    index, read, _ = setup(rows, catalog)
    result = rebuild_month("2026-09", index, catalog, read)
    assert (result.amount, result.purchase_count, result.refund_count) == (200, 1, 2)
    assert result.category_amounts == (("food", 200),)
    assert len(result.purchases) == 3


def test_date_move_updates_both_months_and_clears_empty_month(catalog):
    rows = [row("a", "2025-12-31")]
    index, read, reads = setup(rows, catalog)
    saved = {}
    save = lambda projection: saved.update({projection.month: projection})
    refresh_months(["2025-12"], index, catalog, read, save)
    rows[0][1] = "2026-01-01"
    dirty = index.observe(2, rows[0], catalog)
    assert dirty == {"2025-12", "2026-01"}
    refresh_months(dirty, index, catalog, read, save)
    assert saved["2025-12"].amount == 0
    assert saved["2025-12"].purchases == ()
    assert saved["2026-01"].amount == 100
    snapshot = dict(saved)
    refresh_months(dirty, index, catalog, read, save)
    assert saved == snapshot
    assert set(reads) == {(2, 2)}
    assert index.observe(2, rows[0], catalog) == set()


def test_partial_projection_failure_replays_without_reposting(catalog):
    rows = [row("a", "2026-08-01"), row("b", "2026-09-01")]
    index, read, _ = setup(rows, catalog)
    saved = {}

    def fail_after_save(projection):
        saved[projection.month] = projection
        raise RuntimeError("synthetic_unknown_save_response")

    with pytest.raises(RuntimeError):
        refresh_months(["2026-08", "2026-09"], index, catalog, read, fail_after_save)
    refresh_months(["2026-08", "2026-09"], index, catalog, read,
                   lambda p: saved.update({p.month: p}))
    assert sum(p.amount for p in saved.values()) == 200
    assert len(rows) == 2


@pytest.mark.parametrize("amount", ["not money", "", "NaN", "Infinity", "10.5", True])
def test_invalid_money_is_not_silently_zero(amount, catalog):
    with pytest.raises(ProjectionError, match="invalid_ledger_amount"):
        parse_line(row("a", amount=amount), catalog)


def test_native_serial_date_and_formatted_integer_amount(catalog):
    assert parse_line(row("a", 46266, "￥1,200円"), catalog).day == "2026-09-01"
    assert parse_line(row("a", amount="100.0"), catalog).amount == 100


def test_changed_human_category_requires_fresh_index(catalog):
    rows = [row("a")]
    index, read, _ = setup(rows, catalog)
    rows[0][5:7] = ["その他", "未分類"]
    with pytest.raises(ProjectionError, match="ledger_index_stale"):
        rebuild_month("2026-09", index, catalog, read)
    index.observe(2, rows[0], catalog)
    assert rebuild_month("2026-09", index, catalog, read).category_amounts == (("other", 100),)
    assert rows[0][5:7] == ["その他", "未分類"]


def test_duplicate_and_reordered_ids_stop_projection(catalog):
    with pytest.raises(ProjectionError, match="duplicate_expense_id"):
        setup([row("a", status="inactive"), row("a")], catalog)
    rows = [row("a"), row("b")]
    index, read, _ = setup(rows, catalog)
    rows.reverse()
    with pytest.raises(ProjectionError, match="ledger_index_stale"):
        rebuild_month("2026-09", index, catalog, read)
    with pytest.raises(ProjectionError, match="ledger_index_rebuild_required"):
        index.observe(2, rows[0], catalog)


def test_rename_keeps_id_and_historical_split_does_not_remap(catalog):
    renamed = catalog.rename("food", "食費", "食品")
    assert renamed.resolve("食費", "食料品") == "food"
    assert renamed.resolve("食費", "食品") == "food"
    split = CategoryCatalog([replace(renamed.categories[0], active=False),
                             Category("fresh", "食費", "生鮮食品")])
    assert parse_line(row("a"), split).category_id == "food"
    with pytest.raises(ProjectionError, match="ambiguous_category_alias"):
        catalog.rename("other", "食費", "食料品")


def test_coverage_requires_both_years_every_account_and_excludes_current(catalog):
    amounts = {f"{year}-{month:02d}": value for year, value in [(2025, 100), (2026, 150)]
               for month in range(1, 10)}
    routes = ["card:one", "card:two"]
    coverage = {(month, route): "complete" for month in amounts for route in routes}
    del coverage[("2025-02", "card:two")]
    coverage[("2026-03", "card:one")] = "partial"
    result = compare_years(2026, "2026-09", amounts, coverage, routes)
    assert result.months == (1, 4, 5, 6, 7, 8)
    assert result.current_average == 150
    assert result.previous_average == 100
    assert result.change_percent == 50
    unknown = compare_years(2026, "2026-09", amounts, {}, routes)
    assert unknown.months == ()
    assert unknown.current_average is None


def test_history_reads_only_recent_months_and_preserves_total_pagination(catalog):
    rows = [row(str(i), purchase=str(i)) for i in range(120)]
    index, read, _ = setup(rows, catalog)
    projection = rebuild_month("2026-09", index, catalog, read)
    months = []

    def read_month(month):
        months.append(month)
        return projection if month == "2026-09" else None

    page = history_page(read_month, current_month="2026-09", page=2, page_size=100,
                        search="合成商品", category_id="food")
    assert len(page.rows) == 20 and page.total == 120 and page.pages == 2
    assert len(months) == 13 and min(months) == "2025-09"
    months.clear()
    history_page(read_month, current_month="2026-09", selected_month="2026-09")
    assert months == ["2026-09"]
    with pytest.raises(ProjectionError, match="open_ledger"):
        history_page(read_month, current_month="2026-09", selected_month="2025-08")


def test_one_hundred_thousand_purchases_multiple_items_ten_years(catalog):
    # Interleaved months exercise range indexing without relying on sort order.
    rows = []
    expected = {}
    for purchase in range(100_000):
        month = f"{2017 + purchase % 120 // 12:04d}-{purchase % 12 + 1:02d}"
        expected[month] = expected.get(month, 0) + 90
        rows.extend([row(f"{purchase}-1", month + "-01", 100, str(purchase)),
                     row(f"{purchase}-2", month + "-01", -10, str(purchase))])
    index, read, reads = setup(rows, catalog)
    saved = {}
    refresh_months(expected, index, catalog, read, lambda p: saved.update({p.month: p}))
    assert {m: p.amount for m, p in saved.items()} == expected
    assert sum(p.purchase_count for p in saved.values()) == 100_000
    assert sum(last - first + 1 for first, last in reads) == 200_000
    reads.clear()
    refresh_months(["2026-09"], index, catalog, read, lambda p: saved.update({p.month: p}))
    assert sum(b - a + 1 for a, b in reads) == saved["2026-09"].purchase_count * 2
    months = []

    def recent(month):
        months.append(month)
        return saved.get(month)

    page = history_page(recent, current_month="2026-12")
    assert len(months) == 13
    assert page.total == sum(len(saved[m].purchases) for m in months)
    assert len(page.rows) == 100
