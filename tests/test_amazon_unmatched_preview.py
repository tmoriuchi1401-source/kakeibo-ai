from __future__ import annotations

from app.amazon_unmatched import AmazonUnmatchedPreview


class FakeDB:
    def __init__(self, imports=None, amazon=None):
        self.imports = imports or []
        self.amazon = amazon or []
        self.writes = []

    def get(self, range_name):
        if range_name == "取込データ!A2:L":
            return self.imports
        if range_name == "Amazon注文!A2:M":
            return self.amazon
        raise AssertionError(range_name)

    def append(self, *args, **kwargs):
        self.writes.append(("append", args, kwargs))
        raise AssertionError("preview must not append")

    def update_rows(self, *args, **kwargs):
        self.writes.append(("update_rows", args, kwargs))
        raise AssertionError("preview must not update")


def import_row(import_id, date, amount, *, status="amazon_unmatched", merchant="AMAZON.CO.JP", note=""):
    return [
        import_id, "2026-08-20 10:00:00", "au PAYカード", import_id,
        date, merchant, amount, "一括", status, "", "hash", note,
    ]


def amazon_row(order_id, date, amount, *, note="incremental", asin=None):
    asin = asin or f"ASIN-{order_id}"
    return [
        f"{order_id}:{asin}", order_id, asin, date, "匿名商品", 1, amount,
        "au PAYカード", "その他", "未分類", note, "hash", "2026-08-20 10:00:00",
    ]


def preview(imports, amazon):
    return AmazonUnmatchedPreview(FakeDB(imports, amazon)).preview()


def test_exact_match():
    result = preview([import_row("card:1", "2026-08-10", 1000)], [amazon_row("o1", "2026-08-08", 1000)])
    assert result["exact_match"] == 1


def test_date_outside_window():
    result = preview([import_row("card:1", "2026-08-20", 1000)], [amazon_row("o1", "2026-08-01", 1000)])
    assert result["date_outside_window"] == 1
    assert result["samples"]["date_outside_window"][0]["date_difference_days"] == 19


def test_amount_near_match_reports_difference():
    result = preview([import_row("card:1", "2026-08-10", 1000)], [amazon_row("o1", "2026-08-09", 1150)])
    assert result["amount_near_match"] == 1
    sample = result["samples"]["amount_near_match"][0]
    assert sample["nearest_order_amount"] == 1150
    assert sample["amount_difference"] == 150
    assert sample["date_difference_days"] == 1


def test_multiple_exact_candidates():
    orders = [amazon_row("o1", "2026-08-09", 1000), amazon_row("o2", "2026-08-11", 1000)]
    result = preview([import_row("card:1", "2026-08-10", 1000)], orders)
    assert result["multiple_exact_candidates"] == 1
    assert result["samples"]["multiple_exact_candidates"][0]["candidate_orders"] == 2


def test_no_order_candidate():
    result = preview([import_row("card:1", "2026-08-20", 1000)], [amazon_row("o1", "2026-06-01", 2000)])
    assert result["no_order_candidate"] == 1


def test_installment_candidate_reuses_installment_matcher():
    imports = [
        import_row("card:1", "2026-06-05", 400, merchant="AMAZON.CO.JP 分割払い", note="会員=本人"),
        import_row("card:2", "2026-07-05", 600, merchant="AMAZON.CO.JP 分割払い", note="会員=本人"),
    ]
    result = preview(imports, [amazon_row("o1", "2026-06-01", 1000, note="baseline")])
    assert result["installment_candidate"] == 2


def test_refund_candidate_and_statuses_are_distinguished():
    imports = [
        import_row("card:1", "2026-08-10", 1000, note="返品による返金"),
        import_row("card:2", "2026-08-10", 1000, status="amazon_needs_review"),
    ]
    result = preview(imports, [amazon_row("o1", "2026-08-10", 1000)])
    assert result["refund_or_cancel_candidate"] == 1
    assert result["amazon_unmatched"] == 1
    assert result["amazon_needs_review"] == 1


def test_preview_never_writes_to_sheets():
    db = FakeDB([import_row("card:1", "2026-08-10", 1000)], [amazon_row("o1", "2026-08-10", 1000)])
    AmazonUnmatchedPreview(db).preview()
    assert db.writes == []


def test_non_target_rows_are_ignored():
    rows = [
        import_row("card:1", "2026-08-10", 1000, status="matched_amazon"),
        import_row("card:2", "2026-08-10", 1000, merchant="OTHER STORE"),
    ]
    result = preview(rows, [amazon_row("o1", "2026-08-10", 1000)])
    assert result["diagnosed"] == 0
