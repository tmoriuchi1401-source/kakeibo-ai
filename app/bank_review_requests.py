"""Process checked bank groups from the existing immutable Sheets request."""
from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import replace

from .bank_meaning_rules import RULE_SHEET, plan_registration
from .bank_review_decisions import DECISION_SHEET, from_answer, evaluate
from .bank_review_groups import digest
from .bank_review_store import BankReviewStore
from .bank_review_ui import checked, submitted_answers, validate_rows

REASON_LABELS = {
    "bank_review_choice_missing": "用途と今後の自動判定を選んでください",
    "bank_review_income_category_missing": "収入分類を選んでください",
    "bank_review_income_category_not_applicable": "収入以外の用途では収入分類を空欄にしてください",
    "bank_review_direction_choice_invalid": "入出金方向と用途が一致しません",
    "bank_review_source_account_unconfirmed": "相手銀行と確認済み口座aliasを指定してください",
    "bank_review_source_account_same": "別の自己口座を指定してください",
    "bank_review_source_account_not_applicable": "振替以外の用途では相手口座欄を空欄にしてください",
    "bank_review_source_changed": "対象または記帳状態が変わりました。再表示後に確認してください",
    "bank_review_decision_conflict": "以前の用途判断と競合しています",
    "bank_meaning_rule_conflict": "既存銀行ルールと競合しています",
    "bank_meaning_legacy_conflict": "確認済みJSONルールと競合しています",
    "bank_review_individual_confirmation": "個別確認として保留しています",
}


def _state(row, reason):
    label = REASON_LABELS.get(reason, "安全条件を確認してください")
    row[1] = str(row[1]).split("\n処理結果：")[0] + "\n処理結果：" + label
    row[5] = False


class BankReviewRequestProcessor:
    """source_reader performs fresh PDF/ledger/rule evaluation on every call.

    The canonical metadata and all selected source snapshots are re-read
    immediately before the single atomic metadata mutation. No ledger writes
    or PDF moves are delegated to this processor.
    """
    def __init__(self, db, source_reader, *, store=None, clock=None):
        self.db, self.source_reader = db, source_reader
        self.store = store or BankReviewStore(db)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def process(self, rows, request_id):
        validate_rows(rows)
        output = deepcopy(rows)
        counts = {"bank_groups_checked": 0, "bank_groups_confirmed": 0, "bank_groups_already_confirmed": 0,
                  "bank_rules_registered": 0, "bank_rules_already_registered": 0, "bank_held": 0,
                  "bank_metadata_writes": 0, "bank_ledger_writes": 0}
        selected = [(index, output[index:index + 2]) for index in range(0, len(output), 2)
                    if checked(output[index][5])]
        if not selected:
            return counts, output
        master = self.store.read()
        rules = master.records(RULE_SHEET)
        decisions = master.records(DECISION_SHEET)
        source = self.source_reader()
        current = {group["key"]: group for group in source.groups}
        edits = {RULE_SHEET: [], DECISION_SHEET: []}
        accepted = []
        timestamp = self.clock().isoformat(timespec="seconds")
        for index, pair in selected:
            counts["bank_groups_checked"] += 1
            try:
                answer = submitted_answers(pair, accounts=source.accounts)[0]
                key = answer["group_key"]
                group = current.get(key)
                if group is None or group["membership_digest"] != answer["snapshot_digest"]:
                    raise ValueError("bank_review_source_changed")
                if answer["choice"] == "needs_review":
                    raise ValueError("bank_review_individual_confirmation")
                proposed = from_answer(answer, request_id=request_id, approved_at=timestamp)
                prior = [decision for decision in decisions if decision.decision_id == proposed.decision_id]
                if prior and (len(prior) != 1 or prior[0].meaning() != proposed.meaning()):
                    raise ValueError("bank_review_decision_conflict")
                registration = plan_registration(answer, rules, approved_at=timestamp) if answer["future"] else None
                if registration and registration["state"] == "held":
                    raise ValueError(registration["reason"])
                prospective = rules + ([registration["rule"]] if registration and registration["state"] == "append" else [])
                for member in answer["snapshot"]["members"]:
                    tx = source.transactions.get(member["identity"])
                    if tx is None:
                        raise ValueError("bank_review_source_changed")
                    result = evaluate(tx, [*decisions, proposed], prospective, **source.legacy_rules)
                    if result["state"] == "held":
                        raise ValueError(result["reason"])
                    if result["state"] != "matched":
                        raise ValueError("bank_review_source_changed")
                # Defer all writes until every accepted item's second recheck.
                decision_update = None
                if not prior:
                    number = max(1, len(master.tables[DECISION_SHEET])) + 1 + len(edits[DECISION_SHEET])
                    decision_update = (number, proposed.row())
                elif not prior[0].active or answer["future"] and not prior[0].future:
                    existing = prior[0]
                    number = next(number for number, row in enumerate(master.tables[DECISION_SHEET], 1)
                                  if row and row[0] == existing.decision_id)
                    proposed = replace(proposed, revision=existing.revision + 1)
                    decision_update = (number, proposed.row())
                if decision_update:
                    edits[DECISION_SHEET].append(decision_update)
                    decisions = [decision for decision in decisions if decision.decision_id != proposed.decision_id] + [proposed]
                if registration and registration["state"] in {"append", "revive"}:
                    rule = registration["rule"]
                    if registration["state"] == "append":
                        number = max(1, len(master.tables[RULE_SHEET])) + 1 + sum(
                            number > len(master.tables[RULE_SHEET]) for number, _ in edits[RULE_SHEET])
                    else:
                        number = next(number for number, row in enumerate(master.tables[RULE_SHEET], 1)
                                      if row and row[0] == rule.rule_id)
                    edits[RULE_SHEET].append((number, rule.row()))
                    rules = [existing for existing in rules if existing.rule_id != rule.rule_id] + [rule]
                accepted.append((index, answer, bool(decision_update), registration))
            except ValueError as exc:
                counts["bank_held"] += 1
                _state(output[index], str(exc))
        if accepted:
            live = self.source_reader()
            live_groups = {group["key"]: group for group in live.groups}
            if any(key not in live_groups or live_groups[key]["membership_digest"] != answer["snapshot_digest"]
                   for _, answer, _, _ in accepted for key in [answer["group_key"]]) or digest(live.legacy_rules_serialized) != digest(source.legacy_rules_serialized):
                # Fail before any metadata mutation; a partial group approval
                # must not widen to whatever new members currently match.
                for index, _, _, _ in accepted:
                    _state(output[index], "bank_review_source_changed")
                counts["bank_held"] += len(accepted)
                return counts, output
            counts["bank_metadata_writes"] = self.store.commit(master, edits)
            for index, answer, written, registration in accepted:
                counts["bank_groups_confirmed" if written else "bank_groups_already_confirmed"] += 1
                if registration:
                    counts["bank_rules_already_registered" if registration["state"] == "already_registered"
                           else "bank_rules_registered"] += 1
                output[index][5] = False
                output[index][1] = str(output[index][1]).split("\n処理結果：")[0] + "\n処理結果：用途確認済み・記帳と原本を再確認"
        return counts, output
