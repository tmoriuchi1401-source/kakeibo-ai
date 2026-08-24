from __future__ import annotations

import base64
from email.message import EmailMessage
import hashlib

from app.amazon_email import AmazonMailEvent
from app.amazon_gmail_storage import (
    AMAZON_PARSER_VERSION,
    GmailRawMessage,
    amazon_event_id,
    amazon_stored_event_from_mail,
    fetch_amazon_gmail_messages,
    save_amazon_gmail_events,
)


def test_cli_import_uses_readonly_gmail_and_writable_sheets_db(monkeypatch, capsys):
    import sys

    import app.cli as cli

    gmail_service = object()
    db = object()
    summary = {"fetched": 2, "new": 1}

    class FakeSettings:
        spreadsheet_id = "sheet-id"
        gmail_token_json = "readonly-token"

        def validate(self, **kwargs):
            assert kwargs == {"need_sheet": True, "need_gmail": True}

    def fake_sheets_db(spreadsheet_id, service=None):
        assert spreadsheet_id == "sheet-id"
        assert service is None
        return db

    monkeypatch.setattr(cli, "Settings", FakeSettings)
    monkeypatch.setattr(cli, "SheetsDB", fake_sheets_db)
    monkeypatch.setattr(
        cli,
        "gmail_readonly_service",
        lambda token: gmail_service if token == "readonly-token" else None,
    )
    monkeypatch.setattr(
        cli,
        "import_amazon_gmail_events",
        lambda service, sheets_db: summary
        if service is gmail_service and sheets_db is db else None,
    )
    monkeypatch.setattr(sys, "argv", ["kakeibo", "amazon-gmail-import"])

    cli.main()

    assert capsys.readouterr().out.strip() == str(summary)


def _raw(message_id: str | None, body: str) -> bytes:
    message = EmailMessage()
    if message_id is not None:
        message["Message-ID"] = message_id
    message["Subject"] = "Amazon event"
    message.set_content(body)
    return message.as_bytes()


def _mail(gmail_id: str, *, rfc_id: str | None = None, body: str | None = None):
    return GmailRawMessage(
        gmail_message_id=gmail_id,
        thread_id=f"thread-{gmail_id}",
        raw_mime=_raw(rfc_id, body or gmail_id),
    )


def _event(raw_mime: bytes, *, event_type="order", order_id="123-1234567-1234567", source_hash=None):
    return AmazonMailEvent(
        event_type=event_type,
        order_id=order_id,
        event_date="2026-08-22",
        charged_amount=100,
        order_amount=100,
        refund_amount=None,
        gift_card_amount=None,
        points_amount=None,
        coupon_amount=None,
        discount_amount=None,
        payment_method="Visa",
        shipment_amount=None,
        item_count=1,
        message_id=None,
        source_hash=source_hash or hashlib.sha256(raw_mime).hexdigest(),
        gift_card_used=False,
        points_used=False,
    )


def _parser(raw_mime: bytes):
    return _event(raw_mime)


class FakeDB:
    def __init__(self, *, gmail=(), rfc=(), hashes=()):
        self.identities = {
            "gmail_message_ids": set(gmail),
            "rfc_message_ids": set(rfc),
            "source_hashes": set(hashes),
        }
        self.identity_reads = 0
        self.append_calls = []

    def amazon_event_identity_index(self):
        self.identity_reads += 1
        return {name: set(values) for name, values in self.identities.items()}

    def append(self, sheet, rows):
        self.append_calls.append((sheet, rows))
        for row in rows:
            self.identities["gmail_message_ids"].add(row[1])
            if row[2]:
                self.identities["rfc_message_ids"].add(row[2])
            if row[4]:
                self.identities["source_hashes"].add(row[4])


def _save(db, messages, parser=_parser):
    return save_amazon_gmail_events(
        db, messages, parser=parser, timestamp_factory=lambda: "2026-08-22T00:00:00+00:00",
    )


def test_same_gmail_message_processed_twice_is_saved_once():
    db = FakeDB()
    message = _mail("gmail-1", rfc_id="<one@example.com>")

    first = _save(db, [message])
    second = _save(db, [message])

    assert first["new"] == 1
    assert second["new"] == 0
    assert second["duplicate_gmail_id"] == 1
    assert len(db.append_calls) == 1


def test_different_gmail_id_with_existing_rfc_message_id_is_duplicate():
    db = FakeDB(rfc={"<same@example.com>"})
    result = _save(db, [_mail("gmail-new", rfc_id="<same@example.com>")])

    assert result["duplicate_rfc_message_id"] == 1
    assert result["new"] == 0


def test_source_hash_is_third_duplicate_key():
    message = _mail("gmail-new", rfc_id="<new@example.com>")
    event = _event(message.raw_mime, source_hash="same-hash")
    db = FakeDB(hashes={"same-hash"})

    result = _save(db, [message], parser=lambda _: event)

    assert result["duplicate_source_hash"] == 1
    assert result["new"] == 0


def test_empty_rfc_message_ids_are_not_duplicates():
    db = FakeDB()
    messages = [_mail("gmail-1", body="one"), _mail("gmail-2", body="two")]

    result = _save(db, messages)

    assert result["new"] == 2
    assert result["duplicate_rfc_message_id"] == 0


def test_same_order_id_in_different_messages_creates_distinct_events():
    db = FakeDB()
    messages = [_mail("gmail-1", body="one"), _mail("gmail-2", body="two")]

    result = _save(db, messages)

    assert result["new"] == 2
    assert len(db.append_calls[0][1]) == 2


def test_same_message_repeated_within_run_is_saved_once():
    db = FakeDB()
    message = _mail("gmail-1", rfc_id="<one@example.com>")

    result = _save(db, [message, message])

    assert result["new"] == 1
    assert result["duplicate_gmail_id"] == 1


def test_unknown_event_is_saved_with_unusable_status():
    db = FakeDB()
    message = _mail("gmail-1")

    result = _save(db, [message], parser=lambda raw: _event(
        raw, event_type="unknown", order_id=None,
    ))

    row = db.append_calls[0][1][0]
    assert result["unknown"] == 1
    assert result["unknown_new"] == 1
    assert result["new"] == 1
    assert row[5] == "unknown"
    assert row[18] == "unusable"


def test_duplicate_unknown_is_fetched_unknown_but_not_new_unknown():
    db = FakeDB(gmail={"gmail-1"})

    result = _save(db, [_mail("gmail-1")], parser=lambda raw: _event(
        raw, event_type="unknown", order_id=None,
    ))

    assert result["unknown"] == 1
    assert result["unknown_new"] == 0
    assert result["new"] == 0


def test_undefined_event_type_is_normalized_before_parse_status_is_decided():
    db = FakeDB()
    message = _mail("gmail-1")

    _save(db, [message], parser=lambda raw: _event(raw, event_type="future_type"))

    row = db.append_calls[0][1][0]
    assert row[5] == "unknown"
    assert row[18] == "needs_review"


def test_saved_rows_use_amazon_stored_event_24_column_conversion():
    db = FakeDB()

    _save(db, [_mail("gmail-1")])

    assert len(db.append_calls[0][1][0]) == 24


def test_multiple_new_events_are_appended_in_one_call():
    db = FakeDB()

    result = _save(db, [_mail("gmail-1", body="one"), _mail("gmail-2", body="two")])

    assert result["new"] == 2
    assert db.identity_reads == 1
    assert len(db.append_calls) == 1
    assert len(db.append_calls[0][1]) == 2


def test_storage_writes_only_to_amazon_event_sheet():
    db = FakeDB()

    _save(db, [_mail("gmail-1")])

    assert [sheet for sheet, _ in db.append_calls] == ["Amazonイベント"]


def test_event_id_is_stable_and_based_only_on_gmail_id():
    expected = "AE-" + hashlib.sha256(b"gmail-1").hexdigest()[:24]

    assert amazon_event_id("gmail-1") == expected
    assert amazon_event_id("gmail-1") == amazon_event_id("gmail-1")
    assert amazon_event_id("gmail-1") != amazon_event_id("gmail-2")


def test_conversion_copies_parser_values_and_adds_initial_metadata():
    message = _mail("gmail-1", rfc_id="<one@example.com>")

    stored = amazon_stored_event_from_mail(
        _event(message.raw_mime), message, timestamp="2026-08-22T00:00:00+00:00",
    )

    assert stored.gmail_message_id == "gmail-1"
    assert stored.thread_id == "thread-gmail-1"
    assert stored.rfc_message_id == "<one@example.com>"
    assert stored.parse_status == "parsed"
    assert stored.match_status == "unmatched"
    assert stored.apply_status == "pending"
    assert stored.parser_version == AMAZON_PARSER_VERSION


def test_parser_failure_is_saved_as_unknown_without_raw_body():
    db = FakeDB()

    result = _save(db, [_mail("gmail-1", body="private body")], parser=lambda _: 1 / 0)

    row = db.append_calls[0][1][0]
    assert result["parser_errors"] == 1
    assert result["unknown"] == 1
    assert "private body" not in row


class _Request:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class _MessagesAPI:
    def __init__(self, encoded_raw):
        self.searches = [[{"id": "gmail-1", "threadId": "list-thread"}], [{"id": "gmail-1"}]]
        self.encoded_raw = encoded_raw
        self.get_calls = 0

    def list(self, **kwargs):
        return _Request({"messages": self.searches.pop(0) if self.searches else []})

    def get(self, **kwargs):
        self.get_calls += 1
        return _Request({"raw": self.encoded_raw, "threadId": "raw-thread"})


class _GmailService:
    def __init__(self, api):
        self.api = api

    def users(self):
        return self

    def messages(self):
        return self.api


def test_gmail_fetch_passes_id_thread_and_raw_mime_and_deduplicates_searches():
    raw = _raw("<one@example.com>", "body")
    api = _MessagesAPI(base64.urlsafe_b64encode(raw).decode().rstrip("="))

    messages = fetch_amazon_gmail_messages(_GmailService(api))

    assert messages == [GmailRawMessage("gmail-1", "raw-thread", raw)]
    assert api.get_calls == 1
