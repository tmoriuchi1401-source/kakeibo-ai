import sys

from app import cli
from app.amazon_cancellation_apply_preview import (
    apply_amazon_cancellation_order_statuses,
    plan_cancellation_apply,
    preview_amazon_cancellation_apply,
)
from app.sheets import SheetsDB


PRIVATE_ORDER_ID = "123-PRIVATE-ORDER-ID"


def _event(*, event_type="cancellation", order_id=PRIVATE_ORDER_ID, quantity=2):
    row = [""] * 24
    row[5] = event_type
    row[6] = order_id
    row[17] = quantity
    return row


def _header(*, order_id=PRIVATE_ORDER_ID, quantity=2, status="ordered"):
    row = [""] * 15
    row[0] = order_id
    row[2] = 9999
    row[4] = quantity
    row[5] = status
    row[6:13] = [1000, "none", 0, 0, 0, 0, 0]
    return row


def _order(*, order_id=PRIVATE_ORDER_ID, quantity=2):
    row = [""] * 15
    row[1] = order_id
    row[5] = quantity
    row[6] = 9999
    return row


class ReadOnlyDB:
    def __init__(self, *, events=None, headers=None, orders=None):
        self.events = list(events or [])
        self.headers = list(headers or [])
        self.orders = list(orders or [])
        self.reads = []

    def get(self, rng):
        self.reads.append(rng)
        return {
            "Amazon注文!A2:O": self.orders,
            "Amazon注文ヘッダ!A2:O": self.headers,
            "Amazonイベント!A2:X": self.events,
        }[rng]

    def __getattr__(self, name):
        if name.startswith(("append", "update", "clear", "ensure", "configure")):
            raise AssertionError(f"write method must not be used: {name}")
        raise AttributeError(name)


class WritableDB(ReadOnlyDB):
    def __init__(self, *, events=None, headers=None, orders=None):
        super().__init__(events=events, headers=headers, orders=orders)
        self.writes = []
        self.stale_event_quantity = None
        self.stale_header_status = None
        self.header_reads = 0
        self.fail_writes = False

    def get(self, rng):
        if rng.startswith("Amazonイベント!A") and rng != "Amazonイベント!A2:X":
            row_number = int(rng.split("!A", 1)[1].split(":", 1)[0])
            row = list(self.events[row_number - 2])
            if self.stale_event_quantity is not None:
                row[17] = self.stale_event_quantity
            return [row]
        if rng == "Amazon注文ヘッダ!A2:O":
            self.header_reads += 1
            if self.stale_header_status is not None and self.header_reads > 1:
                rows = [list(row) for row in self.headers]
                rows[0][5] = self.stale_header_status
                return rows
        return super().get(rng)

    def cancel_amazon_order_header(self, row_number):
        if self.fail_writes:
            raise RuntimeError("write failed")
        self.writes.append(("Amazon注文ヘッダ", f"F{row_number}", "cancelled"))
        self.headers[row_number - 2][5] = "cancelled"


def test_full_order_proposes_only_header_order_status_change():
    plan = plan_cancellation_apply(_event(), [_header()], 2)

    assert plan.disposition == "apply"
    assert plan.scope == "full_order"
    assert len(plan.proposed_changes) == 1
    change = plan.proposed_changes[0]
    assert (change.sheet, change.column) == ("Amazon注文ヘッダ", "Order Status")
    assert (change.current_value, change.proposed_value) == ("ordered", "cancelled")
    rendered = repr(plan.proposed_changes)
    for forbidden in (
        "数量", "商品金額", "Order Amount", "Charged Amount", "Refund Amount",
        "Refund Status", "Shipment Amount", "Gift Card Amount", "Points Amount",
        "Discount Amount", "Last Updated At", "Amazonイベント",
    ):
        assert forbidden not in rendered


def test_already_cancelled_is_noop_without_change():
    plan = plan_cancellation_apply(_event(), [_header(status="cancelled")], 2)

    assert plan.disposition == "noop"
    assert plan.reason == "already_cancelled"
    assert plan.proposed_changes == ()


def test_partial_is_review_without_change():
    plan = plan_cancellation_apply(_event(quantity=1), [_header(quantity=3)], 3)

    assert plan.disposition == "review"
    assert plan.scope == "item_or_partial"
    assert plan.reason == "partial_cancellation_item_unresolved"
    assert plan.proposed_changes == ()


def test_missing_and_excess_quantities_are_blocked_without_change():
    missing = plan_cancellation_apply(_event(quantity=""), [_header()], 2)
    excess = plan_cancellation_apply(_event(quantity=3), [_header()], 2)

    assert missing.reason == "missing_cancellation_quantity"
    assert excess.reason == "cancellation_quantity_exceeds_order_quantity"
    assert missing.disposition == excess.disposition == "blocked"
    assert missing.proposed_changes == excess.proposed_changes == ()


def test_missing_order_id_not_found_and_duplicate_header_are_blocked():
    missing_id = plan_cancellation_apply(_event(order_id=""), [], 2)
    not_found = plan_cancellation_apply(_event(), [], 2)
    duplicate = plan_cancellation_apply(_event(), [_header(), _header()], 2)

    assert missing_id.reason == "missing_order_id"
    assert not_found.reason == "order_not_found"
    assert duplicate.reason == "duplicate_order_header"
    assert all(plan.proposed_changes == () for plan in (missing_id, not_found, duplicate))


def test_invalid_or_missing_order_quantity_is_blocked():
    invalid = plan_cancellation_apply(_event(quantity="1.5"), [_header()], 2)
    missing_order = plan_cancellation_apply(_event(quantity=1), [_header(quantity="")], None)

    assert invalid.reason == "invalid_cancellation_quantity"
    assert missing_order.reason == "missing_order_quantity"


def test_preview_summarizes_reasons_and_never_writes():
    db = ReadOnlyDB(
        events=[
            _event(), _event(), _event(quantity=1), _event(quantity=""),
            _event(order_id=""), _event(event_type="return"),
        ],
        headers=[_header()],
        orders=[_order()],
    )
    db.events[1][6] = "already-cancelled"
    db.headers.append(_header(order_id="already-cancelled", status="cancelled"))
    db.orders.append(_order(order_id="already-cancelled"))

    result = preview_amazon_cancellation_apply(db)

    assert result["cancellation_event_count"] == 5
    assert result["would_cancel_order_count"] == 1
    assert result["noop_already_cancelled_count"] == 1
    assert result["review_item_or_partial_count"] == 1
    assert result["review_unknown_count"] == result["blocked_count"] == 2
    assert result["reason_missing_order_id_count"] == 1
    assert result["reason_missing_cancellation_quantity_count"] == 1
    assert PRIVATE_ORDER_ID not in str(result)
    assert db.reads == [
        "Amazon注文!A2:O", "Amazon注文ヘッダ!A2:O", "Amazonイベント!A2:X",
    ]


def test_incomplete_detail_quantities_are_not_summed_as_order_total():
    incomplete = _order(quantity="")
    db = ReadOnlyDB(
        events=[_event(quantity=1)],
        headers=[_header(quantity="")],
        orders=[_order(quantity=1), incomplete],
    )

    result = preview_amazon_cancellation_apply(db)

    assert result["would_cancel_order_count"] == 0
    assert result["reason_missing_order_quantity_count"] == 1


def test_cli_uses_read_only_sheets_and_prints_summary(monkeypatch, capsys):
    class Settings:
        spreadsheet_id = "private-sheet-id"

        def validate(self, **kwargs):
            assert kwargs == {"need_sheet": True}

    service = object()
    db = object()
    monkeypatch.setattr(cli, "Settings", Settings)
    monkeypatch.setattr(cli, "read_only_sheets_service", lambda: service)
    monkeypatch.setattr(cli, "SheetsDB", lambda spreadsheet_id, service=None: db)
    monkeypatch.setattr(
        cli,
        "preview_amazon_cancellation_apply",
        lambda value: {"cancellation_event_count": int(value is db)},
    )
    monkeypatch.setattr(
        sys, "argv", ["kakeibo", "amazon-cancellation-apply-preview"],
    )

    cli.main()

    assert capsys.readouterr().out.strip() == "{'cancellation_event_count': 1}"


def test_order_status_apply_updates_only_f_cell_and_is_idempotent():
    db = WritableDB(events=[_event()], headers=[_header()], orders=[_order()])

    first = apply_amazon_cancellation_order_statuses(db)
    second = apply_amazon_cancellation_order_statuses(db)

    assert db.writes == [("Amazon注文ヘッダ", "F2", "cancelled")]
    assert first["eligible_cancel_count"] == 1
    assert first["updated_order_status_count"] == 1
    assert first["error_count"] == 0
    assert second["eligible_cancel_count"] == 0
    assert second["updated_order_status_count"] == 0
    assert second["noop_already_cancelled_count"] == 1


def test_order_status_apply_rechecks_stale_cancelled_as_noop():
    db = WritableDB(events=[_event()], headers=[_header()], orders=[_order()])
    db.stale_header_status = "cancelled"

    result = apply_amazon_cancellation_order_statuses(db)

    assert db.writes == []
    assert result["updated_order_status_count"] == 0
    assert result["noop_already_cancelled_count"] == 1
    assert result["reason_stale_already_cancelled_count"] == 1


def test_order_status_apply_rechecks_stale_quantity_and_blocks():
    db = WritableDB(events=[_event()], headers=[_header()], orders=[_order()])
    db.stale_event_quantity = 1

    result = apply_amazon_cancellation_order_statuses(db)

    assert db.writes == []
    assert result["updated_order_status_count"] == 0
    assert result["blocked_count"] == 1
    assert result["reason_stale_partial_cancellation_item_unresolved_count"] == 1


def test_order_status_apply_reports_write_error_without_other_writes():
    db = WritableDB(events=[_event()], headers=[_header()], orders=[_order()])
    db.fail_writes = True

    result = apply_amazon_cancellation_order_statuses(db)

    assert db.writes == []
    assert result["updated_order_status_count"] == 0
    assert result["error_count"] == 1


def test_order_status_apply_cli_requires_flag_before_initializing(monkeypatch):
    monkeypatch.setattr(
        cli, "Settings",
        lambda: (_ for _ in ()).throw(AssertionError("must not initialize")),
    )
    monkeypatch.setattr(
        sys, "argv", ["kakeibo", "amazon-cancellation-order-status-apply"],
    )

    try:
        cli.main()
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("CLI must reject missing --apply")


def test_sheets_writer_updates_only_header_f_cell():
    calls = []

    class Request:
        def execute(self):
            return {}

    class Values:
        def update(self, **kwargs):
            calls.append(kwargs)
            return Request()

    class Spreadsheets:
        def values(self):
            return Values()

    class Service:
        def spreadsheets(self):
            return Spreadsheets()

    SheetsDB("sheet-id", service=Service()).cancel_amazon_order_header(7)

    assert calls == [{
        "spreadsheetId": "sheet-id",
        "range": "Amazon注文ヘッダ!F7",
        "valueInputOption": "RAW",
        "body": {"values": [["cancelled"]]},
    }]
