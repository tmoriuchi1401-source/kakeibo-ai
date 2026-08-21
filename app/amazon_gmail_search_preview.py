from __future__ import annotations

import json
import os

from .amazon_gmail_preview import GmailPreviewAuthError, gmail_readonly_service


DOMAIN_QUERIES = {
    "amazon_marker_anywhere": 'in:anywhere "Amazon.co.jp"',
    "amazon_domain_no_period": "in:anywhere from:amazon.co.jp",
    "amazon_domain_2y": "in:anywhere from:amazon.co.jp newer_than:2y",
    "amazon_domain_5y": "in:anywhere from:amazon.co.jp newer_than:5y",
}
SENDER_QUERIES = {
    "sender_auto_confirm": "in:anywhere from:auto-confirm@amazon.co.jp",
    "sender_shipment_tracking": "in:anywhere from:shipment-tracking@amazon.co.jp",
    "sender_order_update": "in:anywhere from:order-update@amazon.co.jp",
    "sender_return": "in:anywhere from:return@amazon.co.jp",
}
SUBJECT_TERMS = (
    "注文", "発送", "お届け", "請求", "支払い", "お支払い", "返金", "返品", "キャンセル",
)


def _bucket(count: int) -> str:
    if count <= 0:
        return "0"
    if count <= 10:
        return "1-10"
    if count <= 100:
        return "11-100"
    return "100+"


def _estimate(messages_api, query: str) -> int:
    response = messages_api.list(
        userId="me", q=query, maxResults=1, includeSpamTrash=True,
    ).execute()
    value = response.get("resultSizeEstimate", 0)
    return max(0, int(value))


def _record(messages_api, queries: dict[str, str]) -> dict[str, dict]:
    return {
        label: {"estimated": count, "bucket": _bucket(count)}
        for label, query in queries.items()
        for count in (_estimate(messages_api, query),)
    }


def _diagnosis(domain: dict[str, dict], senders: dict[str, dict], subjects: dict[str, dict]) -> str:
    marker = domain["amazon_marker_anywhere"]["estimated"]
    no_period = domain["amazon_domain_no_period"]["estimated"]
    two_years = domain["amazon_domain_2y"]["estimated"]
    known_sender_total = sum(value["estimated"] for value in senders.values())
    subject_total = sum(value["estimated"] for value in subjects.values())
    if marker == 0 and no_period == 0 and known_sender_total == 0:
        return "D"
    if marker > 0 and no_period == 0 and known_sender_total == 0:
        return "A"
    if no_period > 0 and two_years == 0:
        return "B"
    if two_years > 0 and subject_total == 0:
        return "C"
    return "E"


def preview_amazon_gmail_search(service) -> dict:
    messages_api = service.users().messages()
    domain = _record(messages_api, DOMAIN_QUERIES)
    senders = _record(messages_api, SENDER_QUERIES)
    subjects = {}
    if domain["amazon_domain_no_period"]["estimated"] > 0:
        subject_queries = {
            f"subject_{index + 1}": f"in:anywhere from:amazon.co.jp subject:{term}"
            for index, term in enumerate(SUBJECT_TERMS)
        }
        subjects = _record(messages_api, subject_queries)
    return {
        "gmail_scope_ok": True,
        "domain_coverage": domain,
        "known_sender_coverage": senders,
        "subject_coverage": subjects,
        "subject_labels": {
            f"subject_{index + 1}": term for index, term in enumerate(SUBJECT_TERMS)
        } if subjects else {},
        "diagnosis": _diagnosis(domain, senders, subjects),
    }


def main() -> int:
    token_json = os.getenv("GOOGLE_GMAIL_TOKEN_JSON", "").strip()
    if not token_json:
        print(json.dumps({"gmail_token_status": "missing"}))
        return 1
    try:
        result = preview_amazon_gmail_search(gmail_readonly_service(token_json))
    except GmailPreviewAuthError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
