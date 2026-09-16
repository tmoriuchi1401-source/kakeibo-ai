"""Explicit rule registration and deactivation, guarded by separate flags."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .category_rules import (
    AGGREGATE_ITEM_NAMES, CategoryRule, RESERVED_ITEM_NAMES, bank_account_alias, narrow_text, parse_rules,
    rule_id_for, valid_rule,
)
from .reconciliation import parse_import_rows


@dataclass(frozen=True)
class RuleApprovalRequest:
    expense_id: str
    category: tuple[str, str]
    kind: str
    product_id: str = ""
    exact_amount: int | None = None


class CategoryRuleApprovalPipeline:
    """Register only an explicitly checked, still-current ledger classification.

    The caller supplies the category it displayed next to the unchecked control;
    this pipeline rereads F/G and refuses it if a concurrent edit changed either
    value.  It never writes a category itself.
    """
    def __init__(self, db, *, save_enabled: bool, now=None):
        self.db = db
        self.save_enabled = save_enabled
        self.now = now or (lambda: datetime.now(timezone.utc))

    def _transactions(self):
        return {tx.import_id: tx for tx in parse_import_rows(self.db.get("取込データ!A2:L"))}

    def register(self, request: RuleApprovalRequest) -> dict:
        if not self.save_enabled:
            return {"state": "disabled", "reason": "category_rule_save_disabled"}
        records = self.db.expense_records()
        found = records.get(request.expense_id)
        if not found:
            return {"state": "held", "reason": "expense_not_found"}
        _, expense = found
        current_category = (narrow_text(expense[5]), narrow_text(expense[6]))
        categories = set(self.db.categories())
        if current_category != request.category:
            return {"state": "held", "reason": "category_changed_concurrently"}
        if current_category not in categories:
            return {"state": "held", "reason": "invalid_category_pair"}
        tx = self._transactions().get(str(expense[10]))
        if not tx:
            return {"state": "held", "reason": "source_import_not_found"}
        kind = narrow_text(request.kind)
        product_id = narrow_text(request.product_id)
        item_name = narrow_text(expense[3])
        merchant = narrow_text(tx.merchant)
        account = bank_account_alias(tx.import_id)
        if str(tx.import_id).startswith("bankpdf:") and not account:
            return {"state": "held", "reason": "bank_account_alias_required"}
        if kind == "service":
            candidate = CategoryRule("", kind, narrow_text(tx.source), account, merchant, "", "", "",
                                     request.exact_amount, current_category, request.expense_id,
                                     self.now(), 1, True)
        elif kind == "store_total":
            if item_name not in AGGREGATE_ITEM_NAMES or tx.source == "Amazon":
                return {"state": "held", "reason": "store_total_requires_aggregate_only"}
            candidate = CategoryRule("", kind, narrow_text(tx.source), account, "", merchant, "", "",
                                     request.exact_amount, current_category, request.expense_id,
                                     self.now(), 1, True)
        elif kind == "product":
            if item_name in RESERVED_ITEM_NAMES or (not product_id and not item_name):
                return {"state": "held", "reason": "specific_product_required"}
            candidate = CategoryRule("", kind, narrow_text(tx.source), account, "", merchant, product_id, item_name,
                                     request.exact_amount, current_category, request.expense_id,
                                     self.now(), 1, True)
        else:
            return {"state": "held", "reason": "unknown_rule_kind"}
        candidate = replace(candidate, rule_id=rule_id_for(candidate))
        if not valid_rule(candidate, categories):
            return {"state": "held", "reason": "unsafe_or_incomplete_condition"}
        existing = parse_rules(self.db.category_rules())
        same = [rule for rule in existing if rule.identity() == candidate.identity()]
        if any(rule.active and rule.category == candidate.category for rule in same):
            return {"state": "already_registered", "rule_id": same[0].rule_id}
        if any(rule.active and rule.category != candidate.category for rule in same):
            return {"state": "held", "reason": "conflicting_active_rule"}
        inactive = next((rule for rule in same if rule.category == candidate.category), None)
        self.db.ensure_category_rule_sheet()
        if inactive:
            revived = replace(candidate, rule_id=inactive.rule_id, revision=inactive.revision + 1)
            old = list(self.db.get(f"カテゴリ自動分類ルール!A{inactive.row_num}:R{inactive.row_num}")[0])
            count = int(old[15]) if len(old) > 15 and str(old[15]).isdigit() else 0
            self.db.update_rows("カテゴリ自動分類ルール", [(inactive.row_num, revived.to_row(applied_count=count))])
            return {"state": "reactivated", "rule_id": revived.rule_id, "revision": revived.revision}
        self.db.append("カテゴリ自動分類ルール", [candidate.to_row()])
        return {"state": "registered", "rule_id": candidate.rule_id, "revision": 1}

    def deactivate(self, rule_id: str) -> dict:
        if not self.save_enabled:
            return {"state": "disabled", "reason": "category_rule_save_disabled"}
        rule = next((rule for rule in parse_rules(self.db.category_rules()) if rule.rule_id == rule_id), None)
        if not rule:
            return {"state": "not_found"}
        if not rule.active:
            return {"state": "already_inactive", "rule_id": rule_id}
        raw = list(self.db.get(f"カテゴリ自動分類ルール!A{rule.row_num}:R{rule.row_num}")[0])
        count = int(raw[15]) if len(raw) > 15 and str(raw[15]).isdigit() else 0
        inactive = replace(rule, active=False, revision=rule.revision + 1)
        self.db.update_rows("カテゴリ自動分類ルール", [(rule.row_num, inactive.to_row(applied_count=count))])
        return {"state": "deactivated", "rule_id": rule_id, "revision": inactive.revision}
