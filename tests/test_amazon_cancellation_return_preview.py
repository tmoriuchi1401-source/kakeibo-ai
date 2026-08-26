from __future__ import annotations

from email.message import EmailMessage

from app.amazon_cancellation_return_preview import (
    _build_review_plan,
    _review_key,
    diagnose_cancellation_order_id,
    diagnose_forwarded_cancellation_order_id,
    fetch_gmail_thread_messages,
    preview_amazon_cancellation_returns,
)
from app.amazon_gmail_storage import GmailRawMessage
from app.amazon_email import parse_amazon_email


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


def _thread_message(name: str, thread_id: str, subject: str, body: str) -> GmailRawMessage:
    return GmailRawMessage(name, thread_id, _raw(subject, body))


def _html_raw(subject: str, html: str) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "Amazon.co.jp <no-reply@amazon.co.jp>"
    message["To"] = "private@example.invalid"
    message.set_content("HTML message")
    message.add_alternative(html, subtype="html")
    return message.as_bytes()


def _preview(messages, *, thread_fetcher=None, parser=None, db=None):
    by_thread = {
        message.thread_id: [
            candidate for candidate in messages
            if candidate.thread_id == message.thread_id
        ]
        for message in messages
    }
    return preview_amazon_cancellation_returns(
        object(),
        fetcher=lambda service: messages,
        db=db,
        thread_fetcher=(
            thread_fetcher or (lambda service, thread_id: by_thread[thread_id])
        ),
        **({"parser": parser} if parser else {}),
    )


class MatchingDB:
    def __init__(
        self, *, orders=None, headers=None, events=None, existing_reviews=None,
        fail=False,
    ):
        self.rows = {
            "Amazon注文!A2:O": list(orders or []),
            "Amazon注文ヘッダ!A2:O": list(headers or []),
            "Amazonイベント!A2:X": list(events or []),
            "要確認!A2:A": list(existing_reviews or []),
        }
        self.fail = fail
        self.reads = []

    def get(self, rng):
        self.reads.append(rng)
        if self.fail:
            raise RuntimeError("private read error")
        return self.rows[rng]

    def __getattr__(self, name):
        if name.startswith(("append", "update", "clear", "ensure")):
            raise AssertionError(f"write method must not be used: {name}")
        raise AttributeError(name)


def _order(order_id, *, order_date="2026-08-20", product="Secret Product", quantity=1,
           amount=1200, shipment_date=""):
    return [
        "key", order_id, "asin", order_date, product, quantity, amount,
        "payment", "major", "minor", "", "hash", "timestamp",
        shipment_date, quantity,
    ]


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
    assert result["cancellation_review_required_count"] == 1
    assert result["review_parser_error"] == 1
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


def test_thread_diagnostic_finds_one_unique_order_id_from_other_message():
    cancellation = _thread_message(
        "cancel-private", "thread-private", "注文のキャンセル",
        "この商品をキャンセルしました",
    )
    order = _thread_message(
        "order-private", "thread-private", "注文確認",
        "注文番号: 123-1234567-1234567",
    )

    result = _preview([cancellation, order])

    assert result["cancellation_thread_id_present"] == 1
    assert result["cancellation_thread_fetched"] == 1
    assert result["cancellation_thread_message_count_2plus"] == 1
    assert result["thread_other_message_count"] == 1
    assert result["thread_order_event_present"] == 1
    assert result["thread_other_order_id_present"] == 1
    assert result["thread_order_id_candidate_count_1"] == 1
    assert result["thread_unique_order_id_candidate_present"] == 1


def test_thread_diagnostic_deduplicates_same_order_id_across_messages():
    order_id = "123-1234567-1234567"
    messages = [
        _thread_message("cancel", "thread", "注文のキャンセル", "キャンセル"),
        _thread_message("order", "thread", "注文確認", f"注文番号: {order_id}"),
        _thread_message("delivery", "thread", "お届け済み", f"注文番号: {order_id}"),
    ]

    result = _preview(messages)

    assert result["thread_order_event_present"] == 1
    assert result["thread_delivery_event_present"] == 1
    assert result["thread_order_id_candidate_count_1"] == 1
    assert result["thread_unique_order_id_candidate_present"] == 1


def test_thread_diagnostic_rejects_two_distinct_order_ids_as_unique():
    messages = [
        _thread_message("cancel", "thread", "注文のキャンセル", "キャンセル"),
        _thread_message(
            "order-1", "thread", "注文確認", "注文番号: 123-1234567-1234567",
        ),
        _thread_message(
            "order-2", "thread", "注文確認", "注文番号: 987-7654321-7654321",
        ),
    ]

    result = _preview(messages)

    assert result["thread_order_id_candidate_count_2plus"] == 1
    assert result["thread_unique_order_id_candidate_present"] == 0


def test_thread_diagnostic_handles_single_message_and_missing_thread_id():
    single = _thread_message(
        "cancel", "thread", "注文のキャンセル", "キャンセル",
    )
    missing = GmailRawMessage(
        "cancel-no-thread", "", _raw("注文のキャンセル", "キャンセル"),
    )

    single_result = _preview([single])
    missing_result = _preview([missing])

    assert single_result["cancellation_thread_message_count_1"] == 1
    assert single_result["thread_other_message_count"] == 0
    assert single_result["thread_order_id_candidate_count_0"] == 1
    assert missing_result["cancellation_thread_id_present"] == 0
    assert missing_result["cancellation_thread_fetched"] == 0
    assert missing_result["thread_order_id_candidate_count_0"] == 1


def test_thread_fetch_failure_is_anonymous_and_does_not_stop_preview():
    cancellation = _thread_message(
        "private-message", "private-thread", "注文のキャンセル", "private body キャンセル",
    )

    result = _preview(
        [cancellation],
        thread_fetcher=lambda service, thread_id: (_ for _ in ()).throw(
            RuntimeError(f"failed {thread_id} private body")
        ),
    )

    assert result["cancellation count"] == 1
    assert result["cancellation_thread_fetch_errors"] == 1
    assert result["thread_unique_order_id_candidate_present"] == 0
    assert "private-thread" not in str(result)
    assert "private body" not in str(result)


def test_thread_parser_error_prevents_unique_candidate():
    cancellation = _thread_message(
        "cancel", "thread", "注文のキャンセル", "キャンセル",
    )
    good = _thread_message(
        "good", "thread", "注文確認", "注文番号: 123-1234567-1234567",
    )
    broken = _thread_message("broken", "thread", "broken", "broken private body")

    def parser(raw):
        if b"broken private body" in raw:
            raise ValueError("private parser error")
        return parse_amazon_email(raw)

    result = _preview([cancellation, good, broken], parser=parser)

    assert result["thread_other_parser_errors"] == 1
    assert result["thread_order_id_candidate_count_1"] == 1
    assert result["thread_unique_order_id_candidate_present"] == 0


def test_thread_fetcher_uses_threads_and_raw_message_gets():
    import base64

    raw = _raw("注文確認", "注文番号: 123-1234567-1234567")
    calls = []

    class Request:
        def __init__(self, response):
            self.response = response

        def execute(self):
            return self.response

    class Threads:
        def get(self, **kwargs):
            calls.append(("thread", kwargs))
            return Request({"messages": [{"id": "message-private"}]})

    class Messages:
        def get(self, **kwargs):
            calls.append(("message", kwargs))
            encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
            return Request({"threadId": "thread-private", "raw": encoded})

    class Users:
        def threads(self):
            return Threads()

        def messages(self):
            return Messages()

    class Service:
        def users(self):
            return Users()

    messages = fetch_gmail_thread_messages(Service(), "thread-private")

    assert len(messages) == 1
    assert calls == [
        ("thread", {
            "userId": "me", "id": "thread-private", "format": "minimal",
        }),
        ("message", {
            "userId": "me", "id": "message-private", "format": "raw",
        }),
    ]


def test_order_matching_candidate_count_zero():
    cancellation = _thread_message(
        "cancel", "thread", "注文のキャンセル",
        "対象商品: Unknown Product\n数量: 3\nキャンセル",
    )
    db = MatchingDB(orders=[_order(
        "123-1234567-1234567", order_date="2026-01-01",
        product="Different Product", quantity=1,
    )])

    result = _preview([cancellation], db=db)

    assert result["cancellation_without_order_id_count"] == 1
    assert result["candidate_count_0"] == 1
    assert result["unique_candidate_strong"] == 0
    assert result["cancellation_review_required_count"] == 1
    assert result["review_missing_order_id"] == 1
    assert result["review_no_candidate"] == 1


def test_order_matching_unique_candidate_strong():
    product = "Private Product Name"
    amount = 1200
    cancellation = _thread_message(
        "cancel", "thread", "注文のキャンセル",
        f"対象商品: {product}\n数量: 2\n金額: {amount}円\nキャンセル",
    )
    db = MatchingDB(orders=[_order(
        "123-1234567-1234567", product=product, quantity=2, amount=amount,
    )])

    result = _preview([cancellation], db=db)

    assert result["candidate_count_1"] == 1
    assert result["date_window_match_present"] == 1
    assert result["product_match_present"] == 1
    assert result["quantity_match_present"] == 1
    assert result["amount_match_present"] == 1
    assert result["unique_candidate_strong"] == 1
    assert result["cancellation_review_required_count"] == 0
    assert result["cancellation_review_not_required_strong_count"] == 1
    assert result["review_planned_new_count"] == 0


def test_order_matching_unique_candidate_medium_for_product_and_date():
    product = "Private Product Name"
    cancellation = _thread_message(
        "cancel", "thread", "注文のキャンセル",
        f"対象商品: {product}\nキャンセル",
    )
    db = MatchingDB(orders=[_order(
        "123-1234567-1234567", product=product, quantity=9, amount=9999,
    )])

    result = _preview([cancellation], db=db)

    assert result["candidate_count_1"] == 1
    assert result["unique_candidate_medium"] == 1
    assert result["unique_candidate_strong"] == 0
    assert result["cancellation_review_required_count"] == 1
    assert result["review_unique_but_not_strong"] == 1


def test_order_matching_unique_candidate_weak_for_date_only():
    cancellation = _thread_message(
        "cancel", "thread", "注文のキャンセル", "キャンセル",
    )
    db = MatchingDB(orders=[_order(
        "123-1234567-1234567", product="Different Product",
    )])

    result = _preview([cancellation], db=db)

    assert result["candidate_count_1"] == 1
    assert result["date_window_match_present"] == 1
    assert result["unique_candidate_weak"] == 1
    assert result["unique_candidate_strong"] == 0
    assert result["cancellation_review_required_count"] == 1
    assert result["review_unique_but_not_strong"] == 1


def test_order_matching_two_candidates_are_not_unique():
    cancellation = _thread_message(
        "cancel", "thread", "注文のキャンセル", "キャンセル",
    )
    db = MatchingDB(orders=[
        _order("123-1234567-1234567"),
        _order("987-7654321-7654321"),
    ])

    result = _preview([cancellation], db=db)

    assert result["candidate_count_2plus"] == 1
    assert result["unique_candidate_strong"] == 0
    assert result["unique_candidate_medium"] == 0
    assert result["unique_candidate_weak"] == 0
    assert result["cancellation_review_required_count"] == 1
    assert result["review_multiple_candidates"] == 1


def test_amount_only_match_is_weak_and_output_is_anonymous():
    order_id = "123-1234567-1234567"
    product = "Private Product Name"
    amount = 1200
    cancellation = _thread_message(
        "private-message", "private-thread", "注文のキャンセル",
        f"金額: {amount}円\nキャンセル",
    )
    db = MatchingDB(orders=[_order(
        order_id, order_date="", product=product, quantity=9, amount=amount,
    )])

    result = _preview([cancellation], db=db)
    rendered = str(result)

    assert result["amount_match_present"] == 1
    assert result["unique_candidate_weak"] == 1
    assert result["unique_candidate_strong"] == 0
    for private_value in (order_id, product, str(amount), "private-thread", "private-message"):
        assert private_value not in rendered


def test_matching_read_error_never_produces_strong_or_writes():
    cancellation = _thread_message(
        "cancel", "thread", "注文のキャンセル",
        "対象商品: Private Product Name\n数量: 2\n金額: 1200円\nキャンセル",
    )
    db = MatchingDB(fail=True)

    result = _preview([cancellation], db=db)

    assert result["matching_source_read_errors"] == 1
    assert result["candidate_count_0"] == 1
    assert result["unique_candidate_strong"] == 0
    assert result["cancellation_review_required_count"] == 1
    assert result["review_source_read_error"] == 1
    assert db.reads == ["Amazon注文!A2:O", "要確認!A2:A"]


def test_review_duplicate_key_is_stable_and_existing_row_is_not_planned_again():
    cancellation = _thread_message(
        "private-message", "private-thread", "注文のキャンセル", "キャンセル",
    )
    first_key = _review_key(cancellation.raw_mime)
    second_key = _review_key(cancellation.raw_mime)
    db = MatchingDB(existing_reviews=[[first_key]])

    result = _preview([cancellation], db=db)
    rendered = str(result)

    assert first_key == second_key
    assert result["review_duplicate_key_available"] == 1
    assert result["review_existing_duplicate_detected"] == 1
    assert result["review_planned_new_count"] == 0
    assert first_key not in rendered
    assert "private-message" not in rendered
    assert "private-thread" not in rendered


def test_review_plan_has_future_logical_row_without_exposing_it_in_summary():
    raw = _raw("注文のキャンセル", "キャンセル")
    plan = _build_review_plan(
        raw,
        source_hash="a" * 64,
        event_date="2026-08-24",
        matching={"candidate_count_2plus": 1},
        cancellation_scope="partial_likely",
    )

    assert plan is not None
    assert plan.source_type == "amazon_cancellation"
    assert plan.status == "要確認"
    assert plan.reasons == ("missing_order_id", "multiple_candidates")
    assert plan.event_date == "2026-08-24"
    assert plan.candidate_count == "2plus"
    assert plan.cancellation_scope == "partial_likely"
    assert plan.review_id == plan.source_event_key


def test_cli_uses_readonly_gmail_and_sheets(monkeypatch, capsys):
    import sys

    import app.cli as cli

    gmail_service = object()

    class FakeSettings:
        gmail_token_json = "readonly-token"
        spreadsheet_id = "private-sheet"

        def validate(self, **kwargs):
            assert kwargs == {"need_gmail": True, "need_sheet": True}

    monkeypatch.setattr(cli, "Settings", FakeSettings)
    monkeypatch.setattr(
        cli, "gmail_readonly_service",
        lambda token: gmail_service if token == "readonly-token" else None,
    )
    sheets_service = object()
    expected_db = object()
    monkeypatch.setattr(cli, "read_only_sheets_service", lambda: sheets_service)
    monkeypatch.setattr(
        cli, "SheetsDB",
        lambda spreadsheet_id, service: (
            expected_db if (spreadsheet_id, service) == ("private-sheet", sheets_service)
            else None
        ),
    )
    monkeypatch.setattr(
        cli, "preview_amazon_cancellation_returns",
        lambda service, db=None: {
            "gmail_read_only": service is gmail_service,
            "sheets_read_only": db is expected_db,
        },
    )
    monkeypatch.setattr(
        sys, "argv", ["kakeibo", "amazon-cancellation-return-preview"],
    )

    cli.main()

    assert capsys.readouterr().out.strip() == (
        "{'gmail_read_only': True, 'sheets_read_only': True}"
    )
