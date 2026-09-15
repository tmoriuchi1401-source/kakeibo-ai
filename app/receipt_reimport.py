"""Read-only comparison for fixed-source receipt reanalysis.

This deliberately does not call ReceiptPipeline.process_bytes: its import marker
is a normal ingestion dedupe guard, not evidence that every line is complete.
AI differences are review candidates, never correction authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from .models import ReceiptResult
from .utils import canonical_hash


@dataclass(frozen=True)
class ReceiptComparison:
    status: str
    reasons: tuple[str, ...]
    existing_items: int
    parsed_items: int
    missing_item_ids: tuple[str, ...]
    analysis_hash_matches: bool


def _row(row, size):
    return list(row) + [""] * max(0, size - len(row))


def _date(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if int(value) != value:
            return None
        try:
            return (date(1899, 12, 30) + timedelta(days=int(value))).isoformat()
        except (ValueError, OverflowError):
            return None
    try:
        return date.fromisoformat(str(value).replace("/", "-")).isoformat()
    except ValueError:
        return None


def _money(value):
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number == number.to_integral_value() else None
    except InvalidOperation:
        return None


def compare_receipt(
    source_id: str, parsed: ReceiptResult, *, receipt_rows, import_rows,
    expense_rows, review_rows, categories,
) -> ReceiptComparison:
    """Compare identity-linked rows; never merge by date/store/amount alone.

    Inputs are private snapshots without headers. All returned diagnostics are
    value-free except stable IDs, which must also stay in private evidence.
    Existing line IDs, hashes, review decisions and timestamps remain untouched.
    """
    rid, iid = f"R-{source_id}", f"receipt:{source_id}"
    receipts = [_row(r, 9) for r in receipt_rows if r and r[0] == rid]
    imports = [_row(r, 12) for r in import_rows if r and r[0] == iid]
    expenses = [_row(r, 13) for r in expense_rows
                if len(r) > 9 and (r[9] == rid or (len(r) > 10 and r[10] == iid))]
    reasons = []
    if len(receipts) != 1 or len(imports) != 1:
        reasons.append("receipt_or_import_identity_missing_or_duplicate")
    keys = [r[0] for r in expenses]
    if len(keys) != len(set(keys)):
        reasons.append("duplicate_expense_identity")
    if any(r[9] != rid or r[10] != iid or r[8] != "receipt" for r in expenses):
        reasons.append("expense_relationship_conflict")
    if any(r[12] != "active" for r in expenses):
        reasons.append("protected_expense_status")
    if any(r[0] in keys and (len(r) <= 10 or r[9] != rid or r[10] != iid)
           for r in expense_rows if r):
        reasons.append("expense_identity_collision")
    if any(len(r) > 9 and r[0] != iid and r[9] in set(keys + [rid, iid])
           for r in import_rows if r):
        reasons.append("linked_payment_requires_review")
    if any(r and r[0] == iid and any(_row(r, 15)[9:15]) for r in review_rows):
        reasons.append("manual_review_protected")

    valid_categories = set(map(tuple, categories))
    if (not _date(parsed.date) or not parsed.merchant.strip() or not parsed.items
            or parsed.total <= 0 or sum(i.amount for i in parsed.items) != parsed.total
            or any((i.major_category, i.minor_category) not in valid_categories for i in parsed.items)):
        reasons.append("parsed_fields_need_review")

    hash_matches = False
    if len(receipts) == 1 and len(imports) == 1:
        receipt, imported = receipts[0], imports[0]
        hash_matches = imported[10] == canonical_hash(parsed.model_dump())
        if imported[2] != "receipt" or imported[3] != source_id:
            reasons.append("import_relationship_conflict")
        if imported[8] != "解析済" or imported[9] or receipt[6] != "解析済":
            reasons.append("existing_decision_requires_review")
        if (_date(receipt[1]) != _date(parsed.date)
                or _date(imported[4]) != _date(parsed.date)
                or receipt[2] != parsed.merchant or imported[5] != parsed.merchant
                or _money(receipt[3]) != parsed.total or _money(imported[6]) != parsed.total
                or receipt[4] != parsed.payment_method or imported[7] != parsed.payment_method):
            reasons.append("header_difference")

    expected_ids = [f"{rid}-{index:02d}" for index in range(1, len(parsed.items) + 1)]
    missing = tuple(key for key in expected_ids if key not in keys)
    if missing:
        reasons.append("missing_detail_candidates")
    if set(keys) - set(expected_ids):
        reasons.append("existing_details_must_not_be_deleted")
    indexed = {r[0]: r for r in expenses}
    for key, item in zip(expected_ids, parsed.items):
        if key not in indexed:
            continue
        row = indexed[key]
        if (_date(row[1]) != _date(parsed.date) or row[2] != parsed.merchant
                or row[3] != item.name or _money(row[4]) != item.amount
                or row[5:7] != [item.major_category, item.minor_category]
                or row[7] != parsed.payment_method):
            reasons.append("detail_difference")
    if expenses and sum((_money(r[4]) or Decimal(0)) for r in expenses) != parsed.total:
        reasons.append("existing_detail_sum_difference")
    if any(_money(r[4]) is None for r in expenses):
        reasons.append("invalid_existing_amount")
    reasons = tuple(dict.fromkeys(reasons))
    return ReceiptComparison("needs_review" if reasons else "unchanged", reasons,
                             len(expenses), len(parsed.items), missing, hash_matches)
