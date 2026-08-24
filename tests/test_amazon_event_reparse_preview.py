from __future__ import annotations

import base64
from email.message import EmailMessage
import sys

import pytest

from app import cli
from app.amazon_email import AmazonMailEvent
from app.amazon_event_reparse_preview import (
    apply_amazon_event_reparse,
    preview_amazon_event_reparse,
)


def _raw(body="body"):
    message = EmailMessage()
    message["Message-ID"] = "<mail@example.com>"
    message.set_content(body)
    return message.as_bytes()


def _event(**overrides):
    values = {
        "event_type": "order", "order_id": "123-1234567-1234567",
        "event_date": "2026-08-20", "charged_amount": None,
        "order_amount": 3980, "refund_amount": None,
        "gift_card_amount": None, "points_amount": None,
        "coupon_amount": None, "discount_amount": None,
        "payment_method": "Visa", "shipment_amount": None, "item_count": 2,
        "message_id": "<mail@example.com>", "source_hash": "hash",
        "gift_card_used": False, "points_used": False,
    }
    values.update(overrides)
    return AmazonMailEvent(**values)


def _row(gmail_id="gmail-1", **overrides):
    row = [""] * 24
    row[0:5] = ["AE-1", gmail_id, "<mail@example.com>", "thread-1", "hash"]
    values = {
        5: "unknown", 6: "", 7: "", 8: "", 9: "", 10: "", 11: "",
        12: "", 13: "", 14: "", 15: "", 16: "", 17: "",
        18: "unusable", 19: "unmatched", 20: "pending",
        21: "amazon_email_v1", 22: "imported", 23: "old-parsed",
    }
    values.update(overrides)
    for index, value in values.items():
        row[index] = value
    return row


class Request:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    def execute(self):
        if self.error:
            raise self.error
        return self.value


class MessagesAPI:
    def __init__(self, responses):
        self.responses = responses
        self.get_calls = []
        self.list_calls = []

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        value = self.responses[kwargs["id"]]
        return Request(error=value) if isinstance(value, Exception) else Request(value)

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        raise AssertionError("reparse must not search Gmail")


class Service:
    def __init__(self, responses):
        self.api = MessagesAPI(responses)

    def users(self):
        return self

    def messages(self):
        return self.api


class ReadOnlyDB:
    def __init__(self, rows):
        self.rows = rows
        self.write_calls = []

    def amazon_event_rows(self):
        return [(index, list(row)) for index, row in enumerate(self.rows, 2)]

    def __getattr__(self, name):
        if name.startswith(("append", "update", "clear")):
            def fail(*args, **kwargs):
                self.write_calls.append((name, args, kwargs))
                raise AssertionError("preview must not write")
            return fail
        raise AttributeError(name)


class WritableDB(ReadOnlyDB):
    def update_amazon_event_cells(self, row_number, cells):
        self.write_calls.append((row_number, cells))


def _response(gmail_id="gmail-1", raw=None):
    encoded = base64.urlsafe_b64encode(raw or _raw()).decode().rstrip("=")
    return {"id": gmail_id, "threadId": "thread-1", "raw": encoded}


def test_direct_get_detects_unknown_to_order_fields_and_never_writes():
    db = ReadOnlyDB([_row()])
    service = Service({"gmail-1": _response()})

    result = preview_amazon_event_reparse(
        service, db, parser=lambda _: _event(),
        timestamp_factory=lambda: "2026-08-23T00:00:00+00:00",
    )

    assert service.api.get_calls == [{"userId": "me", "id": "gmail-1", "format": "raw"}]
    assert service.api.list_calls == []
    assert db.write_calls == []
    assert result["changed_events"] == result["would_update"] == 1
    assert result["event_type_changed"] == 1
    assert result["order_id_changed"] == 1
    assert result["order_amount_changed"] == 1
    assert result["parse_status_changed"] == 1
    fields = result["changes"][0]["fields"]
    assert fields["Event Type"] == {"old": "unknown", "new": "order"}
    assert fields["Order ID"]["new"] == "123-1234567-1234567"
    assert fields["Order Amount"]["new"] == 3980


def test_unchanged_event_is_detected_with_sheet_numeric_normalization():
    row = _row()
    for index, value in {
        5: "order", 6: "123-1234567-1234567", 7: "2026-08-20", 9: "3980",
        16: "Visa", 17: "2", 18: "parsed", 21: "amazon_email_v2",
        23: "same-time",
    }.items():
        row[index] = value
    result = preview_amazon_event_reparse(
        Service({"gmail-1": _response()}), ReadOnlyDB([row]),
        parser=lambda _: _event(), timestamp_factory=lambda: "same-time",
    )

    assert result["changed_events"] == result["would_update"] == 0
    assert result["unchanged_events"] == 1
    assert result["changes"] == []


def test_parser_error_missing_and_identity_mismatch_are_skipped():
    rows = [_row("parser"), _row("missing"), _row("mismatch")]
    service = Service({
        "parser": _response("parser"),
        "missing": RuntimeError("not found"),
        "mismatch": _response("different"),
    })

    result = preview_amazon_event_reparse(
        service, ReadOnlyDB(rows), parser=lambda _: (_ for _ in ()).throw(ValueError("bad")),
    )

    assert result["stored_events"] == 3
    assert result["gmail_fetched"] == 1
    assert result["gmail_missing"] == 1
    assert result["identity_mismatch"] == 1
    assert result["parser_errors"] == 1
    assert result["parser_success"] == 0
    assert result["would_update"] == 0


def test_cli_uses_both_readonly_services(monkeypatch, capsys):
    sheets_service = object()
    gmail_service = object()
    db = object()

    class FakeSettings:
        spreadsheet_id = "sheet-id"
        gmail_token_json = "gmail-token"

        def validate(self, **kwargs):
            assert kwargs == {"need_sheet": True, "need_gmail": True}

    monkeypatch.setattr(cli, "Settings", FakeSettings)
    monkeypatch.setattr(cli, "read_only_sheets_service", lambda: sheets_service)
    monkeypatch.setattr(cli, "gmail_readonly_service", lambda token: gmail_service)
    monkeypatch.setattr(cli, "SheetsDB", lambda sid, service: db)
    monkeypatch.setattr(
        cli, "preview_amazon_event_reparse",
        lambda gmail, sheets: {"ok": gmail is gmail_service and sheets is db},
    )
    monkeypatch.setattr(sys, "argv", ["kakeibo", "amazon-event-reparse-preview"])

    cli.main()

    assert capsys.readouterr().out.strip() == "{'ok': True}"


def test_apply_does_not_erase_existing_values_when_new_values_are_empty():
    row = _row()
    row[5] = "order"
    row[6] = "keep-order-id"
    row[9] = "5000"
    db = WritableDB([row])

    result = apply_amazon_event_reparse(
        Service({"gmail-1": _response()}), db,
        parser=lambda _: _event(
            event_type="unknown", order_id=None, order_amount=None,
            event_date=None, payment_method=None, item_count=None,
        ),
        timestamp_factory=lambda: "new-time",
    )

    assert result["metadata_only_updated_rows"] == 1
    assert result["business_fields_updated_rows"] == 0
    assert db.write_calls == [(2, [(21, "amazon_email_v2"), (23, "new-time")])]


def test_apply_identity_mismatch_and_parser_error_do_not_write():
    mismatch_db = WritableDB([_row()])
    mismatch = apply_amazon_event_reparse(
        Service({"gmail-1": _response("other")}), mismatch_db,
    )
    assert mismatch["identity_mismatch"] == 1
    assert mismatch["skipped_rows"] == 1
    assert mismatch_db.write_calls == []

    parser_db = WritableDB([_row()])
    parser_failure = apply_amazon_event_reparse(
        Service({"gmail-1": _response()}), parser_db,
        parser=lambda _: (_ for _ in ()).throw(ValueError("bad")),
    )
    assert parser_failure["parser_errors"] == 1
    assert parser_failure["error_rows"] == 1
    assert parser_db.write_calls == []


def test_duplicate_gmail_message_id_stops_before_fetch_or_write():
    db = WritableDB([_row("duplicate"), _row("duplicate")])
    service = Service({"duplicate": _response("duplicate")})

    with pytest.raises(RuntimeError, match="Gmail Message IDが重複"):
        apply_amazon_event_reparse(service, db)

    assert service.api.get_calls == []
    assert db.write_calls == []


def test_apply_metadata_only_is_counted():
    row = _row()
    for index, value in {
        5: "order", 6: "123-1234567-1234567", 7: "2026-08-20", 9: "3980",
        16: "Visa", 17: "2", 18: "parsed", 21: "amazon_email_v1",
        23: "old-time",
    }.items():
        row[index] = value
    db = WritableDB([row])

    result = apply_amazon_event_reparse(
        Service({"gmail-1": _response()}), db,
        parser=lambda _: _event(), timestamp_factory=lambda: "new-time",
    )

    assert result["updated_rows"] == 1
    assert result["metadata_only_updated_rows"] == 1
    assert result["business_fields_updated_rows"] == 0
    assert result["field_updates"]["parser_version"] == 1
    assert result["field_updates"]["last_parsed_at"] == 1
    assert db.write_calls == [(2, [(21, "amazon_email_v2"), (23, "new-time")])]


def test_apply_updates_only_changed_cells_and_counts_fields():
    db = WritableDB([_row()])

    result = apply_amazon_event_reparse(
        Service({"gmail-1": _response()}), db,
        parser=lambda _: _event(), timestamp_factory=lambda: "new-time",
    )

    assert result["updated_rows"] == 1
    assert result["business_fields_updated_rows"] == 1
    assert result["metadata_only_updated_rows"] == 0
    assert db.write_calls == [(2, [
        (5, "order"), (6, "123-1234567-1234567"), (7, "2026-08-20"),
        (9, 3980), (16, "Visa"), (17, 2), (18, "parsed"),
        (21, "amazon_email_v2"), (23, "new-time"),
    ])]
    assert result["field_updates"]["order_amount"] == 1
    assert all(column not in {0, 1, 2, 3, 4, 19, 20, 22}
               for column, _value in db.write_calls[0][1])


def test_cli_apply_without_safety_flag_does_not_initialize_or_write(monkeypatch):
    monkeypatch.setattr(
        cli, "Settings",
        lambda: (_ for _ in ()).throw(AssertionError("must not initialize")),
    )
    monkeypatch.setattr(sys, "argv", ["kakeibo", "amazon-event-reparse-apply"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
