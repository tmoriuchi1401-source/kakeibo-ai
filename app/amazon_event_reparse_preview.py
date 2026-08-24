from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from .amazon_email import AmazonMailEvent, parse_amazon_email
from .amazon_gmail_preview import _raw_bytes
from .amazon_gmail_storage import AMAZON_PARSER_VERSION


PARSER_FIELDS = (
    ("event_type", "Event Type", 5),
    ("order_id", "Order ID", 6),
    ("event_date", "Event Date", 7),
    ("charged_amount", "Charged Amount", 8),
    ("order_amount", "Order Amount", 9),
    ("refund_amount", "Refund Amount", 10),
    ("shipment_amount", "Shipment Amount", 11),
    ("gift_card_amount", "Gift Card Amount", 12),
    ("points_amount", "Points Amount", 13),
    ("coupon_amount", "Coupon Amount", 14),
    ("discount_amount", "Discount Amount", 15),
    ("payment_method", "Payment Method", 16),
    ("item_count", "Item Count", 17),
    ("parse_status", "Parse Status", 18),
    ("parser_version", "Parser Version", 21),
    ("last_parsed_at", "Last Parsed At", 23),
)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cell(row: list, index: int):
    value = row[index] if index < len(row) else ""
    return "" if value is None else value


def _comparable(value):
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and float(value).is_integer():
        return str(int(value))
    return str(value).strip()


def _parse_status(event: AmazonMailEvent) -> str:
    if event.event_type != "unknown":
        return "parsed"
    return "needs_review" if event.order_id else "unusable"


def _new_values(event: AmazonMailEvent, timestamp: str) -> dict[str, object]:
    return {
        "event_type": event.event_type,
        "order_id": event.order_id,
        "event_date": event.event_date,
        "charged_amount": event.charged_amount,
        "order_amount": event.order_amount,
        "refund_amount": event.refund_amount,
        "shipment_amount": event.shipment_amount,
        "gift_card_amount": event.gift_card_amount,
        "points_amount": event.points_amount,
        "coupon_amount": event.coupon_amount,
        "discount_amount": event.discount_amount,
        "payment_method": event.payment_method,
        "item_count": event.item_count,
        "parse_status": _parse_status(event),
        "parser_version": AMAZON_PARSER_VERSION,
        "last_parsed_at": timestamp,
    }


def preview_amazon_event_reparse(
    service,
    db,
    *,
    parser: Callable[[bytes], AmazonMailEvent] = parse_amazon_email,
    timestamp_factory: Callable[[], str] = _timestamp,
) -> dict:
    """Reparse stored Gmail messages and report changes without writing."""

    rows = db.amazon_event_rows()
    timestamp = timestamp_factory()
    summary: dict[str, object] = {
        "stored_events": len(rows),
        "gmail_fetched": 0,
        "gmail_missing": 0,
        "identity_mismatch": 0,
        "parser_success": 0,
        "parser_errors": 0,
        "changed_events": 0,
        "unchanged_events": 0,
        "event_type_changed": 0,
        "order_id_changed": 0,
        "order_amount_changed": 0,
        "parse_status_changed": 0,
        "parser_version_changed": 0,
        "would_update": 0,
        "changes": [],
    }
    messages_api = service.users().messages()

    for row_number, row in rows:
        stored_id = str(_cell(row, 1)).strip()
        if not stored_id:
            summary["identity_mismatch"] += 1
            continue
        try:
            response = messages_api.get(
                userId="me", id=stored_id, format="raw",
            ).execute()
        except Exception:
            summary["gmail_missing"] += 1
            continue
        fetched_id = str(response.get("id") or "").strip()
        if fetched_id != stored_id:
            summary["identity_mismatch"] += 1
            continue
        try:
            raw_mime = _raw_bytes(response.get("raw", ""))
        except (TypeError, ValueError):
            summary["gmail_missing"] += 1
            continue
        summary["gmail_fetched"] += 1
        try:
            event = parser(raw_mime)
        except Exception:
            summary["parser_errors"] += 1
            continue
        summary["parser_success"] += 1

        values = _new_values(event, timestamp)
        changes = {}
        for field, label, column in PARSER_FIELDS:
            old = _cell(row, column)
            new = values[field]
            if _comparable(old) != _comparable(new):
                changes[label] = {
                    "old": old,
                    "new": "" if new is None else new,
                }
        if not changes:
            summary["unchanged_events"] += 1
            continue

        summary["changed_events"] += 1
        summary["would_update"] += 1
        for field, counter in (
            ("Event Type", "event_type_changed"),
            ("Order ID", "order_id_changed"),
            ("Order Amount", "order_amount_changed"),
            ("Parse Status", "parse_status_changed"),
            ("Parser Version", "parser_version_changed"),
        ):
            summary[counter] += int(field in changes)
        summary["changes"].append({
            "row": row_number,
            "gmail_message_id": stored_id,
            "fields": changes,
        })

    return summary
