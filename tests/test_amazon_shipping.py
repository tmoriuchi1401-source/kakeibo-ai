import csv

from app.amazon_shipping import AmazonShippingBackfillPipeline, plan_shipping_backfill


HEADERS = [
    "Order ID", "ASIN", "Ship Date", "Original Quantity",
    "Carrier Name & Tracking Number",
]


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=HEADERS)
        writer.writeheader()
        writer.writerows(rows)


def amazon_row(order_id="ORDER-1", asin="ASIN-1", *, ship_date="", shipment_count=""):
    return [
        f"{order_id}|{asin}", order_id, asin, "2026-08-18", "商品", 1, 1000,
        "Visa", "日用品", "雑貨", "baseline", "hash", "2026-08-20",
        ship_date, shipment_count,
    ]


class MemoryDB:
    def __init__(self, rows):
        self.rows = [list(row) for row in rows]
        self.updated = []
        self.other_sheets = {"支出明細": [["expense"]], "取込データ": [["import"]]}

    def get(self, rng):
        assert rng == "Amazon注文!A2:O"
        return [list(row) for row in self.rows]

    def update_shipping_fields(self, updates):
        self.updated.extend((number, list(row)) for number, row in updates)
        for number, row in updates:
            self.rows[number - 2][13:15] = list(row[13:15])


def test_preview_is_read_only_and_matches_by_order_and_asin(tmp_path):
    path = tmp_path / "Order History.csv"
    write_csv(path, [{
        "Order ID": "ORDER-1", "ASIN": "ASIN-1",
        "Ship Date": "2026-08-19T00:00:00Z", "Original Quantity": 1,
        "Carrier Name & Tracking Number": "carrier-hidden",
    }])
    db = MemoryDB([amazon_row()])
    before = [list(row) for row in db.rows]
    result = AmazonShippingBackfillPipeline(db).preview(str(path))
    assert result == {
        "csv_rows": 1, "matched_amazon_rows": 1,
        "would_update_ship_date": 1, "would_update_shipment_count": 1,
        "ambiguous": 0, "unmatched": 0,
    }
    assert db.rows == before
    assert db.updated == []


def test_apply_changes_only_shipping_fields_and_is_idempotent(tmp_path):
    path = tmp_path / "Order History.csv"
    write_csv(path, [{
        "Order ID": "ORDER-1", "ASIN": "ASIN-1",
        "Ship Date": "2026-08-19T00:00:00Z", "Original Quantity": 1,
        "Carrier Name & Tracking Number": "carrier-hidden",
    }])
    db = MemoryDB([amazon_row()])
    original_prefix = list(db.rows[0][:13])
    first = AmazonShippingBackfillPipeline(db).apply(str(path))
    assert first["updated_rows"] == 1
    assert db.rows[0][:13] == original_prefix
    assert db.rows[0][13:] == ["2026-08-19", 1]
    assert db.other_sheets == {"支出明細": [["expense"]], "取込データ": [["import"]]}
    second = AmazonShippingBackfillPipeline(db).apply(str(path))
    assert second["updated_rows"] == 0
    assert second["would_update_ship_date"] == 0
    assert second["would_update_shipment_count"] == 0


def test_shipment_count_uses_distinct_shipping_groups(tmp_path):
    path = tmp_path / "Order History.csv"
    write_csv(path, [
        {"Order ID": "ORDER-1", "ASIN": "ASIN-1", "Ship Date": "2026-08-19",
         "Original Quantity": 1, "Carrier Name & Tracking Number": "tracking-a"},
        {"Order ID": "ORDER-1", "ASIN": "ASIN-2", "Ship Date": "2026-08-20",
         "Original Quantity": 1, "Carrier Name & Tracking Number": "tracking-b"},
    ])
    plan = plan_shipping_backfill(str(path), [
        amazon_row("ORDER-1", "ASIN-1"), amazon_row("ORDER-1", "ASIN-2"),
    ])
    assert plan.summary["matched_amazon_rows"] == 2
    assert {row[14] for _, row in plan.updates} == {2}


def test_ambiguous_and_unmatched_rows_are_not_updated(tmp_path):
    path = tmp_path / "Order History.csv"
    write_csv(path, [
        {"Order ID": "ORDER-DUP", "ASIN": "ASIN", "Ship Date": "2026-08-19",
         "Original Quantity": 1, "Carrier Name & Tracking Number": "a"},
        {"Order ID": "ORDER-DUP", "ASIN": "ASIN", "Ship Date": "2026-08-20",
         "Original Quantity": 1, "Carrier Name & Tracking Number": "b"},
        {"Order ID": "ORDER-MISSING", "ASIN": "ASIN", "Ship Date": "2026-08-20",
         "Original Quantity": 1, "Carrier Name & Tracking Number": "c"},
    ])
    plan = plan_shipping_backfill(str(path), [amazon_row("ORDER-DUP", "ASIN")])
    assert plan.summary["ambiguous"] == 1
    assert plan.summary["unmatched"] == 1
    assert plan.updates == ()


def test_duplicate_sheet_key_is_ambiguous(tmp_path):
    path = tmp_path / "Order History.csv"
    write_csv(path, [{
        "Order ID": "ORDER-1", "ASIN": "ASIN-1", "Ship Date": "2026-08-19",
        "Original Quantity": 1, "Carrier Name & Tracking Number": "a",
    }])
    plan = plan_shipping_backfill(str(path), [amazon_row(), amazon_row()])
    assert plan.summary["ambiguous"] == 1
    assert plan.updates == ()
