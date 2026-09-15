"""Deposit persistence and income replay within the existing bank recurring run.

Uses the existing recurring SQLite summaries for append intent/read-back evidence;
no additional database, classifier, scheduler, or per-transaction approval files.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import os
import sqlite3
import subprocess
from uuid import uuid4

from .bank_income import BankIncomePipeline, INCOME_SHEET, deposit_decisions, validate_income_rows
from .canonical_import import materialize_import_row
from .reconciliation import parse_import_rows
from .sheets import HEADERS


def require_income_actions(repo_root, expected_head):
    workflow = os.environ.get("GITHUB_WORKFLOW_REF", "")
    allowed = {f"tmoriuchi1401-source/kakeibo-ai/.github/workflows/{name}@refs/heads/main"
               for name in ("bank-pdf-recurring.yml", "kakeibo-production.yml")}
    if (os.environ.get("GITHUB_ACTIONS") != "true" or workflow not in allowed
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_SHA") != expected_head
            or subprocess.check_output(["git", "rev-parse", "origin/main"], cwd=repo_root, text=True).strip() != expected_head):
        raise RuntimeError("bank_income_recurring_requires_main_actions_lock")


def _rows(db, sheet):
    if sheet == INCOME_SHEET:
        return list(validate_income_rows(db.get(f"{sheet}!A2:J")).values())
    result = []
    for raw in db.get("取込データ!A2:L"):
        if not any(raw):
            continue
        row = (["" if v is None else v for v in raw] + [""] * 12)[:12]
        result.append(row)
    return result


def _exact_present(db, sheet, expected):
    actual = _rows(db, sheet)
    for row in expected:
        matches = [r for r in actual if r[0] == row[0]]
        if matches != [row]:
            raise RuntimeError("bank_income_recurring_append_reconciliation_required")


class IncomeAppendEvidence:
    """Append intent in the already persisted bank-recurring.sqlite3 summaries."""
    def __init__(self, db, state, summary):
        self.db, self.state, self.summary = db, state, summary

    def _record(self, status, attempt, sheet, rows):
        self.state.record({**self.summary, "run_id": str(uuid4()), "status": status,
            "income_attempt": attempt, "income_target": self.db.sid,
            "income_sheet": sheet, "income_rows": rows}, advance_checkpoint=False)

    def require_existing_history(self):
        with sqlite3.connect(self.state.path) as connection:
            known = connection.execute("SELECT 1 FROM recurring_runs WHERE status IN "
                "('complete','noop','dry_run_ready','dry_run_noop','income_append_pending','income_append_verified') LIMIT 1").fetchone()
        if not known:
            raise RuntimeError("bank_income_recurring_existing_state_required")

    def reconcile(self, *, dry_run):
        with sqlite3.connect(self.state.path) as connection:
            events = [json.loads(row[0]) for row in connection.execute(
                "SELECT summary_json FROM recurring_runs WHERE status IN ('income_append_pending','income_append_verified') ORDER BY rowid")]
        pending = {}
        for event in events:
            if event["income_target"] != self.db.sid or event["income_sheet"] not in {INCOME_SHEET, "取込データ"}:
                raise RuntimeError("bank_income_recurring_evidence_target_mismatch")
            if event["status"] == "income_append_pending":
                pending[event["income_attempt"]] = event
            else:
                pending.pop(event["income_attempt"], None)
        for attempt, event in pending.items():
            _exact_present(self.db, event["income_sheet"], event["income_rows"])
            if not dry_run:
                self._record("income_append_verified", attempt, event["income_sheet"], event["income_rows"])

    def append(self, sheet, rows):
        if not rows:
            return
        attempt = str(uuid4())
        self._record("income_append_pending", attempt, sheet, rows)
        self.db.append_raw(sheet, rows)
        _exact_present(self.db, sheet, rows)
        self._record("income_append_verified", attempt, sheet, rows)


class BankRecurringIncome:
    def __init__(self, db, policy, state, summary, now, rules):
        policy.validate()
        if not policy.income_enabled or db.sid != policy.expected_spreadsheet_id:
            raise RuntimeError("bank_income_recurring_authority_required")
        self.db, self.policy, self.now, self.rules = db, policy, now, rules
        self.evidence = IncomeAppendEvidence(db, state, summary)
        self.new_imports = {}
        self.projected_income = []
        self.initial_imports = db.get("取込データ!A2:L")
        self.existing = {row.import_id: row for row in parse_import_rows(self.initial_imports)}
        self.pipeline = BankIncomePipeline(db, **rules)
        self.pipeline._existing(required=True)
        self.new_review = 0

    def _scope(self, source, alias):
        if (source, alias) not in self.policy.income_accounts:
            raise RuntimeError("bank_income_recurring_account_not_authorized")

    def collect(self, daily, *, held):
        parsed = daily.parsed_result
        if parsed is None or parsed.issues or parsed.balance_consistency_failures:
            raise RuntimeError("bank_income_recurring_pdf_unresolved")
        decisions, _ = deposit_decisions(parsed.transactions, **self.rules)
        for decision in decisions:
            tx = decision.transaction
            if decision.reason in {"invalid_bank_deposit", "bank_identity_collision"}:
                raise RuntimeError("bank_income_recurring_deposit_invalid")
            self._scope(tx.source, tx.account_alias)
            status = "bank_income" if decision.outcome == "confirmed_income" and not held else (
                "needs_review" if held or decision.outcome == "needs_review" else "bank_non_expense")
            canonical = tx.to_canonical()
            canonical = replace(canonical, memo=canonical.memo + ";income_assessment=" + decision.reason
                                + (";file_review_hold" if held else ""))
            row = materialize_import_row(canonical, imported_at=self.now, status=status)
            existing = self.existing.get(row[0])
            if existing:
                # PDF page/row and import time can differ across overlapping PDFs.
                if (existing.source, existing.date, existing.merchant, existing.amount, str(existing.row[10])) != (
                        tx.source, tx.transaction_date, tx.description, tx.signed_amount, tx.source_row_hash):
                    raise RuntimeError("bank_income_recurring_existing_import_conflict")
                continue  # Existing review/link/status remains authoritative.
            previous = self.new_imports.get(row[0])
            if previous:
                if previous[:1] + previous[2:11] != row[:1] + row[2:11]:
                    raise RuntimeError("bank_income_recurring_overlap_conflict")
                continue
            self.new_imports[row[0]] = row
            self.new_review += status == "needs_review"

    def plan(self, expense_ids):
        if self.new_imports and self.db.get("取込データ!A1:L1") != [HEADERS["取込データ"]]:
            raise RuntimeError("bank_income_recurring_import_header_mismatch")
        owner = self
        class ProjectedDB:
            def get(self, rng):
                if rng == "取込データ!A2:L":
                    return owner.initial_imports + list(owner.new_imports.values())
                return owner.db.get(rng)
            def sheet_titles(self):
                return owner.db.sheet_titles()
        plan = BankIncomePipeline(ProjectedDB(), **self.rules).preview()
        if plan["conflicting_import_ids"] or plan["duplicate_import_rows"]:
            raise RuntimeError("bank_income_recurring_existing_conflict")
        self.projected_income = plan["planned_rows"]
        for row in self.projected_income:
            self._scope(row[7], row[5])
        identities = set(expense_ids) | set(self.new_imports) | {row[6] for row in self.projected_income}
        if len(identities) > self.policy.max_rows:
            raise RuntimeError("bank_recurring_shared_row_bound_exceeded")
        return {"planned_income_writes": len(self.projected_income),
                "planned_deposit_imports": len(self.new_imports), "income_existing": plan["existing_income"],
                "planned_deposit_reviews": self.new_review,
                "income_review_pending": plan["classification"]["needs_review"]["count"],
                "bounded_rows": len(identities)}

    def apply(self):
        # No other writer in this Actions lock may replace the approved payload.
        if self.db.get("取込データ!A2:L") != self.initial_imports:
            raise RuntimeError("bank_income_recurring_imports_changed")
        self.evidence.append("取込データ", list(self.new_imports.values()))
        owner = self
        class GuardedDB:
            sid = owner.db.sid
            def get(self, rng):
                return owner.db.get(rng)
            def sheet_titles(self):
                return owner.db.sheet_titles()
            def append_raw(self, sheet, rows):
                if sheet != INCOME_SHEET or rows != owner.projected_income:
                    raise RuntimeError("bank_income_recurring_payload_changed")
                owner.evidence.append(sheet, deepcopy(rows))
        expected = {row[0]: row for row in self.projected_income}
        created = 0
        if expected:
            result = BankIncomePipeline(GuardedDB(), **self.rules).apply(
                tuple(row[6] for row in self.projected_income), income_write_enabled=True,
                approved_spreadsheet_id=self.policy.expected_spreadsheet_id, max_rows=self.policy.max_rows)
            created = result["incomes_created"]
            actual = self.pipeline._existing(required=True)
            if any(actual.get(key) != row for key, row in expected.items()):
                raise RuntimeError("bank_income_recurring_final_readback_failed")
        return {"income_created": created, "deposit_imports_created": len(self.new_imports),
                "deposit_reviews_saved": self.new_review}
