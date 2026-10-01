"""Immutable meaning decisions for explicitly captured transaction members.

These records are bank-only metadata, not import or income ledger rows. A
once-only answer can never authorize a future same-description transaction.
"""
from dataclasses import dataclass
from datetime import datetime
import json
import re

from .bank_income import INCOME_CATEGORIES
from .bank_review_groups import digest, encoded, safe_alias, safe_bank_reference, validate_snapshot

DECISION_SHEET = "銀行確認結果"
DECISION_HEADERS = ["decision ID", "固定group key", "固定snapshot digest", "固定snapshot",
    "判定結果", "収入分類", "自己口座相手銀行", "自己口座相手alias", "今後のルール保存",
    "受付ID", "承認日時", "revision", "active"]
MEANINGS = {"expense", "income", "transfer", "reimbursement", "other_nonwrite", "needs_review"}


def decision_id(answer):
    return "BD-" + digest({"group_key": answer["group_key"], "snapshot_digest": answer["snapshot_digest"]})


@dataclass(frozen=True)
class BankReviewDecision:
    decision_id: str
    group_key: str
    snapshot_digest: str
    snapshot_json: str
    classification: str
    income_category: str
    source_bank: str
    source_alias: str
    future: bool
    request_id: str
    approved_at: str
    revision: int = 1
    active: bool = True

    def validate(self):
        proof = validate_snapshot(self.snapshot_json, self.group_key, self.snapshot_digest)
        if self.decision_id != decision_id({"group_key": self.group_key, "snapshot_digest": self.snapshot_digest}):
            raise ValueError("bank_review_decision_id_invalid")
        if self.classification not in MEANINGS:
            raise ValueError("bank_review_decision_meaning_invalid")
        direction = proof["condition"]["direction"]
        if self.classification == "income" and direction != "incoming" or self.classification == "expense" and direction != "outgoing":
            raise ValueError("bank_review_decision_direction_invalid")
        if self.classification == "income":
            if self.income_category not in INCOME_CATEGORIES:
                raise ValueError("bank_review_decision_income_category_invalid")
        elif self.income_category:
            raise ValueError("bank_review_decision_income_not_applicable")
        if self.classification == "transfer":
            if not safe_alias(self.source_alias) or not safe_bank_reference(self.source_bank):
                raise ValueError("bank_review_decision_source_account_invalid")
            if (self.source_bank, self.source_alias) == (proof["condition"]["bank"], proof["condition"]["account_alias"]):
                raise ValueError("bank_review_decision_source_account_same")
        elif self.source_bank or self.source_alias:
            raise ValueError("bank_review_decision_source_account_not_applicable")
        if (type(self.future) is not bool or type(self.active) is not bool
                or type(self.revision) is not int or self.revision < 1):
            raise ValueError("bank_review_decision_revision_invalid")
        if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", self.request_id):
            raise ValueError("bank_review_decision_request_invalid")
        try:
            if datetime.fromisoformat(self.approved_at).tzinfo is None:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("bank_review_decision_time_invalid") from None
        return self

    @property
    def proof(self):
        self.validate()
        return json.loads(self.snapshot_json)

    def meaning(self):
        return (self.classification, self.income_category, self.source_bank, self.source_alias)

    def row(self):
        self.validate()
        return [self.decision_id, self.group_key, self.snapshot_digest, self.snapshot_json,
                *self.meaning(), self.future, self.request_id, self.approved_at, self.revision, self.active]

    def matches(self, tx):
        if not self.active:
            return False
        # Ledger settlement can change after approval. Only original source
        # values are compared here; live ledger consistency has separate gates.
        matches = [row for row in self.proof["members"] if row["identity"] == tx.source_row_identity]
        return len(matches) == 1 and all(matches[0][key] == getattr(tx, field)
            for key, field in (("identity", "source_row_identity"), ("source_hash", "source_row_hash"),
                ("source", "source"), ("account_alias", "account_alias"), ("date", "transaction_date"),
                ("description", "description"), ("signed_amount", "signed_amount"),
                ("transaction_kind", "transaction_kind")))


def from_answer(answer, *, request_id, approved_at):
    return BankReviewDecision(decision_id(answer), answer["group_key"], answer["snapshot_digest"],
        encoded(answer["snapshot"]), answer["choice"], answer["income_category"],
        answer["source_bank"], answer["source_alias"], answer["future"], request_id, approved_at).validate()


def boolean(value):
    if value is True or value == "TRUE":
        return True
    if value is False or value == "FALSE":
        return False
    raise ValueError("bank_review_boolean_invalid")


def parse_rows(rows):
    result = []
    seen = set()
    for row in rows:
        if not any(row):
            continue
        if len(row) != len(DECISION_HEADERS):
            raise ValueError("bank_review_decision_shape_invalid")
        from .bank_meaning_rules import _integer
        decision = BankReviewDecision(*row[:8], boolean(row[8]), *row[9:11],
            _integer(row[11]), boolean(row[12])).validate()
        if decision.decision_id in seen:
            raise ValueError("bank_review_decision_duplicate_id")
        seen.add(decision.decision_id)
        result.append(decision)
    return result


def evaluate(tx, decisions, rules, **legacy):
    """Compare all applicable one-time, future and legacy explicit meanings."""
    from .bank_meaning_rules import evaluate as evaluate_rules, legacy_meanings
    once = [decision for decision in decisions if decision.matches(tx)]
    future = evaluate_rules(tx, rules, **legacy)
    if future["state"] == "held":
        return future
    if not once:
        return future
    meanings = {decision.meaning() for decision in once}
    if future["state"] == "matched":
        meanings.add(tuple(future[key] for key in ("classification", "income_category", "source_bank", "source_alias")))
    old = legacy_meanings(tx, **legacy)
    if len(meanings) != 1 or len(old) > 1:
        return {"state": "held", "reason": "bank_review_decision_conflict"}
    meaning = next(iter(meanings))
    if old and old != {(meaning[0], meaning[1])}:
        return {"state": "held", "reason": "bank_meaning_legacy_conflict"}
    return {"state": "matched", "classification": meaning[0], "income_category": meaning[1],
            "source_bank": meaning[2], "source_alias": meaning[3],
            "decision_ids": tuple(sorted(decision.decision_id for decision in once)),
            "rule_ids": future.get("rule_ids", ())}
