from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _number(value) -> int | Decimal | None:
    text = _text(value).replace(",", "").replace("¥", "")
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return int(number) if number == number.to_integral_value() else number


def _event_row(raw_row) -> list:
    return list(raw_row) + [""] * max(0, 13 - len(raw_row))


def _header_row(raw_row) -> list:
    return list(raw_row) + [""] * max(0, 15 - len(raw_row))


def _unique(events: list[list], event_type: str, index: int, converter):
    values = {
        value
        for row in events
        if _text(row[0]) == event_type
        for value in [converter(row[index])]
        if value is not None and value != ""
    }
    if len(values) == 1:
        return next(iter(values)), False
    return None, len(values) > 1


def _oldest_order_date(events: list[list]) -> str | None:
    candidates = []
    for row in events:
        if _text(row[0]) != "order" or not _text(row[2]):
            continue
        text = _text(row[2])
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.astimezone(timezone.utc)
        candidates.append((parsed, text))
    return min(candidates, key=lambda candidate: (candidate[0], candidate[1]))[1] \
        if candidates else None


def _sum(events: list[list], event_type: str, index: int):
    values = [
        value
        for row in events
        if _text(row[0]) == event_type
        for value in [_number(row[index])]
        if value is not None
    ]
    return sum(values) if values else None


def _same(left, right, *, numeric: bool = False) -> bool:
    if numeric:
        return _number(left) == _number(right)
    return _text(left) == _text(right)


def recalculate_amazon_order_headers(
    db,
    *,
    timestamp_factory: Callable[[], str] = _utc_timestamp,
) -> dict[str, int]:
    """Rebuild existing order-header state from all persisted Gmail events."""

    groups: dict[str, list[list]] = defaultdict(list)
    skipped_missing_order_id = 0
    for raw_row in db.amazon_order_creation_event_rows():
        row = _event_row(raw_row)
        order_id = _text(row[1])
        if not order_id:
            skipped_missing_order_id += 1
            continue
        groups[order_id].append(row)

    existing = {
        _text(row[0]): (row_num, _header_row(row))
        for row_num, row in db.amazon_order_header_rows()
    }
    updates = []
    conflicts = 0
    update_timestamp = None
    processed = 0

    for order_id in sorted(groups):
        current_entry = existing.get(order_id)
        if current_entry is None:
            continue
        processed += 1
        row_num, old = current_entry
        new = list(old)
        events = groups[order_id]

        order_date = _oldest_order_date(events)
        if order_date is not None:
            new[1] = order_date

        for column, event_index, converter in (
            (2, 4, _number),
            (4, 12, _number),
            (10, 7, _number),
            (11, 8, _number),
            (12, 10, _number),
        ):
            value, conflict = _unique(events, "order", event_index, converter)
            conflicts += int(conflict)
            if value is not None:
                new[column] = value

        payment_method, conflict = _unique(events, "order", 11, _text)
        if payment_method is None and not conflict:
            payment_method, conflict = _unique(events, "payment", 11, _text)
        conflicts += int(conflict)
        if payment_method is not None:
            new[3] = payment_method

        event_types = {_text(row[0]) for row in events}
        if "delivery" in event_types:
            new[5] = "delivered"
        elif "shipment" in event_types:
            new[5] = "partially_shipped"
        elif "order" in event_types:
            new[5] = "ordered"

        charged_amount = _sum(events, "payment", 3)
        refund_amount = _sum(events, "refund", 5)
        shipment_amount = _sum(events, "shipment", 6)
        if charged_amount is not None:
            new[6] = charged_amount
        if refund_amount is not None:
            new[8] = refund_amount
            order_amount = _number(new[2])
            if refund_amount <= 0:
                new[7] = "none"
            else:
                new[7] = "full" if order_amount is not None and refund_amount >= order_amount \
                    else "partial"
        if shipment_amount is not None:
            new[9] = shipment_amount

        numeric_columns = {2, 4, 6, 8, 9, 10, 11, 12}
        changed = any(
            not _same(old[index], new[index], numeric=index in numeric_columns)
            for index in range(14)
        )
        if changed:
            if update_timestamp is None:
                update_timestamp = timestamp_factory()
            new[14] = update_timestamp
            updates.append((row_num, new))

    if updates:
        db.update_amazon_order_headers(updates)
    return {
        "orders": len(groups),
        "updated": len(updates),
        "unchanged": processed - len(updates),
        "skipped_missing_order_id": skipped_missing_order_id,
        "conflicts": conflicts,
    }
