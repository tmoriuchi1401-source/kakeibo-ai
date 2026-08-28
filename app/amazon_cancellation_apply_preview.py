from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Literal, TypeAlias


CancellationDisposition: TypeAlias = Literal["apply", "noop", "review", "blocked"]
CancellationScope: TypeAlias = Literal["full_order", "item_or_partial", "unknown"]


@dataclass(frozen=True)
class ProposedHeaderChange:
    sheet: Literal["Amazon注文ヘッダ"]
    column: Literal["Order Status"]
    column_index: Literal[5]
    current_value: str
    proposed_value: Literal["cancelled"]


@dataclass(frozen=True)
class CancellationApplyPlan:
    disposition: CancellationDisposition
    scope: CancellationScope
    reason: str
    proposed_changes: tuple[ProposedHeaderChange, ...] = ()


@dataclass(frozen=True)
class CancellationStatusUpdate:
    event_row_number: int
    event_id: str
    gmail_message_id: str
    header_row_number: int
    order_id: str


_SUMMARY_FIELDS = (
    "cancellation_event_count",
    "would_cancel_order_count",
    "noop_already_cancelled_count",
    "review_item_or_partial_count",
    "review_unknown_count",
    "blocked_count",
)


def _cell(row: list, index: int) -> str:
    return str(row[index]).strip() if len(row) > index else ""


def _quantity(value: object) -> tuple[int | None, str]:
    text = str(value).strip().replace(",", "") if value is not None else ""
    if not text:
        return None, "missing"
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None, "invalid"
    if not number.is_integer() or number <= 0:
        return None, "invalid"
    return int(number), "valid"


def _complete_order_quantity(rows: list[list]) -> int | None:
    if not rows:
        return None
    quantities = [_quantity(_cell(row, 5))[0] for row in rows]
    if any(quantity is None for quantity in quantities):
        return None
    return sum(quantity for quantity in quantities if quantity is not None)


def plan_cancellation_apply(
    event: list,
    matched_headers: list[list],
    order_total_quantity: int | None,
) -> CancellationApplyPlan:
    """Plan one cancellation without mutating an event, order, or header."""

    order_id = _cell(event, 6)
    if not order_id:
        return CancellationApplyPlan("blocked", "unknown", "missing_order_id")
    if not matched_headers:
        return CancellationApplyPlan("blocked", "unknown", "order_not_found")
    if len(matched_headers) != 1:
        return CancellationApplyPlan("blocked", "unknown", "duplicate_order_header")

    cancellation_quantity, quantity_status = _quantity(_cell(event, 17))
    if quantity_status == "missing":
        return CancellationApplyPlan(
            "blocked", "unknown", "missing_cancellation_quantity",
        )
    if quantity_status == "invalid":
        return CancellationApplyPlan(
            "blocked", "unknown", "invalid_cancellation_quantity",
        )
    if order_total_quantity is None or order_total_quantity <= 0:
        return CancellationApplyPlan("blocked", "unknown", "missing_order_quantity")
    if cancellation_quantity > order_total_quantity:
        return CancellationApplyPlan(
            "blocked", "unknown", "cancellation_quantity_exceeds_order_quantity",
        )
    if cancellation_quantity < order_total_quantity:
        return CancellationApplyPlan(
            "review", "item_or_partial", "partial_cancellation_item_unresolved",
        )

    current_status = _cell(matched_headers[0], 5)
    if current_status.lower() == "cancelled":
        return CancellationApplyPlan("noop", "full_order", "already_cancelled")
    return CancellationApplyPlan(
        "apply",
        "full_order",
        "full_order_cancellation",
        proposed_changes=(ProposedHeaderChange(
            sheet="Amazon注文ヘッダ",
            column="Order Status",
            column_index=5,
            current_value=current_status,
            proposed_value="cancelled",
        ),),
    )


def build_amazon_cancellation_apply_plan(
    db,
) -> tuple[dict[str, int], list[CancellationStatusUpdate]]:
    """Build the shared, write-free plan used by preview and apply."""

    result: Counter[str] = Counter({field: 0 for field in _SUMMARY_FIELDS})
    details_by_order_id: defaultdict[str, list[list]] = defaultdict(list)
    for raw in db.get("Amazon注文!A2:O"):
        row = list(raw)
        order_id = _cell(row, 1)
        if order_id:
            details_by_order_id[order_id].append(row)

    headers_by_order_id: defaultdict[str, list[tuple[int, list]]] = defaultdict(list)
    for row_number, raw in enumerate(db.get("Amazon注文ヘッダ!A2:O"), start=2):
        row = list(raw)
        order_id = _cell(row, 0)
        if order_id:
            headers_by_order_id[order_id].append((row_number, row))

    updates: list[CancellationStatusUpdate] = []
    for event_row_number, raw in enumerate(db.get("Amazonイベント!A2:X"), start=2):
        event = list(raw)
        if _cell(event, 5) != "cancellation":
            continue
        result["cancellation_event_count"] += 1
        order_id = _cell(event, 6)
        header_entries = headers_by_order_id.get(order_id, []) if order_id else []
        headers = [row for _row_number, row in header_entries]
        header_quantity = _quantity(_cell(headers[0], 4))[0] if len(headers) == 1 else None
        order_quantity = header_quantity or _complete_order_quantity(
            details_by_order_id.get(order_id, []),
        )
        plan = plan_cancellation_apply(event, headers, order_quantity)
        result[f"reason_{plan.reason}_count"] += 1
        if plan.disposition == "apply":
            result["would_cancel_order_count"] += 1
            updates.append(CancellationStatusUpdate(
                event_row_number=event_row_number,
                event_id=_cell(event, 0),
                gmail_message_id=_cell(event, 1),
                header_row_number=header_entries[0][0],
                order_id=order_id,
            ))
        elif plan.disposition == "noop":
            result["noop_already_cancelled_count"] += 1
        elif plan.scope == "item_or_partial":
            result["review_item_or_partial_count"] += 1
        else:
            result["review_unknown_count"] += 1
            result["blocked_count"] += 1

    return dict(result), updates


def preview_amazon_cancellation_apply(db) -> dict[str, int]:
    """Summarize prospective header status changes using Sheets reads only."""

    summary, _updates = build_amazon_cancellation_apply_plan(db)
    return summary


def apply_amazon_cancellation_order_statuses(db) -> dict[str, int]:
    """Cancel only headers that remain safe after a last-moment replan."""

    planned, updates = build_amazon_cancellation_apply_plan(db)
    result = dict(planned)
    result["eligible_cancel_count"] = len(updates)
    result["updated_order_status_count"] = 0
    result["error_count"] = 0

    for update in updates:
        try:
            event_rows = db.get(
                f"Amazonイベント!A{update.event_row_number}:X{update.event_row_number}"
            )
            header_entries = [
                (row_number, list(row))
                for row_number, row in enumerate(
                    db.get("Amazon注文ヘッダ!A2:O"), start=2,
                )
                if _cell(list(row), 0) == update.order_id
            ]
            current_details = [
                list(row) for row in db.get("Amazon注文!A2:O")
                if _cell(list(row), 1) == update.order_id
            ]
        except Exception:
            result["error_count"] += 1
            result["reason_stale_read_error_count"] = (
                result.get("reason_stale_read_error_count", 0) + 1
            )
            continue
        if len(event_rows) != 1:
            result["blocked_count"] += 1
            result["reason_stale_event_missing_count"] = (
                result.get("reason_stale_event_missing_count", 0) + 1
            )
            continue
        event = list(event_rows[0])
        if _cell(event, 0) != update.event_id \
                or _cell(event, 1) != update.gmail_message_id:
            result["blocked_count"] += 1
            result["reason_stale_identity_mismatch_count"] = (
                result.get("reason_stale_identity_mismatch_count", 0) + 1
            )
            continue

        headers = [row for _row_number, row in header_entries]
        header_quantity = _quantity(_cell(headers[0], 4))[0] if len(headers) == 1 else None
        order_quantity = header_quantity or _complete_order_quantity(current_details)
        stale_plan = plan_cancellation_apply(event, headers, order_quantity)
        if stale_plan.disposition != "apply" or stale_plan.scope != "full_order":
            if stale_plan.disposition == "noop" and stale_plan.reason == "already_cancelled":
                result["noop_already_cancelled_count"] += 1
            else:
                result["blocked_count"] += 1
            result[f"reason_stale_{stale_plan.reason}_count"] = (
                result.get(f"reason_stale_{stale_plan.reason}_count", 0) + 1
            )
            continue
        if len(header_entries) != 1 \
                or header_entries[0][0] != update.header_row_number:
            result["blocked_count"] += 1
            result["reason_stale_header_identity_mismatch_count"] = (
                result.get("reason_stale_header_identity_mismatch_count", 0) + 1
            )
            continue
        change = stale_plan.proposed_changes[0]
        if change.column_index != 5 or change.proposed_value != "cancelled":
            result["blocked_count"] += 1
            result["reason_invalid_proposed_change_count"] = (
                result.get("reason_invalid_proposed_change_count", 0) + 1
            )
            continue
        try:
            db.cancel_amazon_order_header(update.header_row_number)
        except Exception:
            result["error_count"] += 1
            result["reason_write_error_count"] = (
                result.get("reason_write_error_count", 0) + 1
            )
            continue
        result["updated_order_status_count"] += 1

    return result
