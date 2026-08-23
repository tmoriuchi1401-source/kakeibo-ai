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


def group_amazon_order_events(event_rows) -> tuple[dict[str, list[list]], list[list]]:
    """Group normalized event rows by explicit Order ID."""

    groups: dict[str, list[list]] = defaultdict(list)
    missing = []
    for raw_row in event_rows:
        row = _event_row(raw_row)
        order_id = _text(row[1])
        if order_id:
            groups[order_id].append(row)
        else:
            missing.append(row)
    return dict(groups), missing


def aggregate_amazon_order_events(events: list[list], base_row=None) -> dict:
    """Return the B4-3 header projection and aggregation diagnostics."""

    new = _header_row(base_row or [])
    conflicts = {
        "order_amount": False,
        "payment_method": False,
        "item_count": False,
        "gift_card_amount": False,
        "points_amount": False,
        "discount_amount": False,
    }

    order_date = _oldest_order_date(events)
    if order_date is not None:
        new[1] = order_date

    for name, column, event_index, converter in (
        ("order_amount", 2, 4, _number),
        ("item_count", 4, 12, _number),
        ("gift_card_amount", 10, 7, _number),
        ("points_amount", 11, 8, _number),
        ("discount_amount", 12, 10, _number),
    ):
        value, conflict = _unique(events, "order", event_index, converter)
        conflicts[name] = conflict
        if value is not None:
            new[column] = value

    payment_method, conflict = _unique(events, "order", 11, _text)
    if payment_method is None and not conflict:
        payment_method, conflict = _unique(events, "payment", 11, _text)
    conflicts["payment_method"] = conflict
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

    return {
        "row": new,
        "conflicts": conflicts,
        "charged_amount_calculated": charged_amount is not None,
        "refund_amount_calculated": refund_amount is not None,
        "shipment_amount_calculated": shipment_amount is not None,
    }


def recalculate_amazon_order_headers(
    db,
    *,
    timestamp_factory: Callable[[], str] = _utc_timestamp,
) -> dict[str, int]:
    """Rebuild existing order-header state from all persisted Gmail events."""

    groups, missing = group_amazon_order_events(
        db.amazon_order_creation_event_rows()
    )

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
        events = groups[order_id]
        aggregate = aggregate_amazon_order_events(events, old)
        new = aggregate["row"]
        conflicts += sum(aggregate["conflicts"].values())

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
        "skipped_missing_order_id": len(missing),
        "conflicts": conflicts,
    }
