from copy import deepcopy

from app.amazon_order_header_recalculation import recalculate_amazon_order_headers
from app.sheets import SheetsDB


NOW = "2026-08-23T12:34:56+00:00"


def _event(event_type="order", order_id="ORDER-1", **values):
    row = [event_type, order_id, "", "", "", "", "", "", "", "", "", "", ""]
    indexes = {
        "event_date": 2, "charged_amount": 3, "order_amount": 4,
        "refund_amount": 5, "shipment_amount": 6, "gift_card_amount": 7,
        "points_amount": 8, "discount_amount": 10, "payment_method": 11,
        "item_count": 12,
    }
    for key, value in values.items():
        row[indexes[key]] = value
    return row


def _header(order_id="ORDER-1", **values):
    row = [order_id, "", "", "", "", "ordered", "", "none", "", "", "", "", "", "gmail", "old"]
    indexes = {
        "order_date": 1, "order_amount": 2, "payment_method": 3,
        "item_count": 4, "order_status": 5, "charged_amount": 6,
        "refund_status": 7, "refund_amount": 8, "shipment_amount": 9,
        "gift_card_amount": 10, "points_amount": 11, "discount_amount": 12,
        "source": 13, "last_updated_at": 14,
    }
    for key, value in values.items():
        row[indexes[key]] = value
    return row


class FakeDB:
    def __init__(self, events, headers=None):
        self.events = events
        self.headers = headers or [_header()]
        self.writes = []

    def amazon_order_creation_event_rows(self):
        return deepcopy(self.events)

    def amazon_order_header_rows(self):
        return [(index, deepcopy(row)) for index, row in enumerate(self.headers, 2)]

    def update_amazon_order_headers(self, rows):
        self.writes.append(deepcopy(rows))
        for row_num, row in rows:
            self.headers[row_num - 2] = deepcopy(row)


def _run(db):
    return recalculate_amazon_order_headers(db, timestamp_factory=lambda: NOW)


def test_status_uses_highest_state_regardless_of_event_order():
    events = [_event("shipment"), _event("order"), _event("delivery")]
    results = []
    for ordered_events in (events, list(reversed(events))):
        db = FakeDB(ordered_events)
        _run(db)
        results.append(db.headers[0])
    assert results[0] == results[1]
    assert results[0][5] == "delivered"


def test_order_only_and_shipment_statuses():
    ordered = FakeDB([_event("order")], [_header(order_status="ordered")])
    shipped = FakeDB([_event("order"), _event("shipment")])
    _run(ordered)
    _run(shipped)
    assert ordered.headers[0][5] == "ordered"
    assert shipped.headers[0][5] == "partially_shipped"


def test_cancelled_status_does_not_regress_to_ordered():
    db = FakeDB([_event("order")], [_header(order_status="cancelled")])

    summary = _run(db)

    assert db.headers[0][5] == "cancelled"
    assert summary["updated"] == 0


def test_non_cancelled_status_precedence_is_unchanged():
    cases = (
        ([_event("order")], "ordered"),
        ([_event("order"), _event("shipment")], "partially_shipped"),
        ([_event("order"), _event("shipment"), _event("delivery")], "delivered"),
    )

    for events, expected in cases:
        db = FakeDB(events, [_header(order_status="ordered")])
        _run(db)
        assert db.headers[0][5] == expected


def test_single_item_cancelled_order_like_current_data_stays_cancelled():
    order_id = "249-4045234-9353402"
    db = FakeDB(
        [_event("order", order_id=order_id, order_amount=1499, item_count=1)],
        [_header(
            order_id=order_id, order_amount=1499, item_count=1,
            order_status="cancelled",
        )],
    )

    _run(db)

    assert db.headers[0][0] == order_id
    assert db.headers[0][4:6] == [1, "cancelled"]


def test_old_shipment_added_after_delivery_does_not_regress():
    db = FakeDB([_event("order"), _event("delivery")])
    _run(db)
    db.events.append(_event("shipment"))
    second = _run(db)
    assert db.headers[0][5] == "delivered"
    assert second["updated"] == 0


def test_amounts_are_summed_and_refund_status_is_recalculated():
    db = FakeDB([
        _event("order", order_amount=1000),
        _event("payment", charged_amount=400),
        _event("payment", charged_amount=600),
        _event("refund", refund_amount=200),
        _event("refund", refund_amount=300),
        _event("shipment", shipment_amount=300),
        _event("shipment", shipment_amount=700),
    ])
    _run(db)
    assert db.headers[0][6:10] == [1000, "partial", 500, 1000]


def test_full_and_unknown_order_amount_refund_statuses():
    full = FakeDB([_event("order", order_amount=500), _event("refund", refund_amount=500)])
    partial = FakeDB([_event("order"), _event("refund", refund_amount=1)])
    _run(full)
    _run(partial)
    assert full.headers[0][7] == "full"
    assert partial.headers[0][7] == "partial"


def test_no_refund_preserves_existing_refund_information():
    db = FakeDB([_event("order")], [_header(refund_status="full", refund_amount=100)])
    _run(db)
    assert db.headers[0][7:9] == ["full", 100]


def test_order_fields_and_payment_fallback_are_aggregated():
    db = FakeDB([
        _event("order", event_date="2026-08-22", order_amount=3000,
               gift_card_amount=500, points_amount=100, discount_amount=200,
               item_count=2),
        _event("order", event_date="2026-08-20", order_amount=3000,
               gift_card_amount=500, points_amount=100, discount_amount=200,
               item_count=2),
        _event("payment", payment_method="Visa"),
    ])
    _run(db)
    assert db.headers[0][1:5] == ["2026-08-20", 3000, "Visa", 2]
    assert db.headers[0][10:13] == [500, 100, 200]


def test_conflicts_preserve_existing_values_and_are_counted():
    db = FakeDB([
        _event("order", order_amount=100, payment_method="Visa", item_count=1),
        _event("order", order_amount=200, payment_method="Mastercard", item_count=2),
    ], [_header(order_amount=999, payment_method="existing", item_count=9)])
    summary = _run(db)
    assert db.headers[0][2:5] == [999, "existing", 9]
    assert summary["conflicts"] == 3


def test_conflicting_order_amount_uses_preserved_header_value_for_full_refund():
    db = FakeDB([
        _event("order", order_amount=1000),
        _event("order", order_amount=1100),
        _event("refund", refund_amount=1000),
    ], [_header(order_amount=1000)])
    _run(db)
    assert db.headers[0][2] == 1000
    assert db.headers[0][7] == "full"


def test_cancellation_never_generates_cancelled_or_shipped():
    cancellation = FakeDB([_event("order"), _event("cancellation")])
    shipment = FakeDB([_event("order"), _event("shipment")])
    _run(cancellation)
    _run(shipment)
    assert cancellation.headers[0][5] == "ordered"
    assert shipment.headers[0][5] != "shipped"


def test_unchanged_does_not_write_or_change_timestamp():
    db = FakeDB([_event("order")])
    summary = _run(db)
    assert summary == {
        "orders": 1, "updated": 0, "unchanged": 1,
        "skipped_missing_order_id": 0, "conflicts": 0,
    }
    assert db.writes == []
    assert db.headers[0][14] == "old"


def test_changed_row_gets_injected_timestamp_and_preserves_source():
    db = FakeDB([_event("order"), _event("shipment")], [_header(source="mixed")])
    summary = _run(db)
    assert summary["updated"] == 1
    assert db.headers[0][13:] == ["mixed", NOW]
    assert db.writes[0][0][0] == 2


def test_missing_amount_events_preserve_mixed_source_supplemental_amounts():
    db = FakeDB([_event("order")], [_header(
        source="mixed", charged_amount=1000, refund_status="partial",
        refund_amount=200, shipment_amount=800,
    )])
    summary = _run(db)
    assert db.headers[0][6:10] == [1000, "partial", 200, 800]
    assert db.headers[0][13] == "mixed"
    assert summary["updated"] == 0


def test_missing_order_id_is_skipped_without_inference():
    db = FakeDB([_event(order_id=""), _event(order_id=None), _event()])
    summary = _run(db)
    assert summary["skipped_missing_order_id"] == 2
    assert summary["orders"] == 1


def test_event_without_existing_header_is_not_counted_as_unchanged():
    db = FakeDB([_event(order_id="ORDER-2")], [_header(order_id="ORDER-1")])
    summary = _run(db)
    assert summary["orders"] == 1
    assert summary["updated"] == 0
    assert summary["unchanged"] == 0


def test_sheets_methods_read_and_batch_update_only_order_headers():
    db = SheetsDB("sheet-id", service=object())
    reads = []
    writes = []
    db.get = lambda rng: reads.append(rng) or [["ORDER-1"]]
    db.update_rows = lambda sheet, rows: writes.append((sheet, rows))
    assert db.amazon_order_header_rows() == [(2, ["ORDER-1"])]
    db.update_amazon_order_headers([(2, _header())])
    assert reads == ["Amazon注文ヘッダ!A2:O"]
    assert writes == [("Amazon注文ヘッダ", [(2, _header())])]
