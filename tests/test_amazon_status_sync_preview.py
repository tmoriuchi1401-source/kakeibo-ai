import sys

from app import cli
from app.amazon_review import AMAZON_REVIEW_HEADERS
from app.amazon_status_sync_preview import preview_amazon_status_sync
from app.sheets import HEADERS


PRIVATE_ORDER_ID = "123-PRIVATE-ORDER-ID"
PRIVATE_PRODUCT = "private product name"


def _event(event_type="cancellation", order_id=PRIVATE_ORDER_ID, item_count=2):
    row = [""] * 24
    row[0] = "private-event-id"
    row[1] = "private-gmail-message-id"
    row[3] = "private-gmail-thread-id"
    row[4] = "private-source-hash-full-value"
    row[5] = event_type
    row[6] = order_id
    row[17] = item_count
    return row


def _header(order_id=PRIVATE_ORDER_ID, item_count=2, status="ordered"):
    row = [""] * 15
    row[0] = order_id
    row[4] = item_count
    row[5] = status
    return row


def _order(order_id=PRIVATE_ORDER_ID, quantity=2):
    row = [""] * 15
    row[0] = "private-amazon-key"
    row[1] = order_id
    row[4] = PRIVATE_PRODUCT
    row[5] = quantity
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
            "Amazonイベント!A2:X": self.events,
            "Amazon注文!A2:O": self.orders,
            "Amazon注文ヘッダ!A2:O": self.headers,
        }[rng]

    def __getattr__(self, name):
        if name.startswith(("append", "update", "clear", "ensure", "configure")):
            raise AssertionError(f"write method must not be used: {name}")
        raise AttributeError(name)


def test_ignores_non_cancellation_events():
    result = preview_amazon_status_sync(ReadOnlyDB(
        events=[_event("order"), _event("return"), _event("refund")],
        headers=[_header()], orders=[_order()],
    ))

    assert result["cancellation_events"] == 0
    assert sum(result.values()) == 0


def test_unique_order_id_match_and_full_order_plan():
    result = preview_amazon_status_sync(ReadOnlyDB(
        events=[_event(item_count=2)], headers=[_header(item_count=2)],
        orders=[_order(quantity=2)],
    ))

    assert result["order_id_match"] == 1
    assert result["scope_full_order"] == 1
    assert result["would_cancel_order"] == 1
    assert result["would_require_review"] == 0


def test_order_id_not_found_requires_review():
    result = preview_amazon_status_sync(ReadOnlyDB(events=[_event()]))

    assert result["order_id_not_found"] == 1
    assert result["scope_unknown"] == 1
    assert result["would_require_review"] == 1


def test_order_detail_match_without_header_requires_review():
    result = preview_amazon_status_sync(ReadOnlyDB(
        events=[_event(item_count=2)], orders=[_order(quantity=2)],
    ))

    assert result["order_id_match"] == 1
    assert result["order_id_not_found"] == 0
    assert result["missing_order_header"] == 1
    assert result["scope_full_order"] == 1
    assert result["would_require_review"] == 1
    assert result["would_cancel_order"] == 0


def test_multiple_header_matches_require_review():
    result = preview_amazon_status_sync(ReadOnlyDB(
        events=[_event()], headers=[_header(), _header()], orders=[_order()],
    ))

    assert result["multiple_order_matches"] == 1
    assert result["order_id_match"] == 0
    assert result["would_require_review"] == 1


def test_partial_item_count_plans_item_or_partial():
    result = preview_amazon_status_sync(ReadOnlyDB(
        events=[_event(item_count=1)], headers=[_header(item_count=3)],
        orders=[_order(quantity=3)],
    ))

    assert result["scope_item_or_partial"] == 1
    assert result["would_cancel_item_or_partial"] == 1


def test_detail_quantity_is_safe_fallback_for_scope():
    result = preview_amazon_status_sync(ReadOnlyDB(
        events=[_event(item_count=2)], headers=[_header(item_count="")],
        orders=[_order(quantity=2)],
    ))

    assert result["scope_full_order"] == 1
    assert result["would_cancel_order"] == 1


def test_unknown_scope_requires_review():
    result = preview_amazon_status_sync(ReadOnlyDB(
        events=[_event(item_count="")], headers=[_header(item_count="")],
    ))

    assert result["order_id_match"] == 1
    assert result["scope_unknown"] == 1
    assert result["would_require_review"] == 1


def test_already_cancelled_is_noop():
    result = preview_amazon_status_sync(ReadOnlyDB(
        events=[_event()], headers=[_header(status="cancelled")], orders=[_order()],
    ))

    assert result["would_noop_already_cancelled"] == 1
    assert result["would_cancel_order"] == 0
    assert result["would_require_review"] == 0


def test_missing_order_id_requires_review_without_inference():
    result = preview_amazon_status_sync(ReadOnlyDB(
        events=[_event(order_id="")], headers=[_header()], orders=[_order()],
    ))

    assert result["missing_order_id"] == 1
    assert result["order_id_not_found"] == 0
    assert result["would_require_review"] == 1


def test_reads_only_expected_ranges_and_never_writes():
    db = ReadOnlyDB(events=[_event()], headers=[_header()], orders=[_order()])

    preview_amazon_status_sync(db)

    assert db.reads == [
        "Amazon注文!A2:O", "Amazon注文ヘッダ!A2:O", "Amazonイベント!A2:X",
    ]


def test_summary_contains_no_private_source_data():
    result = preview_amazon_status_sync(ReadOnlyDB(
        events=[_event()], headers=[_header()], orders=[_order()],
    ))
    rendered = str(result)

    for private_value in (
        PRIVATE_ORDER_ID, PRIVATE_PRODUCT, "private-gmail-message-id",
        "private-gmail-thread-id", "private-source-hash-full-value",
    ):
        assert private_value not in rendered


def test_existing_review_schema_is_not_changed():
    assert len(AMAZON_REVIEW_HEADERS) == 14
    assert "Amazon要確認" not in HEADERS


def test_cli_uses_read_only_sheets_and_prints_summary(monkeypatch, capsys):
    class Settings:
        spreadsheet_id = "private-sheet-id"

        def validate(self, **kwargs):
            assert kwargs == {"need_sheet": True}

    service = object()
    db = object()
    monkeypatch.setattr(cli, "Settings", Settings)
    monkeypatch.setattr(cli, "read_only_sheets_service", lambda: service)
    monkeypatch.setattr(
        cli, "SheetsDB",
        lambda spreadsheet_id, service=None: db
        if spreadsheet_id == "private-sheet-id" and service is not None else None,
    )
    monkeypatch.setattr(
        cli, "preview_amazon_status_sync",
        lambda value: {"cancellation_events": 1} if value is db else {},
    )
    monkeypatch.setattr(sys, "argv", ["kakeibo", "amazon-status-sync-preview"])

    cli.main()

    assert capsys.readouterr().out.strip() == "{'cancellation_events': 1}"
