from __future__ import annotations

from collections import Counter
import base64
import json
import os
from typing import Callable

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .amazon_email import (
    AmazonMailEvent,
    diagnose_amazon_email_money_context,
    diagnose_amazon_email_structure,
    parse_amazon_email,
)


GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
SEARCHES = (
    ("order", "in:anywhere from:amazon.co.jp newer_than:1y {subject:注文 subject:ご注文}", 10),
    ("payment", "in:anywhere from:amazon.co.jp newer_than:1y {subject:請求 subject:支払い subject:お支払い}", 5),
    ("shipment", "in:anywhere from:amazon.co.jp newer_than:1y {subject:発送 subject:出荷}", 10),
    ("delivery", "in:anywhere from:amazon.co.jp newer_than:1y {subject:配達 subject:お届け}", 5),
    ("cancellation", "in:anywhere from:amazon.co.jp newer_than:1y {subject:キャンセル subject:取消}", 5),
    ("return", "in:anywhere from:amazon.co.jp newer_than:1y {subject:返品 subject:返送}", 5),
    ("refund", "in:anywhere from:amazon.co.jp newer_than:1y subject:返金", 5),
    ("fallback", "in:anywhere from:amazon.co.jp newer_than:1y", 5),
)
MAX_MESSAGES = sum(limit for _, _, limit in SEARCHES)
EVENT_TYPES = (
    "order", "payment", "shipment", "delivery", "cancellation", "return", "refund", "unknown",
)
SUMMARY_FIELDS = (
    "order_id", "event_date", "charged_amount", "order_amount", "refund_amount",
    "shipment_amount", "payment_method",
)


class GmailPreviewAuthError(RuntimeError):
    pass


def _configured_scopes(info: dict) -> set[str]:
    scopes = info.get("scopes", [])
    if isinstance(scopes, str):
        scopes = scopes.split()
    return {str(scope) for scope in scopes}


def credentials_from_token(
    token_json: str,
    *,
    credentials_type=Credentials,
    request_factory=Request,
):
    try:
        info = json.loads(token_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise GmailPreviewAuthError("gmail_token_status: invalid_json") from exc
    if _configured_scopes(info) != {GMAIL_READONLY}:
        raise GmailPreviewAuthError("gmail_scope_status: insufficient_or_unexpected")
    try:
        credentials = credentials_type.from_authorized_user_info(
            info, scopes=[GMAIL_READONLY],
        )
    except (TypeError, ValueError) as exc:
        raise GmailPreviewAuthError("gmail_token_status: invalid") from exc
    if not credentials.has_scopes([GMAIL_READONLY]):
        raise GmailPreviewAuthError("gmail_scope_status: insufficient")
    if credentials.expired:
        if not credentials.refresh_token:
            raise GmailPreviewAuthError("gmail_token_status: expired")
        try:
            credentials.refresh(request_factory())
        except RefreshError as exc:
            raise GmailPreviewAuthError("gmail_token_status: refresh_failed") from exc
    if not credentials.valid:
        raise GmailPreviewAuthError("gmail_token_status: invalid_or_revoked")
    return credentials


def gmail_readonly_service(token_json: str):
    credentials = credentials_from_token(token_json)
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def _raw_bytes(encoded: str) -> bytes:
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("gmail_raw_status: missing")
    try:
        return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (ValueError, TypeError) as exc:
        raise ValueError("gmail_raw_status: invalid") from exc


def _anonymous_sample(
    source_category: str, event: AmazonMailEvent, structure: dict, money_context: dict,
) -> dict:
    return {
        "source_category": source_category,
        "event_type": event.event_type,
        "order_id_present": event.order_id is not None,
        "event_date_present": event.event_date is not None,
        "charged_amount_present": event.charged_amount is not None,
        "order_amount_present": event.order_amount is not None,
        "refund_amount_present": event.refund_amount is not None,
        "shipment_amount_present": event.shipment_amount is not None,
        "payment_method_present": event.payment_method is not None,
        "message_id_present": event.message_id is not None,
        "auto_applicability": classify_auto_applicability(event),
        "structure": structure,
        "money_context": money_context,
    }


def _outlook(sampled: int, charged: int, order_amount: int) -> str:
    if charged >= 2:
        return "A"
    if charged == 1:
        return "B"
    if sampled >= 3 and order_amount > 0:
        return "C"
    return "D"


def classify_auto_applicability(event: AmazonMailEvent) -> str:
    """Classify a parsed event without applying it to any external system."""
    if event.event_type == "refund":
        return "auto_applicable" if event.order_id and event.refund_amount is not None else "needs_review"
    if event.event_type in {"order", "payment", "shipment", "delivery", "cancellation", "return"}:
        return "auto_applicable" if event.order_id else "needs_review"
    return "needs_review" if event.order_id else "unusable"


def _event_type_summary(events: list[tuple[str, AmazonMailEvent, dict, dict]]) -> dict:
    summary = {}
    for event_type in EVENT_TYPES:
        matching = [event for _, event, _, _ in events if event.event_type == event_type]
        values = {"message_count": len(matching)}
        values.update({
            f"{field}_present_count": sum(getattr(event, field) is not None for event in matching)
            for field in SUMMARY_FIELDS
        })
        summary[event_type] = values
    return summary


def preview_amazon_gmail(
    service,
    *,
    parser: Callable[[bytes], AmazonMailEvent] = parse_amazon_email,
    sample_limit: int = 3,
) -> dict:
    seen: set[str] = set()
    events: list[tuple[str, AmazonMailEvent, dict, dict]] = []
    search_counts: Counter[str] = Counter()
    duplicate_messages_skipped = 0
    messages_api = service.users().messages()

    for search_name, query, limit in SEARCHES:
        if len(seen) >= MAX_MESSAGES:
            break
        response = messages_api.list(userId="me", q=query, maxResults=limit).execute()
        for item in response.get("messages", []):
            message_id = item.get("id")
            if not message_id:
                continue
            if message_id in seen:
                duplicate_messages_skipped += 1
                continue
            if len(seen) >= MAX_MESSAGES:
                break
            seen.add(message_id)
            raw = messages_api.get(userId="me", id=message_id, format="raw").execute()
            raw_bytes = _raw_bytes(raw.get("raw", ""))
            event = parser(raw_bytes)
            events.append((
                search_name,
                event,
                diagnose_amazon_email_structure(raw_bytes, event),
                diagnose_amazon_email_money_context(raw_bytes, event),
            ))
            search_counts[search_name] += 1
            if search_counts[search_name] >= limit:
                break

    event_types = Counter(event.event_type for _, event, _, _ in events)
    failure_reasons = Counter(structure["parser_failure_reason"] for _, _, structure, _ in events)
    money_context = Counter()
    message_context = Counter()
    for _, _, _, context in events:
        for name in ("transaction_likely", "advertisement_likely", "ambiguous"):
            money_context[name] += context[f"{name}_count"]
        message_context[context["message_classification"]] += 1
    presence_fields = (
        "charged_amount", "order_amount", "gift_card_amount", "points_amount",
        "coupon_amount", "discount_amount", "shipment_amount", "payment_method",
    )
    presence = {
        f"{field}_present": sum(getattr(event, field) is not None for _, event, _, _ in events)
        for field in presence_fields
    }
    both = sum(
        event.charged_amount is not None and event.order_amount is not None
        for _, event, _, _ in events
    )
    applicability = Counter(classify_auto_applicability(event) for _, event, _, _ in events)
    return {
        "gmail_scope_ok": True,
        "sampled_messages": len(events),
        "duplicate_messages_skipped": duplicate_messages_skipped,
        "search_categories": {name: search_counts[name] for name, _, _ in SEARCHES},
        "event_types": dict(event_types),
        "unknown_count": event_types["unknown"],
        "event_type_summary": _event_type_summary(events),
        "auto_applicability": {
            name: applicability[name] for name in ("auto_applicable", "needs_review", "unusable")
        },
        "parser_failure_reasons": dict(failure_reasons),
        "money_context": dict(money_context),
        "message_context": dict(message_context),
        **presence,
        "charged_and_order_amount_both_present": both,
        "outlook": _outlook(
            len(events), presence["charged_amount_present"], presence["order_amount_present"],
        ),
        "samples": [
            _anonymous_sample(source_category, event, structure, money_context)
            for source_category, event, structure, money_context in events[:sample_limit]
        ],
    }


def main() -> int:
    token_json = os.getenv("GOOGLE_GMAIL_TOKEN_JSON", "").strip()
    if not token_json:
        print(json.dumps({"gmail_token_status": "missing"}))
        return 1
    try:
        service = gmail_readonly_service(token_json)
        result = preview_amazon_gmail(service)
    except GmailPreviewAuthError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    except RefreshError:
        print(json.dumps({"error": "gmail_token_status: refresh_failed"}))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
