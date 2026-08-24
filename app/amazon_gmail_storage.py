from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import hashlib
from typing import cast

from .amazon_email import AmazonMailEvent, parse_amazon_email
from .amazon_gmail_preview import MAX_MESSAGES, SEARCHES, _raw_bytes
from .amazon_stored_event import (
    AMAZON_EVENT_TYPES,
    AmazonEventType,
    AmazonStoredEvent,
    ParseStatus,
)


AMAZON_PARSER_VERSION = "amazon_email_v2"


@dataclass(frozen=True)
class GmailRawMessage:
    gmail_message_id: str
    thread_id: str
    raw_mime: bytes


def amazon_event_id(gmail_message_id: str) -> str:
    """Build a stable event ID without time-based or random input."""

    if not gmail_message_id:
        raise ValueError("gmail_message_id is required")
    digest = hashlib.sha256(gmail_message_id.encode("utf-8")).hexdigest()
    return f"AE-{digest[:24]}"


def _rfc_message_id(raw_mime: bytes) -> str | None:
    message = BytesParser(policy=policy.default).parsebytes(raw_mime, headersonly=True)
    value = message.get("Message-ID")
    return str(value).strip() if value and str(value).strip() else None


def _parse_status(event_type: str, order_id: str | None) -> str:
    if event_type != "unknown":
        return "parsed"
    return "needs_review" if order_id else "unusable"


def amazon_stored_event_from_mail(
    event: AmazonMailEvent,
    message: GmailRawMessage,
    *,
    timestamp: str,
) -> AmazonStoredEvent:
    """Add storage metadata to a Phase A parser result."""

    event_type = event.event_type if event.event_type in AMAZON_EVENT_TYPES else "unknown"
    return AmazonStoredEvent(
        event_id=amazon_event_id(message.gmail_message_id),
        gmail_message_id=message.gmail_message_id,
        rfc_message_id=_rfc_message_id(message.raw_mime) or event.message_id,
        thread_id=message.thread_id,
        source_hash=event.source_hash,
        event_type=cast(AmazonEventType, event_type),
        order_id=event.order_id,
        event_date=event.event_date,
        charged_amount=event.charged_amount,
        order_amount=event.order_amount,
        refund_amount=event.refund_amount,
        shipment_amount=event.shipment_amount,
        gift_card_amount=event.gift_card_amount,
        points_amount=event.points_amount,
        coupon_amount=event.coupon_amount,
        discount_amount=event.discount_amount,
        payment_method=event.payment_method,
        item_count=event.item_count,
        parse_status=cast(ParseStatus, _parse_status(event_type, event.order_id)),
        match_status="unmatched",
        apply_status="pending",
        parser_version=AMAZON_PARSER_VERSION,
        imported_at=timestamp,
        last_parsed_at=timestamp,
    )


def _unparsed_event(raw_mime: bytes) -> AmazonMailEvent:
    return AmazonMailEvent(
        event_type="unknown",
        order_id=None,
        event_date=None,
        charged_amount=None,
        order_amount=None,
        refund_amount=None,
        gift_card_amount=None,
        points_amount=None,
        coupon_amount=None,
        discount_amount=None,
        payment_method=None,
        shipment_amount=None,
        item_count=None,
        message_id=_rfc_message_id(raw_mime),
        source_hash=hashlib.sha256(raw_mime).hexdigest(),
        gift_card_used=False,
        points_used=False,
    )


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_amazon_gmail_events(
    db,
    messages: Iterable[GmailRawMessage],
    *,
    parser: Callable[[bytes], AmazonMailEvent] = parse_amazon_email,
    timestamp_factory: Callable[[], str] = _utc_timestamp,
) -> dict[str, int]:
    """Parse messages, deduplicate identities in memory, and append once."""

    raw_messages = list(messages)
    identities = db.amazon_event_identity_index()
    gmail_ids = set(identities["gmail_message_ids"])
    rfc_ids = set(identities["rfc_message_ids"])
    source_hashes = set(identities["source_hashes"])
    timestamp = timestamp_factory()
    new_events: list[AmazonStoredEvent] = []
    summary = {
        "fetched": len(raw_messages),
        "parsed": 0,
        "new": 0,
        "duplicate_gmail_id": 0,
        "duplicate_rfc_message_id": 0,
        "duplicate_source_hash": 0,
        "unknown": 0,
        "unknown_new": 0,
        "parser_errors": 0,
    }

    for message in raw_messages:
        try:
            parsed_event = parser(message.raw_mime)
            summary["parsed"] += 1
        except Exception:
            parsed_event = _unparsed_event(message.raw_mime)
            summary["parser_errors"] += 1
        stored = amazon_stored_event_from_mail(parsed_event, message, timestamp=timestamp)
        if stored.event_type == "unknown":
            summary["unknown"] += 1

        if stored.gmail_message_id in gmail_ids:
            summary["duplicate_gmail_id"] += 1
            continue
        if stored.rfc_message_id and stored.rfc_message_id in rfc_ids:
            summary["duplicate_rfc_message_id"] += 1
            continue
        if stored.source_hash and stored.source_hash in source_hashes:
            summary["duplicate_source_hash"] += 1
            continue

        new_events.append(stored)
        if stored.event_type == "unknown":
            summary["unknown_new"] += 1
        gmail_ids.add(stored.gmail_message_id)
        if stored.rfc_message_id:
            rfc_ids.add(stored.rfc_message_id)
        if stored.source_hash:
            source_hashes.add(stored.source_hash)

    if new_events:
        db.append("Amazonイベント", [event.to_row() for event in new_events])
    summary["new"] = len(new_events)
    return summary


def fetch_amazon_gmail_messages(service) -> list[GmailRawMessage]:
    """Fetch unique Gmail messages as IDs, thread IDs, and raw MIME bytes."""

    messages_api = service.users().messages()
    seen: set[str] = set()
    messages: list[GmailRawMessage] = []
    for _, query, limit in SEARCHES:
        if len(seen) >= MAX_MESSAGES:
            break
        response = messages_api.list(userId="me", q=query, maxResults=limit).execute()
        accepted = 0
        for item in response.get("messages", []):
            message_id = item.get("id")
            if not message_id or message_id in seen:
                continue
            if len(seen) >= MAX_MESSAGES or accepted >= limit:
                break
            seen.add(message_id)
            raw = messages_api.get(userId="me", id=message_id, format="raw").execute()
            messages.append(GmailRawMessage(
                gmail_message_id=message_id,
                thread_id=str(raw.get("threadId") or item.get("threadId") or ""),
                raw_mime=_raw_bytes(raw.get("raw", "")),
            ))
            accepted += 1
    return messages


def import_amazon_gmail_events(service, db) -> dict[str, int]:
    """Fetch Gmail once, then persist all new Amazon events in one batch."""

    return save_amazon_gmail_events(db, fetch_amazon_gmail_messages(service))
