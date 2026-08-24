from __future__ import annotations

import pytest

from app.amazon_daily_import import run_amazon_daily_import


def _gmail_summary(new):
    return {
        "fetched": 3,
        "new": new,
        "duplicate_gmail_id": 1 if not new else 0,
        "duplicate_rfc_message_id": 0,
        "duplicate_source_hash": 0,
        "parser_errors": 0,
        "unknown": 0,
    }


def _header_summary(created):
    return {
        "created": created,
        "skipped_existing": 1 if not created else 0,
        "skipped_missing_order_id": 0,
    }


def test_imports_gmail_before_creating_order_headers_and_combines_summary():
    calls = []
    service = object()
    db = object()

    def import_gmail(actual_service, actual_db):
        assert (actual_service, actual_db) == (service, db)
        calls.append("gmail")
        return _gmail_summary(2)

    def create_headers(actual_db):
        assert actual_db is db
        calls.append("headers")
        return _header_summary(1)

    result = run_amazon_daily_import(
        service,
        db,
        gmail_importer=import_gmail,
        order_header_creator=create_headers,
    )

    assert calls == ["gmail", "headers"]
    assert result == {
        "Gmail fetched": 3,
        "Amazonイベント new": 2,
        "duplicate Gmail ID": 0,
        "duplicate RFC Message-ID": 0,
        "duplicate source hash": 0,
        "parser errors": 0,
        "unknown": 0,
        "Amazon注文ヘッダ created": 1,
        "skipped existing": 0,
        "skipped missing order_id": 0,
    }


def test_second_run_reports_no_new_events_or_headers():
    run_number = 0

    def import_gmail(service, db):
        return _gmail_summary(1 if run_number == 0 else 0)

    def create_headers(db):
        nonlocal run_number
        result = _header_summary(1 if run_number == 0 else 0)
        run_number += 1
        return result

    first = run_amazon_daily_import(
        object(), object(), gmail_importer=import_gmail,
        order_header_creator=create_headers,
    )
    second = run_amazon_daily_import(
        object(), object(), gmail_importer=import_gmail,
        order_header_creator=create_headers,
    )

    assert first["Amazonイベント new"] == 1
    assert first["Amazon注文ヘッダ created"] == 1
    assert second["Amazonイベント new"] == 0
    assert second["Amazon注文ヘッダ created"] == 0
    assert second["duplicate Gmail ID"] == 1
    assert second["skipped existing"] == 1


def test_gmail_import_failure_does_not_start_header_creation():
    header_called = False

    def fail_gmail(service, db):
        raise RuntimeError("gmail failed")

    def create_headers(db):
        nonlocal header_called
        header_called = True
        return _header_summary(1)

    with pytest.raises(RuntimeError, match="gmail failed"):
        run_amazon_daily_import(
            object(), object(), gmail_importer=fail_gmail,
            order_header_creator=create_headers,
        )

    assert header_called is False


def test_cli_uses_readonly_gmail_and_writable_sheets(monkeypatch, capsys):
    import sys

    import app.cli as cli

    gmail_service = object()
    db = object()
    summary = {"Gmail fetched": 1, "Amazon注文ヘッダ created": 1}

    class FakeSettings:
        spreadsheet_id = "sheet-id"
        gmail_token_json = "readonly-token"

        def validate(self, **kwargs):
            assert kwargs == {"need_sheet": True, "need_gmail": True}

    monkeypatch.setattr(cli, "Settings", FakeSettings)
    monkeypatch.setattr(
        cli, "SheetsDB", lambda spreadsheet_id: db if spreadsheet_id == "sheet-id" else None,
    )
    monkeypatch.setattr(
        cli, "gmail_readonly_service",
        lambda token: gmail_service if token == "readonly-token" else None,
    )
    monkeypatch.setattr(
        cli, "run_amazon_daily_import",
        lambda service, sheets_db: summary
        if service is gmail_service and sheets_db is db else None,
    )
    monkeypatch.setattr(sys, "argv", ["kakeibo", "amazon-daily-import"])

    cli.main()

    assert capsys.readouterr().out.strip() == str(summary)
