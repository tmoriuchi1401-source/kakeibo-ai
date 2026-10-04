"""Idempotent bank-rule application counts, never financial write authority."""
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
import re

from .bank_income import INCOME_CATEGORIES
from .bank_meaning_rules import RULE_SHEET, MEANINGS, _integer
from .bank_review_groups import digest, safe_alias, safe_bank_reference

USAGE_SHEET = "銀行ルール適用履歴"
USAGE_HEADERS = ["application ID", "rule ID", "初回revision", "transaction identity",
    "source hash", "初回原本SHA256", "判定結果", "収入分類", "自己口座相手銀行",
    "自己口座相手alias", "適用日時", "判定証拠digest"]


def application_id(rule_id, identity):
    # Revival does not count the same transaction again.
    return "BU-" + digest({"rule_id": rule_id, "identity": identity})


@dataclass(frozen=True)
class BankRuleApplication:
    application_id: str
    rule_id: str
    revision: int
    identity: str
    source_hash: str
    pdf_sha256: str
    classification: str
    income_category: str
    source_bank: str
    source_alias: str
    applied_at: str
    evidence_digest: str

    def meaning(self):
        return (self.classification, self.income_category, self.source_bank, self.source_alias)

    def validate(self):
        if (not re.fullmatch(r"BR-[0-9a-f]{64}", self.rule_id)
                or self.application_id != application_id(self.rule_id, self.identity)
                or type(self.revision) is not int or self.revision < 1):
            raise ValueError("bank_rule_application_key_invalid")
        if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                   for value in (self.source_hash, self.pdf_sha256, self.evidence_digest)):
            raise ValueError("bank_rule_application_proof_invalid")
        if not re.fullmatch(r"bankpdf:(?:au-jibun|chiba|docomo-smtb):[a-z][a-z0-9_-]{1,63}:"
                            + self.source_hash[:24] + r"(?::[0-9]{3})?", self.identity):
            raise ValueError("bank_rule_application_identity_invalid")
        if self.classification not in MEANINGS - {"needs_review"}:
            raise ValueError("bank_rule_application_meaning_invalid")
        if (self.classification == "income" and self.income_category not in INCOME_CATEGORIES
                or self.classification != "income" and self.income_category):
            raise ValueError("bank_rule_application_income_invalid")
        if self.classification == "transfer":
            if not safe_bank_reference(self.source_bank) or not safe_alias(self.source_alias):
                raise ValueError("bank_rule_application_account_invalid")
        elif self.source_bank or self.source_alias:
            raise ValueError("bank_rule_application_account_not_applicable")
        try:
            if datetime.fromisoformat(self.applied_at).tzinfo is None:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("bank_rule_application_time_invalid") from None
        return self

    def row(self):
        self.validate()
        return [self.application_id, self.rule_id, self.revision, self.identity,
            self.source_hash, self.pdf_sha256, *self.meaning(), self.applied_at, self.evidence_digest]


def parse_rows(rows):
    result, seen = [], set()
    for row in rows:
        if not any(row):
            continue
        if len(row) != len(USAGE_HEADERS):
            raise ValueError("bank_rule_application_shape_invalid")
        application = BankRuleApplication(*row[:2], _integer(row[2]), *row[3:]).validate()
        if application.application_id in seen:
            raise ValueError("bank_rule_application_duplicate_id")
        seen.add(application.application_id)
        result.append(application)
    return result


def plan_applications(source, resolver, master, *, applied_at):
    """Count exact live-original rule resolutions separately from settlement.

    A preview never commits this plan. Once-only answers, conflicts, inactive
    rules and needs_review do not count. History/count disagreement is held,
    rather than inventing past applications or silently resetting a count.
    """
    rules = {rule.rule_id: rule for rule in master.records(RULE_SHEET)}
    history = master.records(USAGE_SHEET)
    by_id = {entry.application_id: entry for entry in history}
    totals = Counter(entry.rule_id for entry in history)
    for entry in history:
        rule = rules.get(entry.rule_id)
        if rule is None or entry.revision > rule.revision or entry.meaning() != rule.meaning():
            raise ValueError("bank_rule_application_rule_conflict")
    if any(rule.applied_count != totals[rule_id] for rule_id, rule in rules.items()):
        raise ValueError("bank_rule_application_count_mismatch")
    candidates = []
    for identity, tx in source.transactions.items():
        meaning = resolver(tx)
        if (meaning["state"] == "matched" and meaning["classification"] != "needs_review"
                and meaning.get("rule_ids")):
            candidates.append((identity, tx, meaning))
    originals = {}
    if candidates:
        for file in source.files:
            sha = file["pdf_sha256"]
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise ValueError("bank_rule_application_original_invalid")
            for tx in file["parsed"].transactions:
                identity = tx.source_row_identity
                if identity in originals:
                    raise ValueError("bank_rule_application_original_collision")
                originals[identity] = (tx, sha)
    additions = []
    for identity, tx, meaning in sorted(candidates, key=lambda row: row[0]):
        if identity != tx.source_row_identity or identity not in originals or originals[identity][0] != tx:
            raise ValueError("bank_rule_application_original_unavailable")
        result = tuple(meaning[key] for key in ("classification", "income_category", "source_bank", "source_alias"))
        for rule_id in meaning["rule_ids"]:
            rule = rules.get(rule_id)
            if rule is None or not rule.matches(tx) or rule.meaning() != result:
                raise ValueError("bank_rule_application_rule_conflict")
            evidence = digest({"identity": identity, "source_hash": tx.source_row_hash,
                "source": tx.source, "account_alias": tx.account_alias, "date": tx.transaction_date,
                "description": tx.description, "signed_amount": tx.signed_amount,
                "transaction_kind": tx.transaction_kind, "rule_id": rule_id, "meaning": result})
            proposed = BankRuleApplication(application_id(rule_id, identity), rule_id, rule.revision,
                identity, tx.source_row_hash, originals[identity][1], *result, applied_at, evidence).validate()
            old = by_id.get(proposed.application_id)
            if old:
                if old.evidence_digest != evidence or old.source_hash != tx.source_row_hash or old.meaning() != result:
                    raise ValueError("bank_rule_application_existing_conflict")
                continue
            additions.append(proposed)
            by_id[proposed.application_id] = proposed
            totals[rule_id] += 1
    usage_start = len(master.tables[USAGE_SHEET]) + 1 if master.tables[USAGE_SHEET] else 2
    edits = {USAGE_SHEET: [(usage_start + index, entry.row()) for index, entry in enumerate(additions)], RULE_SHEET: []}
    for number, raw in enumerate(master.tables[RULE_SHEET][1:], 2):
        if not any(raw):
            continue
        rule = rules[raw[0]]
        if rule.applied_count != totals[rule.rule_id]:
            edits[RULE_SHEET].append((number, replace(rule, applied_count=totals[rule.rule_id]).row()))
    return edits, {"bank_rule_applications_new": len(additions), "bank_rule_counts_updated": len(edits[RULE_SHEET])}
