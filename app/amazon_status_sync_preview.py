from __future__ import annotations

from collections import Counter, defaultdict

from .amazon_order_header import ORDER_STATUSES


_RESULT_FIELDS = (
    "cancellation_events",
    "order_id_match",
    "order_id_not_found",
    "multiple_order_matches",
    "scope_full_order",
    "scope_item_or_partial",
    "scope_unknown",
    "would_cancel_order",
    "would_cancel_item_or_partial",
    "would_require_review",
    "would_noop_already_cancelled",
    "missing_order_id",
    "missing_order_header",
)


def _cell(row: list, index: int) -> str:
    return str(row[index]).strip() if len(row) > index else ""


def _positive_int(value: str) -> int | None:
    try:
        parsed = int(float(value.replace(",", "")))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _order_item_counts(rows: list[list]) -> dict[str, int]:
    counts: defaultdict[str, int] = defaultdict(int)
    for row in rows:
        order_id = _cell(row, 1)
        quantity = _positive_int(_cell(row, 5))
        if order_id and quantity is not None:
            counts[order_id] += quantity
    return dict(counts)


def _scope(event: list, header: list, detail_count: int | None) -> str:
    cancelled_count = _positive_int(_cell(event, 17))
    order_count = _positive_int(_cell(header, 4)) or detail_count
    if cancelled_count is None or order_count is None or cancelled_count > order_count:
        return "unknown"
    if cancelled_count == order_count:
        return "full_order"
    return "item_or_partial"


def preview_amazon_status_sync(db) -> dict[str, int]:
    """Plan cancellation status changes using Sheets reads only.

    The returned value contains counters only. No source identifiers, item names,
    amounts, or row data are retained in the result.
    """

    result: Counter[str] = Counter({field: 0 for field in _RESULT_FIELDS})
    order_rows = [list(row) for row in db.get("Amazon注文!A2:O") if row]
    detail_counts = _order_item_counts(order_rows)
    detail_order_ids = {_cell(row, 1) for row in order_rows if _cell(row, 1)}
    headers_by_order_id: defaultdict[str, list[list]] = defaultdict(list)
    for raw in db.get("Amazon注文ヘッダ!A2:O"):
        row = list(raw)
        order_id = _cell(row, 0)
        if order_id:
            headers_by_order_id[order_id].append(row)

    for raw in db.get("Amazonイベント!A2:X"):
        event = list(raw)
        if _cell(event, 5) != "cancellation":
            continue
        result["cancellation_events"] += 1
        order_id = _cell(event, 6)
        if not order_id:
            result["missing_order_id"] += 1
            result["scope_unknown"] += 1
            result["would_require_review"] += 1
            continue

        matches = headers_by_order_id.get(order_id, [])
        if not matches and order_id not in detail_order_ids:
            result["order_id_not_found"] += 1
            result["scope_unknown"] += 1
            result["would_require_review"] += 1
            continue
        if len(matches) > 1:
            result["multiple_order_matches"] += 1
            result["scope_unknown"] += 1
            result["would_require_review"] += 1
            continue

        result["order_id_match"] += 1
        if not matches:
            result["missing_order_header"] += 1
            cancelled_count = _positive_int(_cell(event, 17))
            detail_count = detail_counts.get(order_id)
            scope = (
                "full_order" if cancelled_count and cancelled_count == detail_count
                else "item_or_partial" if cancelled_count and detail_count and cancelled_count < detail_count
                else "unknown"
            )
            result[f"scope_{scope}"] += 1
            result["would_require_review"] += 1
            continue
        header = matches[0]
        scope = _scope(event, header, detail_counts.get(order_id))
        result[f"scope_{scope}"] += 1
        if _cell(header, 5).lower() == "cancelled" and "cancelled" in ORDER_STATUSES:
            result["would_noop_already_cancelled"] += 1
        elif scope == "full_order":
            result["would_cancel_order"] += 1
        elif scope == "item_or_partial":
            result["would_cancel_item_or_partial"] += 1
        else:
            result["would_require_review"] += 1

    return {field: result[field] for field in _RESULT_FIELDS}
