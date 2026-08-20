from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re

from .amazon_pipeline import AmazonPipeline
from .reconciliation import ImportTransaction, parse_import_rows
from .sheets import SheetsDB
from .utils import canonical_hash, normalize_store, now_jst_string


FALLBACK_CATEGORY = ("その他", "未分類")
INSTALLMENT_STATUS = "needs_review_amazon_installment"
MATCHED_STATUS = "matched_amazon_installment"
FIRST_PAYMENT_DAYS = 45
COMPLETION_DAYS = 180


@dataclass(frozen=True)
class BaselineItem:
    key: str
    order_id: str
    asin: str
    date: str
    name: str
    quantity: float
    amount: int
    payment_method: str
    major_category: str
    minor_category: str


@dataclass(frozen=True)
class BaselineOrder:
    order_id: str
    date: str
    amount: int
    payment_method: str
    items: tuple[BaselineItem, ...]


@dataclass(frozen=True)
class InstallmentMatch:
    order: BaselineOrder
    installments: tuple[ImportTransaction, ...]


def _money(value) -> int:
    try:
        return int(round(float(str(value).replace(",", ""))))
    except (TypeError, ValueError):
        return 0


def _days_after(date: str, start: str) -> int:
    try:
        return (datetime.strptime(date, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days
    except ValueError:
        return -999


def parse_baseline_orders(rows: list[list]) -> list[BaselineOrder]:
    grouped: dict[str, list[BaselineItem]] = {}
    for raw in rows:
        row = list(raw) + [""] * max(0, 13 - len(raw))
        if not row[0] or str(row[10]).strip() != "baseline":
            continue
        item = BaselineItem(
            key=str(row[0]), order_id=str(row[1]), asin=str(row[2]), date=str(row[3]),
            name=str(row[4]), quantity=float(row[5] or 0), amount=_money(row[6]),
            payment_method=str(row[7]), major_category=str(row[8]), minor_category=str(row[9]),
        )
        if item.order_id and item.date and item.amount > 0:
            grouped.setdefault(item.order_id, []).append(item)
    return [
        BaselineOrder(
            order_id=order_id, date=items[0].date,
            amount=sum(item.amount for item in items), payment_method=items[0].payment_method,
            items=tuple(items),
        )
        for order_id, items in grouped.items()
        # A card row can reference only one target expense ID. Multi-item orders
        # therefore stay manual until an order-level linkage representation exists.
        if len(items) == 1 and len({item.date for item in items}) == 1
    ]


def _member(tx: ImportTransaction) -> str:
    match = re.search(r"(?:^|;\s*)会員=([^;]+)", tx.note)
    return match.group(1).strip() if match else ""


def _has_refund_text(tx: ImportTransaction) -> bool:
    text = " ".join((tx.merchant, str(tx.row[7]), tx.note)).upper()
    return tx.amount <= 0 or any(x in text for x in (
        "返金", "取消", "取り消し", "キャンセル", "払戻", "返品", "REFUND",
    ))


def _installment_buckets(transactions: list[ImportTransaction]) -> list[list[ImportTransaction]]:
    buckets: dict[tuple[str, str], list[ImportTransaction]] = {}
    for tx in transactions:
        if (
            tx.source != "au PAYカード" or tx.status != INSTALLMENT_STATUS or tx.target_id
            or _has_refund_text(tx)
        ):
            continue
        key = (normalize_store(tx.merchant), _member(tx))
        buckets.setdefault(key, []).append(tx)
    return [sorted(rows, key=lambda tx: (tx.date, tx.import_id)) for rows in buckets.values()]


def find_installment_matches(
    transactions: list[ImportTransaction], orders: list[BaselineOrder],
) -> tuple[list[InstallmentMatch], int, int]:
    buckets = _installment_buckets(transactions)
    candidates: list[InstallmentMatch] = []
    for rows in buckets:
        # Never match only a convenient subset of an unresolved bucket. If two
        # installment plans overlap for the same card member, keep both manual.
        if len(rows) < 2:
            continue
        total = sum(tx.amount for tx in rows)
        for order in orders:
            first_days = _days_after(rows[0].date, order.date)
            last_days = _days_after(rows[-1].date, order.date)
            if (
                total == order.amount
                and 0 <= first_days <= FIRST_PAYMENT_DAYS
                and first_days <= last_days <= COMPLETION_DAYS
            ):
                candidates.append(InstallmentMatch(order, tuple(rows)))

    by_order: dict[str, list[InstallmentMatch]] = {}
    by_group: dict[tuple[str, ...], list[InstallmentMatch]] = {}
    for candidate in candidates:
        group_key = tuple(tx.import_id for tx in candidate.installments)
        by_order.setdefault(candidate.order.order_id, []).append(candidate)
        by_group.setdefault(group_key, []).append(candidate)

    matched = []
    used_rows: set[str] = set()
    for candidate in candidates:
        group_key = tuple(tx.import_id for tx in candidate.installments)
        ids = set(group_key)
        if (
            len(by_order[candidate.order.order_id]) == 1
            and len(by_group[group_key]) == 1
            and not ids.intersection(used_rows)
        ):
            matched.append(candidate)
            used_rows.update(ids)
    unmatched_buckets = sum(any(tx.import_id not in used_rows for tx in bucket) for bucket in buckets)
    return matched, len(candidates), unmatched_buckets


class AmazonInstallmentPipeline:
    def __init__(self, db: SheetsDB, ai=None):
        self.db = db
        self.ai = ai

    def _state(self):
        transactions = parse_import_rows(self.db.get("取込データ!A2:L"))
        orders = parse_baseline_orders(self.db.get("Amazon注文!A2:M"))
        matched, candidate_count, unmatched_count = find_installment_matches(transactions, orders)
        expense_ids = set(self.db.expense_index())
        expenses_to_create = sum(
            AmazonPipeline._expense_id(item.key) not in expense_ids
            for match in matched for item in match.order.items
        )
        return transactions, matched, candidate_count, unmatched_count, expenses_to_create

    def preview(self) -> dict:
        transactions, matched, candidate_count, unmatched_count, expenses_to_create = self._state()
        return {
            "installment_rows": sum(tx.status == INSTALLMENT_STATUS for tx in transactions),
            "candidate_groups": candidate_count,
            "matched_groups": len(matched),
            "unmatched_groups": unmatched_count,
            "amazon_orders_to_materialize": len(matched),
            "amazon_expenses_to_create": expenses_to_create,
        }

    def apply(self) -> dict:
        transactions, matched, candidate_count, unmatched_count, expenses_to_create = self._state()
        if not matched:
            result = self.preview()
            result.update({"expenses_created": 0, "installments_updated": 0, "order_imports_created": 0})
            return result

        categories = set(self.db.categories())
        if FALLBACK_CATEGORY not in categories:
            raise RuntimeError("カテゴリマスタに「その他 / 未分類」がありません")
        master = self.db.product_master()
        materialized_items = [item for match in matched for item in match.order.items]
        missing = {item.asin: item for item in materialized_items if item.asin not in master}
        classified = {}
        if missing and self.ai is not None:
            try:
                answer = self.ai.classify_products(
                    [{"asin": asin, "name": item.name} for asin, item in missing.items()],
                    list(categories),
                )
                for product in answer.products:
                    pair = (product.major_category, product.minor_category)
                    classified[product.asin] = (
                        pair if pair in categories else FALLBACK_CATEGORY,
                        product.note,
                    )
            except Exception:
                classified = {}

        product_rows = []
        for asin, item in missing.items():
            pair, note = classified.get(asin, (FALLBACK_CATEGORY, "Amazon分割照合時の分類フォールバック"))
            master[asin] = (pair[0], pair[1], item.name)
            product_rows.append([asin, item.name, pair[0], pair[1], note, now_jst_string()])

        expense_index = self.db.expense_index()
        expense_new = []
        expense_updates = []
        for item in materialized_items:
            spend_id = AmazonPipeline._expense_id(item.key)
            master_category = master.get(item.asin)
            stored_pair = (item.major_category, item.minor_category)
            pair = stored_pair if stored_pair in categories else (
                (master_category[0], master_category[1]) if master_category else FALLBACK_CATEGORY
            )
            expense = [
                spend_id, item.date, "Amazon.co.jp", item.name, item.amount,
                pair[0], pair[1], item.payment_method, "Amazon", "",
                f"amazon:{item.order_id}", f"Amazonキー={item.key}", "active",
            ]
            if spend_id in expense_index:
                expense_updates.append((expense_index[spend_id], expense))
            else:
                expense_new.append(expense)

        import_index = self.db.import_index()
        import_new = []
        import_updates = []
        installment_updates = []
        transactions_by_id = {tx.import_id: tx for tx in transactions}
        for match in matched:
            order = match.order
            import_id = f"amazon:{order.order_id}"
            raw = {
                "order_id": order.order_id, "date": order.date, "amount": order.amount,
                "keys": sorted(item.key for item in order.items),
            }
            canonical = [
                import_id, now_jst_string(), "Amazon", order.order_id, order.date,
                "Amazon.co.jp", order.amount, order.payment_method, "canonical_amazon", "",
                canonical_hash(raw), f"商品明細={len(order.items)}件; baseline分割払い照合済み",
            ]
            if import_id in import_index:
                row_num, old_hash = import_index[import_id]
                existing = transactions_by_id.get(import_id)
                if old_hash != canonical[10] or existing is None or existing.status != "canonical_amazon":
                    import_updates.append((row_num, canonical))
            else:
                import_new.append(canonical)
            target_id = AmazonPipeline._expense_id(order.items[0].key)
            for tx in match.installments:
                updated = list(tx.row)
                updated[8] = MATCHED_STATUS
                updated[9] = target_id
                note = f"Amazonキー={order.items[0].key}; 分割払い照合済み"
                updated[11] = "; ".join(x for x in (tx.note, note) if x)
                installment_updates.append((tx.row_num, updated))

        self.db.append("商品マスタ", product_rows)
        if expense_new or expense_updates:
            self.db.ensure_expense_status_column()
        self.db.append("支出明細", expense_new)
        self.db.update_rows("支出明細", expense_updates)
        self.db.append("取込データ", import_new)
        self.db.update_rows("取込データ", import_updates + installment_updates)
        return {
            "installment_rows": sum(tx.status == INSTALLMENT_STATUS for tx in transactions),
            "candidate_groups": candidate_count,
            "matched_groups": len(matched),
            "unmatched_groups": unmatched_count,
            "amazon_orders_to_materialize": len(matched),
            "amazon_expenses_to_create": expenses_to_create,
            "expenses_created": len(expense_new),
            "expenses_updated": len(expense_updates),
            "installments_updated": len(installment_updates),
            "order_imports_created": len(import_new),
            "order_imports_updated": len(import_updates),
            "products_cached": len(product_rows),
        }
