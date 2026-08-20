from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .reconciliation import ImportTransaction, parse_import_rows, reconcile_transactions
from .sheets import SheetsDB
from .utils import normalize_store


ELIGIBLE_STATUSES = {
    "unclassified_paypay",
    "unclassified_aupay",
    "unclassified_card",
}
PAYMENT_SOURCES = {"PayPay", "au PAY", "au PAYカード"}
FALLBACK_CATEGORY = ("その他", "未分類")


def expense_id(import_id: str) -> str:
    """Return the stable expense ID used by automatic and manual posting."""
    return "M-" + hashlib.sha256(import_id.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class AutoExpenseDecision:
    transaction: ImportTransaction
    action: str
    status: str
    reason: str
    category: tuple[str, str] | None = None


def _combined_text(tx: ImportTransaction) -> str:
    return " ".join((tx.merchant, str(tx.row[7]), tx.note)).upper()


def _amazon_installment(text: str) -> bool:
    compact = re.sub(r"[\s　\-_/\.]+", "", text)
    return (
        "アマゾンブンカツバライ" in compact
        or ("AMAZON" in compact and ("分割" in compact or "BUNKATSU" in compact))
    )


def _refund_or_cancellation(tx: ImportTransaction, text: str) -> bool:
    return tx.amount <= 0 or any(word in text for word in (
        "返金", "取消", "取り消し", "キャンセル", "払戻", "返品", "REFUND",
    ))


def _explicit_transfer(text: str) -> bool:
    # Suica is intentionally absent: until its usage history is imported, a charge
    # is the household's only observable spending record.
    return any(word in text for word in (
        "AU PAY 残高オートチャージ", "AU PAY 残高チャージ",
        "PAYPAYチャージ", "送金", "振替", "資金移動",
    ))


def category_for(tx: ImportTransaction, categories: set[tuple[str, str]]) -> tuple[str, str]:
    text = _combined_text(tx)
    store = normalize_store(tx.merchant)
    rules = (
        (("自動車", "高速料金"), "ETC" in text),
        (("自動車", "ガソリン"), "ENEOS" in text),
        (("通信", "携帯電話"), any(x in text for x in (
            "AU電話利用料", "UQMOBILE", "UQ MOBILE", "KDDI料金",
        ))),
        (("水道・光熱", "電気"), "でんき(KDDI)" in text or "電気料金" in text),
        (("水道・光熱", "ガス"), "ガス料金" in text),
        (("医療・保険", "医療保険"), any(x in text for x in (
            "第一ネオ生命", "メディケア継続保険料",
        ))),
        (("食費", "外食"), "三井リンクラボ新木場" in store),
    )
    for category, matched in rules:
        if matched and category in categories:
            return category
    return FALLBACK_CATEGORY


def auto_expense_decisions(
    transactions: list[ImportTransaction], categories: set[tuple[str, str]],
) -> list[AutoExpenseDecision]:
    receipt_candidates = {
        decision.transaction.import_id
        for decision in reconcile_transactions(transactions)
        if decision.transaction.status in ELIGIBLE_STATUSES
    }
    decisions = []
    for tx in transactions:
        if tx.status not in ELIGIBLE_STATUSES or tx.source not in PAYMENT_SOURCES:
            continue
        text = _combined_text(tx)
        if _amazon_installment(text):
            decisions.append(AutoExpenseDecision(
                tx, "review", "needs_review_amazon_installment", "Amazon分割払いの重複確認が必要",
            ))
        elif _refund_or_cancellation(tx, text):
            decisions.append(AutoExpenseDecision(
                tx, "review", "needs_review_refund", "返金・取消・マイナス取引の確認が必要",
            ))
        elif _explicit_transfer(text):
            decisions.append(AutoExpenseDecision(
                tx, "review", "needs_review_transfer", "チャージ・送金・資金移動の確認が必要",
            ))
        elif tx.import_id in receipt_candidates:
            decisions.append(AutoExpenseDecision(
                tx, "skip", tx.status, "既存レシートとの照合候補があるため先にreconciliationが必要",
            ))
        else:
            decisions.append(AutoExpenseDecision(
                tx, "post", "auto_expense", "明確な決済取引", category_for(tx, categories),
            ))
    return decisions


class AutoExpensePipeline:
    def __init__(self, db: SheetsDB):
        self.db = db

    def preview(self) -> dict:
        transactions = parse_import_rows(self.db.get("取込データ!A2:L"))
        decisions = auto_expense_decisions(transactions, set(self.db.categories()))
        return self._summary(decisions)

    def apply(self) -> dict:
        transactions = parse_import_rows(self.db.get("取込データ!A2:L"))
        categories = set(self.db.categories())
        if FALLBACK_CATEGORY not in categories:
            raise RuntimeError("カテゴリマスタに「その他 / 未分類」がありません")
        decisions = auto_expense_decisions(transactions, categories)
        expense_index = self.db.expense_index()
        expense_new = []
        expense_updates = []
        import_updates = []
        for decision in decisions:
            tx = decision.transaction
            if decision.action == "skip":
                continue
            updated = list(tx.row)
            updated[8] = decision.status
            annotation = f"自動判定={decision.reason}"
            updated[11] = "; ".join(x for x in (tx.note, annotation) if x)
            if decision.action == "post":
                spend_id = expense_id(tx.import_id)
                updated[9] = spend_id
                category = decision.category or FALLBACK_CATEGORY
                expense = [
                    spend_id, tx.date, tx.merchant, "自動計上", tx.amount,
                    category[0], category[1], tx.row[7], tx.source, "", tx.import_id,
                    decision.reason, "active",
                ]
                if spend_id in expense_index:
                    expense_updates.append((expense_index[spend_id], expense))
                else:
                    expense_new.append(expense)
            import_updates.append((tx.row_num, updated))
        if expense_new or expense_updates:
            self.db.ensure_expense_status_column()
        self.db.append("支出明細", expense_new)
        self.db.update_rows("支出明細", expense_updates)
        self.db.update_rows("取込データ", import_updates)
        result = self._summary(decisions)
        result.update({
            "expenses_created": len(expense_new),
            "expenses_updated": len(expense_updates),
            "imports_updated": len(import_updates),
        })
        return result

    @staticmethod
    def _summary(decisions: list[AutoExpenseDecision]) -> dict:
        return {
            "candidates": len(decisions),
            "auto_expense": sum(x.action == "post" for x in decisions),
            "needs_review": sum(x.action == "review" for x in decisions),
            "skipped": sum(x.action == "skip" for x in decisions),
        }
