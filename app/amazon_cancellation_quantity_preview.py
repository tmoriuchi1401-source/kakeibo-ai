from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable

from .amazon_cancellation_order_id_diagnose import _locate_source
from .amazon_cancellation_return_preview import fetch_gmail_thread_messages
from .amazon_email import (
    AmazonMailEvent,
    diagnose_cancellation_quantity,
    parse_amazon_email,
)
from .amazon_gmail_storage import GmailRawMessage
from .amazon_status_sync_preview import _cell, _order_item_counts, _positive_int, _scope


_FIELDS = (
    "target_cancellation_events",
    "source_email_found",
    "quantity_found",
    "quantity_still_missing",
    "quantity_invalid_or_ambiguous",
    "would_resolve_full_order",
    "would_resolve_item_or_partial",
    "would_remain_scope_unknown",
)


def _scope_without_header(event: list, detail_count: int | None) -> str:
    quantity = _positive_int(_cell(event, 17))
    if quantity is None or detail_count is None or quantity > detail_count:
        return "unknown"
    return "full_order" if quantity == detail_count else "item_or_partial"


def preview_amazon_cancellation_quantities(
    service,
    db,
    *,
    parser: Callable[[bytes], AmazonMailEvent] = parse_amazon_email,
    thread_fetcher: Callable[[object, str], list[GmailRawMessage]] = fetch_gmail_thread_messages,
) -> dict[str, int]:
    """Preview cancellation quantity parsing without persisting source data."""

    result: Counter[str] = Counter({field: 0 for field in _FIELDS})
    order_rows = [list(row) for row in db.get("Amazon注文!A2:O") if row]
    detail_counts = _order_item_counts(order_rows)
    detail_ids = {_cell(row, 1) for row in order_rows if _cell(row, 1)}
    headers_by_id: defaultdict[str, list[list]] = defaultdict(list)
    for raw in db.get("Amazon注文ヘッダ!A2:O"):
        row = list(raw)
        if _cell(row, 0):
            headers_by_id[_cell(row, 0)].append(row)

    for _row_number, raw in db.amazon_event_rows():
        event = list(raw)
        order_id = _cell(event, 6)
        if _cell(event, 5) != "cancellation" or not order_id:
            continue
        headers = headers_by_id.get(order_id, [])
        if len(headers) > 1 or (not headers and order_id not in detail_ids):
            continue
        header = headers[0] if headers else None
        current_scope = (
            _scope(event, header, detail_counts.get(order_id))
            if header else _scope_without_header(event, detail_counts.get(order_id))
        )
        if current_scope != "unknown":
            continue
        result["target_cancellation_events"] += 1

        source = _locate_source(service, event, thread_fetcher=thread_fetcher)
        if source.message is None:
            result["quantity_still_missing"] += 1
            result["would_remain_scope_unknown"] += 1
            continue
        result["source_email_found"] += 1
        try:
            parsed = parser(source.message.raw_mime)
            quantity_status = diagnose_cancellation_quantity(source.message.raw_mime)
        except Exception:
            parsed = None
            quantity_status = "invalid_or_ambiguous"

        quantity = _positive_int(str(parsed.item_count)) if parsed is not None else None
        order_total = (
            _positive_int(_cell(header, 4)) if header else None
        ) or detail_counts.get(order_id)
        if quantity_status in {"invalid_or_ambiguous", "not_cancellation"}:
            result["quantity_invalid_or_ambiguous"] += 1
            result["would_remain_scope_unknown"] += 1
        elif quantity_status != "found" or quantity is None:
            result["quantity_still_missing"] += 1
            result["would_remain_scope_unknown"] += 1
        elif order_total is None or quantity > order_total:
            result["quantity_invalid_or_ambiguous"] += 1
            result["would_remain_scope_unknown"] += 1
        elif quantity == order_total:
            result["quantity_found"] += 1
            result["would_resolve_full_order"] += 1
        else:
            result["quantity_found"] += 1
            result["would_resolve_item_or_partial"] += 1

    return {field: result[field] for field in _FIELDS}
