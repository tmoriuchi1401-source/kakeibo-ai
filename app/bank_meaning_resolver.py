"""Compiled, read-only bank meaning lookup for ingestion and income replay."""
from collections import defaultdict

from .bank_meaning_rules import RULE_SHEET
from .bank_review_decisions import DECISION_SHEET, evaluate
from .bank_review_store import BankReviewStore


class BankMeaningResolver:
    def __init__(self, rules, decisions, *, legacy_rules, store=None, master=None):
        self.rules = tuple(rule.validate() for rule in rules)
        self.decisions = tuple(decision.validate() for decision in decisions)
        self.legacy_rules = legacy_rules
        self.store, self.master = store, master
        self.by_identity = defaultdict(list)
        self.by_description = defaultdict(list)
        for decision in self.decisions:
            if decision.active:
                for member in decision.proof["members"]:
                    self.by_identity[member["identity"]].append(decision)
        for rule in self.rules:
            if not rule.active:
                continue
            if not any(decision.active and decision.future and decision.group_key == rule.approved_group_key
                       and decision.meaning() == rule.meaning() and decision.proof["condition"] == rule.condition()
                       for decision in self.decisions):
                raise ValueError("bank_meaning_rule_approval_missing")
            self.by_description[(rule.bank, rule.account_alias, rule.description)].append(rule)

    def __call__(self, tx):
        parts = tx.source_row_identity.split(":")
        bank = parts[1] if len(parts) >= 4 and parts[0] == "bankpdf" else ""
        return evaluate(tx, self.by_identity.get(tx.source_row_identity, ()),
            self.by_description.get((bank, tx.account_alias, tx.description), ()), **self.legacy_rules)

    def require_unchanged(self):
        if self.store is None or self.master is None:
            raise ValueError("bank_meaning_live_master_required")
        if self.store.read().fingerprint != self.master.fingerprint:
            raise ValueError("bank_meaning_master_changed_before_apply")


class BankMeaningGuardedDB:
    """Recheck the bank master at the existing sealed append's dispatch point.

    The canonical transport and its capability, journal and lease gates are
    unchanged. Only its final Sheets request gets an additional bank guard.
    """
    def __init__(self, db, resolver):
        self._db = db
        guard = resolver.require_unchanged

        class Request:
            def __init__(self, request):
                self.request = request
            def execute(self, *args, **kwargs):
                guard()
                return self.request.execute(*args, **kwargs)

        class Values:
            def __init__(self, values):
                self.values = values
            def __getattr__(self, name):
                return getattr(self.values, name)
            def append(self, **kwargs):
                if (kwargs.get("spreadsheetId") != db.sid
                        or kwargs.get("range") != "取込データ!A:L"):
                    raise ValueError("bank_meaning_write_target_invalid")
                return Request(self.values.append(**kwargs))

        class Spreadsheets:
            def __init__(self, spreadsheets):
                self.spreadsheets = spreadsheets
            def __getattr__(self, name):
                return getattr(self.spreadsheets, name)
            def values(self):
                return Values(self.spreadsheets.values())

        class Service:
            def spreadsheets(self):
                return Spreadsheets(db.svc.spreadsheets())

        self.svc = Service()

    def __getattr__(self, name):
        return getattr(self._db, name)


def load(db, *, legacy_rules):
    store = BankReviewStore(db)
    master = store.read()
    return BankMeaningResolver(master.records(RULE_SHEET), master.records(DECISION_SHEET),
                               legacy_rules=legacy_rules, store=store, master=master)


def from_environment(db, *, legacy_rules, env=None):
    import os
    env = os.environ if env is None else env
    flag = env.get("BANK_REVIEW_ENABLED", "false")
    if flag not in {"", "false", "true"}:
        raise ValueError("bank_review_enabled_flag_invalid")
    return load(db, legacy_rules=legacy_rules) if flag == "true" else None
