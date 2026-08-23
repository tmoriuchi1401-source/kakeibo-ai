from app.amazon_order_header_creation import create_amazon_order_headers
from app.sheets import HEADERS, SheetsDB


AMAZON_EVENT_HEADERS = [
    "イベントID", "Gmail Message ID", "RFC Message-ID", "Thread ID", "Source Hash",
    "Event Type", "Order ID", "Event Date", "Charged Amount", "Order Amount",
    "Refund Amount", "Shipment Amount", "Gift Card Amount", "Points Amount",
    "Coupon Amount", "Discount Amount", "Payment Method", "Item Count", "Parse Status",
    "Match Status", "Apply Status", "Parser Version", "Imported At", "Last Parsed At",
]
AMAZON_ORDER_HEADERS = [
    "Amazonキー", "Order ID", "ASIN", "注文日", "商品名", "数量", "商品金額", "支払方法",
    "大カテゴリ", "小カテゴリ", "備考", "データハッシュ", "最終取込日時", "発送日", "発送数",
]


def _event(
    event_type="order",
    order_id="ORDER-1",
    event_date="2026-08-23",
    order_amount=3000,
    gift_card_amount=500,
    points_amount=100,
    discount_amount=200,
    payment_method="Visa",
    item_count=2,
):
    # Amazonイベント F:R
    return [
        event_type, order_id, event_date, 9999, order_amount, 8888, 7777,
        gift_card_amount, points_amount, 6666, discount_amount, payment_method,
        item_count,
    ]


class FakeDB:
    def __init__(self, events, existing_ids=()):
        self.events = events
        self.header_ids = set(existing_ids)
        self.read_counts = {"header_ids": 0, "events": 0}
        self.append_calls = []

    def amazon_order_header_ids(self):
        self.read_counts["header_ids"] += 1
        return set(self.header_ids)

    def amazon_order_creation_event_rows(self):
        self.read_counts["events"] += 1
        return self.events

    def append(self, sheet, rows):
        self.append_calls.append((sheet, rows))
        self.header_ids.update(row[0] for row in rows)


def _run(db):
    return create_amazon_order_headers(
        db, timestamp_factory=lambda: "2026-08-23T12:34:56+00:00"
    )


def test_new_order_event_creates_one_header_with_mapped_values():
    db = FakeDB([_event()])

    summary = _run(db)

    assert summary == {
        "total_order_events": 1,
        "created": 1,
        "skipped_existing": 0,
        "skipped_missing_order_id": 0,
    }
    assert db.append_calls == [("Amazon注文ヘッダ", [[
        "ORDER-1", "2026-08-23", 3000, "Visa", 2, "ordered", "", "none", "",
        "", 500, 100, 200, "gmail", "2026-08-23T12:34:56+00:00",
    ]])]
    assert db.read_counts == {"header_ids": 1, "events": 1}


def test_existing_order_id_is_skipped_without_write():
    db = FakeDB([_event()], existing_ids={"ORDER-1"})

    assert _run(db) == {
        "total_order_events": 1,
        "created": 0,
        "skipped_existing": 1,
        "skipped_missing_order_id": 0,
    }
    assert db.append_calls == []


def test_missing_order_id_is_skipped_and_non_order_event_is_ignored():
    db = FakeDB([_event(order_id=None), _event(event_type="shipment")])

    assert _run(db) == {
        "total_order_events": 1,
        "created": 0,
        "skipped_existing": 0,
        "skipped_missing_order_id": 1,
    }
    assert db.append_calls == []


def test_duplicate_order_events_create_one_row_in_one_append():
    db = FakeDB([_event(), _event(order_amount=4000), _event(order_id="ORDER-2")])

    summary = _run(db)

    assert summary == {
        "total_order_events": 3,
        "created": 2,
        "skipped_existing": 1,
        "skipped_missing_order_id": 0,
    }
    assert len(db.append_calls) == 1
    assert [row[0] for row in db.append_calls[0][1]] == ["ORDER-1", "ORDER-2"]


def test_rerun_does_not_create_duplicate_header():
    db = FakeDB([_event()])

    first = _run(db)
    second = _run(db)

    assert first["created"] == 1
    assert second == {
        "total_order_events": 1,
        "created": 0,
        "skipped_existing": 1,
        "skipped_missing_order_id": 0,
    }
    assert len(db.append_calls) == 1


def test_missing_values_become_empty_cells_without_inference():
    db = FakeDB([_event(
        event_date=None,
        order_amount="",
        gift_card_amount=None,
        points_amount="",
        discount_amount=None,
        payment_method="",
        item_count=None,
    )])

    _run(db)

    assert db.append_calls[0][1][0] == [
        "ORDER-1", "", "", "", "", "ordered", "", "none", "", "", "", "",
        "", "gmail", "2026-08-23T12:34:56+00:00",
    ]


def test_existing_amazon_schemas_are_unchanged():
    assert HEADERS["Amazonイベント"] == AMAZON_EVENT_HEADERS
    assert HEADERS["Amazon注文"] == AMAZON_ORDER_HEADERS


def test_dedicated_reads_use_only_header_ids_and_creation_event_columns():
    db = SheetsDB("sheet-id", service=object())
    calls = []
    db.get = lambda rng: calls.append(rng) or (
        [[" ORDER-1 "], [""], ["ORDER-2"]]
        if rng == "Amazon注文ヘッダ!A2:A"
        else [["order", "ORDER-3"]]
    )

    assert db.amazon_order_header_ids() == {"ORDER-1", "ORDER-2"}
    assert db.amazon_order_creation_event_rows() == [["order", "ORDER-3"]]
    assert calls == ["Amazon注文ヘッダ!A2:A", "Amazonイベント!F2:R"]
