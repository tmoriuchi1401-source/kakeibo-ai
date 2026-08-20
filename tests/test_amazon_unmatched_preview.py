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


def amount_structure(imports, amazon):
    return preview(imports, amazon)["amount_structure"]["summary"]


def test_single_item_amount_match():
    orders = [
        amazon_row("o1", "2026-08-09", 1000, asin="item-1"),
        amazon_row("o1", "2026-08-09", 200, asin="item-2"),
    ]
    result = amount_structure([import_row("card:1", "2026-08-10", 1000)], orders)
    assert result["single_item"]["unique"] == 1


def test_unique_two_item_same_order_sum():
    orders = [
        amazon_row("o1", "2026-08-09", 400, asin="item-1"),
        amazon_row("o1", "2026-08-09", 600, asin="item-2"),
        amazon_row("o1", "2026-08-09", 50, asin="item-3"),
    ]
    result = amount_structure([import_row("card:1", "2026-08-10", 1000)], orders)
    assert result["same_order_2_items"]["unique"] == 1


def test_unique_three_item_same_order_sum():
    orders = [
        amazon_row("o1", "2026-08-09", 200, asin="item-1"),
        amazon_row("o1", "2026-08-09", 300, asin="item-2"),
        amazon_row("o1", "2026-08-09", 500, asin="item-3"),
        amazon_row("o1", "2026-08-09", 75, asin="item-4"),
    ]
    result = amount_structure([import_row("card:1", "2026-08-10", 1000)], orders)
    assert result["same_order_3_items"]["unique"] == 1


def test_multiple_same_order_subsets_are_ambiguous():
    amounts = [400, 600, 300, 700, 25]
    orders = [
        amazon_row("o1", "2026-08-09", amount, asin=f"item-{index}")
        for index, amount in enumerate(amounts)
    ]
    result = amount_structure([import_row("card:1", "2026-08-10", 1000)], orders)
    assert result["same_order_2_items"]["ambiguous"] == 1


def test_unique_two_order_sum():
    orders = [
        amazon_row("o1", "2026-08-09", 400),
        amazon_row("o2", "2026-08-11", 600),
        amazon_row("o3", "2026-08-10", 50),
    ]
    result = amount_structure([import_row("card:1", "2026-08-10", 1000)], orders)
    assert result["nearby_2_orders"]["unique"] == 1


def test_multiple_order_sums_are_ambiguous():
    amounts = [400, 600, 300, 700, 25]
    orders = [amazon_row(f"o{index}", "2026-08-09", amount) for index, amount in enumerate(amounts)]
    result = amount_structure([import_row("card:1", "2026-08-10", 1000)], orders)
    assert result["nearby_2_orders"]["ambiguous"] == 1


def test_unique_three_order_sum():
    orders = [
        amazon_row("o1", "2026-08-09", 200),
        amazon_row("o2", "2026-08-10", 300),
        amazon_row("o3", "2026-08-11", 500),
        amazon_row("o4", "2026-08-10", 25),
    ]
    result = amount_structure([import_row("card:1", "2026-08-10", 1000)], orders)
    assert result["nearby_3_orders"]["unique"] == 1


def test_date_window_simulation_for_10_14_21_and_30_days():
    imports = [
        import_row("card:1", "2026-08-20", 1000),
        import_row("card:2", "2026-08-20", 2000),
        import_row("card:3", "2026-08-20", 3000),
        import_row("card:4", "2026-08-20", 4000),
    ]
    orders = [
        amazon_row("o1", "2026-08-11", 1000),
        amazon_row("o2", "2026-08-08", 2000),
        amazon_row("o3", "2026-08-02", 3000),
        amazon_row("o4", "2026-07-26", 4000),
    ]
    simulation = preview(imports, orders)["date_window_simulation"]
    assert simulation["plus_minus_10_days"] == {"unique": 1, "ambiguous": 0, "unmatched": 3}
    assert simulation["plus_minus_14_days"] == {"unique": 2, "ambiguous": 0, "unmatched": 2}
    assert simulation["plus_minus_21_days"] == {"unique": 3, "ambiguous": 0, "unmatched": 1}
    assert simulation["plus_minus_30_days"] == {"unique": 4, "ambiguous": 0, "unmatched": 0}


def test_date_window_simulation_detects_ambiguity():
    imports = [import_row("card:1", "2026-08-20", 1000)]
    orders = [
        amazon_row("o1", "2026-08-11", 1000),
        amazon_row("o2", "2026-08-08", 1000),
    ]
    simulation = preview(imports, orders)["date_window_simulation"]
    assert simulation["plus_minus_10_days"]["unique"] == 1
    assert simulation["plus_minus_14_days"]["ambiguous"] == 1


def test_adjustment_schema_distinguishes_confirmed_columns_from_note_inference():
    orders = [amazon_row("o1", "2026-08-09", 1100, note="ポイント利用の可能性")]
    result = preview([import_row("card:1", "2026-08-10", 1000)], orders)
    evidence = result["amount_differences"]["amazon_csv_evidence"]
    assert evidence["confirmed_dedicated_adjustment_columns"] == []
    assert evidence["source_csv_extra_columns_observable"] is False
    assert evidence["inferred_note_keyword_hits"]["ポイント"] == 1


def test_repeated_amount_difference_and_rate_are_counted():
    imports = [
        import_row("card:1", "2026-08-10", 900),
        import_row("card:2", "2026-08-12", 900),
    ]
    orders = [
        amazon_row("o1", "2026-08-09", 1000),
        amazon_row("o2", "2026-08-13", 1000),
    ]
    analysis = preview(imports, orders)["amount_differences"]
    assert {"difference": 100, "count": 2} in analysis["repeated_amount_differences"]
    assert {"rate": 10.0, "count": 2} in analysis["repeated_difference_rates_percent"]


def test_date_direction_uses_order_date_to_card_date():
    imports = [
        import_row("card:1", "2026-08-20", 1000),
        import_row("card:2", "2026-08-01", 2000),
    ]
    orders = [
        amazon_row("o1", "2026-08-01", 1000),
        amazon_row("o2", "2026-08-20", 2000),
    ]
    direction = preview(imports, orders)["date_direction"]
    assert direction["amazon_order_before_card"] == 1
    assert direction["card_before_amazon_order"] == 1
