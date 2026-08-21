from __future__ import annotations

import base64
from email.message import EmailMessage
import json

import pytest
from google.auth.exceptions import RefreshError

from app.amazon_gmail_preview import (
    GMAIL_READONLY,
    GmailPreviewAuthError,
    SEARCHES,
    credentials_from_token,
    preview_amazon_gmail,
)


def raw_mail(subject: str, body: str) -> str:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "Amazon.co.jp <no-reply@amazon.co.jp>"
    message["To"] = "private@example.invalid"
    message["Message-ID"] = "<private@example.invalid>"
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")


class RequestCall:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class FakeMessages:
    def __init__(self, search_results, raw_messages):
        self.search_results = list(search_results)
        self.raw_messages = raw_messages
        self.operations = []

    def list(self, **kwargs):
        self.operations.append(("list", kwargs))
        return RequestCall({"messages": self.search_results.pop(0) if self.search_results else []})

    def get(self, **kwargs):
        self.operations.append(("get", kwargs))
        return RequestCall({"raw": self.raw_messages[kwargs["id"]]})


class FakeUsers:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


class FakeService:
    def __init__(self, search_results, raw_messages):
        self.messages_api = FakeMessages(search_results, raw_messages)

    def users(self):
        return FakeUsers(self.messages_api)


def test_order_search_uses_domain_and_subject_without_specific_sender():
    name, query, limit = SEARCHES[0]
    assert (name, limit) == ("order", 2)
    assert query == "in:anywhere from:amazon.co.jp newer_than:2y subject:注文"
    assert "auto-confirm" not in query


def test_preview_uses_only_list_and_raw_get_and_aggregates_amounts():
    raws = {
        "payment": raw_mail("ご請求のお知らせ", """
注文番号: 123-1234567-1234567
注文合計: 1,500円
カード請求額: 1,200円
ギフトカード利用額: 200円
Amazonポイント利用額: 100円
支払い方法: Visa
"""),
        "shipment": raw_mail("発送のお知らせ", "発送分合計: 980円"),
    }
    service = FakeService(
        [[{"id": "payment"}], [{"id": "shipment"}]], raws,
    )
    result = preview_amazon_gmail(service)

    assert result["sampled_messages"] == 2
    assert result["order_search_sampled"] == 1
    assert result["fallback_sampled"] == 1
    assert result["charged_amount_present"] == 1
    assert result["order_amount_present"] == 1
    assert result["gift_card_amount_present"] == 1
    assert result["points_amount_present"] == 1
    assert result["shipment_amount_present"] == 1
    assert result["payment_method_present"] == 1
    assert result["charged_and_order_amount_both_present"] == 1
    assert result["parser_failure_reasons"]
    assert {name for name, _ in service.messages_api.operations} == {"list", "get"}
    assert all(
        kwargs.get("format") == "raw"
        for name, kwargs in service.messages_api.operations if name == "get"
    )


def test_order_and_fallback_are_capped_at_two_each_and_four_total():
    ids = [f"m{i}" for i in range(6)]
    raws = {item: raw_mail("ご注文の確認", "注文合計: 100円") for item in ids}
    searches = [
        [{"id": "m0"}, {"id": "m1"}],
        [{"id": "m0"}, {"id": "m1"}, {"id": "m2"}, {"id": "m3"}, {"id": "m4"}],
    ]
    service = FakeService(searches, raws)
    result = preview_amazon_gmail(service)
    get_ids = [kwargs["id"] for name, kwargs in service.messages_api.operations if name == "get"]
    assert result["sampled_messages"] == 4
    assert result["order_search_sampled"] == 2
    assert result["fallback_sampled"] == 2
    assert len(get_ids) == len(set(get_ids))
    assert [kwargs["maxResults"] for name, kwargs in service.messages_api.operations if name == "list"] == [2, 4]


def test_zero_messages_is_successful():
    result = preview_amazon_gmail(FakeService([[], []], {}))
    assert result["sampled_messages"] == 0
    assert result["outlook"] == "D"
    assert result["samples"] == []


def test_samples_are_anonymized():
    body = """
注文番号: 123-1234567-1234567
氏名: 秘密太郎
住所: 東京都秘密区
追跡番号: TRACK-SECRET
カード請求額: 500円
"""
    service = FakeService([[{"id": "m1"}], []], {"m1": raw_mail("ご請求", body)})
    serialized = json.dumps(preview_amazon_gmail(service), ensure_ascii=False)
    assert "秘密太郎" not in serialized
    assert "東京都" not in serialized
    assert "TRACK-SECRET" not in serialized
    assert "123-1234567-1234567" not in serialized
    assert "private@example.invalid" not in serialized
    assert "m1" not in serialized
    assert '"source_category": "order"' in serialized
    assert '"body_length_band"' in serialized
    assert '"money_candidate_count"' in serialized
    assert '"parser_failure_reason"' in serialized
    assert '"money_context"' in serialized
    assert "123-1234567-1234567" not in serialized
    assert "TRACK-SECRET" not in serialized


class FakeCredentials:
    expired = False
    refresh_token = "refresh"
    valid = True

    @classmethod
    def from_authorized_user_info(cls, info, scopes):
        instance = cls()
        instance.requested_scopes = scopes
        return instance

    def has_scopes(self, scopes):
        return scopes == [GMAIL_READONLY]

    def refresh(self, request):
        self.expired = False


def token(scopes=None):
    return json.dumps({
        "token": "masked", "refresh_token": "masked", "client_id": "masked",
        "client_secret": "masked", "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": scopes if scopes is not None else [GMAIL_READONLY],
    })


def test_only_gmail_readonly_scope_is_accepted():
    credentials = credentials_from_token(token(), credentials_type=FakeCredentials)
    assert credentials.requested_scopes == [GMAIL_READONLY]


@pytest.mark.parametrize("scopes", [[], ["https://www.googleapis.com/auth/gmail.modify"], [GMAIL_READONLY, "extra"]])
def test_missing_or_extra_scope_fails_safely(scopes):
    with pytest.raises(GmailPreviewAuthError, match="scope"):
        credentials_from_token(token(scopes), credentials_type=FakeCredentials)


def test_expired_token_without_refresh_fails_safely():
    class Expired(FakeCredentials):
        expired = True
        refresh_token = None
        valid = False

    with pytest.raises(GmailPreviewAuthError, match="expired"):
        credentials_from_token(token(), credentials_type=Expired)


def test_refresh_failure_does_not_expose_token():
    class BrokenRefresh(FakeCredentials):
        expired = True
        valid = False

        def refresh(self, request):
            raise RefreshError("secret should not be propagated")

    with pytest.raises(GmailPreviewAuthError, match="refresh_failed") as exc:
        credentials_from_token(token(), credentials_type=BrokenRefresh)
    assert "secret" not in str(exc.value)


def test_module_has_no_sheets_or_ledger_dependency():
    import app.amazon_gmail_preview as module
    source_names = set(module.__dict__)
    assert "SheetsDB" not in source_names
    assert "ReconciliationPipeline" not in source_names
    assert "AutoExpensePipeline" not in source_names
