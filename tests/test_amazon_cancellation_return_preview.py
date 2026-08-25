from __future__ import annotations

from email.message import EmailMessage

from app.amazon_cancellation_return_preview import (
    preview_amazon_cancellation_returns,
)
from app.amazon_gmail_storage import GmailRawMessage


def _raw(subject: str, body: str) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "Amazon.co.jp <no-reply@amazon.co.jp>"
    message["To"] = "private@example.invalid"
    message["Date"] = "Mon, 24 Aug 2026 12:00:00 +0900"
    message["Message-ID"] = "<private@example.invalid>"
    message.set_content(body)
    return message.as_bytes()


def _message(name: str, subject: str, body: str) -> GmailRawMessage:
    return GmailRawMessage(name, f"thread-{name}", _raw(subject, body))


def _preview(messages):
    return preview_amazon_cancellation_returns(
        object(), fetcher=lambda service: messages,
    )


def test_counts_cancellation_and_return_presence_and_clues():
    result = _preview([
        _message("c1", "注文のキャンセル", """
注文番号: 123-1234567-1234567
注文をキャンセルしました
"""),
        _message("r1", "返品リクエスト", """
注文番号: 123-1234567-1234567
返品商品
数量: 1
返金予定額
"""),
    ])

    assert result["fetched Amazon messages"] == 2
    assert result["cancellation count"] == 1
    assert result["return count"] == 1
    assert result["cancellation order_id present"] == 1
    assert result["return order_id present"] == 1
    assert result["cancellation event_date present"] == 1
    assert result["return event_date present"] == 1
    assert result["full cancellation clue present"] == 1
    assert result["cancellation full_likely"] == 1
    assert result["return item clue present"] == 1
    assert result["return quantity clue present"] == 1
    assert result["return amount clue present"] == 1
    assert result["return item_specific"] == 1


def test_classifies_partial_and_ambiguous_cancellations():
    result = _preview([
        _message("partial", "注文のキャンセル", "この商品をキャンセルしました"),
        _message("ambiguous", "注文のキャンセル", "キャンセルされました"),
    ])

    assert result["partial cancellation clue present"] == 1
    assert result["cancellation partial_likely"] == 1
    assert result["ambiguous cancellation"] == 1


def test_classifies_order_level_and_ambiguous_returns():
    result = _preview([
        _message("order", "返品リクエスト", "ご注文全体の返品を受け付けました"),
        _message("ambiguous", "返品リクエスト", "返品を受け付けました"),
    ])

    assert result["return order_level"] == 1
    assert result["return ambiguous"] == 1


def test_parser_errors_are_anonymously_attributed_by_clue():
    messages = [_message("broken", "注文のキャンセル", "private body")]
    result = preview_amazon_cancellation_returns(
        object(), fetcher=lambda service: messages,
        parser=lambda raw: (_ for _ in ()).throw(ValueError("private parser error")),
    )

    assert result["cancellation parser errors"] == 1
    assert "private" not in str(result)


def test_cli_uses_readonly_gmail_without_constructing_sheets(monkeypatch, capsys):
    import sys

    import app.cli as cli

    gmail_service = object()

    class FakeSettings:
        gmail_token_json = "readonly-token"

        def validate(self, **kwargs):
            assert kwargs == {"need_gmail": True}

    monkeypatch.setattr(cli, "Settings", FakeSettings)
    monkeypatch.setattr(
        cli, "gmail_readonly_service",
        lambda token: gmail_service if token == "readonly-token" else None,
    )
    monkeypatch.setattr(
        cli, "preview_amazon_cancellation_returns",
        lambda service: {"read_only": service is gmail_service},
    )
    monkeypatch.setattr(
        cli, "SheetsDB", lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Sheets must not be constructed")
        ),
    )
    monkeypatch.setattr(
        sys, "argv", ["kakeibo", "amazon-cancellation-return-preview"],
    )

    cli.main()

    assert capsys.readouterr().out.strip() == "{'read_only': True}"
