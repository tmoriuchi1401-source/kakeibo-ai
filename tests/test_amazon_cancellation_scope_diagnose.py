import base64
from email.message import EmailMessage
import sys

from app import cli
from app.amazon_cancellation_scope_diagnose import diagnose_amazon_cancellation_scopes


PRIVATE_ORDER_ID = "123-1234567-1234567"
PRIVATE_PRODUCT = "private product name"


def _event(*, order_id=PRIVATE_ORDER_ID, item_count="", event_type="cancellation"):
    row = [""] * 24
    row[:8] = [
        "private-event", "private-gmail", "private-rfc", "private-thread",
        "private-source-hash", event_type, order_id, "2026-08-24",
    ]
    row[17] = item_count
    return row


def _header(*, order_id=PRIVATE_ORDER_ID, item_count=2):
    row = [""] * 15
    row[0] = order_id
    row[4] = item_count
    row[5] = "ordered"
    return row


def _order(*, order_id=PRIVATE_ORDER_ID, product=PRIVATE_PRODUCT, quantity=1):
    row = [""] * 15
    row[0] = "private-key"
    row[1] = order_id
    row[4] = product
    row[5] = quantity
    return row


def _raw(body=f"対象商品: {PRIVATE_PRODUCT}\n数量: 1\n金額: 1200円"):
    message = EmailMessage()
    message["Subject"] = "private cancellation subject"
    message["From"] = "private@example.com"
    message.set_content(body)
    return message.as_bytes()


class Request:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class Messages:
    def __init__(self, raw):
        self.raw = raw

    def get(self, **kwargs):
        encoded = base64.urlsafe_b64encode(self.raw).decode().rstrip("=")
        return Request({
            "id": kwargs["id"], "threadId": "private-thread", "raw": encoded,
        })

    def modify(self, **kwargs):
        raise AssertionError("Gmail write must not be used")


class Service:
    def __init__(self, raw=None):
        self.api = Messages(raw or _raw())

    def users(self):
        return self

    def messages(self):
        return self.api


class ReadOnlyDB:
    def __init__(self, *, events=None, headers=None, orders=None):
        self.events = list(events or [])
        self.headers = list(headers or [])
        self.orders = list(orders or [])

    def amazon_event_rows(self):
        return [(number, list(row)) for number, row in enumerate(self.events, 2)]

    def get(self, rng):
        return {
            "Amazon注文!A2:O": self.orders,
            "Amazon注文ヘッダ!A2:O": self.headers,
        }[rng]

    def __getattr__(self, name):
        if name.startswith(("append", "update", "clear", "ensure", "configure")):
            raise AssertionError(f"write method must not be used: {name}")
        raise AttributeError(name)


def _diagnose(*, events=None, headers=None, orders=None, raw=None):
    return diagnose_amazon_cancellation_scopes(
        Service(raw), ReadOnlyDB(events=events, headers=headers, orders=orders),
        thread_fetcher=lambda *_: [],
    )


def test_missing_order_id_and_non_cancellation_are_excluded():
    result = _diagnose(
        events=[_event(order_id=""), _event(event_type="return")],
        headers=[_header()], orders=[_order()],
    )

    assert result["matched_cancellation_events"] == 0
    assert result["scope_unknown_events"] == 0


def test_only_unique_order_match_is_counted():
    result = _diagnose(
        events=[_event()], headers=[_header(), _header()], orders=[_order()],
    )

    assert result["matched_cancellation_events"] == 0


def test_known_full_scope_is_not_an_unknown_target_and_quantity_equal_is_visible():
    result = _diagnose(
        events=[_event(item_count=2)], headers=[_header(item_count=2)],
        orders=[_order(quantity=2)],
    )

    assert result["matched_cancellation_events"] == 1
    assert result["scope_unknown_events"] == 0
    assert result["quantity_equals_order_total"] == 1


def test_known_partial_scope_is_not_target_and_quantity_less_is_visible():
    result = _diagnose(
        events=[_event(item_count=1)], headers=[_header(item_count=2)],
        orders=[_order(quantity=2)],
    )

    assert result["scope_unknown_events"] == 0
    assert result["quantity_less_than_order_total"] == 1


def test_single_item_order_missing_cancellation_quantity():
    result = _diagnose(
        events=[_event()], headers=[_header(item_count=1)], orders=[_order()],
    )

    assert result["scope_unknown_events"] == 1
    assert result["order_single_item"] == 1
    assert result["cancellation_quantity_unknown"] == 1
    assert result["unknown_missing_cancellation_quantity"] == 1
    assert result["would_be_decidable_with_cancellation_quantity"] == 1


def test_multiple_item_order_and_item_match_unique():
    result = _diagnose(
        events=[_event()], headers=[_header(item_count=2)],
        orders=[_order(), _order(product="different private product")],
    )

    assert result["order_multiple_items"] == 1
    assert result["source_has_product_clue"] == 1
    assert result["item_match_unique"] == 1


def test_item_match_multiple_is_counted_without_changing_reason_priority():
    result = _diagnose(
        events=[_event()], headers=[_header(item_count=2)],
        orders=[_order(), _order()],
    )

    assert result["item_match_multiple"] == 1
    assert result["unknown_missing_cancellation_quantity"] == 1


def test_item_match_not_possible_and_missing_identifier_opportunity():
    result = _diagnose(
        events=[_event()], headers=[_header(item_count=2)],
        orders=[_order(), _order(product="another product")], raw=_raw("キャンセル"),
    )

    assert result["item_match_not_possible"] == 1
    assert result["would_be_decidable_with_item_identifier"] == 1


def test_missing_order_quantity_is_diagnosed():
    result = _diagnose(
        events=[_event(item_count=1)], headers=[_header(item_count="")],
        orders=[_order(quantity="")],
    )

    assert result["order_total_quantity_unknown"] == 1
    assert result["order_item_quantities_incomplete"] == 1
    assert result["unknown_missing_order_quantity"] == 1
    assert result["would_be_decidable_with_order_item_quantity"] == 1


def test_excess_quantity_is_conflicting():
    result = _diagnose(
        events=[_event(item_count=3)], headers=[_header(item_count=2)],
        orders=[_order(quantity=2)],
    )

    assert result["quantity_exceeds_order_total"] == 1
    assert result["unknown_conflicting_quantity"] == 1


def test_unknown_reason_is_exactly_one_per_unknown_event():
    result = _diagnose(
        events=[_event()], headers=[_header(item_count=2)], orders=[_order(quantity=2)],
    )
    reason_total = sum(value for key, value in result.items() if key.startswith("unknown_"))

    assert reason_total == result["scope_unknown_events"] == 1


def test_output_contains_counts_only_and_no_private_data():
    result = _diagnose(
        events=[_event()], headers=[_header()], orders=[_order()],
    )
    rendered = str(result)

    for private in (
        PRIVATE_ORDER_ID, PRIVATE_PRODUCT, "private-gmail", "private-thread",
        "private-source-hash", "private@example.com", "1200",
    ):
        assert private not in rendered
    assert all(isinstance(value, int) for value in result.values())


def test_cli_uses_both_read_only_services(monkeypatch, capsys):
    sheets_service = object()
    gmail_service = object()
    db = object()

    class Settings:
        spreadsheet_id = "private-sheet-id"
        gmail_token_json = "private-token"

        def validate(self, **kwargs):
            assert kwargs == {"need_gmail": True, "need_sheet": True}

    monkeypatch.setattr(cli, "Settings", Settings)
    monkeypatch.setattr(cli, "read_only_sheets_service", lambda: sheets_service)
    monkeypatch.setattr(cli, "gmail_readonly_service", lambda token: gmail_service)
    monkeypatch.setattr(cli, "SheetsDB", lambda sid, service: db)
    monkeypatch.setattr(
        cli, "diagnose_amazon_cancellation_scopes",
        lambda gmail, sheets: {"scope_unknown_events": int(
            gmail is gmail_service and sheets is db
        )},
    )
    monkeypatch.setattr(
        sys, "argv", ["kakeibo", "amazon-cancellation-scope-diagnose"],
    )

    cli.main()

    assert capsys.readouterr().out.strip() == "{'scope_unknown_events': 1}"
