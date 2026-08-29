from __future__ import annotations

import base64
from collections import Counter, defaultdict
from datetime import date, datetime
from email import policy
from email.message import Message
from email.parser import BytesParser
import re
import unicodedata

from .reconciliation import ImportTransaction, parse_import_rows
from .payment_coverage_status_preview import (
    SOURCES,
    build_payment_coverage_context,
)


_EVENT_TYPES = ("order", "cancellation", "shipment", "delivery", "payment", "return", "refund")
_NO_CHARGE_PATTERNS = (
    "この注文の請求は行われていません",
    "このご注文の請求は行われていません",
    "ご請求は行われていません",
    "請求されません",
    "you have not been charged",
    "you were not charged",
)


def _cell(row: list, index: int) -> str:
    return str(row[index]).strip() if len(row) > index and row[index] is not None else ""


def _integer(value: object) -> int | None:
    text = str(value).strip().replace(",", "").replace("¥", "") if value is not None else ""
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else None


def _date(value: str) -> date | None:
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        try:
            return date.fromisoformat(value.strip()[:10])
        except (TypeError, ValueError):
            return None


def _message_text(raw_mime: bytes) -> str:
    message = BytesParser(policy=policy.default).parsebytes(raw_mime)
    parts = [str(message.get("Subject", ""))]
    candidates = message.walk() if message.is_multipart() else (message,)
    for part in candidates:
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        try:
            text = str(part.get_content())
        except (LookupError, UnicodeError):
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/html":
            text = re.sub(r"<[^>]+>", " ", text)
        parts.append(text)
    return unicodedata.normalize("NFKC", "\n".join(parts)).lower()


def amazon_no_charge_assertion(raw_mime: bytes) -> bool:
    """Detect an explicit Amazon no-charge statement without projecting money."""

    text = _message_text(raw_mime)
    compact = re.sub(r"\s+", "", text)
    return any(pattern in text or re.sub(r"\s+", "", pattern) in compact
               for pattern in _NO_CHARGE_PATTERNS)


def _gmail_raw_message(service, message_id: str) -> bytes | None:
    if not service or not message_id:
        return None
    response = service.users().messages().get(
        userId="me", id=message_id, format="raw",
    ).execute()
    encoded = response.get("raw", "")
    if not encoded:
        return None
    return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))


def _targets_order(tx: ImportTransaction, order_id: str) -> bool:
    targets = {order_id, f"amazon:{order_id}"}
    note = unicodedata.normalize("NFKC", tx.note)
    return (
        tx.target_id in targets
        or tx.target_id.startswith(f"{order_id}|")
        or f"Amazonキー=amazon:{order_id}" in note
        or f"Amazonキー={order_id}" in note
    )


def _refund_like(tx: ImportTransaction) -> bool:
    text = " ".join((tx.status, tx.merchant, tx.note)).lower()
    return tx.amount < 0 or any(term in text for term in (
        "返金", "取消", "キャンセル", "refund", "reversal",
    ))


def _candidate_ids(
    transactions: list[ImportTransaction], order_id: str,
) -> tuple[set[str], set[str]]:
    linked = [tx for tx in transactions if _targets_order(tx, order_id)]
    refunds = [tx for tx in linked if _refund_like(tx)]
    charges = [tx for tx in linked if tx.amount > 0 and tx not in refunds]
    return (
        {f"import:{tx.import_id}" for tx in charges},
        {f"import:{tx.import_id}" for tx in refunds},
    )


def _expense_candidate_ids(rows: list[list], order_id: str) -> tuple[set[str], set[str]]:
    charge_ids: set[str] = set()
    refund_ids: set[str] = set()
    for raw in rows:
        row = list(raw)
        text = unicodedata.normalize("NFKC", " ".join(str(value) for value in row))
        if order_id not in text and f"amazon:{order_id}" not in text:
            continue
        amount = _integer(_cell(row, 4)) or 0
        identity = f"import:{_cell(row, 10)}" if _cell(row, 10) else (
            f"expense:{_cell(row, 0)}" if _cell(row, 0)
            else f"row:{len(charge_ids) + len(refund_ids)}"
        )
        if amount < 0 or any(term in text.lower() for term in ("返金", "取消", "refund")):
            refund_ids.add(identity)
        elif amount > 0:
            charge_ids.add(identity)
    return charge_ids, refund_ids


def _timeline(events: list[list]) -> dict:
    result = {}
    for event_type in _EVENT_TYPES:
        matching = [event for event in events if _cell(event, 5) == event_type]
        dates = sorted({_cell(event, 7) for event in matching if _cell(event, 7)})
        result[event_type] = {"present": bool(matching), "dates": dates}
    # Payment and charge are the same stored event concept today.
    result["charge"] = dict(result["payment"])
    return result


def _absence(timeline: dict, event_type: str, amazon_coverage: str) -> bool | None:
    if timeline[event_type]["present"]:
        return False
    return True if amazon_coverage == "complete" else None


def preview_amazon_payment_coverage(db, gmail_service=None, *, as_of: date | None = None) -> dict:
    """Diagnose cancellation/payment evidence using read-only Sheets and Gmail calls."""

    as_of = as_of or date.today()
    header_rows = [list(raw) for raw in db.get("Amazon注文ヘッダ!A2:O")]
    headers_by_order: defaultdict[str, list[list]] = defaultdict(list)
    for row in header_rows:
        if _cell(row, 0):
            headers_by_order[_cell(row, 0)].append(row)

    details_by_order: defaultdict[str, list[list]] = defaultdict(list)
    for raw in db.get("Amazon注文!A2:O"):
        row = list(raw)
        if _cell(row, 1):
            details_by_order[_cell(row, 1)].append(row)

    events_by_order: defaultdict[str, list[list]] = defaultdict(list)
    event_rows = [list(raw) for raw in db.get("Amazonイベント!A2:X")]
    for row in event_rows:
        if _cell(row, 6):
            events_by_order[_cell(row, 6)].append(row)

    import_rows = [list(raw) for raw in db.get("取込データ!A2:L")]
    transactions = parse_import_rows(import_rows)
    expense_rows = db.get("支出明細!A2:M")
    coverage_context = build_payment_coverage_context(
        import_rows, event_rows, header_rows, as_of=as_of,
    )
    coverage_by_order = {
        row["order_id"]: row for row in coverage_context["orders"]
    }
    rows = []
    for order_id in sorted(events_by_order):
        events = events_by_order[order_id]
        cancellations = [event for event in events if _cell(event, 5) == "cancellation"]
        if not cancellations:
            continue
        headers = headers_by_order.get(order_id, [])
        header = headers[0] if len(headers) == 1 else None
        timeline = _timeline(events)
        order_date = _cell(header or [], 1) or next(iter(timeline["order"]["dates"]), "")
        cancellation_date = next(iter(timeline["cancellation"]["dates"]), "")

        no_charge = False
        assertion_read_error = False
        for event in cancellations:
            try:
                raw_mime = _gmail_raw_message(gmail_service, _cell(event, 1))
                no_charge = no_charge or bool(raw_mime and amazon_no_charge_assertion(raw_mime))
            except Exception:
                assertion_read_error = True

        order_coverage = coverage_by_order.get(order_id, {})
        coverage_by_source = order_coverage.get("source_coverage", {
            source: {
                "coverage_status": "unknown",
                "covers_required_window": None,
                "completeness_reason": "missing_order_coverage_evaluation",
            }
            for source in SOURCES
        })
        coverage_status = order_coverage.get(
            "overall_payment_coverage_status", "unknown",
        )
        tx_charges, tx_refunds = _candidate_ids(transactions, order_id)
        expense_charges, expense_refunds = _expense_candidate_ids(expense_rows, order_id)
        charge_count = len(tx_charges | expense_charges)
        refund_count = len(tx_refunds | expense_refunds)
        ambiguous_count = (
            (charge_count if charge_count > 1 else 0)
            + (refund_count if refund_count > 1 else 0)
        )

        if ambiguous_count:
            state = "ambiguous"
        elif refund_count:
            state = "refund_candidate_found"
        elif charge_count:
            state = "charge_candidate_found"
        elif no_charge:
            state = "amazon_declared_not_charged"
        elif coverage_status == "unknown":
            state = "payment_coverage_unknown"
        else:
            state = "insufficient_evidence"
        reason = {
            "incomplete": "payment_coverage_incomplete",
            "unknown": "payment_coverage_unknown",
            "complete": "payment_review_required",
        }[coverage_status]

        basis_date = _date(cancellation_date) or _date(order_date)
        elapsed_days = (as_of - basis_date).days if basis_date else None
        header_quantity = _integer(_cell(header or [], 4))
        cancellation_quantities = [_integer(_cell(event, 17)) for event in cancellations]
        full_order = (
            len(headers) == 1 and _cell(header or [], 5).lower() == "cancelled"
            and header_quantity is not None
            and len(cancellations) == 1
            and cancellation_quantities == [header_quantity]
        )
        diagnostics = {
            "full_order_cancelled": full_order,
            "unique_cancellation_match": len(headers) == 1 and len(cancellations) == 1,
            "shipment_absent": _absence(
                timeline, "shipment", coverage_by_source["amazon_gmail"]["coverage_status"],
            ),
            "delivery_absent": _absence(
                timeline, "delivery", coverage_by_source["amazon_gmail"]["coverage_status"],
            ),
            "return_absent": _absence(
                timeline, "return", coverage_by_source["amazon_gmail"]["coverage_status"],
            ),
            "refund_absent": _absence(
                timeline, "refund", coverage_by_source["amazon_gmail"]["coverage_status"],
            ),
            "amazon_no_charge_assertion": no_charge,
            "payment_coverage_complete": coverage_status == "complete",
            "matching_charge_absent": False if charge_count else None,
            "ambiguity_absent": ambiguous_count == 0,
        }
        rows.append({
            "order_id": order_id,
            "order_date": order_date or None,
            "cancellation_date": cancellation_date or None,
            "required_window": order_coverage.get("required_window"),
            "event_timeline": timeline,
            "amazon_no_charge_assertion": no_charge,
            "amazon_no_charge_assertion_source": "cancellation_email" if no_charge else None,
            "amazon_no_charge_assertion_read_error": assertion_read_error,
            "payment_coverage_status": coverage_status,
            "payment_coverage_by_source": coverage_by_source,
            "matching_charge_candidate_count": charge_count,
            "matching_refund_candidate_count": refund_count,
            "ambiguous_candidate_count": ambiguous_count,
            "elapsed_days": elapsed_days,
            "candidate_state": state,
            "action": "wait_payment",
            "reason": reason,
            "close_condition_diagnostics": diagnostics,
        })

    counts = Counter(row["candidate_state"] for row in rows)
    actions = Counter(row["action"] for row in rows)
    return {
        "sampled_order_count": len(rows),
        "amazon_no_charge_assertion_count": sum(row["amazon_no_charge_assertion"] for row in rows),
        "payment_coverage_incomplete_count": sum(
            row["payment_coverage_status"] == "incomplete" for row in rows
        ),
        "payment_coverage_unknown_count": sum(
            row["payment_coverage_status"] == "unknown" for row in rows
        ),
        "payment_coverage_complete_count": sum(
            row["payment_coverage_status"] == "complete" for row in rows
        ),
        "charge_candidate_found_count": counts["charge_candidate_found"],
        "refund_candidate_found_count": counts["refund_candidate_found"],
        "ambiguous_count": counts["ambiguous"],
        "action_counts": {"wait_payment": actions["wait_payment"]},
        "rows": rows,
    }
