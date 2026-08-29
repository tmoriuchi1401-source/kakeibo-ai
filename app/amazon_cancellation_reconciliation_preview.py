from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, TypeAlias

from .reconciliation import ImportTransaction, parse_import_rows


ReconciliationAction: TypeAlias = Literal[
    "no_action", "wait_payment", "wait_refund", "ready_to_close", "needs_review",
]
CancellationScope: TypeAlias = Literal["full_order", "item_or_partial", "unknown"]

_ACTIONS = (
    "no_action", "wait_payment", "wait_refund", "ready_to_close", "needs_review",
)
_PAYMENT_SOURCES = {"au PAY", "au PAYカード", "PayPay"}


@dataclass(frozen=True)
class CancellationReconciliationRow:
    order_id: str
    amazon_status: str
    cancellation_scope: CancellationScope
    cancellation_quantity: int | None
    order_amount: int | None
    payment_information: dict
    reconciliation_action: ReconciliationAction
    reason: str


def _cell(row: list, index: int) -> str:
    return str(row[index]).strip() if len(row) > index and row[index] is not None else ""


def _number(value: object) -> int | None:
    text = str(value).strip().replace(",", "").replace("¥", "") \
        if value is not None else ""
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return int(number) if number == number.to_integral_value() else None


def _positive_quantity(value: object) -> int | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _order_quantity(detail_rows: list[list], header: list | None) -> int | None:
    header_quantity = _positive_quantity(_cell(header or [], 4))
    if header_quantity is not None:
        return header_quantity
    if not detail_rows:
        return None
    quantities = [_positive_quantity(_cell(row, 5)) for row in detail_rows]
    return sum(quantities) if all(value is not None for value in quantities) else None


def _order_amount(detail_rows: list[list], header: list | None) -> int | None:
    header_amount = _number(_cell(header or [], 2))
    if header_amount is not None:
        return header_amount
    if not detail_rows:
        return None
    amounts = [_number(_cell(row, 6)) for row in detail_rows]
    return sum(amounts) if all(value is not None for value in amounts) else None


def _scope(event: list, order_quantity: int | None) -> CancellationScope:
    cancelled = _positive_quantity(_cell(event, 17))
    if cancelled is None or order_quantity is None or cancelled > order_quantity:
        return "unknown"
    return "full_order" if cancelled == order_quantity else "item_or_partial"


def _targets_order(transaction: ImportTransaction, order_id: str) -> bool:
    targets = {order_id, f"amazon:{order_id}"}
    return transaction.target_id in targets or transaction.target_id.startswith(f"{order_id}|")


def _payment_information(
    header: list | None,
    transactions: list[ImportTransaction],
    order_id: str,
) -> tuple[dict, bool, bool, bool]:
    linked = [tx for tx in transactions if _targets_order(tx, order_id)]
    payment_rows = [tx for tx in linked if tx.source in _PAYMENT_SOURCES]
    reconciliation_rows = [tx for tx in linked if tx.source not in _PAYMENT_SOURCES]
    positive_payments = [tx for tx in payment_rows if tx.amount > 0]
    payment_refunds = [tx for tx in payment_rows if tx.amount < 0]

    charged_amount = _number(_cell(header or [], 6))
    refund_status = _cell(header or [], 7) or "unknown"
    refund_amount = _number(_cell(header or [], 8))
    explicitly_no_charge = charged_amount == 0
    charge_found = bool(positive_payments) or (charged_amount is not None and charged_amount > 0)
    refund_found = bool(payment_refunds) or (
        refund_amount is not None and refund_amount > 0 and refund_status in {"partial", "full"}
    )
    info = {
        "header_charged_amount": charged_amount,
        "header_refund_status": refund_status,
        "header_refund_amount": refund_amount,
        "matched_payment_count": len(payment_rows),
        "matched_payment_amount": sum(tx.amount for tx in positive_payments),
        "matched_refund_count": len(payment_refunds),
        "matched_refund_amount": sum(abs(tx.amount) for tx in payment_refunds),
        "reconciliation_count": len(reconciliation_rows),
        "payment_statuses": sorted({tx.status for tx in payment_rows}),
        "reconciliation_statuses": sorted({tx.status for tx in reconciliation_rows}),
    }
    return info, charge_found, refund_found, explicitly_no_charge or bool(payment_rows)


def _decision(
    *, scope: CancellationScope, amazon_status: str, header_count: int,
    payment_info: dict, charge_found: bool, refund_found: bool,
    payment_observed: bool,
) -> tuple[ReconciliationAction, str]:
    if scope == "unknown":
        return "needs_review", "cancellation_scope_unknown"
    if header_count != 1:
        return "needs_review", "insufficient_payment_data"
    if any("needs_review" in status or status == "要確認" for status in (
        payment_info["payment_statuses"] + payment_info["reconciliation_statuses"]
    )):
        return "needs_review", "payment_match_ambiguous"
    non_installments = [
        status for status in payment_info["payment_statuses"]
        if status != "matched_amazon_installment"
    ]
    if payment_info["matched_payment_count"] > 1 and non_installments:
        return "needs_review", "payment_match_ambiguous"
    if amazon_status.lower() != "cancelled":
        return "no_action", "amazon_order_not_cancelled"
    if scope == "item_or_partial":
        if charge_found and not refund_found:
            return "wait_refund", "partial_cancel_refund_pending"
        return "needs_review", "partial_cancel_amount_unresolved"
    if charge_found:
        if refund_found:
            charged = max(
                payment_info["header_charged_amount"] or 0,
                payment_info["matched_payment_amount"],
            )
            refunded = max(
                payment_info["header_refund_amount"] or 0,
                payment_info["matched_refund_amount"],
            )
            if payment_info["header_refund_status"] == "full" or (
                charged > 0 and refunded >= charged
            ):
                return "ready_to_close", "full_order_cancelled_refund_matched"
            return "wait_refund", "full_order_cancelled_refund_pending"
        return "wait_refund", "full_order_cancelled_refund_pending"
    if payment_observed:
        return "ready_to_close", "full_order_cancelled_no_charge_found"
    return "wait_payment", "insufficient_payment_data"


def preview_amazon_cancellation_reconciliation(db) -> dict:
    """Return cancellation/payment reconciliation advice using Sheets reads only."""

    details_by_order: defaultdict[str, list[list]] = defaultdict(list)
    for raw in db.get("Amazon注文!A2:O"):
        row = list(raw)
        if _cell(row, 1):
            details_by_order[_cell(row, 1)].append(row)

    headers_by_order: defaultdict[str, list[list]] = defaultdict(list)
    for raw in db.get("Amazon注文ヘッダ!A2:O"):
        row = list(raw)
        if _cell(row, 0):
            headers_by_order[_cell(row, 0)].append(row)

    transactions = parse_import_rows(db.get("取込データ!A2:L"))
    rows = []
    for raw in db.get("Amazonイベント!A2:X"):
        event = list(raw)
        if _cell(event, 5) != "cancellation":
            continue
        order_id = _cell(event, 6)
        headers = headers_by_order.get(order_id, []) if order_id else []
        header = headers[0] if len(headers) == 1 else None
        quantity = _positive_quantity(_cell(event, 17))
        total_quantity = _order_quantity(details_by_order.get(order_id, []), header)
        scope = _scope(event, total_quantity) if order_id else "unknown"
        payment_info, charge_found, refund_found, payment_observed = \
            _payment_information(header, transactions, order_id) if order_id else (
                {
                    "header_charged_amount": None, "header_refund_status": "unknown",
                    "header_refund_amount": None, "matched_payment_count": 0,
                    "matched_payment_amount": 0, "matched_refund_count": 0,
                    "matched_refund_amount": 0, "reconciliation_count": 0,
                    "payment_statuses": [], "reconciliation_statuses": [],
                }, False, False, False,
            )
        action, reason = _decision(
            scope=scope, amazon_status=_cell(header or [], 5),
            header_count=len(headers), payment_info=payment_info,
            charge_found=charge_found, refund_found=refund_found,
            payment_observed=payment_observed,
        )
        rows.append(CancellationReconciliationRow(
            order_id=order_id,
            amazon_status=_cell(header or [], 5) or "unknown",
            cancellation_scope=scope,
            cancellation_quantity=quantity,
            order_amount=_order_amount(details_by_order.get(order_id, []), header),
            payment_information=payment_info,
            reconciliation_action=action,
            reason=reason,
        ))

    action_counts = Counter(row.reconciliation_action for row in rows)
    scope_counts = Counter(row.cancellation_scope for row in rows)
    return {
        "sampled_cancellation_count": len(rows),
        "action_counts": {action: action_counts[action] for action in _ACTIONS},
        "full_order_count": scope_counts["full_order"],
        "item_or_partial_count": scope_counts["item_or_partial"],
        "unknown_count": scope_counts["unknown"],
        "needs_review_count": action_counts["needs_review"],
        "rows": [asdict(row) for row in rows],
    }
