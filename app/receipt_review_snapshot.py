"""Immutable, identity-bound item evidence for receipts held before posting.

Only a normal receipt that passed the privacy gate reaches this writer.  The
import row is still the commit marker: candidate rows are saved first and a
retry must see the identical candidate rather than silently replace it.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation


SHEET = "解析候補明細"
HEADERS = ["候補ID", "取込ID", "解析ハッシュ", "行番号", "商品名", "数量", "金額",
           "大カテゴリ", "小カテゴリ", "備考", "レシート合計"]


def yen(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    clean = str(value).strip().replace(",", "").replace("¥", "").replace("円", "")
    try:
        amount = Decimal(clean)
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount != amount.to_integral_value():
        return None
    return int(amount)


def candidate_rows(import_id, analysis_hash, result):
    return [[f"{import_id}:{index:03d}", import_id, analysis_hash, index,
             item.name, item.quantity, item.amount, item.major_category,
             item.minor_category, item.note, result.total]
            for index, item in enumerate(result.items, 1)]


def _same_row(old, new):
    old = old + [""] * max(0, len(new) - len(old))
    if len(old) != len(new):
        return False
    for index, (actual, expected) in enumerate(zip(old, new)):
        if index in {3, 6, 10}:
            if yen(actual) != yen(expected):
                return False
        elif index == 5:
            try:
                if Decimal(str(actual)) != Decimal(str(expected)):
                    return False
            except InvalidOperation:
                return False
        elif str(actual) != str(expected):
            return False
    return True


def save_candidate(db, import_id, analysis_hash, result):
    """Save the exact rejected candidate once; refuse a changed replay."""
    expected = candidate_rows(import_id, analysis_hash, result)
    db.ensure_sheet(SHEET, HEADERS)
    existing = [list(row) for row in db.get(f"'{SHEET}'!A2:K")
                if len(row) > 1 and row[1] == import_id]
    if existing:
        if len(existing) != len(expected) or any(
            not _same_row(old, new) for old, new in zip(existing, expected)
        ):
            raise ValueError("receipt_candidate_replay_conflict")
        return
    db.append_raw(SHEET, expected)


def verified_candidate_range(tx, rows):
    """Return its own contiguous candidate range and sum, or fail closed."""
    found = [(number, list(row) + [""] * max(0, len(HEADERS) - len(row)))
             for number, row in rows if len(row) > 1 and row[1] == tx.import_id]
    if not found or tx.source != "receipt" or tx.import_id != "receipt:" + str(tx.row[3]):
        return None
    numbers = [number for number, _ in found]
    if numbers != list(range(numbers[0], numbers[0] + len(found))):
        return None
    amounts = []
    for index, (_, row) in enumerate(found, 1):
        amount = yen(row[6])
        if (row[0] != f"{tx.import_id}:{index:03d}" or row[2] != tx.row[10]
                or str(row[3]) != str(index) or not str(row[4]).strip()
                or yen(row[10]) != tx.amount or amount is None):
            return None
        amounts.append(amount)
    return numbers[0], numbers[-1], sum(amounts)

