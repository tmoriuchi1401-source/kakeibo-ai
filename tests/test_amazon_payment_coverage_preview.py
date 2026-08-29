import base64
from datetime import date
from email.message import EmailMessage
import sys

from app import cli
from app.amazon_payment_coverage_preview import preview_amazon_payment_coverage


ORDER_ID = "249-4045234-9353402"


def _event(event_type, *, event_date="2026-08-27", message_id="gmail-cancel", quantity=""):
    row = [""] * 24
    row[1] = message_id
    row[5] = event_type
    row[6] = ORDER_ID
    row[7] = event_date
    row[17] = quantity
    return row


def _header():
    row = [""] * 15
    row[0] = ORDER_ID
    row[1] = "2026-08-27"
    row[2] = 1499
    row[4] = 1
    row[5] = "cancelled"
    return row


def _transaction(import_id="card:1", *, amount=1499, status="matched_amazon"):
    return [
        import_id, "", "au PAYカード", "", "2026-08-27", "Amazon.co.jp",
        amount, "", status, f"amazon:{ORDER_ID}", "", "",
    ]


def _raw(text):
    message = EmailMessage()
    message["Subject"] = "商品がキャンセルされました"
    message.set_content(text)
    return message.as_bytes()


class Request:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class Messages:
    def __init__(self, raw):
        self.raw = raw
        self.reads = []

    def get(self, **kwargs):
        self.reads.append(kwargs)
        encoded = base64.urlsafe_b64encode(self.raw).decode().rstrip("=")
        return Request({"raw": encoded})


class Users:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


class Gmail:
    def __init__(self, text=""):
        self.messages_api = Messages(_raw(text))

    def users(self):
        return Users(self.messages_api)


class ReadOnlyDB:
    def __init__(self, *, events=None, transactions=None, expenses=None):
        self.values = {
            "Amazon注文ヘッダ!A2:O": [_header()],
            "Amazon注文!A2:O": [],
            "Amazonイベント!A2:X": events or [
                _event("order", message_id="gmail-order"),
                _event("cancellation", quantity=1),
            ],
            "取込データ!A2:L": transactions or [],
            "支出明細!A2:M": expenses or [],
        }
        self.reads = []

    def get(self, rng):
        self.reads.append(rng)
        return self.values[rng]

    def __getattr__(self, name):
        if name.startswith(("append", "update", "clear", "ensure", "configure")):
            raise AssertionError(f"write method used: {name}")
        raise AttributeError(name)


def _preview(*, text="", events=None, transactions=None, expenses=None, as_of=date(2026, 8, 29)):
    db = ReadOnlyDB(events=events, transactions=transactions, expenses=expenses)
    return preview_amazon_payment_coverage(db, Gmail(text), as_of=as_of), db


def test_explicit_no_charge_with_unknown_coverage_waits_for_payment():
    result, _ = _preview(text="注文はキャンセルされました。この注文の請求は行われていません。")
    row = result["rows"][0]

    assert row["amazon_no_charge_assertion"] is True
    assert row["amazon_no_charge_assertion_source"] == "cancellation_email"
    assert row["payment_coverage_status"] == "unknown"
    assert set(row["payment_coverage_by_source"].values()) == {"unknown"}
    assert row["matching_charge_candidate_count"] == 0
    assert row["candidate_state"] == "amazon_declared_not_charged"
    assert (row["action"], row["reason"]) == ("wait_payment", "payment_coverage_unknown")
    assert row["close_condition_diagnostics"]["payment_coverage_complete"] is False
    assert result["amazon_no_charge_assertion_count"] == 1


def test_no_assertion_with_unknown_coverage_is_insufficient():
    result, _ = _preview(text="注文はキャンセルされました。")
    row = result["rows"][0]

    assert row["amazon_no_charge_assertion"] is False
    assert row["candidate_state"] == "payment_coverage_unknown"
    assert (row["action"], row["reason"]) == ("wait_payment", "insufficient_payment_data")


def test_charge_candidate_is_reported():
    result, _ = _preview(transactions=[_transaction()])
    row = result["rows"][0]

    assert row["matching_charge_candidate_count"] == 1
    assert row["candidate_state"] == "charge_candidate_found"
    assert row["close_condition_diagnostics"]["matching_charge_absent"] is False


def test_multiple_candidates_are_ambiguous():
    result, _ = _preview(transactions=[_transaction("card:1"), _transaction("card:2")])
    row = result["rows"][0]

    assert row["ambiguous_candidate_count"] == 2
    assert row["candidate_state"] == "ambiguous"
    assert row["close_condition_diagnostics"]["ambiguity_absent"] is False
    assert result["ambiguous_count"] == 1


def test_expense_and_import_row_for_same_import_are_one_candidate():
    expense = [
        "expense:1", "2026-08-27", "Amazon.co.jp", "商品", 1499,
        "", "", "カード", "Amazon", "", "card:1",
        f"Amazonキー=amazon:{ORDER_ID}", "active",
    ]
    result, _ = _preview(transactions=[_transaction("card:1")], expenses=[expense])

    assert result["rows"][0]["matching_charge_candidate_count"] == 1
    assert result["rows"][0]["ambiguous_candidate_count"] == 0


def test_shipment_delivery_return_and_refund_make_absence_false():
    events = [
        _event("order", message_id="order"),
        _event("cancellation", message_id="cancel", quantity=1),
        _event("shipment", message_id="shipment"),
        _event("delivery", message_id="delivery"),
        _event("return", message_id="return"),
        _event("refund", message_id="refund"),
    ]
    result, _ = _preview(events=events)
    diagnostics = result["rows"][0]["close_condition_diagnostics"]

    assert diagnostics["shipment_absent"] is False
    assert diagnostics["delivery_absent"] is False
    assert diagnostics["return_absent"] is False
    assert diagnostics["refund_absent"] is False
    assert result["rows"][0]["event_timeline"]["refund"]["dates"] == ["2026-08-27"]


def test_refund_candidate_is_reported():
    result, _ = _preview(transactions=[_transaction(amount=-1499, status="needs_review_refund")])
    row = result["rows"][0]

    assert row["matching_refund_candidate_count"] == 1
    assert row["candidate_state"] == "refund_candidate_found"
    assert result["refund_candidate_found_count"] == 1


def test_one_charge_and_one_refund_are_a_pair_not_ambiguous():
    result, _ = _preview(transactions=[
        _transaction("card:charge"),
        _transaction("card:refund", amount=-1499, status="needs_review_refund"),
    ])
    row = result["rows"][0]

    assert row["matching_charge_candidate_count"] == 1
    assert row["matching_refund_candidate_count"] == 1
    assert row["ambiguous_candidate_count"] == 0
    assert row["candidate_state"] == "refund_candidate_found"


def test_elapsed_days_is_display_only():
    early, _ = _preview(as_of=date(2026, 8, 28))
    late, _ = _preview(as_of=date(2027, 8, 28))

    assert early["rows"][0]["elapsed_days"] == 1
    assert late["rows"][0]["elapsed_days"] == 366
    assert early["rows"][0]["action"] == late["rows"][0]["action"] == "wait_payment"
    assert early["rows"][0]["reason"] == late["rows"][0]["reason"]


def test_preview_reads_expected_ranges_and_never_writes():
    result, db = _preview()

    assert result["sampled_order_count"] == 1
    assert db.reads == [
        "Amazon注文ヘッダ!A2:O", "Amazon注文!A2:O", "Amazonイベント!A2:X",
        "取込データ!A2:L", "支出明細!A2:M",
    ]


def test_cli_uses_read_only_sheets_and_gmail(monkeypatch, capsys):
    class Settings:
        spreadsheet_id = "sheet-id"
        gmail_token_json = "token"

        def validate(self, **kwargs):
            assert kwargs == {"need_sheet": True, "need_gmail": True}

    sheets_service = object()
    gmail_service = object()
    db = object()
    monkeypatch.setattr(cli, "Settings", Settings)
    monkeypatch.setattr(cli, "read_only_sheets_service", lambda: sheets_service)
    monkeypatch.setattr(cli, "gmail_readonly_service", lambda token: gmail_service)
    monkeypatch.setattr(cli, "SheetsDB", lambda spreadsheet_id, service=None: db)
    monkeypatch.setattr(
        cli, "preview_amazon_payment_coverage",
        lambda value, gmail: {"ok": value is db and gmail is gmail_service},
    )
    monkeypatch.setattr(sys, "argv", ["kakeibo", "amazon-payment-coverage-preview"])

    cli.main()

    assert capsys.readouterr().out.strip() == "{'ok': True}"
