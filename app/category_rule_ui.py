"""Opt-in, phone-width request surface for approved category rules."""
from __future__ import annotations

import hashlib
import json

from .auto_expense import FALLBACK_CATEGORY
from .category_rule_pipeline import CategoryRuleApprovalPipeline, RuleApprovalRequest
from .category_rules import AGGREGATE_ITEM_NAMES, bank_account_alias, narrow_text, parse_rules
from .reconciliation import parse_import_rows
from .category_rule_choices import DECLINE, checked as _checked

# Keep the only operator controls in the first six narrow columns.  The
# condition key and source proof are intentionally retained (but hidden), so
# sorting or inserting rows can never retarget a checked request.
UI_HEADERS = ["条件・代表取引", "件数・状態", "大カテゴリ", "小カテゴリ", "今後の自動分類", "過去分の候補に追加", "固定条件キー", "代表支出ID", "日付", "データ元", "種別", "承認スナップショット"]


class CategoryRuleUIPipeline:
    def __init__(self, db, *, ui_enabled: bool, save_enabled: bool):
        self.db=db; self.ui_enabled=ui_enabled; self.save_enabled=save_enabled

    def refresh(self):
        if not self.ui_enabled:
            return {"state":"disabled", "reason":"category_rule_ui_disabled"}
        transactions={tx.import_id:tx for tx in parse_import_rows(self.db.get("取込データ!A2:L"))}
        old={}
        for row in self.db.category_rule_ui_rows():
            # The former UI has key/category/snapshot in D/F:G/J.  Preserve
            # its entered categories and checks during the one-way layout
            # migration rather than clearing a person's pending decision.
            if len(row) >= 12:
                key=narrow_text(row[6])
                old[key]={"future":row[4], "past":row[5], "major":row[2], "minor":row[3], "snapshot":row[11], "status":row[1]}
            elif len(row) >= 10:
                key=narrow_text(row[3])
                old[key]={"future":row[2], "past":row[10] if len(row)>10 else False,
                          "major":row[5], "minor":row[6], "snapshot":row[9]}
        rules=parse_rules(self.db.category_rules()) if hasattr(self.db, "category_rules") else []
        declined_conditions={}
        for prior in old.values():
            if prior.get("future") == DECLINE:
                try:
                    proof=json.loads(prior.get("snapshot", ""))
                    identity=(proof["kind"],proof["source"],proof["account_alias"],proof["merchant"],"","","","")
                    declined_conditions[identity]=prior
                except (KeyError, TypeError, ValueError):
                    pass
        rows=[]
        grouped={}
        for expense_id,(_, expense) in sorted(self.db.expense_records().items()):
            category=(narrow_text(expense[5]),narrow_text(expense[6]))
            tx=transactions.get(str(expense[10]))
            if (not tx or expense[12] != "active" or not all(category)
                    or tx.status != "auto_expense" or tx.target_id != expense_id):
                continue
            account=bank_account_alias(tx.import_id)
            identity=("service", narrow_text(tx.source), account, narrow_text(tx.merchant), "", "", "", "")
            if category == FALLBACK_CATEGORY:
                if narrow_text(expense[3]) in AGGREGATE_ITEM_NAMES:
                    grouped.setdefault(identity, []).append((expense_id, expense, tx))
                continue
            # One conservative option: source + exact billing/store text. Users
            # may decline it; product and aggregate scopes require explicit CLI
            # detail until their own review controls are approved.
            # A past-only classification changes a group's key to an expense
            # key. Carry its declined future decision by the exact condition.
            prior=old.get(expense_id, declined_conditions.get(identity, {}))
            checked=_checked(prior.get("future", ""))
            past_checked=str(prior.get("past", "")).upper() in {"TRUE","1"}
            entered=(narrow_text(prior.get("major")), narrow_text(prior.get("minor")))
            # F:G is an initial proposal only.  Once a person has selected a
            # pair in this UI, keep that independent registration proposal
            # rather than rebuilding C:D from the ledger on every refresh.
            proposal=entered if any(entered) else category
            account_text=f" / 口座={account}" if account else ""
            baseline_snapshot=CategoryRuleApprovalPipeline.snapshot_for(
                expense_id=expense_id, category=category, kind="service", tx=tx,
                item_name=narrow_text(expense[3]),
            )
            proposal_kind="classified_override" if proposal != category else ""
            snapshot=CategoryRuleApprovalPipeline.snapshot_for(
                expense_id=expense_id, category=proposal, kind="service", tx=tx,
                item_name=narrow_text(expense[3]),
                proposal=proposal_kind,
            )
            # A pending row initially snapshots F:G.  A person's first C:D
            # override plus its checks is one decision, not a stale approval.
            # Subsequent category/source changes compare against the proposal
            # snapshot and require a new check while retaining C:D.
            initial_override = bool((checked or past_checked) and proposal != category
                                    and _canonical_snapshot(prior.get("snapshot"))
                                    == _canonical_snapshot(baseline_snapshot))
            changed_checked_condition = bool((checked or past_checked) and prior.get("snapshot")
                                             and prior.get("snapshot") != snapshot and not initial_override)
            if changed_checked_condition:
                checked=past_checked=False
            same=[rule for rule in rules if rule.identity() == identity]
            if any(rule.active and rule.category == proposal for rule in same):
                state="登録済み"; checked=False
            elif any(rule.active and rule.category != proposal for rule in same):
                state="競合"; checked=False
            else:
                state="登録待ち" if checked else ("再承認が必要" if changed_checked_condition else "未登録")
            declined=prior.get("future") == DECLINE
            if declined:
                state="既存ルールあり・追加登録しない" if any(rule.active for rule in same) else DECLINE
            elif not checked and not changed_checked_condition and not same and prior.get("snapshot") == snapshot:
                state=_held_status(prior.get("status")) or state
            condition=f"{tx.source}{account_text} / {narrow_text(tx.merchant)}（完全一致・金額不問・今後の未分類のみ）\n{state}"
            if changed_checked_condition:
                old_condition="以前の条件"
                condition=f"{old_condition}\n現在: {tx.source}{account_text} / {narrow_text(tx.merchant)}（完全一致）\n再承認が必要（条件またはカテゴリが変更）"
            rows.append([
                f"{tx.source} / {narrow_text(tx.merchant)}\n代表 {expense[1]}",
                condition, proposal[0], proposal[1], DECLINE if declined else checked, past_checked,
                expense_id, expense_id, expense[1], tx.source, "service", snapshot,
            ])
        # Unclassified records are grouped strictly by the full reusable
        # service identity.  They are proposals only: category entry and each
        # of future-rule / historical-preview approvals remain independent.
        for identity, members in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])):
            expense_id, expense, tx = sorted(members, key=lambda item: (str(item[1][1]), item[0]))[0]
            key="group:" + hashlib.sha256("\x1f".join(identity).encode("utf-8")).hexdigest()[:24]
            prior=old.get(key, {})
            major=narrow_text(prior.get("major")); minor=narrow_text(prior.get("minor"))
            snapshot=CategoryRuleApprovalPipeline.snapshot_for(
                expense_id=expense_id, category=(major, minor), kind="service", tx=tx,
                item_name=narrow_text(expense[3]), proposal="fallback_group",
            )
            prior_snapshot=narrow_text(prior.get("snapshot"))
            checked=_checked(prior.get("future", ""))
            past_checked=str(prior.get("past", "")).upper() in {"TRUE", "1"}
            # An empty initial category is not an approval snapshot.  A person
            # may choose their first category and check either independent
            # action in one edit; only a later change to an already selected
            # category/condition clears that approval.
            changed=bool((checked or past_checked) and _snapshot_has_category(prior_snapshot)
                         and prior_snapshot != snapshot)
            if changed: checked=past_checked=False
            amount=sum(int(float(str(item[1][4]).replace(",", ""))) for item in members)
            account=identity[2]; account_text=f" / 口座={account}" if account else ""
            state=("カテゴリを選択" if not (major and minor) else ("再承認が必要" if changed else "登録待ち"))
            same=[rule for rule in rules if rule.active and rule.identity() == identity]
            if same and not changed and major and minor:
                state="登録済み" if all(rule.category == (major, minor) for rule in same) else "競合"
                checked=False
            declined=prior.get("future") == DECLINE
            if declined:
                state="既存ルールあり・追加登録しない" if same else DECLINE
            elif not checked and not changed and not same and prior_snapshot == snapshot:
                state=_held_status(prior.get("status")) or state
            rows.append([
                f"{tx.source}{account_text} / {narrow_text(tx.merchant)}\n代表 {expense[1]} {expense_id}",
                f"未分類 {len(members)}件 / {amount}円\n完全一致・金額不問・自動計上のみ\n{state}",
                major, minor, DECLINE if declined else checked, past_checked, key, expense_id, expense[1], tx.source, "service", snapshot,
            ])
        if hasattr(self.db, "replace_category_rule_ui_rows"):
            for row in rows:
                if _checked(row[5]):
                    # Keep the future state last for the read-only daily queue.
                    lines=row[1].split("\n")
                    row[1]="\n".join(lines[:-1]+["過去分: 2で期間・プレビューを選択",lines[-1]])
            self.db.replace_category_rule_ui_rows(rows, UI_HEADERS)
        else:  # Minimal test and legacy adapter compatibility.
            self.db.ensure_category_rule_ui_sheet(UI_HEADERS)
            self.db.clear("カテゴリ自動分類!A2:L")
            self.db.append("カテゴリ自動分類", rows)
        return {"state":"refreshed", "candidates":len(rows), "unclassified_groups":len(grouped), "checked_carried":sum(_checked(row[4]) for row in rows)}

    def apply_checked(self):
        if not self.ui_enabled or not self.save_enabled:
            return {"state":"disabled", "reason":"category_rule_ui_or_save_disabled"}
        results=[]; updates=[]
        for row_num,row in enumerate(self.db.category_rule_ui_rows(), start=2):
            cells=list(row)+[""]*max(0,12-len(row))
            if not _checked(cells[4]): continue
            try:
                snapshot=json.loads(narrow_text(cells[11]))
            except (TypeError, ValueError, json.JSONDecodeError):
                snapshot={}
            result=CategoryRuleApprovalPipeline(self.db, save_enabled=True).register(
                RuleApprovalRequest(narrow_text(cells[7]), (narrow_text(cells[2]), narrow_text(cells[3])),
                                    narrow_text(cells[10]), condition_snapshot=narrow_text(cells[11]),
                                    allow_fallback_origin=snapshot.get("proposal") == "fallback_group",
                                    allow_classified_override=snapshot.get("proposal") == "classified_override"))
            results.append((narrow_text(cells[6]), result))
            cells[4] = False
            cells[1] += "\n" + (result["state"] if "reason" not in result else f"{result['state']}: {result['reason']}")
            updates.append((row_num, cells[:12]))
        if updates:
            self.db.update_rows("カテゴリ自動分類", updates)
        return {"state":"applied", "checked":len(results), "results":results}


def _held_status(value):
    lines=str(value or "").split("\n")
    return next((line for line in reversed(lines) if line.startswith("held:")), "")


def _snapshot_has_category(value: object) -> bool:
    try:
        category=json.loads(narrow_text(value)).get("category", [])
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(category, list) and len(category) == 2 and all(narrow_text(item) for item in category)


def _canonical_snapshot(value: object) -> str:
    try:
        return json.dumps(json.loads(narrow_text(value)), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
