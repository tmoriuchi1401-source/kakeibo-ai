from __future__ import annotations

from .amazon_order_header_recalculation import (
    _header_row,
    _text,
    aggregate_amazon_order_events,
    group_amazon_order_events,
)


CONFLICT_FIELDS = (
    "order_amount",
    "payment_method",
    "item_count",
    "gift_card_amount",
    "points_amount",
    "discount_amount",
)


def preview_amazon_order_headers(db) -> dict:
    """Summarize stored event materialization without writing any data."""

    event_rows = db.amazon_order_creation_event_rows()
    groups, missing = group_amazon_order_events(event_rows)
    header_sheet_exists = "Amazon注文ヘッダ" in db.sheet_titles()
    header_rows = db.amazon_order_header_rows() if header_sheet_exists else []
    existing = {
        _text(row[0]): _header_row(row)
        for _, row in header_rows
    }
    order_ids = {
        order_id
        for order_id, events in groups.items()
        if any(_text(row[0]) == "order" for row in events)
    }
    new_candidates = sorted(order_ids - existing.keys())
    overlaps = set(groups) & existing.keys()
    conflict_counts = {name: 0 for name in CONFLICT_FIELDS}
    conflict_order_ids = []
    order_statuses = {name: 0 for name in ("ordered", "partially_shipped", "delivered")}
    refund_statuses = {name: 0 for name in ("none", "partial", "full")}
    amount_counts = {name: 0 for name in ("charged", "refund", "shipment")}

    for order_id in sorted(groups):
        base = existing.get(order_id, [order_id, "", "", "", "", "", "", "none"])
        aggregate = aggregate_amazon_order_events(groups[order_id], base)
        row = aggregate["row"]
        if row[5] in order_statuses:
            order_statuses[row[5]] += 1
        if row[7] in refund_statuses:
            refund_statuses[row[7]] += 1
        amount_counts["charged"] += int(aggregate["charged_amount_calculated"])
        amount_counts["refund"] += int(aggregate["refund_amount_calculated"])
        amount_counts["shipment"] += int(aggregate["shipment_amount_calculated"])
        has_conflict = False
        for name in CONFLICT_FIELDS:
            conflict_counts[name] += int(aggregate["conflicts"][name])
            has_conflict = has_conflict or aggregate["conflicts"][name]
        if has_conflict:
            conflict_order_ids.append(order_id)

    normalized_events = [row for events in groups.values() for row in events] + missing
    order_events = [row for row in normalized_events if _text(row[0]) == "order"]
    return {
        "amazon_order_header_sheet_exists": header_sheet_exists,
        "amazon_events": len(normalized_events),
        "order_events": len(order_events),
        "events_with_order_id": len(normalized_events) - len(missing),
        "events_without_order_id": len(missing),
        "unique_order_ids": len(groups),
        "new_header_candidates": len(new_candidates),
        "existing_headers": len(header_rows),
        "existing_order_id_overlaps": len(overlaps),
        "recalculation_targets": len(overlaps),
        "conflicts": sum(conflict_counts.values()),
        "order_statuses": order_statuses,
        "refund_statuses": refund_statuses,
        "calculated_amount_order_ids": amount_counts,
        "anomalies": {
            "events_without_order_id": len(missing),
            "order_events_without_order_id": sum(
                _text(row[0]) == "order" for row in missing
            ),
            "order_events_without_order_amount": sum(
                not _text(row[4]) for row in order_events
            ),
            "conflicts_by_field": conflict_counts,
        },
        "samples": {
            "new_header_candidate_order_ids": new_candidates[:5],
            "conflict_order_ids": conflict_order_ids[:5],
            "missing_order_id_event_types": sorted(_text(row[0]) for row in missing)[:5],
        },
    }
