from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.aupay_card_recurring import SqliteRecurringRunState
from app.bank_pdf_pipeline import NormalizedBankTransaction, SOURCE
from app.bank_pdf_recurring import ProtectedBankRecurringAuthorityProvider, run_bank_pdf_recurring
from test_bank_pdf_recurring import _DB, _Drive, _authority_file, _parsed


@pytest.mark.parametrize("mode", ["match", "date", "amount", "hash", "source", "description",
    "record_id", "payment_method", "duplicate_rows", "unknown", "parse_issue", "balance", "income", "collision",
    "readback_amount", "readback_missing"])
def test_existing_identity_requires_content_match_and_all_archive_guards(tmp_path, monkeypatch, mode):
    monkeypatch.setattr("app.bank_pdf_recurring.subprocess.check_output", lambda *a, **k: "a" * 40)
    tx = NormalizedBankTransaction(SOURCE, "jibun-primary", "2026-09-14", "匿名振替", -500,
        1, 1, "bankpdf:au-jibun:jibun-primary:" + "a" * 24, "a" * 64, "withdrawal")
    row = [tx.source_row_identity, "", SOURCE, tx.source_row_identity, tx.transaction_date,
        tx.description, -500, "銀行口座", "bank_non_expense", "", tx.source_row_hash, ""]
    field = {"source": 2, "record_id": 3, "date": 4, "description": 5, "amount": 6,
             "payment_method": 7, "hash": 10}.get(mode)
    if field is not None: row[field] = -501 if field == 6 else "mismatch"
    db = _DB([row, row] if mode == "duplicate_rows" else [row])
    if mode.startswith("readback_"):
        class ChangedDB(_DB):
            calls = 0
            def get(self, range_name):
                if range_name != "取込データ!A2:L": return []
                self.calls += 1
                if self.calls == 1: return [row]
                changed = list(row)
                changed[6] = -501
                return [] if mode == "readback_missing" else [changed]
        db = ChangedDB()
    daily = SimpleNamespace(parsed_result=_parsed(transactions=(tx,),
        issues=("issue",) if mode == "parse_issue" else (), balance_failures=int(mode == "balance")),
        expense_candidate_identities=(), summary={"parsed": 1, "existing_duplicate": 1,
        "true_unknown": int(mode == "unknown"), "collision": int(mode == "collision")})
    monkeypatch.setattr("app.bank_pdf_recurring.build_bank_daily_preview", lambda *a, **k: daily)
    if mode == "income":
        monkeypatch.setattr("app.bank_pdf_recurring._existing_income_settled", lambda *a, **k: False)
    # Any bank write in a duplicate-only run is a test failure.
    monkeypatch.setattr("app.bank_pdf_recurring.run_bank_recurring_production_batch",
        lambda *a, **k: pytest.fail("unexpected bank write"))
    file = {"id": "fixed-pdf", "mimeType": "application/pdf", "parents": ["A" * 20],
            "modifiedTime": "2026-09-12T00:00:00Z", "appProperties": {}}
    drive = _Drive({"files": [file]})
    state = SqliteRecurringRunState(tmp_path / "state.sqlite3", repo_root=Path.cwd())
    provider = ProtectedBankRecurringAuthorityProvider(_authority_file(tmp_path), repo_root=Path.cwd())
    opts = dict(drive_service=drive, db=db, state=state, authority_provider=provider,
        repo_root=Path.cwd(), dry_run=False, processed_folder_id="B" * 20, download=lambda _: b"pdf")
    result = run_bank_pdf_recurring(**opts, now=datetime.fromisoformat("2026-09-14T12:00:00+09:00"))
    assert result["failure"] == 0
    assert result["written"] == result["write_requests"] == 0
    assert result["files_processed"] == int(mode == "match")
    status = result["file_statuses"][0]
    assert status["status"] == ("processed" if mode == "match" else "withheld")
    if field is not None or mode == "duplicate_rows":
        assert "existing_content_mismatch" in status["reasons"]
    elif mode.startswith("readback_"):
        assert "archive_readback_failed" in status["reasons"]
        return
    elif mode != "match":
        expected = {"unknown": "transaction_review", "parse_issue": "parse_issue",
            "balance": "balance_consistency_failure", "income": "unresolved_income", "collision": "collision"}
        assert expected[mode] in status["reasons"]
    # Repeat a held run or simulate the processed folder being absent from Inbox.
    if mode == "match": drive._files.response = {"files": []}
    replay = run_bank_pdf_recurring(**opts, now=datetime.fromisoformat("2026-09-15T12:00:00+09:00"))
    assert replay["written"] == replay["write_requests"] == 0
    assert replay["files_processed"] == 0
