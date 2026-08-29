import sys

from app import cli
from app.amazon_cancellation_reconciliation_preview import (
    preview_amazon_cancellation_reconciliation,
)


ORDER_ID = "123-TEST-ORDER"


def _event(*, order_id=ORDER_ID, quantity=2, event_type="cancellation"):
    row = [""] * 24
    row[5] = event_type
    row[6] = order_id
    row[17] = quantity
    return row


def _header(
    *, order_id=ORDER_ID, quantity=2, status="cancelled", order_amount=1000,
    charged_amount="", refund_status="none", refund_amount="",
):
    row = [""] * 15
    row[0] = order_id
    row[2] = order_amount
    row[4] = quantity
    row[5] = status
    row[6] = charged_amount
    row[7] = refund_status
    row[8] = refund_amount
    return row


def _order(*, order_id=ORDER_ID, quantity=2, amount=1000):
    row = [""] * 15
    row[1] = order_id
    row[5] = quantity
    row[6] = amount
    return row


def _transaction(
    import_id="card:1", *, source="au PAYカード", amount=1000,
    status="matched_amazon", target_id=f"amazon:{ORDER_ID}",
):
    return [
        import_id, "2026-08-20", source, "", "2026-08-19", "Amazon.co.jp",
        amount, "", status, target_id, "", "",
    ]


class ReadOnlyDB:
    def __init__(self, *, events=None, headers=None, orders=None, transactions=None):
        self.values = {
            "Amazon注文!A2:O": list(orders or []),
            "Amazon注文ヘッダ!A2:O": list(headers or []),
            "取込データ!A2:L": list(transactions or []),
            "Amazonイベント!A2:X": list(events or []),
        }
        self.reads = []

    def get(self, rng):
        self.reads.append(rng)
        return self.values[rng]

    def __getattr__(self, name):
        if name.startswith(("append", "update", "clear", "ensure", "configure")):
            raise AssertionError(f"write method must not be used: {name}")
        raise AttributeError(name)


def _preview(*, event=None, header=None, transactions=None):
    db = ReadOnlyDB(
        events=[event or _event()], headers=[header or _header()],
        orders=[_order()], transactions=transactions,
    )
    return preview_amazon_cancellation_reconciliation(db), db


def test_full_order_with_explicit_zero_charge_is_ready_to_close():
    result, _ = _preview(header=_header(charged_amount=0))

    row = result["rows"][0]
    assert row["reconciliation_action"] == "ready_to_close"
    assert row["reason"] == "full_order_cancelled_no_charge_found"


def test_full_order_without_safe_payment_observation_waits_for_payment():
    result, _ = _preview()

    assert result["rows"][0]["reconciliation_action"] == "wait_payment"
    assert result["rows"][0]["reason"] == "insufficient_payment_data"


def test_full_order_charge_without_refund_waits_for_refund():
    result, _ = _preview(transactions=[_transaction()])

    row = result["rows"][0]
    assert row["reconciliation_action"] == "wait_refund"
    assert row["reason"] == "full_order_cancelled_refund_pending"
    assert row["payment_information"]["matched_payment_amount"] == 1000


def test_full_order_charge_and_full_refund_is_ready_to_close():
    result, _ = _preview(
        header=_header(
            charged_amount=1000, refund_status="full", refund_amount=1000,
        ),
        transactions=[_transaction()],
    )

    assert result["rows"][0]["reconciliation_action"] == "ready_to_close"
    assert result["rows"][0]["reason"] == "full_order_cancelled_refund_matched"


def test_partial_charge_waits_for_refund_without_calculating_residual_amount():
    result, _ = _preview(event=_event(quantity=1), transactions=[_transaction()])

    row = result["rows"][0]
    assert row["cancellation_scope"] == "item_or_partial"
    assert row["reconciliation_action"] == "wait_refund"
    assert row["reason"] == "partial_cancel_refund_pending"
    assert "remaining_amount" not in row


def test_partial_with_refund_still_requires_review():
    result, _ = _preview(
        event=_event(quantity=1),
        header=_header(charged_amount=1000, refund_status="partial", refund_amount=400),
    )

    assert result["rows"][0]["reconciliation_action"] == "needs_review"
    assert result["rows"][0]["reason"] == "partial_cancel_amount_unresolved"


def test_unknown_scope_and_missing_header_require_review():
    unknown, _ = _preview(event=_event(quantity=""))
    missing = preview_amazon_cancellation_reconciliation(ReadOnlyDB(
        events=[_event()], orders=[_order()],
    ))

    assert unknown["rows"][0]["reason"] == "cancellation_scope_unknown"
    assert missing["rows"][0]["reconciliation_action"] == "needs_review"
    assert missing["rows"][0]["amazon_status"] == "unknown"
    assert missing["rows"][0]["reason"] == "insufficient_payment_data"


def test_ambiguous_payment_match_requires_review():
    transactions = [
        _transaction("card:1"),
        _transaction("card:2", source="PayPay"),
    ]
    result, _ = _preview(transactions=transactions)

    assert result["rows"][0]["reconciliation_action"] == "needs_review"
    assert result["rows"][0]["reason"] == "payment_match_ambiguous"


def test_not_yet_cancelled_order_is_no_action():
    result, _ = _preview(header=_header(status="ordered", charged_amount=0))

    assert result["rows"][0]["reconciliation_action"] == "no_action"
    assert result["rows"][0]["reason"] == "amazon_order_not_cancelled"


def test_summary_has_all_actions_and_scope_counts_and_reads_only():
    result, db = _preview(header=_header(charged_amount=0))

    assert result["sampled_cancellation_count"] == 1
    assert result["action_counts"] == {
        "no_action": 0, "wait_payment": 0, "wait_refund": 0,
        "ready_to_close": 1, "needs_review": 0,
    }
    assert result["full_order_count"] == 1
    assert result["item_or_partial_count"] == result["unknown_count"] == 0
    assert result["needs_review_count"] == 0
    assert db.reads == [
        "Amazon注文!A2:O", "Amazon注文ヘッダ!A2:O",
        "取込データ!A2:L", "Amazonイベント!A2:X",
    ]


def test_cli_uses_read_only_sheets(monkeypatch, capsys):
    class Settings:
        spreadsheet_id = "sheet-id"

        def validate(self, **kwargs):
            assert kwargs == {"need_sheet": True}

    service = object()
    db = object()
    monkeypatch.setattr(cli, "Settings", Settings)
    monkeypatch.setattr(cli, "read_only_sheets_service", lambda: service)
    monkeypatch.setattr(cli, "SheetsDB", lambda spreadsheet_id, service=None: db)
    monkeypatch.setattr(
        cli, "preview_amazon_cancellation_reconciliation",
        lambda value: {"sampled_cancellation_count": int(value is db)},
    )
    monkeypatch.setattr(
        sys, "argv", ["kakeibo", "amazon-cancellation-reconciliation-preview"],
    )

    cli.main()

    assert capsys.readouterr().out.strip() == "{'sampled_cancellation_count': 1}"
