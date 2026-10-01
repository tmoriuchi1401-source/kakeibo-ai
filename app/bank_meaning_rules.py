"""Bank meaning rules in their own canonical sheet; no category schema changes.

Exact condition matches are evaluated together with legacy confirmed JSON
rules. Conflicting meanings have no priority order and always require review.
This module prepares rules; persistence belongs to the submitted-request
writer, with live source recheck and read-back, rather than routine ingestion.
"""
from dataclasses import dataclass
from datetime import datetime
import re

from .bank_income import INCOME_CATEGORIES
from .bank_review_groups import CONDITION_FIELDS, digest, encoded, safe_alias, validate_snapshot
from .bank_reconciliation import normalize_bank_description

RULE_SHEET = "銀行自動分類ルール"
RULE_HEADERS = ["rule ID", "銀行", "口座alias", "入出金方向", "normalized description", "原文摘要",
    "transaction kind", "counterparty", "金額条件", "判定結果", "収入分類", "自己口座相手銀行",
    "自己口座相手alias", "承認group key", "承認日時", "revision", "active", "適用件数"]
MEANINGS = {"expense", "income", "transfer", "reimbursement", "other_nonwrite", "needs_review"}


@dataclass(frozen=True)
class BankMeaningRule:
    rule_id: str
    bank: str
    account_alias: str
    direction: str
    normalized_description: str
    description: str
    transaction_kind: str
    counterparty: str
    amount_partition: int | None
    classification: str
    income_category: str
    source_bank: str
    source_alias: str
    approved_group_key: str
    approved_at: str
    revision: int = 1
    active: bool = True
    applied_count: int = 0

    def validate(self):
        if self.bank not in {"au-jibun", "docomo-smtb", "chiba"} or not safe_alias(self.account_alias):
            raise ValueError("bank_meaning_rule_account_invalid")
        if self.direction not in {"incoming", "outgoing"} or self.transaction_kind != (
                "deposit" if self.direction == "incoming" else "withdrawal"):
            raise ValueError("bank_meaning_rule_direction_invalid")
        if (not self.description.strip()
                or normalize_bank_description(self.description) != self.normalized_description):
            raise ValueError("bank_meaning_rule_description_invalid")
        if self.amount_partition is not None and (type(self.amount_partition) is not int or self.amount_partition <= 0):
            raise ValueError("bank_meaning_rule_amount_invalid")
        if (type(self.revision) is not int or self.revision < 1 or type(self.active) is not bool
                or type(self.applied_count) is not int or self.applied_count < 0):
            raise ValueError("bank_meaning_rule_revision_invalid")
        if self.classification not in MEANINGS or (
                self.classification == "income" and self.direction != "incoming") or (
                self.classification == "expense" and self.direction != "outgoing"):
            raise ValueError("bank_meaning_rule_result_invalid")
        if self.classification == "income":
            if self.income_category not in INCOME_CATEGORIES:
                raise ValueError("bank_meaning_rule_income_category_invalid")
        elif self.income_category:
            raise ValueError("bank_meaning_rule_income_category_not_applicable")
        if self.classification == "transfer":
            if (self.source_bank not in {"au-jibun", "docomo-smtb", "chiba"}
                    or not safe_alias(self.source_alias)
                    or (self.source_bank, self.source_alias) == (self.bank, self.account_alias)):
                raise ValueError("bank_meaning_rule_transfer_account_invalid")
        elif self.source_bank or self.source_alias:
            raise ValueError("bank_meaning_rule_transfer_not_applicable")
        try:
            if datetime.fromisoformat(self.approved_at).tzinfo is None:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("bank_meaning_rule_approval_time_invalid") from None
        if (self.rule_id != rule_id(self.condition())
                or self.approved_group_key != "bank-group:" + digest(self.condition())):
            raise ValueError("bank_meaning_rule_condition_key_invalid")
        return self

    def condition(self):
        return {field: getattr(self, field) for field in CONDITION_FIELDS}

    def meaning(self):
        return (self.classification, self.income_category, self.source_bank, self.source_alias)

    def row(self):
        self.validate()
        return [self.rule_id, self.bank, self.account_alias, self.direction,
            self.normalized_description, self.description, self.transaction_kind, self.counterparty,
            "" if self.amount_partition is None else self.amount_partition,
            self.classification, self.income_category, self.source_bank, self.source_alias,
            self.approved_group_key, self.approved_at, self.revision, self.active, self.applied_count]

    def matches(self, tx, *, counterparty=""):
        self.validate()
        # No PDF/file-ID/name condition and no edit to tx.source_row_identity.
        namespace = tx.source_row_identity.split(":")[1] if tx.source_row_identity.startswith("bankpdf:") else ""
        return self.active and (namespace, tx.account_alias,
            "incoming" if tx.signed_amount > 0 else "outgoing", normalize_bank_description(tx.description),
            tx.description, tx.transaction_kind, counterparty) == (
                self.bank, self.account_alias, self.direction, self.normalized_description,
                self.description, self.transaction_kind, self.counterparty) and (
                    self.amount_partition is None or abs(tx.signed_amount) == self.amount_partition)


def rule_id(conditions):
    return "BR-" + digest(conditions)


def from_answer(answer, *, approved_at):
    if answer["future"] is not True:
        raise ValueError("bank_meaning_rule_future_not_approved")
    validate_snapshot(encoded(answer["snapshot"]), answer["group_key"], answer["snapshot_digest"])
    conditions = answer["snapshot"]["condition"]
    rule = BankMeaningRule(rule_id=rule_id(conditions), **conditions,
        classification=answer["choice"], income_category=answer["income_category"],
        source_bank=answer["source_bank"], source_alias=answer["source_alias"],
        approved_group_key=answer["group_key"], approved_at=approved_at)
    return rule.validate()


def _integer(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    raise ValueError("bank_meaning_rule_integer_invalid")


def parse_rows(rows):
    result = []
    seen = set()
    for row in rows:
        if not any(row):
            continue
        if len(row) != len(RULE_HEADERS):
            raise ValueError("bank_meaning_rule_shape_invalid")
        active = row[16]
        if active is not True and active is not False and active not in ("TRUE", "FALSE"):
            raise ValueError("bank_meaning_rule_active_invalid")
        rule = BankMeaningRule(*row[:8], None if row[8] == "" else _integer(row[8]),
            *row[9:15], _integer(row[15]), active is True or active == "TRUE", _integer(row[17])).validate()
        if rule.rule_id in seen:
            raise ValueError("bank_meaning_rule_duplicate_id")
        seen.add(rule.rule_id)
        result.append(rule)
    return result


def legacy_meanings(tx, *, confirmed_internal_transfers=(), confirmed_non_own_classifications=()):
    key = (normalize_bank_description(tx.description),
           "incoming" if tx.signed_amount > 0 else "outgoing", tx.account_alias)
    meanings = {(meaning, ("利息" if key[0] in {"利息", "普通預金利息", "リソク"}
                  else "その他確認済収入") if meaning == "income" else "")
        for text, direction, alias, meaning in confirmed_non_own_classifications
        if (text, direction, alias) == key}
    if key in confirmed_internal_transfers:
        meanings.add(("transfer", ""))
    return meanings


def evaluate(tx, rules, *, counterparty="", **legacy):
    """Return an explicit meaning, no match, or conflict requiring review.

    Even identical Sheet matches are checked against all legacy exact rules.
    An explicit legacy review is a conflict, never silently overwritten.
    """
    matching = [rule for rule in rules if rule.matches(tx, counterparty=counterparty)]
    old = legacy_meanings(tx, **legacy)
    if not matching:
        return {"state": "no_sheet_rule"}
    meanings = {rule.meaning() for rule in matching}
    if len(meanings) != 1 or len(old) > 1:
        return {"state": "held", "reason": "bank_meaning_rule_conflict"}
    meaning = next(iter(meanings))
    if old and old != {(meaning[0], meaning[1])}:
        return {"state": "held", "reason": "bank_meaning_legacy_conflict"}
    return {"state": "matched", "classification": meaning[0], "income_category": meaning[1],
            "source_bank": meaning[2], "source_alias": meaning[3],
            "rule_ids": tuple(sorted(rule.rule_id for rule in matching))}


def plan_registration(answer, rules, *, approved_at):
    """Idempotent registration/revival proposal; active changes need review."""
    from dataclasses import replace
    proposed = from_answer(answer, approved_at=approved_at)
    existing = [rule for rule in rules if rule.rule_id == proposed.rule_id]
    if len(existing) > 1:
        return {"state": "held", "reason": "bank_meaning_rule_duplicate_id"}
    if not existing:
        return {"state": "append", "rule": proposed}
    current = existing[0].validate()
    if current.meaning() != proposed.meaning():
        return {"state": "held", "reason": "bank_meaning_rule_conflict"}
    if current.active:
        return {"state": "already_registered", "rule": current}
    return {"state": "revive", "rule": replace(proposed, revision=current.revision + 1,
        applied_count=current.applied_count)}
