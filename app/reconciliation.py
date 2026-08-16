from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .sheets import SheetsDB
from .utils import normalize_store


@dataclass(frozen=True)
class ImportTransaction:
    row_num: int
    import_id: str
    source: str
    date: str
    merchant: str
    amount: int
    status: str
    target_id: str
    note: str
    row: list


@dataclass(frozen=True)
class ReconcileDecision:
    transaction: ImportTransaction
    status: str
    target_id: str
    candidate_ids: tuple[str, ...]
    reason: str


def _money(value) -> int:
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def parse_import_rows(rows: list[list]) -> list[ImportTransaction]:
    transactions = []
    for row_num, raw in enumerate(rows, start=2):
        row = list(raw) + [""] * max(0, 12 - len(raw))
        if not row[0]:
            continue
        transactions.append(ImportTransaction(
            row_num=row_num,
            import_id=str(row[0]),
            source=str(row[2]),
            date=str(row[4]),
            merchant=str(row[5]),
            amount=_money(row[6]),
            status=str(row[8]),
            target_id=str(row[9]),
            note=str(row[11]),
            row=row[:12],
        ))
    return transactions


def _days(left: str, right: str) -> int:
    try:
        a = datetime.strptime(left, "%Y-%m-%d")
        b = datetime.strptime(right, "%Y-%m-%d")
        return abs((a - b).days)
    except ValueError:
        return 999


def merchants_match(left: str, right: str) -> bool:
    a, b = normalize_store(left), normalize_store(right)
    if not a or not b:
        return False
    if a == b:
        return True
    # Conservative allowance for branch/company suffix differences.
    return min(len(a), len(b)) >= 4 and (a in b or b in a)


def reconcile_transactions(transactions: list[ImportTransaction]) -> list[ReconcileDecision]:
    receipts = [
        tx for tx in transactions
        if tx.source == "receipt" and tx.status in {"解析済", "canonical_receipt"}
        and tx.amount > 0 and tx.date
    ]
    secondaries = [
        tx for tx in transactions
        if (
            tx.source == "au PAY" and tx.status == "unclassified_aupay"
        ) or (
            tx.source == "au PAYカード" and tx.status == "unclassified_card"
        )
    ]

    candidate_map: dict[str, list[ImportTransaction]] = {}
    for tx in secondaries:
        tolerance = 1 if tx.source == "au PAY" else 3
        candidate_map[tx.import_id] = [
            receipt for receipt in receipts
            if receipt.amount == tx.amount
            and _days(receipt.date, tx.date) <= tolerance
            and merchants_match(receipt.merchant, tx.merchant)
        ]

    # A receipt claimed by multiple payment records is never merged automatically.
    reverse: dict[str, list[str]] = {}
    for tx_id, candidates in candidate_map.items():
        for candidate in candidates:
            reverse.setdefault(candidate.import_id, []).append(tx_id)

    decisions = []
    for tx in secondaries:
        candidates = candidate_map[tx.import_id]
        ids = tuple(candidate.import_id for candidate in candidates)
        if len(candidates) == 1 and len(reverse[candidates[0].import_id]) == 1:
            decisions.append(ReconcileDecision(
                tx, "matched_receipt", candidates[0].import_id, ids,
                "金額・日付・店舗が一意に一致",
            ))
        elif candidates:
            decisions.append(ReconcileDecision(
                tx, "needs_review_duplicate", "", ids,
                "一致候補が複数、または同じレシートを複数取引が参照",
            ))
    return decisions


class ReconciliationPipeline:
    def __init__(self, db: SheetsDB):
        self.db = db

    def preview(self) -> dict:
        transactions = parse_import_rows(self.db.get("取込データ!A2:L"))
        decisions = reconcile_transactions(transactions)
        return self._summary(transactions, decisions)

    def apply(self) -> dict:
        transactions = parse_import_rows(self.db.get("取込データ!A2:L"))
        decisions = reconcile_transactions(transactions)
        updates = []
        for decision in decisions:
            row = list(decision.transaction.row)
            row[8] = decision.status
            row[9] = decision.target_id
            annotation = f"照合={decision.reason}"
            if decision.candidate_ids:
                annotation += "; 候補=" + ",".join(decision.candidate_ids)
            row[11] = "; ".join(x for x in (decision.transaction.note, annotation) if x)
            updates.append((decision.transaction.row_num, row))
        self.db.update_rows("取込データ", updates)
        result = self._summary(transactions, decisions)
        result["updated"] = len(updates)
        return result

    @staticmethod
    def _summary(transactions, decisions) -> dict:
        return {
            "transactions": len(transactions),
            "matched_receipt": sum(x.status == "matched_receipt" for x in decisions),
            "needs_review": sum(x.status == "needs_review_duplicate" for x in decisions),
            "unchanged": len(transactions) - len(decisions),
        }
