from __future__ import annotations

from email.message import EmailMessage

from app.amazon_cancellation_return_preview import (
    diagnose_cancellation_order_id,
    diagnose_forwarded_cancellation_order_id,
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


def _html_raw(subject: str, html: str) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "Amazon.co.jp <no-reply@amazon.co.jp>"
    message["To"] = "private@example.invalid"
    message.set_content("HTML message")
    message.add_alternative(html, subtype="html")
    return message.as_bytes()


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


def test_order_id_diagnostic_finds_normal_patterns_by_source():
    order_id = "123-1234567-1234567"

    subject = diagnose_cancellation_order_id(_raw(f"キャンセル {order_id}", "none"))
    plain = diagnose_cancellation_order_id(_raw("キャンセル", order_id))
    html = diagnose_cancellation_order_id(_html_raw(
        "キャンセル", f'<a href="https://example.invalid/orders/{order_id}">{order_id}</a>',
    ))

    assert subject["subject_order_id_pattern_present"]
    assert plain["plain_order_id_pattern_present"]
    assert html["html_visible_order_id_pattern_present"]
    assert html["html_raw_order_id_pattern_present"]
    assert html["href_order_id_pattern_present"]


def test_order_id_diagnostic_classifies_noncanonical_candidates():
    unicode_dash = diagnose_cancellation_order_id(
        _raw("キャンセル", "注文番号: 123−1234567−1234567"),
    )
    fullwidth = diagnose_cancellation_order_id(
        _raw("キャンセル", "注文ID: １２３-１２３４５６７-１２３４５６７"),
    )
    alternate = diagnose_cancellation_order_id(
        _raw("キャンセル", "Order #: 123 1234567 1234567"),
    )

    assert unicode_dash["unicode_dash_candidate_present"]
    assert fullwidth["fullwidth_digit_candidate_present"]
    assert alternate["alternate_format_candidate_present"]
    assert alternate["label_near_numeric_candidate_present"]


def test_order_id_diagnostic_finds_html_tag_split_candidate():
    result = diagnose_cancellation_order_id(_html_raw(
        "キャンセル", "注文番号: 123-<span>1234567</span>-1234567",
    ))

    assert result["split_order_id_candidate_present"]
    assert result["html_visible_order_id_pattern_present"]
    assert not result["html_raw_order_id_pattern_present"]

    newline = diagnose_cancellation_order_id(
        _raw("キャンセル", "注文番号: 123-1234567-\n1234567"),
    )
    assert newline["split_order_id_candidate_present"]


def test_preview_order_id_diagnostics_are_anonymous():
    order_id = "123-1234567-1234567"
    result = _preview([_message(
        "private-message-id", "注文のキャンセル", f"注文番号: {order_id}",
    )])
    rendered = str(result)

    assert result["plain_order_id_pattern_present"] == 1
    assert order_id not in rendered
    assert "注文番号" not in rendered
    assert "private-message-id" not in rendered


def test_forwarded_diagnostic_finds_order_id_in_nested_rfc822():
    order_id = "123-1234567-1234567"
    nested = EmailMessage()
    nested["Subject"] = "Amazon cancellation"
    nested.set_content(f"Order ID: {order_id}")
    outer = EmailMessage()
    outer["Subject"] = "Forwarded message"
    outer.set_content("Forwarded attachment")
    outer.add_attachment(nested)

    result = diagnose_forwarded_cancellation_order_id(outer.as_bytes())

    assert result["nested_rfc822_present"]
    assert result["nested_order_id_pattern_present"]
    assert result["forwarded_order_id_candidate_count_1"]


def test_forwarded_diagnostic_finds_header_and_original_subject_order_id():
    order_id = "123-1234567-1234567"
    result = diagnose_forwarded_cancellation_order_id(_raw(
        "Fwd: cancellation",
        f"""---------- Forwarded message ---------
From: sender@example.invalid
Date: Mon, 24 Aug 2026 12:00:00 +0900
Subject: Cancellation for {order_id}
To: recipient@example.invalid

Original body
""",
    ))

    assert result["forwarded_message_clue_present"]
    assert result["forwarded_header_block_present"]
    assert result["forwarded_header_order_id_pattern_present"]
    assert result["original_subject_clue_present"]
    assert result["original_subject_order_id_pattern_present"]


def test_forwarded_diagnostic_finds_order_id_in_quoted_block():
    order_id = "123-1234567-1234567"
    result = diagnose_forwarded_cancellation_order_id(_raw(
        "Fwd: cancellation", f"> 注文番号: {order_id}\n> キャンセルされました",
    ))

    assert result["quoted_block_present"]
    assert result["quoted_order_id_pattern_present"]
    assert result["forwarded_order_id_candidate_count_1"]

    html_result = diagnose_forwarded_cancellation_order_id(_html_raw(
        "Fwd: cancellation",
        f'<div class="yahoo_quoted">注文番号: {order_id}</div>',
    ))
    assert html_result["quoted_block_present"]
    assert html_result["quoted_order_id_pattern_present"]


def test_forwarded_candidate_count_classifies_zero_one_and_two_plus():
    zero = diagnose_forwarded_cancellation_order_id(_raw(
        "Fwd: cancellation", "> no order id",
    ))
    one = diagnose_forwarded_cancellation_order_id(_raw(
        "Fwd: cancellation", "> 123-1234567-1234567",
    ))
    two = diagnose_forwarded_cancellation_order_id(_raw(
        "Fwd: cancellation",
        "> 123-1234567-1234567\n> 987-7654321-7654321",
    ))

    assert zero["forwarded_order_id_candidate_count_0"]
    assert one["forwarded_order_id_candidate_count_1"]
    assert one["forwarded_order_id_unique_candidate_present"]
    assert two["forwarded_order_id_candidate_count_2plus"]


def test_forwarded_candidate_count_deduplicates_and_output_is_anonymous():
    order_id = "123-1234567-1234567"
    raw = _raw(
        "Fwd: cancellation",
        f"""-----Original Message-----
From: sender@example.invalid
Subject: Order {order_id}

> Duplicate order {order_id}
> Private original body
""",
    )

    result = diagnose_forwarded_cancellation_order_id(raw)
    rendered = str(result)

    assert result["forwarded_order_id_candidate_count_1"]
    assert order_id not in rendered
    assert "Private original body" not in rendered


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
