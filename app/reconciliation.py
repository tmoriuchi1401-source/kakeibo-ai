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
    imported_at: str = ""


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
            imported_at=str(row[1]),
        ))
    return transactions


def _days(left: str, right: str) -> int:
    try:
        a = datetime.strptime(left, "%Y-%m-%d")
        b = datetime.strptime(right, "%Y-%m-%d")
        return abs((a - b).days)
    except ValueError:
        return 999


def _date(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return None


def _months_ago(value: datetime, months: int) -> datetime:
    year = value.year
    month = value.month - months
    while month <= 0:
        year -= 1
        month += 12
    days = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return value.replace(year=year, month=month, day=min(value.day, days[month - 1]))


def reconciliation_scope(transactions: list[ImportTransaction], *, months: int = 6,
                         as_of: datetime | None = None,
                         store_aliases: dict[str, str] | None = None) -> list[ImportTransaction]:
    """Keep recent/unresolved rows plus historical rows that can match them."""
    as_of = as_of or datetime.now()
    cutoff = _months_ago(as_of, months)
    aliases = store_aliases or {}
    def unresolved(status: str) -> bool:
        return (
            status == "要確認"
            or status.startswith("needs_review")
            or status.endswith("_needs_review")
            or status in {
                "unclassified_aupay", "unclassified_card", "unclassified_paypay",
                "amazon_unmatched",
            }
        )
    selected = {
        tx.import_id for tx in transactions
        if unresolved(tx.status)
        or any(parsed is not None and parsed >= cutoff for parsed in
               (_date(tx.date), _date(tx.imported_at)))
    }
    active = [tx for tx in transactions if tx.import_id in selected]
    amounts = {tx.amount for tx in active if tx.amount > 0}
    active_by_amount: dict[int, list[ImportTransaction]] = {}
    for tx in active:
        active_by_amount.setdefault(tx.amount, []).append(tx)
    # A related historical counterpart remains eligible even when its own date
    # and import timestamp are outside the lookback window.
    for tx in transactions:
        if tx.import_id in selected or tx.amount not in amounts:
            continue
        for subject in active_by_amount.get(tx.amount, []):
            is_receipt_payment = (
                tx.source == "receipt"
                and subject.status in {
                    "unclassified_aupay", "unclassified_card", "unclassified_paypay",
                }
                and _days(tx.date, subject.date) <= (
                    1 if subject.source in {"au PAY", "PayPay"} else 3
                )
            )
            is_amazon_pair = (
                {tx.source, subject.source} == {"receipt", "Amazon"}
                and _days(tx.date, subject.date) <= 7
            )
            if (is_receipt_payment or is_amazon_pair) and merchants_match(
                    tx.merchant, subject.merchant, aliases):
                selected.add(tx.import_id)
                break
    return [tx for tx in transactions if tx.import_id in selected]


def parse_store_aliases(rows: list[list]) -> dict[str, str]:
    aliases = {}
    for raw in rows:
        row = list(raw) + [""] * max(0, 3 - len(raw))
        name = normalize_store(str(row[1]))
        canonical = normalize_store(str(row[2]))
        if name and canonical:
            aliases[name] = canonical
    return aliases


def _canonical_store(value: str, aliases: dict[str, str]) -> str:
    store = normalize_store(value)
    seen = set()
    while store in aliases and store not in seen:
        seen.add(store)
        store = aliases[store]
    return store


def merchants_match(left: str, right: str, aliases: dict[str, str] | None = None) -> bool:
    aliases = aliases or {}
    a, b = _canonical_store(left, aliases), _canonical_store(right, aliases)
    if not a or not b:
        return False
    if a == b:
        return True
    # Conservative allowance for branch/company suffix differences.
    return min(len(a), len(b)) >= 4 and (a in b or b in a)


def reconcile_transactions(
    transactions: list[ImportTransaction],
    store_aliases: dict[str, str] | None = None,
) -> list[ReconcileDecision]:
    store_aliases = store_aliases or {}
    receipts = [
        tx for tx in transactions
        if tx.source == "receipt" and tx.status in {"解析済", "canonical_receipt"}
        and tx.amount > 0 and tx.date
    ]
    receipts_by_amount: dict[int, list[ImportTransaction]] = {}
    for receipt in receipts:
        receipts_by_amount.setdefault(receipt.amount, []).append(receipt)
    secondaries = [
        tx for tx in transactions
        if (
            tx.source == "au PAY" and tx.status == "unclassified_aupay"
        ) or (
            tx.source == "au PAYカード" and tx.status == "unclassified_card"
        ) or (
            tx.source == "PayPay" and tx.status == "unclassified_paypay"
        ) or (
            tx.source in {"au PAY", "au PAYカード", "PayPay"}
            and tx.status == "auto_expense"
        )
    ]

    candidate_map: dict[str, list[ImportTransaction]] = {}
    for tx in secondaries:
        tolerance = 1 if tx.source in {"au PAY", "PayPay"} else 3
        candidate_map[tx.import_id] = [
            receipt for receipt in receipts_by_amount.get(tx.amount, [])
            if _days(receipt.date, tx.date) <= tolerance
            and merchants_match(receipt.merchant, tx.merchant, store_aliases)
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

    amazon_orders = [
        tx for tx in transactions
        if tx.source == "Amazon" and tx.status == "canonical_amazon"
        and tx.amount > 0 and tx.date
    ]
    amazon_by_amount: dict[int, list[ImportTransaction]] = {}
    for order in amazon_orders:
        amazon_by_amount.setdefault(order.amount, []).append(order)
    amazon_candidates: dict[str, list[ImportTransaction]] = {}
    for receipt in receipts:
        # Amazon is canonical only when the receipt itself also identifies Amazon.
        amazon_candidates[receipt.import_id] = [
            order for order in amazon_by_amount.get(receipt.amount, [])
            if _days(order.date, receipt.date) <= 7
            and merchants_match(order.merchant, receipt.merchant, store_aliases)
        ]
    amazon_reverse: dict[str, list[str]] = {}
    for receipt_id, candidates in amazon_candidates.items():
        for candidate in candidates:
            amazon_reverse.setdefault(candidate.import_id, []).append(receipt_id)
    for receipt in receipts:
        candidates = amazon_candidates[receipt.import_id]
        ids = tuple(candidate.import_id for candidate in candidates)
        if len(candidates) == 1 and len(amazon_reverse[candidates[0].import_id]) == 1:
            decisions.append(ReconcileDecision(
                receipt, "matched_amazon", candidates[0].import_id, ids,
                "Amazon注文合計・日付・店舗が一意に一致",
            ))
        elif candidates:
            decisions.append(ReconcileDecision(
                receipt, "needs_review_duplicate", "", ids,
                "Amazon注文の一致候補が複数、または一注文を複数レシートが参照",
            ))
    return decisions


class ReconciliationPipeline:
    def __init__(self, db: SheetsDB, lookback_months: int = 6):
        self.db = db
        self.lookback_months = lookback_months

    def preview(self) -> dict:
        transactions = parse_import_rows(self.db.get("取込データ!A2:L"))
        aliases = parse_store_aliases(self.db.get("店舗!A2:C"))
        scoped = reconciliation_scope(
            transactions, months=self.lookback_months, store_aliases=aliases,
        )
        decisions = reconcile_transactions(scoped, aliases)
        result = self._summary(transactions, decisions)
        result["scoped_transactions"] = len(scoped)
        return result

    def apply(self) -> dict:
        self.db.ensure_expense_status_column()
        transactions = parse_import_rows(self.db.get("取込データ!A2:L"))
        aliases = parse_store_aliases(self.db.get("店舗!A2:C"))
        scoped = reconciliation_scope(
            transactions, months=self.lookback_months, store_aliases=aliases,
        )
        decisions = reconcile_transactions(scoped, aliases)
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
        excluded_expenses=[]
        for decision in decisions:
            should_exclude = (
                decision.status == "matched_amazon"
                or (
                    decision.status == "matched_receipt"
                    and decision.transaction.status == "auto_expense"
                )
            )
            if not should_exclude:
                continue
            for row_num,raw in self.db.expense_rows_for_import(decision.transaction.import_id):
                expense=list(raw)+[""]*max(0,13-len(raw))
                expense=expense[:13]
                expense[12]="duplicate_excluded"
                excluded_expenses.append((row_num,expense))
        if excluded_expenses:
            self.db.ensure_expense_status_column()
            self.db.update_rows("支出明細",excluded_expenses)
        result = self._summary(transactions, decisions)
        result["scoped_transactions"] = len(scoped)
        result["updated"] = len(updates)
        result["expenses_excluded"] = len(excluded_expenses)
        return result

    @staticmethod
    def _summary(transactions, decisions) -> dict:
        return {
            "transactions": len(transactions),
            "matched_receipt": sum(x.status == "matched_receipt" for x in decisions),
            "matched_amazon": sum(x.status == "matched_amazon" for x in decisions),
            "needs_review": sum(x.status == "needs_review_duplicate" for x in decisions),
            "unchanged": len(transactions) - len(decisions),
        }
