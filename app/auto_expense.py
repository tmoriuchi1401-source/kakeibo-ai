from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import datetime

from .reconciliation import ImportTransaction, parse_import_rows, reconcile_transactions
from .bank_pdf_pipeline import DOCOMO_SMTB_SOURCE
from .bank_reconciliation import (
    ASSET_FORMATION_CATEGORY,
    ASSET_FORMATION_IMPORT_STATUS,
)
from .sheets import SheetsDB
from .utils import normalize_store
from .category_rules import match_transaction, parse_rules


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
    from .amazon_money_runtime import money_enabled
    from .amazon_money_mail import is_amazon_money_merchant
    money_mode=money_enabled()
    receipt_candidates = {
        decision.transaction.import_id
        for decision in reconcile_transactions(transactions)
        if decision.transaction.status in ELIGIBLE_STATUSES
    }
    decisions = []
    for tx in transactions:
        if (
            tx.source == DOCOMO_SMTB_SOURCE
            and tx.status == ASSET_FORMATION_IMPORT_STATUS
        ):
            if tx.amount >= 0:
                decisions.append(AutoExpenseDecision(
                    tx, "review", "needs_review_asset_formation_sign",
                    "資産形成支出の金額符号が不正",
                ))
            else:
                decisions.append(AutoExpenseDecision(
                    tx, "post", "auto_expense",
                    "銀行の資産形成支出authority", ASSET_FORMATION_CATEGORY,
                ))
            continue
        if tx.status not in ELIGIBLE_STATUSES or tx.source not in PAYMENT_SOURCES:
            continue
        text = _combined_text(tx)
        if money_mode and (is_amazon_money_merchant(tx.merchant) or _amazon_installment(text)):
            decisions.append(AutoExpenseDecision(
                tx,"review","needs_review_amazon_money","日常のAmazon金銭確認で確定情報と既存計上を確認",
            ))
        elif _amazon_installment(text):
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
    def __init__(self, db: SheetsDB, *, category_rule_auto_apply_enabled: bool = False):
        self.db = db
        self.category_rule_auto_apply_enabled = category_rule_auto_apply_enabled

    def _with_approved_rule(self, decisions, categories):
        if not self.category_rule_auto_apply_enabled or not hasattr(self.db, "category_rules"):
            return decisions
        rules = parse_rules(self.db.category_rules())
        updated = []
        for decision in decisions:
            # Rules fill only the established fallback. Existing authoritative
            # decisions and manual/product categories cannot be overwritten.
            if decision.action != "post" or decision.category != FALLBACK_CATEGORY:
                updated.append(decision)
                continue
            matched = match_transaction(
                rules, decision.transaction, categories, aggregate_only=True,
            )
            if matched.state == "matched" and matched.rule:
                updated.append(replace(
                    decision, category=matched.rule.category,
                    reason=f"{decision.reason}; 承認ルール={matched.rule.rule_id}/r{matched.rule.revision}",
                ))
            elif matched.state in {"conflict", "held"}:
                updated.append(replace(decision, reason=f"{decision.reason}; 承認ルール={matched.state}（保留）"))
            else:
                updated.append(decision)
        return updated

    def preview(self) -> dict:
        transactions = parse_import_rows(self.db.get("取込データ!A2:L"))
        categories = set(self.db.categories())
        decisions = self._with_approved_rule(auto_expense_decisions(transactions, categories), categories)
        return self._summary(decisions)

    def apply(self) -> dict:
        transactions = parse_import_rows(self.db.get("取込データ!A2:L"))
        categories = set(self.db.categories())
        if FALLBACK_CATEGORY not in categories:
            raise RuntimeError("カテゴリマスタに「その他 / 未分類」がありません")
        decisions = self._with_approved_rule(auto_expense_decisions(transactions, categories), categories)
        expense_index = self.db.expense_index()
        records = self.db.expense_records() if hasattr(self.db, "expense_records") else {}
        expense_new = []
        expense_updates = []
        import_updates = []
        rule_rows = self.db.category_rules() if (
            self.category_rule_auto_apply_enabled and hasattr(self.db, "category_rules")
        ) else []
        rules_by_marker = {(rule.rule_id, rule.revision): rule for rule in parse_rules(rule_rows)}
        rule_trace: dict[tuple[str, int], list] = {}
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
                posted_amount = (
                    abs(tx.amount)
                    if tx.status == ASSET_FORMATION_IMPORT_STATUS else tx.amount
                )
                expense = [
                    spend_id, tx.date, tx.merchant, "自動計上", posted_amount,
                    category[0], category[1], tx.row[7], tx.source, "", tx.import_id,
                    decision.reason, "active",
                ]
                if spend_id in expense_index:
                    existing = records.get(spend_id)
                    # A replay after an append but before the import status
                    # update must never erase a user-confirmed F/G selection.
                    if existing:
                        old = existing[1]
                        old_category = (str(old[5]).strip(), str(old[6]).strip())
                        if old_category in categories and old_category != FALLBACK_CATEGORY:
                            expense[5:7] = list(old_category)
                            expense[11] = old[11]
                    expense_updates.append((expense_index[spend_id], expense))
                else:
                    expense_new.append(expense)
                    matched = re.search(r"承認ルール=(CR-[0-9a-f]+)/r(\d+)", decision.reason)
                    if matched:
                        marker = (matched.group(1), int(matched.group(2)))
                        rule = rules_by_marker.get(marker)
                        if rule:
                            if marker in rule_trace:
                                rule_trace[marker][2] += 1
                                rule_trace[marker][3] = spend_id
                            else:
                                raw = list(rule_rows[rule.row_num - 2]) + [""] * max(0, 18 - len(rule_rows[rule.row_num - 2]))
                                count = int(raw[15]) if str(raw[15]).strip().isdigit() else 0
                                rule_trace[marker] = [rule.row_num, raw, count + 1, spend_id]
            import_updates.append((tx.row_num, updated))
        if expense_new or expense_updates:
            self.db.ensure_expense_status_column()
        self.db.append("支出明細", expense_new)
        self.db.update_rows("支出明細", expense_updates)
        self.db.update_rows("取込データ", import_updates)
        # The per-expense note is the immutable trace; the master keeps a
        # compact latest-ID/count index for inspection without another ledger.
        for _, (row_num, raw, count, spend_id) in rule_trace.items():
            raw[15:18] = [count, spend_id, datetime.now().astimezone().isoformat()]
            self.db.update_rows("カテゴリ自動分類ルール", [(row_num, raw[:18])])
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
            "category_rule_applied": sum("承認ルール=CR-" in x.reason for x in decisions),
            "category_rule_held": sum("承認ルール=held" in x.reason for x in decisions),
            "category_rule_conflicts": sum("承認ルール=conflict" in x.reason for x in decisions),
        }
