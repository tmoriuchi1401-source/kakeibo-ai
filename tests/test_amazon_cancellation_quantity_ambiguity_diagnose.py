import base64
from email.message import EmailMessage
import sys

from app import cli
from app.amazon_cancellation_quantity_ambiguity_diagnose import (
    diagnose_amazon_cancellation_quantity_ambiguity,
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
    return row


def _order(quantity=2):
    row = [""] * 15
    row[0] = "private-key"
    row[1] = PRIVATE_ORDER_ID
    row[4] = "private product"
    row[5] = quantity
    return row


def _raw(plain, *, html=None, cancellation=True):
    message = EmailMessage()
    message["Subject"] = (
        "商品が正常にキャンセルされました" if cancellation else "private notice"
    )
    message["From"] = "private@example.com"
    message.set_content(plain)
    if html is not None:
        message.add_alternative(html, subtype="html")
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
    def __init__(self, events=None):
        self.events = list(events or [_event()])

    def amazon_event_rows(self):
        return [(number, list(row)) for number, row in enumerate(self.events, 2)]

    def get(self, rng):
        return {
            "Amazon注文!A2:O": [_order()],
            "Amazon注文ヘッダ!A2:O": [_header()],
        }[rng]

    def __getattr__(self, name):
        if name.startswith(("append", "update", "clear", "ensure", "configure")):
            raise AssertionError(f"write method must not be used: {name}")
        raise AttributeError(name)


def _diagnose(raw, events=None):
    return diagnose_amazon_cancellation_quantity_ambiguity(
        Service(raw), ReadOnlyDB(events), thread_fetcher=lambda *_: [],
    )


def test_only_target_event_is_diagnosed():
    result = _diagnose(
        _raw("数量: 1\n数量: 2"),
        [_event(), _event(event_type="return"), _event(order_id="")],
    )

    assert result["target_events"] == 1
    assert result["ambiguous_quantity_events"] == 1


def test_quantity_found_and_known_scope_are_excluded():
    found = _diagnose(_raw("数量: 1"))
    known = _diagnose(_raw("数量: 1\n数量: 2"), [_event(item_count=2)])

    assert found["target_events"] == 0
    assert known["target_events"] == 0


def test_candidate_zero_and_one_are_classified():
    zero = _diagnose(_raw("数量: abc", cancellation=False))
    one = _diagnose(_raw("数量: 3"))

    assert zero["candidate_occurrences_zero"] == 1
    assert zero["ambiguity_malformed_quantity"] == 1
    assert one["candidate_occurrences_one"] == 1
    assert one["distinct_quantity_values_one"] == 1
    assert one["would_still_be_ambiguous_with_unique_distinct_value_rule"] == 1


def test_same_label_same_value_duplicate_is_safe_rule_candidate():
    result = _diagnose(_raw("数量: 1\n数量: 1"))

    assert result["candidate_occurrences_multiple"] == 1
    assert result["distinct_quantity_values_one"] == 1
    assert result["duplicate_same_label_same_value"] == 1
    assert result["ambiguity_same_value_duplicate"] == 1
    assert result["would_be_safe_with_unique_distinct_value_rule"] == 1


def test_different_labels_same_value_is_safe_rule_candidate():
    result = _diagnose(_raw("数量: 1\nキャンセル数量: 1"))

    assert result["duplicate_different_label_same_value"] == 1
    assert result["distinct_quantity_values_one"] == 1
    assert result["would_be_safe_with_unique_distinct_value_rule"] == 1


def test_conflicting_values_remain_ambiguous():
    result = _diagnose(_raw("数量: 1\n数量: 2"))

    assert result["distinct_quantity_values_multiple"] == 1
    assert result["conflicting_values"] == 1
    assert result["ambiguity_conflicting_values"] == 1
    assert result["would_still_be_ambiguous_with_unique_distinct_value_rule"] == 1


def test_plain_html_duplicate_and_source_format_are_counted():
    result = _diagnose(_raw("数量: 1", html="<div>数量: 1</div>"))

    assert result["source_has_plain_text"] == 1
    assert result["source_has_html"] == 1
    assert result["source_has_both"] == 1
    assert result["html_plaintext_duplicate"] == 1
    assert result["would_be_safe_with_unique_distinct_value_rule"] == 1


def test_nonpositive_value_remains_ambiguous():
    result = _diagnose(_raw("数量: 0"))

    assert result["ambiguity_malformed_quantity"] == 1
    assert result["would_still_be_ambiguous_with_unique_distinct_value_rule"] == 1


def test_final_reason_is_exactly_one_per_event():
    result = _diagnose(_raw("数量: 1\n数量: 1"))
    reason_total = sum(
        value for key, value in result.items()
        if key.startswith("ambiguity_")
    )

    assert reason_total == result["ambiguous_quantity_events"] == 1


def test_output_has_counts_only_and_no_private_data():
    result = _diagnose(_raw("数量: 1\n数量: 1"))
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
        cli, "diagnose_amazon_cancellation_quantity_ambiguity",
        lambda gmail, sheets: {"target_events": int(
            gmail is gmail_service and sheets is db
        )},
    )
    monkeypatch.setattr(
        sys, "argv",
        ["kakeibo", "amazon-cancellation-quantity-ambiguity-diagnose"],
    )

    cli.main()

    assert capsys.readouterr().out.strip() == "{'target_events': 1}"
