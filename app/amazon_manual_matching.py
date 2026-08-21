from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import re
import unicodedata

from .aupay_card_pipeline import _amazon_extended_eligible, is_amazon
from .reconciliation import ImportTransaction


@dataclass(frozen=True)
class AmazonOrder:
    order_id: str
    order_date: str
    order_amount: int
    item_count: int
    short_item_summary: str
    major_categories: tuple[str, ...]
    payment_method: str
    source_kind: str
    order_fingerprint: str


@dataclass(frozen=True)
class AmazonManualCandidate:
    candidate_id: str
    card_import_id: str
    order_id: str
    card_date: str
    order_date: str
    card_amount: int
    order_amount: int
    amount_difference: int
    amount_difference_rate: float
    date_difference_days: int
    item_count: int
    short_item_summary: str
    major_categories: tuple[str, ...]
    payment_method: str
    source_kind: str
    order_fingerprint: str


@dataclass(frozen=True)
class CandidateSearchResult:
    candidates: tuple[AmazonManualCandidate, ...]
    total_candidate_count: int


@dataclass(frozen=True)
class ManualMatchRequest:
    card: ImportTransaction
    candidate: AmazonManualCandidate


@dataclass(frozen=True)
class ExistingManualUsage:
    order_ids: frozenset[str]
    unresolved_manual_rows: tuple[str, ...]


@dataclass(frozen=True)
class BatchValidationResult:
    valid: bool
    errors_by_card: dict[str, tuple[str, ...]]


def _text(value) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _money(value) -> int:
    try:
        return int(round(float(_text(value).replace(",", "").replace("¥", ""))))
    except (TypeError, ValueError):
        return 0


def _date(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _stable_hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _short_summary(names: list[str], item_count: int, limit: int = 24) -> str:
    first = next((name for name in names if name), "商品")
    shortened = first if len(first) <= limit else first[:limit] + "…"
    return shortened + (f" ほか{item_count - 1}件" if item_count > 1 else "")


def aggregate_amazon_orders(rows: list[list]) -> list[AmazonOrder]:
    grouped: dict[str, list[list]] = {}
    for raw in rows:
        row = list(raw) + [""] * max(0, 13 - len(raw))
        order_id = _text(row[1])
        if order_id:
            grouped.setdefault(order_id, []).append(row[:13])

    orders = []
    for order_id, items in grouped.items():
        dates = sorted({_text(row[3]) for row in items if _text(row[3])})
        names = [_text(row[4]) for row in items]
        categories = tuple(sorted({_text(row[8]) for row in items if _text(row[8])}))
        methods = sorted({_text(row[7]) for row in items if _text(row[7])})
        kinds = sorted({_text(row[10]) for row in items if _text(row[10])})
        item_count = len(items)
        fingerprint_data = {
            "order_id": order_id,
            "date": dates[0] if dates else "",
            "amount": sum(_money(row[6]) for row in items),
            "items": sorted(
                (_text(row[2]), _money(row[6]), _text(row[11])) for row in items
            ),
        }
        orders.append(AmazonOrder(
            order_id=order_id,
            order_date=dates[0] if dates else "",
            order_amount=fingerprint_data["amount"],
            item_count=item_count,
            short_item_summary=_short_summary(names, item_count),
            major_categories=categories,
            payment_method=" / ".join(methods),
            source_kind=kinds[0] if len(kinds) == 1 else "mixed" if kinds else "unknown",
            order_fingerprint=_stable_hash(fingerprint_data),
        ))
    return sorted(orders, key=lambda order: (order.order_date, order.order_id))


def candidate_id(card_import_id: str, order_id: str) -> str:
    digest = hashlib.sha256(
        f"amazon-manual-v1\0{card_import_id}\0{order_id}".encode("utf-8")
    ).hexdigest()[:32]
    return f"amcand:{digest}"


def is_manual_match_target(card: ImportTransaction) -> bool:
    payment_type = str(card.row[7]) if len(card.row) > 7 else ""
    return (
        card.source == "au PAYカード"
        and is_amazon(card.merchant)
        and card.status == "amazon_unmatched"
        and "手動照合=" not in card.note
        and _amazon_extended_eligible(card.merchant, payment_type, card.note)
    )


def _candidate(card: ImportTransaction, order: AmazonOrder) -> AmazonManualCandidate:
    card_date = _date(card.date)
    order_date = _date(order.order_date)
    days = (card_date - order_date).days if card_date and order_date else 999
    difference = order.order_amount - card.amount
    rate = abs(difference) / card.amount if card.amount else math.inf
    return AmazonManualCandidate(
        candidate_id=candidate_id(card.import_id, order.order_id),
        card_import_id=card.import_id,
        order_id=order.order_id,
        card_date=card.date,
        order_date=order.order_date,
        card_amount=card.amount,
        order_amount=order.order_amount,
        amount_difference=difference,
        amount_difference_rate=rate,
        date_difference_days=days,
        item_count=order.item_count,
        short_item_summary=order.short_item_summary,
        major_categories=order.major_categories,
        payment_method=order.payment_method,
        source_kind=order.source_kind,
        order_fingerprint=order.order_fingerprint,
    )


def find_amazon_candidates(
    card: ImportTransaction,
    orders: list[AmazonOrder],
    *,
    max_days: int = 30,
    limit: int | None = 3,
) -> CandidateSearchResult:
    if not is_manual_match_target(card):
        return CandidateSearchResult((), 0)
    found = []
    for order in orders:
        candidate = _candidate(card, order)
        if 0 <= candidate.date_difference_days <= max_days:
            found.append(candidate)
    found.sort(key=lambda item: (
        item.order_amount != item.card_amount,
        abs(item.amount_difference),
        item.amount_difference_rate,
        item.date_difference_days,
        item.payment_method != (str(card.row[7]) if len(card.row) > 7 else ""),
        item.item_count,
        item.order_id,
    ))
    selected = found if limit is None else found[:max(0, limit)]
    return CandidateSearchResult(tuple(selected), len(found))


_AMAZON_KEY_RE = re.compile(r"(?:^|;\s*)Amazonキー=(?:amazon:)?([^;|\s]+)")


def existing_manual_usage(transactions: list[ImportTransaction]) -> ExistingManualUsage:
    used: set[str] = set()
    unresolved = []
    for tx in transactions:
        if "手動照合=" not in tx.note:
            continue
        matches = _AMAZON_KEY_RE.findall(tx.note)
        if matches:
            used.update(matches)
            continue
        if tx.target_id.startswith("amazon:") and len(tx.target_id) > len("amazon:"):
            used.add(tx.target_id[len("amazon:"):])
        else:
            unresolved.append(tx.import_id)
    return ExistingManualUsage(frozenset(used), tuple(sorted(unresolved)))


def validate_manual_batch(
    requests: list[ManualMatchRequest],
    current_orders: list[AmazonOrder],
    existing_transactions: list[ImportTransaction],
) -> BatchValidationResult:
    order_by_id = {order.order_id: order for order in current_orders}
    usage = existing_manual_usage(existing_transactions)
    batch_counts: dict[str, int] = {}
    for request in requests:
        batch_counts[request.candidate.order_id] = batch_counts.get(request.candidate.order_id, 0) + 1

    errors_by_card: dict[str, tuple[str, ...]] = {}
    for request in requests:
        card, selected = request.card, request.candidate
        errors = []
        if not is_manual_match_target(card):
            errors.append("card transaction is not an unresolved eligible Amazon charge")
        if selected.card_import_id != card.import_id or selected.candidate_id != candidate_id(
            card.import_id, selected.order_id,
        ):
            errors.append("candidate does not belong to the card transaction")
        current = order_by_id.get(selected.order_id)
        if current is None:
            errors.append("Amazon order no longer exists")
        else:
            regenerated = _candidate(card, current)
            if not 0 <= regenerated.date_difference_days <= 30:
                errors.append("candidate is outside the current search window")
            if regenerated.order_fingerprint != selected.order_fingerprint:
                errors.append("Amazon order changed after candidate generation")
        if selected.order_id in usage.order_ids:
            errors.append("Amazon order is already used by another manual match")
        if usage.unresolved_manual_rows:
            errors.append("existing manual match has an unresolvable Amazon order reference")
        if batch_counts[selected.order_id] > 1:
            errors.append("Amazon order is selected more than once in this batch")
        if errors:
            errors_by_card[card.import_id] = tuple(dict.fromkeys(errors))
    return BatchValidationResult(not errors_by_card, errors_by_card)


def audit_information(candidate: AmazonManualCandidate) -> dict:
    return {
        "candidate_id": candidate.candidate_id,
        "amazon_order_id": candidate.order_id,
        "card_amount": candidate.card_amount,
        "amazon_order_amount": candidate.order_amount,
        "amount_difference": candidate.amount_difference,
        "amount_difference_rate": round(candidate.amount_difference_rate, 6),
        "date_difference_days": candidate.date_difference_days,
        "item_count": candidate.item_count,
        "payment_method": candidate.payment_method,
        "source_kind": candidate.source_kind,
    }
