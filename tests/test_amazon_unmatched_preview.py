from __future__ import annotations

import json
import sys

import pandas as pd
import pytest

from app.amazon_csv_diagnostics import payment_method_class
from app.amazon_unmatched import (
    AmazonUnmatchedPreview,
    export_amazon_unmatched_input,
    load_amazon_unmatched_input,
)


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


def raw_row(
    order_id, date, total, *, payment="Visa", ship_date=None, tracking="TRACK-TEST",
    shipment_subtotal=0, shipment_tax=0, shipping=0, unit_price=0, unit_tax=0,
    quantity=1, asin=None,
):
    return {
        "ASIN": asin or f"ASIN-{order_id}", "Order Date": date, "Order ID": order_id,
        "Original Quantity": quantity, "Payment Method Type": payment,
        "Product Name": "匿名商品", "Total Amount": total,
        "Ship Date": ship_date or date, "Carrier Name & Tracking Number": tracking,
        "Shipment Item Subtotal": shipment_subtotal,
        "Shipment Item Subtotal Tax": shipment_tax, "Shipment Status": "Shipped",
        "Shipping Charge": shipping, "Total Discounts": 0, "Unit Price": unit_price,
        "Unit Price Tax": unit_tax, "Order Status": "Closed", "Currency": "JPY",
    }


def raw_csv_preview(tmp_path, raw_rows, *, amount=1000, date="2026-08-10"):
    path = tmp_path / "Order History.csv"
    pd.DataFrame(raw_rows).to_csv(path, index=False, encoding="utf-8-sig")
    sheet_rows = [
        amazon_row(row["Order ID"], row["Order Date"], row["Total Amount"], asin=row["ASIN"])
        for row in raw_rows
    ]
    return AmazonUnmatchedPreview(
        FakeDB([import_row("card:1", date, amount)], sheet_rows),
    ).preview(str(path))["raw_csv_diagnostics"]


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


def test_preview_counts_only_strict_unique_extended_matches():
    imports = [
        import_row("card:1", "2026-08-16", 1000),
        import_row("card:2", "2026-08-16", 2000),
        import_row("card:3", "2026-08-16", 3000),
    ]
    orders = [
        amazon_row("o1", "2026-08-01", 1000),
        amazon_row("o2", "2026-08-01", 2000),
        amazon_row("o3", "2026-08-03", 2000),
        amazon_row("o4", "2026-07-01", 3000),
    ]
    result = preview(imports, orders)
    assert result["date_outside_window"] == 3
    assert result["extended_match_candidates"] == 1
    assert result["extended_match_samples"][0]["candidate_orders"] == 1


def test_payment_method_classifies_gift_and_single_methods():
    assert payment_method_class(["Gift Card; Visa"]) == "gift_or_mixed"
    assert payment_method_class(["Visa"]) == "single_payment_method"
    assert payment_method_class(["Visa", "Mastercard"]) == "multiple_payment_methods"


def test_unique_shipment_group_match(tmp_path):
    rows = [raw_row(
        "o1", "2026-08-09", 1100, shipment_subtotal=900, shipment_tax=100,
    )]
    result = raw_csv_preview(tmp_path, rows)
    assert result["shipment_amount_match"]["unique"] == 1


def test_multiple_shipment_group_matches_are_ambiguous(tmp_path):
    rows = [
        raw_row("o1", "2026-08-09", 1100, shipment_subtotal=900, shipment_tax=100),
        raw_row("o2", "2026-08-11", 1200, shipment_subtotal=950, shipment_tax=50),
    ]
    result = raw_csv_preview(tmp_path, rows)
    assert result["shipment_amount_match"]["ambiguous"] == 1


def test_shipment_subtotal_formula_match(tmp_path):
    rows = [raw_row(
        "o1", "2026-08-09", 1100, shipment_subtotal=850, shipment_tax=100, shipping=50,
    )]
    result = raw_csv_preview(tmp_path, rows)
    assert result["method_matches"]["shipment_subtotal_plus_tax_plus_shipping"]["unique"] == 1


def test_unit_price_formula_match(tmp_path):
    rows = [raw_row(
        "o1", "2026-08-09", 1100, unit_price=450, unit_tax=50, quantity=2,
    )]
    result = raw_csv_preview(tmp_path, rows)
    assert result["method_matches"]["row_unit_price_plus_tax_times_quantity"]["unique"] == 1


def test_multiple_matching_formulas_are_kept_manual(tmp_path):
    rows = [raw_row(
        "o1", "2026-08-09", 1100, shipment_subtotal=900, shipment_tax=100,
        unit_price=900, unit_tax=100,
    )]
    result = raw_csv_preview(tmp_path, rows)
    assert result["final_classification"]["multiple_possible_explanations"] == 1


def test_gift_payment_is_only_a_candidate_without_gift_amount(tmp_path):
    rows = [raw_row("o1", "2026-08-09", 1100, payment="Gift Card; Visa")]
    result = raw_csv_preview(tmp_path, rows)
    assert result["final_classification"]["gift_or_mixed_payment_candidate"] == 1


def test_csv_can_remain_insufficient(tmp_path):
    rows = [raw_row("o1", "2026-08-09", 1100)]
    result = raw_csv_preview(tmp_path, rows)
    assert result["final_classification"]["csv_still_insufficient"] == 1


def test_csv_diagnostics_remain_read_only(tmp_path):
    rows = [raw_row("o1", "2026-08-09", 1100)]
    path = tmp_path / "Order History.csv"
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
    db = FakeDB(
        [import_row("card:1", "2026-08-10", 1000)],
        [amazon_row("o1", "2026-08-09", 1100)],
    )
    AmazonUnmatchedPreview(db).preview(str(path))
    assert db.writes == []
    assert path.exists()


def test_export_contains_only_amount_near_match_and_minimum_fields(tmp_path):
    imports = [
        import_row("card:amount", "2026-08-10", 1000, note="会員=個人情報"),
        import_row("card:date", "2026-08-20", 2000, note="会員=個人情報"),
    ]
    orders = [
        amazon_row("o1", "2026-08-09", 1100),
        amazon_row("o2", "2026-08-01", 2000),
    ]
    db = FakeDB(imports, orders)
    output = tmp_path / "amazon-unmatched-input.json"

    result = export_amazon_unmatched_input(db, output)
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert result["exported"] == 1
    assert len(payload["transactions"]) == 1
    exported = payload["transactions"][0]
    assert set(exported) == {"diagnostic_id", "card_date", "card_amount", "payment_type"}
    assert exported["card_amount"] == 1000
    serialized = output.read_text(encoding="utf-8")
    assert "card:amount" not in serialized
    assert "個人情報" not in serialized
    assert "o1" not in serialized
    assert db.writes == []


def test_transactions_json_and_csv_run_without_sheets(tmp_path, monkeypatch, capsys):
    csv_path = tmp_path / "Order History.csv"
    pd.DataFrame([raw_row(
        "o1", "2026-08-09", 1100, shipment_subtotal=900, shipment_tax=100,
    )]).to_csv(csv_path, index=False, encoding="utf-8-sig")
    json_path = tmp_path / "amazon-unmatched-input.json"
    json_path.write_text(json.dumps({
        "schema_version": 1,
        "transactions": [{
            "diagnostic_id": "a" * 24,
            "card_date": "2026-08-10",
            "card_amount": 1000,
            "payment_type": "一括",
        }],
    }), encoding="utf-8")
    import app.cli as cli
    monkeypatch.setattr(cli, "make", lambda *_: (_ for _ in ()).throw(AssertionError("Sheets used")))
    monkeypatch.setattr(sys, "argv", [
        "app.cli", "amazon-unmatched-preview", "--amazon-csv", str(csv_path),
        "--transactions-json", str(json_path),
    ])

    cli.main()

    output = capsys.readouterr().out
    assert "raw_csv_diagnostics" in output
    assert "shipment_amount_match" in output


def test_exported_transactions_can_drive_csv_diagnostics(tmp_path):
    json_path = tmp_path / "input.json"
    json_path.write_text(json.dumps({
        "schema_version": 1,
        "transactions": [{
            "diagnostic_id": "b" * 24, "card_date": "2026-08-10",
            "card_amount": 1000, "payment_type": "一括",
        }],
    }), encoding="utf-8")
    transactions = load_amazon_unmatched_input(json_path)
    assert len(transactions) == 1
    assert transactions[0].amount == 1000
    assert transactions[0].merchant == "AMAZON.CO.JP"


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        json.dumps({"schema_version": 1, "transactions": [{"card_amount": 1000}]}),
        json.dumps({
            "schema_version": 1,
            "transactions": [{
                "diagnostic_id": "c" * 24, "card_date": "invalid",
                "card_amount": 1000, "payment_type": "一括",
            }],
        }),
        json.dumps({
            "schema_version": 1,
            "transactions": [{
                "diagnostic_id": "d" * 24, "card_date": "2026-08-10",
                "card_amount": 1000, "payment_type": "一括", "merchant": "secret",
            }],
        }),
    ],
)
def test_invalid_or_overbroad_transactions_json_is_rejected(tmp_path, payload):
    path = tmp_path / "bad.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError):
        load_amazon_unmatched_input(path)
