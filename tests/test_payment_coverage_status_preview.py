from datetime import date
import sys

from app import cli
from app.payment_coverage_status_preview import (
    CoverageEvidence,
    evaluate_coverage,
    overall_status,
    preview_payment_coverage_status,
)


ORDER_ID = "249-4045234-9353402"


def _event(event_type, event_date):
    row = [""] * 24
    row[5] = event_type
    row[6] = ORDER_ID
    row[7] = event_date
    row[22] = "2026-08-29T00:00:00+00:00"
    return row


def _header():
    row = [""] * 15
    row[0] = ORDER_ID
    row[1] = "2026-08-27"
    row[5] = "cancelled"
    return row


def _import(source, transaction_date, imported_at="2026-08-29 09:00:00"):
    return [
        f"{source}:{transaction_date}", imported_at, source, "", transaction_date,
        "店舗", 100, "", "status", "", "hash", "",
    ]


class ReadOnlyDB:
    def __init__(self, imports=None):
        self.values = {
            "取込データ!A2:L": list(imports or []),
            "Amazonイベント!A2:X": [
                _event("order", "2026-08-27"),
                _event("cancellation", "2026-08-27"),
            ],
            "Amazon注文ヘッダ!A2:O": [_header()],
        }
        self.reads = []

    def get(self, rng):
        self.reads.append(rng)
        return self.values[rng]

    def __getattr__(self, name):
        if name.startswith(("append", "update", "clear", "ensure", "configure")):
            raise AssertionError(f"write method used: {name}")
        raise AttributeError(name)


def test_no_coverage_evidence_is_unknown():
    result = evaluate_coverage(CoverageEvidence(source="paypay"))

    assert result["coverage_status"] == "unknown"
    assert result["completeness_reason"] == "no_import_completeness_record"


def test_partial_observed_range_is_incomplete():
    result = evaluate_coverage(
        CoverageEvidence(
            source="paypay", coverage_start="2026-08-28", coverage_end="2026-08-29",
        ),
        required_start=date(2026, 8, 27), required_end=date(2026, 8, 29),
    )

    assert result["coverage_status"] == "incomplete"
    assert result["covers_required_window"] is False


def test_covering_range_without_completion_proof_is_unknown():
    result = evaluate_coverage(
        CoverageEvidence(
            source="paypay", coverage_start="2026-08-01", coverage_end="2026-08-31",
        ),
        required_start=date(2026, 8, 27), required_end=date(2026, 8, 29),
    )

    assert result["covers_required_window"] is True
    assert result["coverage_status"] == "unknown"


def test_explicit_pagination_completion_can_prove_complete():
    result = evaluate_coverage(
        CoverageEvidence(
            source="fixture", coverage_start="2026-08-01", coverage_end="2026-08-31",
            completion_proven=True, evidence_type="pagination_manifest",
            completeness_reason="all_pages_fetched",
        ),
        required_start=date(2026, 8, 27), required_end=date(2026, 8, 29),
    )

    assert result["coverage_status"] == "complete"
    assert result["completeness_reason"] == "all_pages_fetched"


def test_unknown_source_makes_overall_unknown():
    assert overall_status([
        {"coverage_status": "complete"}, {"coverage_status": "unknown"},
    ]) == "unknown"


def test_explicit_incomplete_has_overall_precedence():
    assert overall_status([
        {"coverage_status": "complete"}, {"coverage_status": "unknown"},
        {"coverage_status": "incomplete"},
    ]) == "incomplete"


def test_preview_reports_source_ranges_and_order_window_without_writing():
    db = ReadOnlyDB(imports=[
        _import("au PAYカード", "2026-08-27"),
        _import("au PAY", "2026-08-28"),
        _import("PayPay", "2026-08-29"),
    ])
    result = preview_payment_coverage_status(db, as_of=date(2026, 8, 29))
    sources = {row["source"]: row for row in result["source_coverage"]}
    order = result["orders"][0]

    assert sources["au_pay_card"]["coverage_start"] == "2026-08-27"
    assert sources["au_pay_card"]["coverage_status"] == "unknown"
    assert sources["au_pay"]["coverage_status"] == "incomplete"
    assert sources["amazon_gmail"]["coverage_status"] == "incomplete"
    assert order["required_window"] == {
        "start": "2026-08-27", "end": "2026-08-29",
        "basis": "order_date_to_preview_date_no_fixed_grace_period",
    }
    assert order["source_coverage"]["paypay"]["coverage_status"] == "incomplete"
    assert order["overall_payment_coverage_status"] == "incomplete"
    assert db.reads == [
        "取込データ!A2:L", "Amazonイベント!A2:X", "Amazon注文ヘッダ!A2:O",
    ]


def test_preview_does_not_turn_observed_imported_data_range_complete():
    db = ReadOnlyDB(imports=[
        _import("PayPay", "2026-08-01"), _import("PayPay", "2026-08-31"),
    ])
    result = preview_payment_coverage_status(db, as_of=date(2026, 8, 29))
    order = result["orders"][0]

    assert order["source_coverage"]["paypay"]["covers_required_window"] is True
    assert order["source_coverage"]["paypay"]["coverage_status"] == "unknown"
    assert order["source_coverage"]["imported_data"]["coverage_status"] == "unknown"


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
        cli, "preview_payment_coverage_status",
        lambda value: {"read_only": value is db},
    )
    monkeypatch.setattr(sys, "argv", ["kakeibo", "payment-coverage-status-preview"])

    cli.main()

    assert capsys.readouterr().out.strip() == "{'read_only': True}"
