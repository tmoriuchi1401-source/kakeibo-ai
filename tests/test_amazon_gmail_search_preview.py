from __future__ import annotations

import json

import pytest

from app.amazon_gmail_preview import GMAIL_READONLY, GmailPreviewAuthError, credentials_from_token
from app.amazon_gmail_search_preview import preview_amazon_gmail_search


class RequestCall:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class ListOnlyMessages:
    def __init__(self, counts):
        self.counts = counts
        self.operations = []

    def list(self, **kwargs):
        self.operations.append(("list", kwargs))
        return RequestCall({
            "resultSizeEstimate": self.counts.get(kwargs["q"], 0),
            "messages": [{"id": "must-not-be-read"}],
        })

    def get(self, **kwargs):
        raise AssertionError("messages.get must not be called")


class FakeService:
    def __init__(self, counts):
        self.messages_api = ListOnlyMessages(counts)

    def users(self):
        service = self

        class Users:
            def messages(self):
                return service.messages_api

        return Users()


def count_map(**values):
    queries = {
        "marker": 'in:anywhere "Amazon.co.jp"',
        "domain": "in:anywhere from:amazon.co.jp",
        "two": "in:anywhere from:amazon.co.jp newer_than:2y",
        "five": "in:anywhere from:amazon.co.jp newer_than:5y",
        "auto": "in:anywhere from:auto-confirm@amazon.co.jp",
    }
    return {queries[key]: value for key, value in values.items()}


def test_zero_results_use_only_messages_list_and_diagnose_d():
    service = FakeService({})
    result = preview_amazon_gmail_search(service)
    assert result["diagnosis"] == "D"
    assert result["subject_coverage"] == {}
    assert {name for name, _ in service.messages_api.operations} == {"list"}
    assert all(call["maxResults"] == 1 for _, call in service.messages_api.operations)


def test_sender_condition_difference_diagnoses_a():
    result = preview_amazon_gmail_search(FakeService(count_map(marker=7)))
    assert result["diagnosis"] == "A"


def test_period_condition_difference_diagnoses_b():
    result = preview_amazon_gmail_search(FakeService(count_map(marker=8, domain=8, five=8, two=0)))
    assert result["diagnosis"] == "B"
    assert result["domain_coverage"]["amazon_domain_no_period"]["bucket"] == "1-10"


def test_subject_condition_difference_diagnoses_c():
    result = preview_amazon_gmail_search(FakeService(count_map(marker=8, domain=8, two=4, five=8)))
    assert result["diagnosis"] == "C"
    assert len(result["subject_coverage"]) == 9


def test_matching_subject_requires_more_diagnosis_e():
    counts = count_map(marker=8, domain=8, two=4, five=8, auto=4)
    counts["in:anywhere from:amazon.co.jp subject:注文"] = 2
    result = preview_amazon_gmail_search(FakeService(counts))
    assert result["diagnosis"] == "E"


def test_message_ids_and_response_payload_are_never_output():
    serialized = json.dumps(preview_amazon_gmail_search(FakeService({})))
    assert "must-not-be-read" not in serialized
    assert "messages" not in serialized


def test_count_buckets_do_not_collect_all_message_ids():
    result = preview_amazon_gmail_search(FakeService(count_map(marker=1000, domain=1000, two=500, five=900)))
    assert result["domain_coverage"]["amazon_domain_no_period"] == {
        "estimated": 1000, "bucket": "100+",
    }


class FakeCredentials:
    expired = False
    refresh_token = "masked"
    valid = True

    @classmethod
    def from_authorized_user_info(cls, info, scopes):
        return cls()

    def has_scopes(self, scopes):
        return scopes == [GMAIL_READONLY]


def test_scope_shortage_stops_safely():
    token = json.dumps({"scopes": ["https://www.googleapis.com/auth/gmail.modify"]})
    with pytest.raises(GmailPreviewAuthError, match="scope"):
        credentials_from_token(token, credentials_type=FakeCredentials)


def test_module_has_no_sheets_dependencies():
    import app.amazon_gmail_search_preview as module
    assert "SheetsDB" not in module.__dict__
    assert "ReconciliationPipeline" not in module.__dict__
