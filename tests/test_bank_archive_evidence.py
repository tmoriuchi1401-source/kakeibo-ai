from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from app.auto_expense import expense_id
from app.aupay_card_recurring import SqliteRecurringRunState
from app.bank_income import income_id
from app.bank_pdf_pipeline import BankParseIssue, parse_jibun_bank_pages
from app.bank_pdf_recurring import ProtectedBankRecurringAuthorityProvider, run_bank_pdf_recurring
from app.bank_reconciliation import normalize_bank_description
from test_bank_pdf_pipeline import page, transaction_row
from test_bank_pdf_recurring import _Drive, _authority_file


@pytest.mark.parametrize("case", ["expense_saved", "income_saved", "source_conflict", "expense_conflict",
    "income_conflict", "unknown", "income_missing", "transfer", "explicit_review", "collision", "parser",
    "income_vs_current_transfer", "income_vs_current_transfer_mismatch", "income_typed_conflict", "expense_readback_changed"])
def test_completed_postings_are_distinct_from_unresolved_reclassification(tmp_path, monkeypatch, case):
    positive = case in {"income_saved", "income_conflict", "income_missing", "transfer", "income_vs_current_transfer", "income_vs_current_transfer_mismatch", "income_typed_conflict"}
    result = parse_jibun_bank_pages([page(1, transaction_row("2026/09/14", "利息" if case == "income_typed_conflict" else "分類未登録",
        credit="500" if positive else None, debit=None if positive else "500", balance="1000"))])
    tx = result.transactions[0]
    if case in {"parser", "collision"}:
        result = replace(result, issues=(BankParseIssue(1, 1, "identity_collision" if case == "collision" else "amount_invalid"),))
    imported = [tx.source_row_identity, "", tx.source, tx.source_row_identity, tx.transaction_date,
        tx.description, tx.signed_amount, "銀行口座", "bank_income" if positive else "auto_expense",
        "" if positive else expense_id(tx.source_row_identity), tx.source_row_hash, ""]
    income = [income_id(tx.source_row_identity), tx.transaction_date, 500, "その他確認済収入",
        tx.description, tx.account_alias, tx.source_row_identity, tx.source, "operator_confirmed_income", tx.source_row_hash]
    expense = [expense_id(tx.source_row_identity), tx.transaction_date, tx.description, "自動計上",
        500, "その他", "未分類", "銀行口座", tx.source, "", tx.source_row_identity, "", "active"]
    if case == "source_conflict": imported[6] = -501
    if case == "expense_conflict": expense[4] = 501
    if case in {"income_conflict", "income_vs_current_transfer_mismatch"}: income[2] = 501
    class DB:
        sid = "sheet"
        expense_reads = 0
        def get(self, rng):
            if rng == "取込データ!A2:L": return [] if case in {"unknown", "transfer"} else [imported]
            if rng == "収入明細!A2:J": return [income] if positive and case not in {"income_missing", "transfer"} else []
            if rng == "支出明細!A2:M":
                self.expense_reads += 1
                changed = list(expense)
                if case == "expense_readback_changed" and self.expense_reads > 1: changed[4] = 501
                return [changed] if not positive else []
            return []
        def append_raw(self, *a, **k): pytest.fail("unexpected sheet write")
    monkeypatch.setattr("app.bank_steady_state.BankPdfPipeline.parse", lambda *a, **k: result)
    monkeypatch.setattr("app.bank_pdf_recurring.subprocess.check_output", lambda *a, **k: "a" * 40)
    monkeypatch.setattr("app.bank_pdf_recurring.run_bank_recurring_production_batch",
        lambda *a, **k: pytest.fail("unexpected bank write"))
    key = (normalize_bank_description(tx.description), "incoming" if positive else "outgoing", tx.account_alias)
    rules = {"confirmed_internal_transfers": frozenset({key}) if case in {"transfer", "income_vs_current_transfer", "income_vs_current_transfer_mismatch"} else frozenset(),
             "confirmed_non_own_classifications": frozenset({(*key, "needs_review")}) if case == "explicit_review" else frozenset()}
    file = {"id": "pdf", "mimeType": "application/pdf", "parents": ["A" * 20],
        "modifiedTime": "2026-09-12T00:00:00Z", "appProperties": {}}
    drive = _Drive({"files": [file]})
    opts = dict(drive_service=drive, db=DB(),
        state=SqliteRecurringRunState(tmp_path / "state.sqlite3", repo_root=Path.cwd()),
        authority_provider=ProtectedBankRecurringAuthorityProvider(_authority_file(tmp_path), repo_root=Path.cwd()),
        repo_root=Path.cwd(), dry_run=False, processed_folder_id="B" * 20, download=lambda _: b"pdf", **rules)
    first = run_bank_pdf_recurring(**opts, now=datetime.fromisoformat("2026-09-14T12:00:00+09:00"))
    ready = case in {"expense_saved", "income_saved", "transfer"}
    assert first["failure"] == first["written"] == first["write_requests"] == 0
    assert first["files_processed"] == int(ready)
    if case in {"expense_saved", "income_saved"}:
        assert first["duplicate"] == first["review"] == first["review_resolved_existing"] == 1
        assert first["review_unresolved"] == 0
        assert first["unresolved_income"] == 0
    if ready: drive._files.response = {"files": []}
    second = run_bank_pdf_recurring(**opts, now=datetime.fromisoformat("2026-09-15T12:00:00+09:00"))
    assert second["written"] == second["write_requests"] == second["files_processed"] == 0
