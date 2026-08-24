from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
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
METADATA_FIELDS = {"parser_version", "last_parsed_at"}


@dataclass(frozen=True)
class ReparseUpdate:
    row_number: int
    gmail_message_id: str
    changes: dict[str, dict[str, object]]
    cells: tuple[tuple[int, object], ...]

    @property
    def business_fields(self) -> tuple[str, ...]:
        return tuple(
            field for field, _label, _column in PARSER_FIELDS
            if field not in METADATA_FIELDS and field in self.changes
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


def _has_value(value) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


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


def _valid_business_value(field: str, value, event: AmazonMailEvent) -> bool:
    if not _has_value(value):
        return False
    if field == "event_type":
        return value != "unknown"
    if field == "parse_status":
        return event.event_type != "unknown" or _has_value(event.order_id)
    return True


def _assert_unique_gmail_ids(rows: list[tuple[int, list]]) -> None:
    ids = [str(_cell(row, 1)).strip() for _row_number, row in rows]
    duplicates = sorted(value for value, count in Counter(ids).items() if value and count > 1)
    if duplicates:
        raise RuntimeError(
            "AmazonイベントのGmail Message IDが重複しています: "
            + ", ".join(duplicates)
        )


def _base_summary(stored_events: int) -> dict[str, object]:
    field_updates = {field: 0 for field, _label, _column in PARSER_FIELDS}
    return {
        "stored_events": stored_events,
        "gmail_fetched": 0,
        "gmail_missing": 0,
        "identity_mismatch": 0,
        "parser_success": 0,
        "parser_errors": 0,
        "updated_rows": 0,
        "business_fields_updated_rows": 0,
        "metadata_only_updated_rows": 0,
        "skipped_rows": 0,
        "error_rows": 0,
        "unchanged_rows": 0,
        "field_updates": field_updates,
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


def build_amazon_event_reparse_plan(
    service,
    db,
    *,
    parser: Callable[[bytes], AmazonMailEvent] = parse_amazon_email,
    timestamp_factory: Callable[[], str] = _timestamp,
) -> tuple[dict[str, object], list[ReparseUpdate]]:
    """Build a shared, write-free reparse plan for preview and apply."""

    rows = db.amazon_event_rows()
    _assert_unique_gmail_ids(rows)
    summary = _base_summary(len(rows))
    updates: list[ReparseUpdate] = []
    timestamp = timestamp_factory()
    messages_api = service.users().messages()

    for row_number, row in rows:
        stored_id = str(_cell(row, 1)).strip()
        if not stored_id:
            summary["identity_mismatch"] += 1
            summary["skipped_rows"] += 1
            continue
        try:
            response = messages_api.get(
                userId="me", id=stored_id, format="raw",
            ).execute()
        except Exception:
            summary["gmail_missing"] += 1
            summary["skipped_rows"] += 1
            summary["error_rows"] += 1
            continue
        fetched_id = str(response.get("id") or "").strip()
        if fetched_id != stored_id:
            summary["identity_mismatch"] += 1
            summary["skipped_rows"] += 1
            continue
        try:
            raw_mime = _raw_bytes(response.get("raw", ""))
        except (TypeError, ValueError):
            summary["gmail_missing"] += 1
            summary["skipped_rows"] += 1
            summary["error_rows"] += 1
            continue
        summary["gmail_fetched"] += 1
        try:
            event = parser(raw_mime)
        except Exception:
            summary["parser_errors"] += 1
            summary["skipped_rows"] += 1
            summary["error_rows"] += 1
            continue
        summary["parser_success"] += 1

        values = _new_values(event, timestamp)
        changes: dict[str, dict[str, object]] = {}
        cells: list[tuple[int, object]] = []
        for field, label, column in PARSER_FIELDS:
            new = values[field]
            if field not in METADATA_FIELDS and not _valid_business_value(field, new, event):
                continue
            old = _cell(row, column)
            if _comparable(old) == _comparable(new):
                continue
            changes[field] = {"old": old, "new": new, "label": label}
            cells.append((column, new))

        if not changes:
            summary["unchanged_rows"] += 1
            summary["unchanged_events"] += 1
            continue

        update = ReparseUpdate(row_number, stored_id, changes, tuple(cells))
        updates.append(update)
        summary["changed_events"] += 1
        summary["would_update"] += 1
        for field in changes:
            summary["field_updates"][field] += 1
        for field in ("event_type", "order_id", "order_amount", "parse_status", "parser_version"):
            summary[f"{field}_changed"] += int(field in changes)
        summary["changes"].append({
            "row": row_number,
            "gmail_message_id": stored_id,
            "fields": {
                change["label"]: {"old": change["old"], "new": change["new"]}
                for change in changes.values()
            },
        })

    return summary, updates


def preview_amazon_event_reparse(
    service,
    db,
    *,
    parser: Callable[[bytes], AmazonMailEvent] = parse_amazon_email,
    timestamp_factory: Callable[[], str] = _timestamp,
) -> dict:
    """Reparse stored Gmail messages and report changes without writing."""

    summary, _updates = build_amazon_event_reparse_plan(
        service, db, parser=parser, timestamp_factory=timestamp_factory,
    )
    return summary


def apply_amazon_event_reparse(
    service,
    db,
    *,
    parser: Callable[[bytes], AmazonMailEvent] = parse_amazon_email,
    timestamp_factory: Callable[[], str] = _timestamp,
) -> dict:
    """Apply a reparse plan using cell-limited writes."""

    summary, updates = build_amazon_event_reparse_plan(
        service, db, parser=parser, timestamp_factory=timestamp_factory,
    )
    planned_field_updates = summary["field_updates"]
    summary["field_updates"] = {field: 0 for field in planned_field_updates}

    for update in updates:
        try:
            db.update_amazon_event_cells(update.row_number, list(update.cells))
        except Exception:
            summary["error_rows"] += 1
            summary["skipped_rows"] += 1
            continue
        summary["updated_rows"] += 1
        if update.business_fields:
            summary["business_fields_updated_rows"] += 1
        else:
            summary["metadata_only_updated_rows"] += 1
        for field in update.changes:
            summary["field_updates"][field] += 1

    return summary
