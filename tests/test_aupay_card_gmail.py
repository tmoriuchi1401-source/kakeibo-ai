import base64
import sys
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from app import cli
from app.aupay_mail_pipeline import AuPayCardMailPipeline, parse_aupay_card_raw
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


def test_gmail_import_writes_existing_card_transaction_contract(monkeypatch):
    raw = raw_message(detail(1, "匿名店舗", 1200))
    service = service_for(raw)
    monkeypatch.setattr("app.aupay_mail_pipeline.gmail_service", lambda _: service)
    db = CardDB()

    result = AuPayCardMailPipeline(db).import_gmail("token", "card-query")

    assert result["new"] == 1
    assert result["parsed_transactions"] == 1
    assert service.messages_api.list_calls[0]["q"] == "card-query"
    assert service.messages_api.get_calls == [
        {"userId": "me", "id": "gmail-unit-1", "format": "raw"}
    ]
    assert db.rows[0][2:10] == [
        "au PAYカード", db.rows[0][0], "2026-08-08", "匿名店舗", 1200,
        "メール通知", "unclassified_card", "",
    ]
    assert "unit-card-message" not in str(db.rows)


def test_same_gmail_notification_is_idempotent(monkeypatch):
    raw = raw_message(detail(1, "匿名店舗", 1200))
    db = CardDB()
    first = service_for(raw)
    monkeypatch.setattr("app.aupay_mail_pipeline.gmail_service", lambda _: first)
    assert AuPayCardMailPipeline(db).import_gmail("token", "card-query")["new"] == 1

    second = service_for(raw)
    monkeypatch.setattr("app.aupay_mail_pipeline.gmail_service", lambda _: second)
    result = AuPayCardMailPipeline(db).import_gmail("token", "card-query")

    assert result["new"] == 0
    assert result["unchanged"] == 1
    assert len(db.rows) == 1


def test_missing_rfc_message_id_fails_closed_without_sheet_write(monkeypatch):
    service = service_for(raw_message(detail(1, "匿名店舗", 1200), message_id=None))
    monkeypatch.setattr("app.aupay_mail_pipeline.gmail_service", lambda _: service)
    db = CardDB()

    result = AuPayCardMailPipeline(db).import_gmail("token", "card-query")

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


def test_existing_amazon_and_autocharge_classification_is_preserved(monkeypatch):
    raw = raw_message(
        detail(1, "AMAZON.CO.JP", 1200),
        detail(2, "au PAY 残高オートチャージ", 3000),
    )
    service = service_for(raw)
    monkeypatch.setattr("app.aupay_mail_pipeline.gmail_service", lambda _: service)
    db = CardDB()

    result = AuPayCardMailPipeline(db).import_gmail("token", "card-query")

    assert result["amazon_unmatched"] == 1
    assert result["aupay_charge"] == 1
    assert [row[8] for row in db.rows] == [
        "amazon_unmatched", "transfer_aupay_charge",
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


def test_import_fails_closed_when_gmail_read_is_incomplete(monkeypatch):
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
    db = CardDB()

    with pytest.raises(RuntimeError, match="gmail_collection_incomplete"):
        AuPayCardMailPipeline(db).import_gmail("token", "card-query")

    assert db.rows == []
    assert db.append_calls == []
