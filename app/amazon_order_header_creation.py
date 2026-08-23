from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from .amazon_order_header import AmazonOrderHeader


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_amazon_order_headers(
    db,
    *,
    timestamp_factory: Callable[[], str] = _utc_timestamp,
) -> dict[str, int]:
    """Create missing order headers from stored order events without updates."""

    known_order_ids = db.amazon_order_header_ids()
    event_rows = db.amazon_order_creation_event_rows()
    timestamp = timestamp_factory()
    new_headers: list[AmazonOrderHeader] = []
    summary = {
        "total_order_events": 0,
        "created": 0,
        "skipped_existing": 0,
        "skipped_missing_order_id": 0,
    }

    for raw_row in event_rows:
        row = list(raw_row) + [""] * max(0, 13 - len(raw_row))
        if str(row[0]).strip() != "order":
            continue

        summary["total_order_events"] += 1
        order_id = str(row[1]).strip() if row[1] is not None else ""
        if not order_id:
            summary["skipped_missing_order_id"] += 1
            continue
        if order_id in known_order_ids:
            summary["skipped_existing"] += 1
            continue

        new_headers.append(AmazonOrderHeader(
            order_id=order_id,
            order_date=row[2] or None,
            order_amount=row[4] if row[4] != "" else None,
            payment_method=row[11] or None,
            item_count=row[12] if row[12] != "" else None,
            order_status="ordered",
            charged_amount=None,
            refund_status="none",
            refund_amount=None,
            shipment_amount=None,
            gift_card_amount=row[7] if row[7] != "" else None,
            points_amount=row[8] if row[8] != "" else None,
            discount_amount=row[10] if row[10] != "" else None,
            source="gmail",
            last_updated_at=timestamp,
        ))
        known_order_ids.add(order_id)

    if new_headers:
        db.append("Amazon注文ヘッダ", [header.to_row() for header in new_headers])
    summary["created"] = len(new_headers)
    return summary
