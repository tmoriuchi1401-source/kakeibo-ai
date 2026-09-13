import base64
import sys
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from app import cli
from app.aupay_mail_pipeline import (
    AUPAY_CARD_STATEMENT_AUTHORITY_STATUS,
    AuPayCardMailPipeline,
    _execute_gmail,
    collect_aupay_card_statement_authorities,
    parse_aupay_card_raw,
    parse_aupay_card_raw_partial,
    parse_aupay_card_statement_raw,
)
from app.settings import Settings


def detail(number, merchant, amount, date="2026年8月8日"):
    return f"""No.{number:03d}--------
▼ご利用日
{date}
▼ご利用金額
{amount:,}円
▼ご利用先
{merchant}
"""


def return_detail(number, merchant, amount, date="2026年8月8日", marker="返品"):
    return f"""No.{number:03d}--------
▼ご利用日
{date}
▼ご利用金額
-{amount:,}円({marker})
▼ご利用先
{merchant}
"""


def raw_message(*blocks, message_id="<unit-card-message@example.invalid>",
                subject="【ご利用詳細】au PAY カード"):
    message = EmailMessage()
    message["Subject"] = subject
    if message_id is not None:
        message["Message-ID"] = message_id
    message.set_content(
        "▼カード情報\nau PAY カード\n本会員さま ご利用分\n\n"
        + "\n".join(blocks)
    )
    return message.as_bytes()


def statement_message(*, amount=12345, payment_date="2026年9月10日",
                      cycle="2026年8月ご請求分",
                      message_id="<unit-statement@example.invalid>"):
    message = EmailMessage()
    message["Subject"] = f"【au PAY カード】{cycle} ご請求額確定のお知らせ"
    if message_id is not None:
        message["Message-ID"] = message_id
    message.set_content(
        f"{cycle}\nご請求額：{amount:,}円\nお支払日：{payment_date}\n"
    )
    return message.as_bytes()


def encoded(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class Request:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class Messages:
    def __init__(self, pages, raw_messages):
        self.pages = list(pages)
        self.raw_messages = raw_messages
        self.list_calls = []
        self.get_calls = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return Request(self.pages.pop(0) if self.pages else {"messages": []})

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return Request({"raw": self.raw_messages[kwargs["id"]]})


class GmailService:
    def __init__(self, pages, raw_messages):
        self.messages_api = Messages(pages, raw_messages)

    def users(self):
        return self

    def messages(self):
        return self.messages_api


class CardDB:
    def __init__(self):
        self.rows = []
        self.append_calls = []

    def import_ids(self):
        return {row[0] for row in self.rows}

    def get(self, rng):
        assert rng == "Amazon注文!A2:M"
        return []

    def append(self, sheet, rows):
        assert sheet == "取込データ"
        if not rows:
            return
        self.append_calls.append(rows)
        self.rows.extend(rows)


def service_for(raw, message_id="gmail-unit-1"):
    return GmailService(
        [{"messages": [{"id": message_id}]}],
        {message_id: encoded(raw)},
    )


def test_raw_parser_preserves_multiple_card_details():
    rows = parse_aupay_card_raw(raw_message(
        detail(1, "匿名給油所", 3549),
        detail(2, "匿名書店", 1050, "2026年8月13日"),
    ))

    assert [(row["date"], row["merchant"], row["amount"]) for row in rows] == [
        ("2026-08-08", "匿名給油所", 3549),
        ("2026-08-13", "匿名書店", 1050),
    ]
    assert rows[0]["import_id"].startswith("aupaycard-mail:")
    assert rows[0]["import_id"] != rows[1]["import_id"]
    assert [row["transaction_kind"] for row in rows] == ["purchase", "purchase"]


def test_card_statement_parser_extracts_issuer_total_date_cycle_and_identity():
    statement = parse_aupay_card_statement_raw(statement_message())

    assert statement.issuer == "au PAYカード"
    assert statement.statement_total_yen == 12345
    assert statement.payment_date == "2026-09-10"
    assert statement.billing_cycle == "2026-08"
    assert statement.statement_identity.startswith("aupay-card-statement:")
    assert statement.to_import_transaction().status == AUPAY_CARD_STATEMENT_AUTHORITY_STATUS


def test_usage_detail_mail_is_never_statement_total_authority():
    with pytest.raises(ValueError, match="usage_detail_forbidden"):
        parse_aupay_card_statement_raw(raw_message(detail(1, "匿名店舗", 1200)))


@pytest.mark.parametrize("raw", [
    statement_message(amount=0),
    statement_message(payment_date="不明"),
    statement_message(cycle="請求月不明"),
    statement_message(message_id=None),
])
def test_card_statement_parser_fails_closed_without_exact_authority(raw):
    with pytest.raises(ValueError):
        parse_aupay_card_statement_raw(raw)


def test_statement_collector_uses_gmail_readonly_shape_and_returns_counts_only():
    raw = statement_message()
    service = service_for(raw)

    statements, summary = collect_aupay_card_statement_authorities(
        service, 'from:kddi-fs.com subject:"ご請求額"', max_results=10,
    )

    assert len(statements) == 1
    assert summary["found"] == 1
    assert summary["parsed_statements"] == 1
    assert summary["rejected_messages"] == 0
    assert summary["write_attempted"] == 0
    assert service.messages_api.get_calls == [
        {"userId": "me", "id": "gmail-unit-1", "format": "raw"},
    ]
    assert "12345" not in str(summary)


def test_same_billing_cycle_resend_is_deduplicated_without_purchase_summing():
    first = statement_message(message_id="<statement-first@example.invalid>")
    resend = statement_message(message_id="<statement-resend@example.invalid>")
    service = GmailService(
        [{"messages": [{"id": "gmail-1"}, {"id": "gmail-2"}]}],
        {"gmail-1": encoded(first), "gmail-2": encoded(resend)},
    )

    statements, summary = collect_aupay_card_statement_authorities(
        service, 'from:kddi-fs.com subject:"ご請求額"', max_results=10,
    )

    assert len(statements) == 1
    assert summary["parsed_statements"] == 2
    assert summary["unique_statement_authorities"] == 1
    assert summary["duplicate_statement_notification"] == 1
    assert summary["conflicting_statement_cycle"] == 0


def test_conflicting_same_cycle_statement_notifications_fail_closed():
    first = statement_message(message_id="<statement-first@example.invalid>")
    conflict = statement_message(
        amount=12346, message_id="<statement-conflict@example.invalid>",
    )
    service = GmailService(
        [{"messages": [{"id": "gmail-1"}, {"id": "gmail-2"}]}],
        {"gmail-1": encoded(first), "gmail-2": encoded(conflict)},
    )

    statements, summary = collect_aupay_card_statement_authorities(
        service, 'from:kddi-fs.com subject:"ご請求額"', max_results=10,
    )

    assert statements == ()
    assert summary["conflicting_statement_cycle"] == 1
    assert summary["collection_complete"] is False


def test_partial_parser_preserves_signed_return_semantics():
    result = parse_aupay_card_raw_partial(raw_message(
        return_detail(1, "匿名返品先", 1200),
    ))

    assert result.status == "accepted"
    assert result.review_items == ()
    assert result.accepted_items[0].transaction_kind == "return"
    assert result.accepted_items[0].amount_yen == -1200
    with pytest.raises(ValueError, match="partial parse reconciliation"):
        parse_aupay_card_raw(raw_message(return_detail(1, "匿名返品先", 1200)))


def test_partial_parser_accepts_purchase_and_return_in_one_mail():
    result = parse_aupay_card_raw_partial(raw_message(
        detail(1, "匿名購入先", 1200),
        return_detail(2, "匿名返品先", 500),
    ))

    assert result.status == "accepted"
    assert [(item.transaction_kind, item.amount_yen) for item in result.accepted_items] == [
        ("purchase", 1200), ("return", -500),
    ]


def test_partial_parser_keeps_valid_item_when_sibling_is_malformed():
    malformed = """No.002--------
▼ご利用日
2026年8月9日
▼ご利用金額
500円
"""
    result = parse_aupay_card_raw_partial(raw_message(
        detail(1, "匿名購入先", 1200), malformed,
    ))

    assert result.status == "partial"
    assert len(result.accepted_items) == 1
    assert result.accepted_items[0].item_number == 1
    assert len(result.review_items) == 1
    review = result.review_items[0]
    assert review.item_number == 2
    assert review.reason == "line_item_required_field_invalid"
    assert review.field_reasons == ("merchant_missing",)
    assert review.evidence_classifications == ("merchant_field_unusable",)
    assert "匿名購入先" not in str(review)


def test_unknown_negative_format_remains_line_item_review():
    result = parse_aupay_card_raw_partial(raw_message(
        return_detail(1, "匿名調整先", 1200, marker="調整"),
    ))

    assert result.status == "rejected"
    assert result.accepted_items == ()
    assert result.review_items[0].field_reasons == ("amount_unknown_negative_format",)
    assert result.review_items[0].evidence_classifications == (
        "negative_amount", "deterministic_return_evidence_unavailable",
    )


def test_return_word_does_not_change_positive_item_to_return():
    positive_with_return_word = detail(1, "匿名購入先", 1200) + "備考: 返品\n"
    result = parse_aupay_card_raw_partial(raw_message(positive_with_return_word))

    assert result.status == "accepted"
    assert result.accepted_items[0].transaction_kind == "purchase"
    assert result.accepted_items[0].amount_yen == 1200


def test_raw_parser_accepts_multipart_message_when_plain_part_is_present():
    message = BytesParser(policy=policy.default).parsebytes(raw_message(
        detail(1, "fixture merchant", 1200),
    ))
    message.add_alternative("<p>non-authoritative HTML alternative</p>", subtype="html")

    rows = parse_aupay_card_raw(message.as_bytes())

    assert len(rows) == 1
    assert rows[0]["amount"] == 1200


def test_raw_parser_fails_closed_for_html_only_message():
    source = BytesParser(policy=policy.default).parsebytes(raw_message(
        detail(1, "fixture merchant", 1200),
    ))
    message = EmailMessage()
    message["Subject"] = source["Subject"]
    message["Message-ID"] = source["Message-ID"]
    message.set_content("<p>HTML-only card notice</p>", subtype="html")

    with pytest.raises(ValueError, match="text/plain"):
        parse_aupay_card_raw(message.as_bytes())


def test_legacy_raw_gmail_import_is_disabled_before_api_or_sheet_access(monkeypatch):
    raw = raw_message(detail(1, "匿名店舗", 1200))
    service = service_for(raw)
    monkeypatch.setattr("app.aupay_mail_pipeline.gmail_service", lambda _: service)
    db = CardDB()

    with pytest.raises(RuntimeError, match="raw_card_gmail_import_disabled"):
        AuPayCardMailPipeline(db).import_gmail("token", "card-query", max_results=2612)

    assert service.messages_api.list_calls == []
    assert service.messages_api.get_calls == []
    assert db.rows == []
    assert db.append_calls == []


def test_missing_rfc_message_id_fails_closed_without_sheet_write(monkeypatch):
    service = service_for(raw_message(detail(1, "匿名店舗", 1200), message_id=None))
    monkeypatch.setattr("app.aupay_mail_pipeline.gmail_service", lambda _: service)
    db = CardDB()

    result = AuPayCardMailPipeline().preview_gmail("token", "card-query")

    assert result["needs_review"] == 1
    assert result["missing_message_id"] == 1
    assert result["parsed_transactions"] == 0
    assert db.rows == []
    assert db.append_calls == []


def test_malformed_and_non_card_mail_are_counted_without_body_output(monkeypatch):
    malformed = raw_message("""No.001--------
▼ご利用日
2026年8月8日
▼ご利用金額
1,200円
""")
    non_card = raw_message(
        detail(1, "匿名店舗", 1200), subject="一般のお知らせ",
    )
    service = GmailService(
        [{"messages": [{"id": "malformed"}, {"id": "other"}]}],
        {"malformed": encoded(malformed), "other": encoded(non_card)},
    )
    monkeypatch.setattr("app.aupay_mail_pipeline.gmail_service", lambda _: service)

    result = AuPayCardMailPipeline().preview_gmail("token", "card-query")

    assert result["needs_review"] == 2
    assert result["missing_required_fields"] == 1
    assert result["non_card_notice"] == 1
    assert result["parsed_transactions"] == 0
    assert "匿名店舗" not in str(result)


def test_gmail_preview_reports_partial_mail_without_dropping_valid_sibling(monkeypatch):
    malformed = """No.002--------
▼ご利用日
2026年8月9日
▼ご利用金額
500円
"""
    service = service_for(raw_message(detail(1, "匿名購入先", 1200), malformed))
    monkeypatch.setattr("app.aupay_mail_pipeline.gmail_service", lambda _: service)

    result = AuPayCardMailPipeline().preview_gmail("token", "card-query")

    assert result["accepted_messages"] == 0
    assert result["partial_messages"] == 1
    assert result["accepted_line_items"] == 1
    assert result["review_line_items"] == 1
    assert result["purchase_line_items"] == 1
    assert result["return_line_items"] == 0
    assert result["parser_review_mail_count"] == 1
    assert result["needs_review"] == 1
    assert result["parsed_transactions"] == 1
    assert "匿名購入先" not in str(result)


def test_gmail_preview_counts_known_return_as_accepted_but_not_review(monkeypatch):
    service = service_for(raw_message(
        detail(1, "匿名購入先", 1200),
        return_detail(2, "匿名返品先", 500),
    ))
    monkeypatch.setattr("app.aupay_mail_pipeline.gmail_service", lambda _: service)

    result = AuPayCardMailPipeline().preview_gmail("token", "card-query")

    assert result["accepted_messages"] == 1
    assert result["partial_messages"] == 0
    assert result["accepted_line_items"] == 2
    assert result["purchase_line_items"] == 1
    assert result["return_line_items"] == 1
    assert result["parser_review_mail_count"] == 0


def test_parser_preserves_merchants_for_downstream_classification():
    raw = raw_message(
        detail(1, "AMAZON.CO.JP", 1200),
        detail(2, "au PAY 残高オートチャージ", 3000),
    )
    rows = parse_aupay_card_raw(raw)

    assert [row["merchant"] for row in rows] == [
        "AMAZON.CO.JP", "au PAY 残高オートチャージ",
    ]


def test_preview_cli_is_read_only_and_prints_only_summary(monkeypatch, capsys):
    class Settings:
        gmail_token_json = "token"
        aupay_card_gmail_query = "card-query"

        def validate(self, **kwargs):
            assert kwargs == {"need_gmail": True}

    class PreviewPipeline:
        def preview_gmail(self, token, query, max_results):
            assert (token, query, max_results) == ("token", "card-query", 7)
            return {"found": 1, "parsed_transactions": 1, "needs_review": 0}

    monkeypatch.setattr(cli, "Settings", Settings)
    monkeypatch.setattr(cli, "AuPayCardMailPipeline", lambda: PreviewPipeline())
    monkeypatch.setattr(sys, "argv", ["app.cli", "card-gmail-preview", "--max-results", "7"])

    cli.main()

    assert capsys.readouterr().out.strip() == "{'found': 1, 'parsed_transactions': 1, 'needs_review': 0}"


def test_apply_plan_preview_cli_uses_read_only_sheet_service(monkeypatch, capsys):
    read_service = object()
    db = object()

    class Settings:
        gmail_token_json = "token"
        aupay_card_gmail_query = "card-query"
        spreadsheet_id = "sheet-id"

        def validate(self, **kwargs):
            assert kwargs == {"need_gmail": True, "need_sheet": True}

    class PreviewPipeline:
        def __init__(self, actual_db):
            assert actual_db is db

        def preview_apply_plan(self, token, query, max_results):
            assert (token, query, max_results) == ("token", "card-query", 5000)
            return {"apply_plan_status": "blocked", "apply_candidate_count": 0}

    monkeypatch.setattr(cli, "Settings", Settings)
    monkeypatch.setattr(cli, "read_only_sheets_service", lambda: read_service)
    monkeypatch.setattr(
        cli, "SheetsDB",
        lambda spreadsheet_id, service: db
        if (spreadsheet_id, service) == ("sheet-id", read_service) else None,
    )
    monkeypatch.setattr(cli, "AuPayCardMailPipeline", PreviewPipeline)
    monkeypatch.setattr(
        sys, "argv", ["app.cli", "card-gmail-apply-plan-preview", "--max-results", "5000"],
    )

    cli.main()

    assert "'apply_plan_status': 'blocked'" in capsys.readouterr().out


def test_apply_plan_preview_performs_separate_preapply_read_without_write(monkeypatch):
    service = service_for(raw_message(detail(1, "匿名店舗", 1200)))
    monkeypatch.setattr("app.aupay_mail_pipeline.gmail_service", lambda _: service)

    class ReadOnlyPlanDB:
        def __init__(self):
            self.get_calls = []
            self.external_write_count = 0

        def get(self, rng):
            self.get_calls.append(rng)
            if rng == "取込データ!A1:L1":
                return [[
                    "取込ID", "取込日時", "データ元", "元データID", "日付", "店舗",
                    "金額", "支払方法", "処理状態", "統合先支出ID", "元データハッシュ", "備考",
                ]]
            assert rng == "取込データ!A2:L"
            return []

        def append(self, *_args, **_kwargs):
            self.external_write_count += 1
            raise AssertionError("preview reached writer")

    db = ReadOnlyPlanDB()

    result = AuPayCardMailPipeline(db).preview_apply_plan(
        "token", "card-query", max_results=100,
    )

    assert result["apply_candidate_count"] == 1
    assert result["revalidated_new_count"] == 1
    assert result["would_write_count"] == 1
    assert result["execution_status"] == "dry_run_ready"
    assert result["external_write_count"] == 0
    assert db.get_calls == [
        "取込データ!A2:L", "取込データ!A1:L1", "取込データ!A2:L",
    ]
    assert db.external_write_count == 0


def test_raw_import_cli_is_rejected_before_settings_or_google_access(monkeypatch):
    monkeypatch.setattr(
        cli, "Settings", lambda: pytest.fail("Settings must not be loaded"),
    )
    monkeypatch.setattr(sys, "argv", ["app.cli", "card-gmail-import"])

    with pytest.raises(RuntimeError, match="raw_card_gmail_import_disabled"):
        cli.main()


def test_card_gmail_query_is_configurable_without_changing_wallet_query(monkeypatch):
    monkeypatch.setenv("AUPAY_CARD_GMAIL_QUERY", "label:card-test newer_than:7d")
    settings = Settings()

    assert settings.aupay_card_gmail_query == "label:card-test newer_than:7d"
    assert "wallet.auone.jp" in settings.aupay_gmail_query


def test_default_card_gmail_query_matches_sender_domain_with_bounded_date(monkeypatch):
    monkeypatch.delenv("AUPAY_CARD_GMAIL_QUERY", raising=False)

    query = Settings().aupay_card_gmail_query

    assert "from:kddi-fs.com" in query
    assert "newer_than:30d" in query


def test_gmail_read_error_is_counted_without_upstream_detail(monkeypatch):
    monkeypatch.setattr("app.aupay_mail_pipeline.time.sleep", lambda _: None)
    class FailingRequest:
        def execute(self):
            raise HttpError(Response({"status": "429"}), b"sensitive upstream detail")

    class FailingRawMessages(Messages):
        def get(self, **kwargs):
            return FailingRequest()

    class FailingRawService:
        def __init__(self):
            self.messages_api = FailingRawMessages(
                [{"messages": [{"id": "gmail-unit-1"}]}], {},
            )

        def users(self):
            return self

        def messages(self):
            return self.messages_api

    monkeypatch.setattr(
        "app.aupay_mail_pipeline.gmail_service", lambda _: FailingRawService(),
    )

    result = AuPayCardMailPipeline().preview_gmail("token", "card-query")

    assert result["needs_review"] == 1
    assert result["gmail_read_failed"] == 1
    assert "sensitive upstream detail" not in str(result)


def test_preview_marks_collection_incomplete_when_gmail_read_fails(monkeypatch):
    monkeypatch.setattr("app.aupay_mail_pipeline.time.sleep", lambda _: None)
    class FailingRequest:
        def execute(self):
            raise HttpError(Response({"status": "429"}), b"sensitive upstream detail")

    class FailingRawMessages(Messages):
        def get(self, **kwargs):
            return FailingRequest()

    class FailingRawService:
        def __init__(self):
            self.messages_api = FailingRawMessages(
                [{"messages": [{"id": "gmail-unit-1"}]}], {},
            )

        def users(self):
            return self

        def messages(self):
            return self.messages_api

    monkeypatch.setattr(
        "app.aupay_mail_pipeline.gmail_service", lambda _: FailingRawService(),
    )
    result = AuPayCardMailPipeline().preview_gmail("token", "card-query")

    assert result["gmail_read_failed"] == 1
    assert result["collection_complete"] is False


def test_transient_retry_succeeds_with_bounded_backoff():
    calls = []
    sleeps = []
    class Flaky:
        def execute(self):
            calls.append(1)
            if len(calls) < 3:
                raise HttpError(Response({"status": "429"}), b"rate limited")
            return {"ok": True}
    result, retries = _execute_gmail(lambda: Flaky(), sleeper=sleeps.append)
    assert result == {"ok": True}
    assert retries == 2
    assert sleeps == [0.5, 1.0]


def test_transient_retry_stops_at_limit():
    calls = []
    class Failing:
        def execute(self):
            calls.append(1)
            raise HttpError(Response({"status": "503"}), b"temporary")
    with pytest.raises(HttpError):
        _execute_gmail(lambda: Failing(), sleeper=lambda _: None, max_attempts=3)
    assert len(calls) == 3


def test_permanent_error_is_not_retried():
    calls = []
    class Permanent:
        def execute(self):
            calls.append(1)
            raise HttpError(Response({"status": "401"}), b"permanent")
    with pytest.raises(HttpError):
        _execute_gmail(lambda: Permanent(), sleeper=lambda _: None)
    assert len(calls) == 1


def test_second_detail_missing_required_field_fails_closed():
    malformed_second = """No.002--------
▼ご利用日
2026年8月9日
▼ご利用金額
500円
"""
    with pytest.raises(ValueError, match="No.002"):
        parse_aupay_card_raw(raw_message(detail(1, "匿名店舗", 1200), malformed_second))
