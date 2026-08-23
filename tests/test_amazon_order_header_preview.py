from copy import deepcopy
import sys

from app import cli
from app.amazon_order_header_preview import preview_amazon_order_headers


def _event(event_type="order", order_id="ORDER-1", **values):
    row = [event_type, order_id, "", "", "", "", "", "", "", "", "", "", ""]
    indexes = {
        "charged_amount": 3, "order_amount": 4, "refund_amount": 5,
        "shipment_amount": 6, "gift_card_amount": 7, "points_amount": 8,
        "discount_amount": 10, "payment_method": 11, "item_count": 12,
    }
    for key, value in values.items():
        row[indexes[key]] = value
    return row


def _header(order_id, order_amount="", refund_status="none"):
    return [
        order_id, "", order_amount, "", "", "ordered", "", refund_status,
        "", "", "", "", "", "gmail", "old",
    ]


class ReadOnlyDB:
    def __init__(self, events, headers=(), *, header_sheet_exists=True):
        self.events = events
        self.headers = list(headers)
        self.header_sheet_exists = header_sheet_exists
        self.header_reads = 0
        self.write_calls = []

    def sheet_titles(self):
        return ["Amazonイベント"] + (
            ["Amazon注文ヘッダ"] if self.header_sheet_exists else []
        )

    def amazon_order_creation_event_rows(self):
        return deepcopy(self.events)

    def amazon_order_header_rows(self):
        self.header_reads += 1
        if not self.header_sheet_exists:
            raise AssertionError("missing header sheet must not be read")
        return [(index, deepcopy(row)) for index, row in enumerate(self.headers, 2)]

    def update_amazon_order_headers(self, rows):
        self.write_calls.append(rows)
        raise AssertionError("preview must not write")


def test_preview_is_read_only_and_counts_candidates_and_existing_overlaps():
    db = ReadOnlyDB([
        _event(order_id="NEW-1"),
        _event(order_id="EXISTING-1"),
        _event("payment", order_id="EXISTING-1", charged_amount=100),
    ], [_header("EXISTING-1"), _header("HEADER-ONLY")])

    result = preview_amazon_order_headers(db)

    assert result["amazon_order_header_sheet_exists"] is True
    assert result["new_header_candidates"] == 1
    assert result["existing_headers"] == 2
    assert result["existing_order_id_overlaps"] == 1
    assert result["recalculation_targets"] == 1
    assert result["samples"]["new_header_candidate_order_ids"] == ["NEW-1"]
    assert db.write_calls == []


def test_preview_continues_without_amazon_order_header_sheet_and_never_writes():
    db = ReadOnlyDB([
        _event(order_id="NEW-1"),
        _event(order_id="NEW-1"),
        _event(order_id="NEW-2"),
    ], header_sheet_exists=False)

    result = preview_amazon_order_headers(db)

    assert result["amazon_order_header_sheet_exists"] is False
    assert result["existing_headers"] == 0
    assert result["new_header_candidates"] == 2
    assert result["samples"]["new_header_candidate_order_ids"] == ["NEW-1", "NEW-2"]
    assert db.header_reads == 0
    assert db.write_calls == []


def test_preview_counts_statuses_refunds_and_calculated_amounts():
    db = ReadOnlyDB([
        _event("order", order_id="ORDERED", order_amount=100),
        _event("order", order_id="SHIPPED", order_amount=1000),
        _event("shipment", order_id="SHIPPED", shipment_amount=800),
        _event("payment", order_id="SHIPPED", charged_amount=400),
        _event("payment", order_id="SHIPPED", charged_amount=600),
        _event("refund", order_id="SHIPPED", refund_amount=200),
        _event("order", order_id="DELIVERED", order_amount=500),
        _event("shipment", order_id="DELIVERED", shipment_amount=500),
        _event("delivery", order_id="DELIVERED"),
        _event("refund", order_id="DELIVERED", refund_amount=200),
        _event("refund", order_id="DELIVERED", refund_amount=300),
    ])

    result = preview_amazon_order_headers(db)

    assert result["order_statuses"] == {
        "ordered": 1, "partially_shipped": 1, "delivered": 1,
    }
    assert result["refund_statuses"] == {"none": 1, "partial": 1, "full": 1}
    assert result["calculated_amount_order_ids"] == {
        "charged": 1, "refund": 2, "shipment": 2,
    }


def test_preview_reports_each_conflict_field_and_missing_order_id_anomalies():
    first = _event(
        order_amount=100, payment_method="Visa", item_count=1,
        gift_card_amount=10, points_amount=20, discount_amount=30,
    )
    second = _event(
        order_amount=200, payment_method="Mastercard", item_count=2,
        gift_card_amount=11, points_amount=21, discount_amount=31,
    )
    db = ReadOnlyDB([
        first, second, _event(order_id="", order_amount=""),
        _event("payment", order_id=None, charged_amount=100),
    ])

    result = preview_amazon_order_headers(db)

    assert result["amazon_events"] == 4
    assert result["order_events"] == 3
    assert result["events_with_order_id"] == 2
    assert result["events_without_order_id"] == 2
    assert result["unique_order_ids"] == 1
    assert result["conflicts"] == 6
    assert result["anomalies"] == {
        "events_without_order_id": 2,
        "order_events_without_order_id": 1,
        "order_events_without_order_amount": 1,
        "conflicts_by_field": {
            "order_amount": 1, "payment_method": 1, "item_count": 1,
            "gift_card_amount": 1, "points_amount": 1, "discount_amount": 1,
        },
    }
    assert result["samples"]["conflict_order_ids"] == ["ORDER-1"]
    assert result["samples"]["missing_order_id_event_types"] == ["order", "payment"]


def test_preview_samples_are_limited_to_five_and_summary_is_order_independent():
    events = []
    for index in range(7):
        order_id = f"ORDER-{index}"
        events.extend([
            _event(order_id=order_id, order_amount=100),
            _event(order_id=order_id, order_amount=200),
        ])
    events.extend(_event("unknown", order_id="") for _ in range(7))

    forward = preview_amazon_order_headers(ReadOnlyDB(events))
    reverse = preview_amazon_order_headers(ReadOnlyDB(list(reversed(events))))

    assert forward == reverse
    assert len(forward["samples"]["new_header_candidate_order_ids"]) == 5
    assert len(forward["samples"]["conflict_order_ids"]) == 5
    assert len(forward["samples"]["missing_order_id_event_types"]) == 5


def test_cli_uses_read_only_sheets_service_and_prints_preview(monkeypatch, capsys):
    read_service = object()
    db = object()

    class FakeSettings:
        spreadsheet_id = "sheet-id"

        def validate(self, **kwargs):
            assert kwargs == {"need_sheet": True}

    monkeypatch.setattr(cli, "Settings", FakeSettings)
    monkeypatch.setattr(cli, "read_only_sheets_service", lambda: read_service)
    monkeypatch.setattr(
        cli, "SheetsDB",
        lambda spreadsheet_id, service: db
        if spreadsheet_id == "sheet-id" and service is read_service else None,
    )
    monkeypatch.setattr(cli, "preview_amazon_order_headers", lambda value: {"ok": value is db})
    monkeypatch.setattr(sys, "argv", ["kakeibo", "amazon-order-header-preview"])

    cli.main()

    assert capsys.readouterr().out.strip() == "{'ok': True}"
