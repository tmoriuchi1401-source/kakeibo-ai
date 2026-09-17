"""Explicit rule registration and deactivation, guarded by separate flags."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json

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
    # The phone UI supplies the exact condition it rendered.  The pipeline
    # compares it with a fresh source read before saving so a stale checked row
    # can never silently become a different rule.
    condition_snapshot: str = ""
    allow_fallback_origin: bool = False
    # A categorized representative can supply a separately selected, valid
    # future-rule category without changing its historical F:G values.
    allow_classified_override: bool = False


class CategoryRuleApprovalPipeline:
    """Register only an explicitly checked, still-current rule proposal.

    The caller supplies the category it displayed next to the unchecked control;
    this pipeline rereads F/G to bind the representative identity, and accepts
    a different category only when the UI's fresh proposal snapshot explicitly
    marks it.  It never writes a category itself.
    """
    def __init__(self, db, *, save_enabled: bool, now=None):
        self.db = db
        self.save_enabled = save_enabled
        self.now = now or (lambda: datetime.now(timezone.utc))

    def _transactions(self):
        return {tx.import_id: tx for tx in parse_import_rows(self.db.get("取込データ!A2:L"))}

    @staticmethod
    def snapshot_for(*, expense_id: str, category: tuple[str, str], kind: str,
                     tx, product_id: str = "", item_name: str = "",
                     exact_amount: int | None = None, proposal: str = "") -> str:
        """A canonical, comparison-only snapshot of the displayed condition."""
        payload = {
            "v": 1, "expense_id": narrow_text(expense_id),
            "category": [narrow_text(category[0]), narrow_text(category[1])],
            "kind": narrow_text(kind), "source": narrow_text(tx.source),
            "account_alias": bank_account_alias(tx.import_id),
            "merchant": narrow_text(tx.merchant), "product_id": narrow_text(product_id),
            "item_name": narrow_text(item_name), "amount": exact_amount,
            "import_id": narrow_text(tx.import_id), "import_status": narrow_text(tx.status),
            "target_id": narrow_text(tx.target_id),
            "proposal": narrow_text(proposal),
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def register(self, request: RuleApprovalRequest) -> dict:
        if not self.save_enabled:
            return {"state": "disabled", "reason": "category_rule_save_disabled"}
        records = self.db.expense_records()
        found = records.get(request.expense_id)
        if not found:
            return {"state": "held", "reason": "expense_not_found"}
        _, expense = found
        current_category = (narrow_text(expense[5]), narrow_text(expense[6]))
        requested_category = (narrow_text(request.category[0]), narrow_text(request.category[1]))
        categories = set(self.db.categories())
        fallback_proposal = request.allow_fallback_origin and current_category == ("その他", "未分類")
        classified_override = request.allow_classified_override and current_category != requested_category
        if current_category != requested_category and not (fallback_proposal or classified_override):
            return {"state": "held", "reason": "category_changed_concurrently"}
        if requested_category not in categories:
            return {"state": "held", "reason": "invalid_category_pair"}
        tx = self._transactions().get(str(expense[10]))
        if not tx:
            return {"state": "held", "reason": "source_import_not_found"}
        kind = narrow_text(request.kind)
        # A service/aggregate rule can learn only from the established
        # automatic-posting path, whose target is this exact stable expense ID.
        # Product rules have their own narrow provenance contract below.
        if kind in {"service", "store_total"} and (
            tx.status != "auto_expense" or tx.target_id != request.expense_id
        ):
            return {"state": "held", "reason": "source_not_eligible_for_learning"}
        product_id = narrow_text(request.product_id)
        item_name = narrow_text(expense[3])
        merchant = narrow_text(tx.merchant)
        account = bank_account_alias(tx.import_id)
        if str(tx.import_id).startswith("bankpdf:") and not account:
            return {"state": "held", "reason": "bank_account_alias_required"}
        if request.condition_snapshot:
            current_snapshot = self.snapshot_for(
                expense_id=request.expense_id, category=requested_category, kind=kind,
                tx=tx, product_id=product_id, item_name=item_name,
                exact_amount=request.exact_amount, proposal=("fallback_group" if fallback_proposal
                                                              else "classified_override" if classified_override else ""),
            )
            try:
                supplied = json.dumps(json.loads(request.condition_snapshot), ensure_ascii=False,
                                      sort_keys=True, separators=(",", ":"))
            except (TypeError, ValueError, json.JSONDecodeError):
                return {"state": "held", "reason": "invalid_condition_snapshot"}
            if supplied != current_snapshot:
                return {"state": "held", "reason": "condition_changed_concurrently"}
        if kind == "service":
            if item_name not in AGGREGATE_ITEM_NAMES:
                return {"state": "held", "reason": "service_requires_aggregate_only"}
            candidate = CategoryRule("", kind, narrow_text(tx.source), account, merchant, "", "", "",
                                     request.exact_amount, requested_category, request.expense_id,
                                     self.now(), 1, True)
        elif kind == "store_total":
            if item_name not in AGGREGATE_ITEM_NAMES or tx.source == "Amazon":
                return {"state": "held", "reason": "store_total_requires_aggregate_only"}
            candidate = CategoryRule("", kind, narrow_text(tx.source), account, "", merchant, "", "",
                                     request.exact_amount, requested_category, request.expense_id,
                                     self.now(), 1, True)
        elif kind == "product":
            if item_name in RESERVED_ITEM_NAMES or (not product_id and not item_name):
                return {"state": "held", "reason": "specific_product_required"}
            if tx.source == "Amazon" and not product_id:
                return {"state": "held", "reason": "product_namespace_required"}
            candidate = CategoryRule("", kind, narrow_text(tx.source), account, "", merchant, product_id, item_name,
                                     request.exact_amount, requested_category, request.expense_id,
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
