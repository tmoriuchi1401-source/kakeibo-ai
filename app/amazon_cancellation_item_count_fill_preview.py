from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Literal, TypeAlias

from .amazon_cancellation_order_id_diagnose import _locate_source
from .amazon_cancellation_return_preview import fetch_gmail_thread_messages
from .amazon_email import (
    AmazonMailEvent,
    _CANCELLATION_QUANTITY_PATTERNS,
    _body_parts,
    _message,
    parse_amazon_email,
)
from .amazon_gmail_storage import GmailRawMessage


FillDisposition: TypeAlias = Literal["fill", "skip", "blocked"]


@dataclass(frozen=True)
class ProposedItemCountChange:
    sheet: Literal["Amazonイベント"] = "Amazonイベント"
    column: Literal["Item Count"] = "Item Count"
    column_index: Literal[17] = 17
    current_value: Literal[""] = ""
    proposed_value: int = 0


@dataclass(frozen=True)
class ItemCountFillPlan:
    disposition: FillDisposition
    reason: str
    proposed_changes: tuple[ProposedItemCountChange, ...] = ()


@dataclass(frozen=True)
class ItemCountFillUpdate:
    row_number: int
    gmail_message_id: str
    saved_order_id: str
    parsed_order_id: str
    header_match_count: int
    parsed_quantity: int
    quantity_candidates: tuple[int, ...]
    order_total_quantity: int
    proposed_change: ProposedItemCountChange


def _cell(row: list, index: int) -> str:
    return str(row[index]).strip() if len(row) > index else ""


def _positive_integer(value: object) -> int | None:
    text = str(value).strip().replace(",", "") if value is not None else ""
    if not text:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    if not number.is_integer() or number <= 0:
        return None
    return int(number)


def _complete_order_quantity(rows: list[list]) -> int | None:
    if not rows:
        return None
    quantities = [_positive_integer(_cell(row, 5)) for row in rows]
    if any(quantity is None for quantity in quantities):
        return None
    return sum(quantity for quantity in quantities if quantity is not None)


def plan_cancellation_item_count_fill(
    *,
    event_type: str,
    saved_item_count: object,
    saved_order_id: str,
    parsed_order_id: str | None,
    header_match_count: int,
    parsed_quantity: object,
    quantity_candidates: tuple[int, ...],
    order_total_quantity: int | None,
) -> ItemCountFillPlan:
    """Plan one R-column fill from already parsed, identity-checked input."""

    if event_type != "cancellation":
        return ItemCountFillPlan("skip", "not_cancellation")
    if str(saved_item_count).strip():
        return ItemCountFillPlan("skip", "existing_item_count")
    if not saved_order_id:
        return ItemCountFillPlan("blocked", "missing_saved_order_id")
    if not parsed_order_id:
        return ItemCountFillPlan("blocked", "missing_parsed_order_id")
    if saved_order_id != parsed_order_id:
        return ItemCountFillPlan("blocked", "order_id_mismatch")
    if header_match_count == 0:
        return ItemCountFillPlan("blocked", "order_not_found")
    if header_match_count != 1:
        return ItemCountFillPlan("blocked", "duplicate_order_header")

    quantity = _positive_integer(parsed_quantity)
    if parsed_quantity is None or str(parsed_quantity).strip() == "":
        return ItemCountFillPlan("blocked", "missing_parsed_quantity")
    if quantity is None:
        return ItemCountFillPlan("blocked", "invalid_parsed_quantity")
    distinct_values = set(quantity_candidates)
    if not distinct_values:
        return ItemCountFillPlan("blocked", "missing_parsed_quantity")
    if any(value <= 0 for value in distinct_values):
        return ItemCountFillPlan("blocked", "invalid_parsed_quantity")
    if len(distinct_values) != 1 or quantity not in distinct_values:
        return ItemCountFillPlan("blocked", "ambiguous_parsed_quantity")
    if order_total_quantity is None or order_total_quantity <= 0:
        return ItemCountFillPlan("blocked", "missing_order_quantity")
    if quantity > order_total_quantity:
        return ItemCountFillPlan("blocked", "quantity_exceeds_order_quantity")
    return ItemCountFillPlan(
        "fill",
        "safe_item_count_fill",
        (ProposedItemCountChange(proposed_value=quantity),),
    )


def _quantity_candidates(raw_mime: bytes) -> tuple[int, ...]:
    message, _raw = _message(raw_mime)
    plain, html, _html_sources = _body_parts(message)
    return tuple(
        int(match.group(1))
        for text in (plain, html)
        for pattern in _CANCELLATION_QUANTITY_PATTERNS
        for match in pattern.finditer(text)
    )


def build_amazon_cancellation_item_count_fill_plan(
    service,
    db,
    *,
    parser=parse_amazon_email,
    thread_fetcher=fetch_gmail_thread_messages,
) -> tuple[dict[str, int], list[ItemCountFillUpdate]]:
    """Build the shared, write-free plan used by preview and apply."""

    order_rows = [list(row) for row in db.get("Amazon注文!A2:O") if row]
    details_by_id: defaultdict[str, list[list]] = defaultdict(list)
    for row in order_rows:
        if _cell(row, 1):
            details_by_id[_cell(row, 1)].append(row)

    headers_by_id: defaultdict[str, list[list]] = defaultdict(list)
    for raw in db.get("Amazon注文ヘッダ!A2:O"):
        row = list(raw)
        if _cell(row, 0):
            headers_by_id[_cell(row, 0)].append(row)

    result: Counter[str] = Counter({
        "cancellation_event_count": 0,
        "would_fill_item_count": 0,
        "already_has_item_count": 0,
        "blocked_count": 0,
        "skipped_count": 0,
        "non_cancellation_event_count": 0,
    })
    updates: list[ItemCountFillUpdate] = []
    for row_number, raw in db.amazon_event_rows():
        event = list(raw)
        event_type = _cell(event, 5)
        if event_type != "cancellation":
            result["non_cancellation_event_count"] += 1
            result["skipped_count"] += 1
            result["reason_not_cancellation_count"] += 1
            continue
        result["cancellation_event_count"] += 1
        if _cell(event, 17):
            result["already_has_item_count"] += 1
            result["skipped_count"] += 1
            result["reason_existing_item_count_count"] += 1
            continue

        source = _locate_source(service, event, thread_fetcher=thread_fetcher)
        if source.ambiguous:
            result["blocked_count"] += 1
            result["reason_identity_mismatch_count"] += 1
            continue
        if source.message is None:
            result["blocked_count"] += 1
            result["reason_gmail_missing_count"] += 1
            continue
        try:
            parsed: AmazonMailEvent = parser(source.message.raw_mime)
            candidates = _quantity_candidates(source.message.raw_mime)
        except Exception:
            result["blocked_count"] += 1
            result["reason_parser_error_count"] += 1
            continue

        saved_order_id = _cell(event, 6)
        matches = headers_by_id.get(saved_order_id, []) if saved_order_id else []
        header_quantity = (
            _positive_integer(_cell(matches[0], 4)) if len(matches) == 1 else None
        )
        order_total = header_quantity or _complete_order_quantity(
            details_by_id.get(saved_order_id, []),
        )
        plan = plan_cancellation_item_count_fill(
            event_type=event_type,
            saved_item_count=_cell(event, 17),
            saved_order_id=saved_order_id,
            parsed_order_id=parsed.order_id,
            header_match_count=len(matches),
            parsed_quantity=parsed.item_count,
            quantity_candidates=candidates,
            order_total_quantity=order_total,
        )
        result[f"reason_{plan.reason}_count"] += 1
        if plan.disposition == "fill":
            result["would_fill_item_count"] += 1
            change = plan.proposed_changes[0]
            updates.append(ItemCountFillUpdate(
                row_number=row_number,
                gmail_message_id=_cell(event, 1),
                saved_order_id=saved_order_id,
                parsed_order_id=str(parsed.order_id),
                header_match_count=len(matches),
                parsed_quantity=change.proposed_value,
                quantity_candidates=candidates,
                order_total_quantity=order_total,
                proposed_change=change,
            ))
        elif plan.disposition == "skip":
            result["skipped_count"] += 1
        else:
            result["blocked_count"] += 1

    return dict(result), updates


def preview_amazon_cancellation_item_count_fills(
    service,
    db,
    *,
    parser=parse_amazon_email,
    thread_fetcher=fetch_gmail_thread_messages,
) -> dict[str, int]:
    """Preview safe cancellation Item Count fills without any writes."""

    summary, _updates = build_amazon_cancellation_item_count_fill_plan(
        service, db, parser=parser, thread_fetcher=thread_fetcher,
    )
    return summary


def apply_amazon_cancellation_item_count_fills(
    service,
    db,
    *,
    parser=parse_amazon_email,
    thread_fetcher=fetch_gmail_thread_messages,
) -> dict[str, int]:
    """Fill only still-blank Amazon event Item Count cells from a shared plan."""

    planned, updates = build_amazon_cancellation_item_count_fill_plan(
        service, db, parser=parser, thread_fetcher=thread_fetcher,
    )
    result = dict(planned)
    result["eligible_fill_count"] = len(updates)
    result["updated_item_count_count"] = 0
    result["skipped_existing_item_count_count"] = 0
    result["error_count"] = 0

    for update in updates:
        try:
            rows = db.get(f"Amazonイベント!A{update.row_number}:X{update.row_number}")
        except Exception:
            result["error_count"] += 1
            result["reason_stale_read_error_count"] = (
                result.get("reason_stale_read_error_count", 0) + 1
            )
            continue
        if len(rows) != 1:
            result["blocked_count"] += 1
            result["reason_stale_event_missing_count"] = (
                result.get("reason_stale_event_missing_count", 0) + 1
            )
            continue
        current = list(rows[0])
        if _cell(current, 1) != update.gmail_message_id:
            result["blocked_count"] += 1
            result["reason_stale_identity_mismatch_count"] = (
                result.get("reason_stale_identity_mismatch_count", 0) + 1
            )
            continue
        stale_plan = plan_cancellation_item_count_fill(
            event_type=_cell(current, 5),
            saved_item_count=_cell(current, 17),
            saved_order_id=_cell(current, 6),
            parsed_order_id=update.parsed_order_id,
            header_match_count=update.header_match_count,
            parsed_quantity=update.parsed_quantity,
            quantity_candidates=update.quantity_candidates,
            order_total_quantity=update.order_total_quantity,
        )
        if stale_plan.disposition != "fill":
            if stale_plan.reason == "existing_item_count":
                result["skipped_existing_item_count_count"] += 1
            else:
                result["blocked_count"] += 1
            result[f"reason_stale_{stale_plan.reason}_count"] = (
                result.get(f"reason_stale_{stale_plan.reason}_count", 0) + 1
            )
            continue
        change = stale_plan.proposed_changes[0]
        if change.column_index != 17 or change.current_value != "" \
                or change.proposed_value <= 0:
            result["blocked_count"] += 1
            result["reason_invalid_proposed_change_count"] = (
                result.get("reason_invalid_proposed_change_count", 0) + 1
            )
            continue
        try:
            db.update_amazon_event_item_count(
                update.row_number, change.proposed_value,
            )
        except Exception:
            result["error_count"] += 1
            result["reason_write_error_count"] = (
                result.get("reason_write_error_count", 0) + 1
            )
            continue
        result["updated_item_count_count"] += 1

    return result
