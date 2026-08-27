import base64
from email.message import EmailMessage
import hashlib
import sys

from app import cli
from app.amazon_cancellation_order_id_diagnose import (
    diagnose_amazon_cancellation_order_ids,
)
from app.amazon_gmail_storage import GmailRawMessage


PRIVATE_ORDER_ID = "123-1234567-1234567"
SECOND_PRIVATE_ORDER_ID = "999-7654321-1234567"


def _raw(body="この商品をキャンセル\n対象商品: private product\n数量: 1\n金額: 1200円"):
    message = EmailMessage()
    message["Subject"] = "注文のキャンセル"
    message["From"] = "private@example.com"
    message["Message-ID"] = "<private-rfc@example.com>"
    message["Date"] = "Mon, 24 Aug 2026 12:00:00 +0900"
    message.set_content(body)
    return message.as_bytes()


def _row(event_type="cancellation", order_id="", gmail_id="gmail-private"):
    raw = _raw()
    row = [""] * 24
    row[:7] = [
        "private-event-key", gmail_id, "<private-rfc@example.com>",
        "thread-private", hashlib.sha256(raw).hexdigest(), event_type, order_id,
    ]
    row[7] = "2026-08-24"
    return row


def _order(order_id=PRIVATE_ORDER_ID):
    return [
        "private-key", order_id, "private-asin", "2026-08-20",
        "private product", 1, 1200, "", "", "", "", "private-hash", "", "", 1,
    ]


def _header(order_id=PRIVATE_ORDER_ID):
    return [order_id, "2026-08-20", 1200, "", 1, "ordered"]


class ReadOnlyDB:
    def __init__(self, rows, *, orders=None, headers=None):
        self.rows = list(rows)
        self.orders = list(orders or [])
        self.headers = list(headers or [])
        self.reads = []

    def amazon_event_rows(self):
        self.reads.append("amazon_event_rows")
        return [(number, list(row)) for number, row in enumerate(self.rows, 2)]

    def get(self, rng):
        self.reads.append(rng)
        return {
            "Amazon注文!A2:O": self.orders,
            "Amazon注文ヘッダ!A2:O": self.headers,
            "Amazonイベント!A2:X": self.rows,
        }[rng]

    def __getattr__(self, name):
        if name.startswith(("append", "update", "clear", "ensure", "configure")):
            raise AssertionError(f"write method must not be used: {name}")
        raise AttributeError(name)


class Request:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    def execute(self):
        if self.error:
            raise self.error
        return self.value


class Messages:
    def __init__(self, responses):
        self.responses = responses
        self.get_calls = []

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        value = self.responses.get(kwargs["id"], RuntimeError("not found"))
        return Request(error=value) if isinstance(value, Exception) else Request(value=value)

    def modify(self, **kwargs):
        raise AssertionError("Gmail write must not be used")


class Service:
    def __init__(self, responses=None):
        self.api = Messages(responses or {})

    def users(self):
        return self

    def messages(self):
        return self.api


def _response(raw=None):
    encoded = base64.urlsafe_b64encode(raw or _raw()).decode().rstrip("=")
    return {"id": "gmail-private", "threadId": "thread-private", "raw": encoded}


def _diagnose(rows=None, *, response=True, thread_messages=(), orders=None, headers=None):
    service = Service({"gmail-private": _response()} if response else {})
    db = ReadOnlyDB(rows or [_row()], orders=orders, headers=headers)
    result = diagnose_amazon_cancellation_order_ids(
        service, db,
        thread_fetcher=lambda _service, _thread_id: list(thread_messages),
    )
    return result, service, db


def test_targets_only_cancellation_with_missing_order_id():
    result, _, _ = _diagnose([
        _row(), _row(order_id=PRIVATE_ORDER_ID), _row(event_type="return"),
    ])

    assert result["missing_order_id_events"] == 1


def test_source_metadata_and_direct_gmail_are_found_uniquely():
    result, service, _ = _diagnose()

    assert result["has_gmail_message_id"] == 1
    assert result["has_gmail_thread_id"] == 1
    assert result["has_rfc_message_id"] == 1
    assert result["has_source_hash"] == 1
    assert result["gmail_source_found"] == 1
    assert result["gmail_source_ambiguous"] == 0
    assert service.api.get_calls == [
        {"userId": "me", "id": "gmail-private", "format": "raw"},
    ]


def test_source_not_found_is_reviewed():
    row = _row(gmail_id="")
    row[2] = row[3] = row[4] = ""
    result, _, _ = _diagnose([row], response=False)

    assert result["gmail_source_not_found"] == 1
    assert result["still_requires_review"] == 1


def test_reparse_recovers_order_id_first():
    result, _, _ = _diagnose(
        response=True,
        thread_messages=[GmailRawMessage("other", "thread-private", _raw(PRIVATE_ORDER_ID))],
    )
    # The direct source response is replaced with an ID-bearing message here.
    service = Service({"gmail-private": _response(_raw(PRIVATE_ORDER_ID))})
    result = diagnose_amazon_cancellation_order_ids(
        service, ReadOnlyDB([_row()]), thread_fetcher=lambda *_: [],
    )

    assert result["reparse_order_id_found"] == 1
    assert result["recoverable_by_reparse"] == 1
    assert result["recoverable_by_thread"] == 0


def test_reparse_still_missing_then_thread_unique_recovers():
    related = GmailRawMessage("related", "thread-private", _raw(PRIVATE_ORDER_ID))
    result, _, _ = _diagnose(thread_messages=[related])

    assert result["reparse_order_id_still_missing"] == 1
    assert result["thread_order_id_unique"] == 1
    assert result["recoverable_by_thread"] == 1


def test_forwarded_order_id_clue_is_counted_without_exposing_value():
    forwarded = _raw(
        "---------- Forwarded message ---------\n"
        "From: private@example.com\nSubject: cancellation\n"
        f"> 注文番号: {PRIVATE_ORDER_ID}\n> キャンセルされました"
    )
    service = Service({"gmail-private": _response(forwarded)})
    result = diagnose_amazon_cancellation_order_ids(
        service, ReadOnlyDB([_row()]), thread_fetcher=lambda *_: [],
        parser=lambda _: type("Event", (), {"order_id": None, "event_date": None})(),
    )

    assert result["forwarded_message_clue_present"] == 1
    assert result["forwarded_order_id_unique_candidate_present"] == 1
    assert PRIVATE_ORDER_ID not in str(result)


def test_thread_multiple_order_ids_is_ambiguous_and_reviewed():
    messages = [
        GmailRawMessage("one", "thread-private", _raw(PRIVATE_ORDER_ID)),
        GmailRawMessage("two", "thread-private", _raw(SECOND_PRIVATE_ORDER_ID)),
    ]
    result, _, _ = _diagnose(thread_messages=messages)

    assert result["thread_order_id_ambiguous"] == 1
    assert result["order_candidate_none"] == 0
    assert result["still_requires_review"] == 1


def test_unique_strong_order_candidate_is_recoverable():
    result, _, _ = _diagnose(orders=[_order()], headers=[_header()])

    assert result["thread_order_id_none"] == 1
    assert result["order_candidate_unique"] == 1
    assert result["order_candidate_unique_strong"] == 1
    assert result["recoverable_by_unique_candidate"] == 1


def test_multiple_order_candidates_require_review():
    result, _, _ = _diagnose(
        orders=[_order(), _order(SECOND_PRIVATE_ORDER_ID)],
        headers=[_header(), _header(SECOND_PRIVATE_ORDER_ID)],
    )

    assert result["order_candidate_multiple"] == 1
    assert result["recoverable_by_unique_candidate"] == 0
    assert result["still_requires_review"] == 1


def test_reparse_error_is_counted_without_writes():
    result = diagnose_amazon_cancellation_order_ids(
        Service({"gmail-private": _response()}), ReadOnlyDB([_row()]),
        parser=lambda _: (_ for _ in ()).throw(ValueError("private parser error")),
        thread_fetcher=lambda *_: [],
    )

    assert result["reparse_error"] == 1
    assert result["still_requires_review"] == 1


def test_final_classification_is_exactly_one_per_target():
    result, _, _ = _diagnose()
    final_total = sum(result[field] for field in (
        "recoverable_by_reparse", "recoverable_by_thread",
        "recoverable_by_unique_candidate", "still_requires_review",
    ))

    assert final_total == result["missing_order_id_events"]


def test_output_has_counts_only_and_no_private_data():
    result, _, _ = _diagnose(orders=[_order()], headers=[_header()])
    rendered = str(result)

    for private in (
        PRIVATE_ORDER_ID, "private product", "gmail-private", "thread-private",
        "private-rfc", "private-event-key", "private@example.com",
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
        cli, "diagnose_amazon_cancellation_order_ids",
        lambda gmail, sheets: {"missing_order_id_events": int(
            gmail is gmail_service and sheets is db
        )},
    )
    monkeypatch.setattr(
        sys, "argv", ["kakeibo", "amazon-cancellation-order-id-diagnose"],
    )

    cli.main()

    assert capsys.readouterr().out.strip() == "{'missing_order_id_events': 1}"
