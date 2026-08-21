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

from .amazon_email import AmazonMailEvent, diagnose_amazon_email_structure, parse_amazon_email


GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
SEARCHES = (
    (
        "order",
        "in:anywhere from:amazon.co.jp newer_than:2y subject:注文",
        2,
    ),
    (
        "fallback",
        "in:anywhere from:amazon.co.jp newer_than:2y",
        2,
    ),
)
MAX_MESSAGES = 4


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


def _anonymous_sample(source_category: str, event: AmazonMailEvent, structure: dict) -> dict:
    return {
        "source_category": source_category,
        "event_type": event.event_type,
        "charged_amount": event.charged_amount,
        "order_amount": event.order_amount,
        "gift_card_amount": event.gift_card_amount,
        "points_amount": event.points_amount,
        "coupon_amount": event.coupon_amount,
        "discount_amount": event.discount_amount,
        "shipment_amount": event.shipment_amount,
        "order_id_present": event.order_id is not None,
        "payment_method_present": event.payment_method is not None,
        "structure": structure,
    }


def _outlook(sampled: int, charged: int, order_amount: int) -> str:
    if charged >= 2:
        return "A"
    if charged == 1:
        return "B"
    if sampled >= 3 and order_amount > 0:
        return "C"
    return "D"


def preview_amazon_gmail(
    service,
    *,
    parser: Callable[[bytes], AmazonMailEvent] = parse_amazon_email,
    sample_limit: int = 3,
) -> dict:
    seen: set[str] = set()
    events: list[tuple[str, AmazonMailEvent, dict]] = []
    search_counts: Counter[str] = Counter()
    messages_api = service.users().messages()

    for search_name, query, limit in SEARCHES:
        if len(seen) >= MAX_MESSAGES:
            break
        request_limit = min(MAX_MESSAGES, limit + len(seen))
        response = messages_api.list(userId="me", q=query, maxResults=request_limit).execute()
        for item in response.get("messages", []):
            message_id = item.get("id")
            if not message_id or message_id in seen or len(seen) >= MAX_MESSAGES:
                continue
            seen.add(message_id)
            raw = messages_api.get(userId="me", id=message_id, format="raw").execute()
            raw_bytes = _raw_bytes(raw.get("raw", ""))
            event = parser(raw_bytes)
            events.append((
                search_name, event, diagnose_amazon_email_structure(raw_bytes, event),
            ))
            search_counts[search_name] += 1
            if search_counts[search_name] >= limit:
                break

    event_types = Counter(event.event_type for _, event, _ in events)
    failure_reasons = Counter(structure["parser_failure_reason"] for _, _, structure in events)
    presence_fields = (
        "charged_amount", "order_amount", "gift_card_amount", "points_amount",
        "coupon_amount", "discount_amount", "shipment_amount", "payment_method",
    )
    presence = {
        f"{field}_present": sum(getattr(event, field) is not None for _, event, _ in events)
        for field in presence_fields
    }
    both = sum(
        event.charged_amount is not None and event.order_amount is not None
        for _, event, _ in events
    )
    return {
        "gmail_scope_ok": True,
        "sampled_messages": len(events),
        "order_search_sampled": search_counts["order"],
        "fallback_sampled": search_counts["fallback"],
        "event_types": dict(event_types),
        "parser_failure_reasons": dict(failure_reasons),
        **presence,
        "charged_and_order_amount_both_present": both,
        "outlook": _outlook(
            len(events), presence["charged_amount_present"], presence["order_amount_present"],
        ),
        "samples": [
            _anonymous_sample(source_category, event, structure)
            for source_category, event, structure in events[:sample_limit]
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
