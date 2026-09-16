"""Opt-in, phone-width request surface for approved category rules."""
from __future__ import annotations

from .category_rule_pipeline import CategoryRuleApprovalPipeline, RuleApprovalRequest
from .category_rules import bank_account_alias, narrow_text, parse_rules
from .reconciliation import parse_import_rows

UI_HEADERS = ["対象・カテゴリ", "条件・状態", "登録する", "支出ID", "日付", "大分類", "小分類", "データ元", "種別"]


class CategoryRuleUIPipeline:
    def __init__(self, db, *, ui_enabled: bool, save_enabled: bool):
        self.db=db; self.ui_enabled=ui_enabled; self.save_enabled=save_enabled

    def refresh(self):
        if not self.ui_enabled:
            return {"state":"disabled", "reason":"category_rule_ui_disabled"}
        transactions={tx.import_id:tx for tx in parse_import_rows(self.db.get("取込データ!A2:L"))}
        old={narrow_text(row[3]): row for row in self.db.category_rule_ui_rows() if len(row)>3 and narrow_text(row[3])}
        rules=parse_rules(self.db.category_rules()) if hasattr(self.db, "category_rules") else []
        rows=[]
        for expense_id,(_, expense) in sorted(self.db.expense_records().items()):
            category=(narrow_text(expense[5]),narrow_text(expense[6]))
            tx=transactions.get(str(expense[10]))
            if (not tx or expense[12] != "active" or not all(category)
                    or category == ("その他","未分類")
                    or tx.status != "auto_expense" or tx.target_id != expense_id):
                continue
            # One conservative option: source + exact billing/store text. Users
            # may decline it; product and aggregate scopes require explicit CLI
            # detail until their own review controls are approved.
            checked=str((old.get(expense_id) or [""]*3)[2]).upper() in {"TRUE","1"}
            account=bank_account_alias(tx.import_id)
            account_text=f" / 口座={account}" if account else ""
            identity=("service", narrow_text(tx.source), account, narrow_text(tx.merchant), "", "", "", "")
            same=[rule for rule in rules if rule.identity() == identity]
            if any(rule.active and rule.category == category for rule in same):
                state="登録済み"; checked=False
            elif any(rule.active and rule.category != category for rule in same):
                state="競合"; checked=False
            else:
                state="登録待ち" if checked else "未登録"
            rows.append([
                f"{expense[1]}\n{category[0]} / {category[1]}",
                f"{tx.source}{account_text} / {narrow_text(tx.merchant)}（完全一致・金額不問・今後の未分類のみ）\n{state}",
                checked, expense_id, expense[1], category[0], category[1], tx.source, "service",
            ])
        self.db.ensure_category_rule_ui_sheet(UI_HEADERS)
        self.db.clear("カテゴリ自動分類!A2:I")
        self.db.append("カテゴリ自動分類", rows)
        return {"state":"refreshed", "candidates":len(rows), "checked_carried":sum(bool(row[2]) for row in rows)}

    def apply_checked(self):
        if not self.ui_enabled or not self.save_enabled:
            return {"state":"disabled", "reason":"category_rule_ui_or_save_disabled"}
        results=[]; updates=[]
        for row_num,row in enumerate(self.db.category_rule_ui_rows(), start=2):
            cells=list(row)+[""]*max(0,9-len(row))
            if str(cells[2]).upper() not in {"TRUE","1"}: continue
            result=CategoryRuleApprovalPipeline(self.db, save_enabled=True).register(
                RuleApprovalRequest(narrow_text(cells[3]), (narrow_text(cells[5]), narrow_text(cells[6])), narrow_text(cells[8])))
            results.append((narrow_text(cells[3]), result))
            cells[2] = False
            cells[1] += "\n" + (result["state"] if "reason" not in result else f"{result['state']}: {result['reason']}")
            updates.append((row_num, cells[:9]))
        if updates:
            self.db.update_rows("カテゴリ自動分類", updates)
        return {"state":"applied", "checked":len(results), "results":results}
