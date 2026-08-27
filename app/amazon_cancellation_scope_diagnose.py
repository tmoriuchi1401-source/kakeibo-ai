from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable

from .amazon_cancellation_order_id_diagnose import _locate_source
from .amazon_cancellation_return_preview import (
    _AMOUNT_CLUE_RE,
    _QUANTITY_CLUE_RE,
    _diagnostic_text,
    _normalized_product,
    _product_clues,
    fetch_gmail_thread_messages,
)
from .amazon_gmail_storage import GmailRawMessage
from .amazon_status_sync_preview import _cell, _order_item_counts, _positive_int, _scope


_FIELDS = (
    "matched_cancellation_events",
    "scope_unknown_events",
    "has_cancelled_quantity",
    "has_order_amount",
    "has_refund_amount",
    "source_email_found",
    "source_has_quantity_clue",
    "source_has_product_clue",
    "source_has_amount_clue",
    "order_header_found",
    "order_items_found",
    "order_single_item",
    "order_multiple_items",
    "order_total_quantity_known",
    "order_total_quantity_unknown",
    "order_item_quantities_complete",
    "order_item_quantities_incomplete",
    "cancellation_quantity_known",
    "cancellation_quantity_unknown",
    "quantity_equals_order_total",
    "quantity_less_than_order_total",
    "quantity_exceeds_order_total",
    "item_match_unique",
    "item_match_none",
    "item_match_multiple",
    "item_match_not_possible",
    "unknown_missing_cancellation_quantity",
    "unknown_missing_order_quantity",
    "unknown_item_match_not_possible",
    "unknown_multiple_item_candidates",
    "unknown_conflicting_quantity",
    "unknown_insufficient_source_data",
    "unknown_other",
    "would_be_decidable_with_cancellation_quantity",
    "would_be_decidable_with_item_identifier",
    "would_be_decidable_with_order_item_quantity",
    "still_ambiguous_even_with_available_data",
)


def _scope_without_header(event: list, detail_count: int | None) -> str:
    cancelled_count = _positive_int(_cell(event, 17))
    if cancelled_count is None or detail_count is None or cancelled_count > detail_count:
        return "unknown"
    return "full_order" if cancelled_count == detail_count else "item_or_partial"


def _item_match_count(clues: set[str], order_rows: list[list]) -> int | None:
    if not clues or not order_rows:
        return None
    names = [_normalized_product(_cell(row, 4)) for row in order_rows]
    return sum(bool(name and name in clues) for name in names)


def diagnose_amazon_cancellation_scopes(
    service,
    db,
    *,
    thread_fetcher: Callable[[object, str], list[GmailRawMessage]] = fetch_gmail_thread_messages,
) -> dict[str, int]:
    """Explain current unknown cancellation scopes using read-only evidence."""

    result: Counter[str] = Counter({field: 0 for field in _FIELDS})
    order_rows = [list(row) for row in db.get("Amazon注文!A2:O") if row]
    details_by_id: defaultdict[str, list[list]] = defaultdict(list)
    for row in order_rows:
        if _cell(row, 1):
            details_by_id[_cell(row, 1)].append(row)
    detail_counts = _order_item_counts(order_rows)

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
        details = details_by_id.get(order_id, [])
        headers = headers_by_id.get(order_id, [])
        if len(headers) > 1 or (not headers and not details):
            continue
        result["matched_cancellation_events"] += 1
        header = headers[0] if headers else None
        scope = (
            _scope(event, header, detail_counts.get(order_id))
            if header else _scope_without_header(event, detail_counts.get(order_id))
        )
        cancelled_quantity = _positive_int(_cell(event, 17))
        result["has_cancelled_quantity"] += cancelled_quantity is not None
        result["has_order_amount"] += _positive_int(_cell(event, 9)) is not None
        result["has_refund_amount"] += _positive_int(_cell(event, 10)) is not None

        result["order_header_found"] += header is not None
        result["order_items_found"] += bool(details)
        result["order_single_item"] += len(details) == 1
        result["order_multiple_items"] += len(details) > 1
        detail_quantities_complete = bool(details) and all(
            _positive_int(_cell(row, 5)) is not None for row in details
        )
        result["order_item_quantities_complete"] += detail_quantities_complete
        result["order_item_quantities_incomplete"] += bool(details) and not detail_quantities_complete

        order_quantity = (
            _positive_int(_cell(header, 4)) if header else None
        ) or detail_counts.get(order_id)
        result["order_total_quantity_known"] += order_quantity is not None
        result["order_total_quantity_unknown"] += order_quantity is None
        result["cancellation_quantity_known"] += cancelled_quantity is not None
        result["cancellation_quantity_unknown"] += cancelled_quantity is None
        if cancelled_quantity is not None and order_quantity is not None:
            result["quantity_equals_order_total"] += cancelled_quantity == order_quantity
            result["quantity_less_than_order_total"] += cancelled_quantity < order_quantity
            result["quantity_exceeds_order_total"] += cancelled_quantity > order_quantity

        if scope != "unknown":
            continue
        result["scope_unknown_events"] += 1

        source = _locate_source(service, event, thread_fetcher=thread_fetcher)
        product_clues: set[str] = set()
        if source.message is not None:
            result["source_email_found"] += 1
            text = _diagnostic_text(source.message.raw_mime)
            product_clues = _product_clues(text)
            result["source_has_quantity_clue"] += bool(_QUANTITY_CLUE_RE.search(text))
            result["source_has_product_clue"] += bool(product_clues)
            result["source_has_amount_clue"] += bool(_AMOUNT_CLUE_RE.search(text))

        item_matches = _item_match_count(product_clues, details)
        if item_matches is None:
            result["item_match_not_possible"] += 1
        elif item_matches == 0:
            result["item_match_none"] += 1
        elif item_matches == 1:
            result["item_match_unique"] += 1
        else:
            result["item_match_multiple"] += 1

        if cancelled_quantity is None:
            reason = "unknown_missing_cancellation_quantity"
        elif order_quantity is None:
            reason = "unknown_missing_order_quantity"
        elif cancelled_quantity > order_quantity:
            reason = "unknown_conflicting_quantity"
        elif item_matches is None:
            reason = "unknown_item_match_not_possible"
        elif item_matches > 1:
            reason = "unknown_multiple_item_candidates"
        elif source.message is None:
            reason = "unknown_insufficient_source_data"
        else:
            reason = "unknown_other"
        result[reason] += 1

        could_improve = False
        if cancelled_quantity is None and order_quantity is not None:
            result["would_be_decidable_with_cancellation_quantity"] += 1
            could_improve = True
        if order_quantity is None and cancelled_quantity is not None:
            result["would_be_decidable_with_order_item_quantity"] += 1
            could_improve = True
        if item_matches is None and len(details) > 1:
            result["would_be_decidable_with_item_identifier"] += 1
            could_improve = True
        if not could_improve:
            result["still_ambiguous_even_with_available_data"] += 1

    return {field: result[field] for field in _FIELDS}
