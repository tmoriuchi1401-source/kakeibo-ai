import base64
from email.message import EmailMessage
import sys

from app import cli
from app.amazon_cancellation_quantity_preview import (
    preview_amazon_cancellation_quantities,
)


PRIVATE_ORDER_ID = "123-1234567-1234567"


def _event(*, event_type="cancellation", order_id=PRIVATE_ORDER_ID, item_count=""):
    row = [""] * 24
    row[:7] = [
        "private-event", "private-gmail", "private-rfc", "private-thread",
        "private-source-hash", event_type, order_id,
    ]
    row[17] = item_count
    return row


def _header(item_count=2):
    row = [""] * 15
    row[0] = PRIVATE_ORDER_ID
    row[4] = item_count
    row[5] = "ordered"
    return row


def _order(quantity=2):
    row = [""] * 15
    row[0] = "private-key"
    row[1] = PRIVATE_ORDER_ID
    row[4] = "private product"
    row[5] = quantity
    return row


def _raw(quantity_line="数量: 1"):
    message = EmailMessage()
    message["Subject"] = "商品が正常にキャンセルされました"
    message["From"] = "private@example.com"
    message.set_content(f"注文番号: {PRIVATE_ORDER_ID}\n{quantity_line}")
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
    def __init__(self, raw):
        self.api = Messages(raw)

    def users(self):
        return self

    def messages(self):
        return self.api


class ReadOnlyDB:
    def __init__(self, events, *, headers=None, orders=None):
        self.events = list(events)
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


def _preview(*, events=None, quantity_line="数量: 1", order_total=2):
    return preview_amazon_cancellation_quantities(
        Service(_raw(quantity_line)),
        ReadOnlyDB(
            events or [_event()], headers=[_header(order_total)],
            orders=[_order(order_total)],
        ),
        thread_fetcher=lambda *_: [],
    )


def test_non_cancellation_and_missing_order_id_are_excluded():
    result = _preview(events=[
        _event(event_type="return"), _event(order_id=""),
    ])

    assert result["target_cancellation_events"] == 0


def test_known_scope_is_excluded():
    result = _preview(events=[_event(item_count=2)], order_total=2)

    assert result["target_cancellation_events"] == 0


def test_equal_quantity_would_resolve_full_order():
    result = _preview(quantity_line="数量: 2", order_total=2)

    assert result["target_cancellation_events"] == 1
    assert result["source_email_found"] == 1
    assert result["quantity_found"] == 1
    assert result["would_resolve_full_order"] == 1
    assert result["would_remain_scope_unknown"] == 0


def test_less_quantity_would_resolve_item_or_partial():
    result = _preview(quantity_line="数量: 1", order_total=2)

    assert result["quantity_found"] == 1
    assert result["would_resolve_item_or_partial"] == 1


def test_repeated_same_quantity_would_resolve_scope():
    result = _preview(quantity_line="数量: 2\n数量: 2", order_total=2)

    assert result["quantity_found"] == 1
    assert result["quantity_invalid_or_ambiguous"] == 0
    assert result["would_resolve_full_order"] == 1


def test_quantity_above_order_total_is_invalid():
    result = _preview(quantity_line="数量: 3", order_total=2)

    assert result["quantity_found"] == 0
    assert result["quantity_invalid_or_ambiguous"] == 1
    assert result["would_remain_scope_unknown"] == 1


def test_nonpositive_and_conflicting_quantity_remain_unknown():
    for quantity_line in ("数量: 0", "数量: -1", "数量: 1\n数量: 2"):
        result = _preview(quantity_line=quantity_line)
        assert result["quantity_invalid_or_ambiguous"] == 1
        assert result["would_remain_scope_unknown"] == 1


def test_missing_quantity_is_distinct_from_invalid():
    result = _preview(quantity_line="金額: 1200円")

    assert result["quantity_still_missing"] == 1
    assert result["quantity_invalid_or_ambiguous"] == 0


def test_output_is_counts_only_and_contains_no_private_values():
    result = _preview(quantity_line="数量: 1", order_total=2)
    rendered = str(result)

    for private in (
        PRIVATE_ORDER_ID, "private product", "private-gmail", "private-thread",
        "private-source-hash", "private@example.com",
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
        cli, "preview_amazon_cancellation_quantities",
        lambda gmail, sheets: {"target_cancellation_events": int(
            gmail is gmail_service and sheets is db
        )},
    )
    monkeypatch.setattr(
        sys, "argv", ["kakeibo", "amazon-cancellation-quantity-preview"],
    )

    cli.main()

    assert capsys.readouterr().out.strip() == "{'target_cancellation_events': 1}"
