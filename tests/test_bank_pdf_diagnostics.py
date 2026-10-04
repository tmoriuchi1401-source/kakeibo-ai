from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from app.bank_pdf_diagnostics import diagnose_pdf
from app.bank_pdf_recurring import BankPdfWindow
from app.bank_pdf_pipeline import BankPdfResult, BankParseIssue, NormalizedBankTransaction, SOURCE
from app.canonical_import import materialize_import_row


def transaction():
    return NormalizedBankTransaction(SOURCE, "test-account", "2026-09-01", "SECRET_DESCRIPTION",
        -100, 1, 2, "bankpdf:au-jibun:test-account:" + "a" * 24, "a" * 64, "withdrawal")


class ReadOnlyDB:
    sid = "sheet"
    def __init__(self, imports=()): self.imports = list(imports)
    def get(self, key): return self.imports if key == "取込データ!A2:L" else []


def report(monkeypatch, *, rows=(), issues=(), balance=0):
    tx = transaction()
    parsed = BankPdfResult(1, 1, (tx,), issues, balance, 0, (tx.to_canonical(),))
    monkeypatch.setattr("app.bank_steady_state.BankPdfPipeline.parse", lambda *a, **k: parsed)
    monkeypatch.setattr("app.bank_steady_state.pdf_digest", lambda p: "d" * 64)
    return diagnose_pdf(ReadOnlyDB(rows), "unused.pdf", file={"id": "fixed-file",
        "modifiedTime": "2026-09-01T00:00:00Z"},
        window=BankPdfWindow(datetime.fromisoformat("2026-09-30T00:00:00Z"),
            datetime.fromisoformat("2026-09-30T01:00:00Z")),
        rules=dict(confirmed_internal_transfers=frozenset(), confirmed_non_own_classifications=frozenset()))


def saved_row():
    return materialize_import_row(transaction().to_canonical(), imported_at=datetime.now(timezone.utc),
        status="auto_expense", target_id="SECRET_TARGET")


def test_diagnosis_separates_existing_review_from_matching_content(monkeypatch):
    result = report(monkeypatch, rows=[saved_row()])
    assert result["existing_duplicate"] == result["review_existing_exact"] == 1
    assert result["true_unknown"] == 1
    assert result["duplicate_mismatch"] == 0
    assert result["transactions"][0]["existing_exact"]
    assert result["reasons"] == ["transaction_review"]
    payload = json.dumps(result)
    for secret in ("SECRET_DESCRIPTION", "SECRET_TARGET", transaction().source_row_identity):
        assert secret not in payload


@pytest.mark.parametrize("column,value", [(4, "2026-01-01"), (6, -101), (10, "b" * 64)])
def test_identity_match_is_not_content_match(monkeypatch, column, value):
    row = saved_row()
    row[column] = value
    result = report(monkeypatch, rows=[row])
    assert result["existing_duplicate"] == 1
    assert result["duplicate_mismatch"] == 1
    assert result["review_existing_exact"] == 0
    assert "existing_content_mismatch" in result["reasons"]


def test_diagnosis_keeps_parse_and_balance_failures_visible(monkeypatch):
    result = report(monkeypatch, issues=(BankParseIssue(1, 3, "amount_non_positive"),), balance=1)
    assert not result["parse_ok"]
    assert result["issues"] == [{"page": 1, "row": 3, "reason": "amount_non_positive"}]
    assert result["balance_consistency_failures"] == 1
    assert set(result["reasons"]) == {"parse_issue", "balance_consistency_failure", "transaction_review"}


def test_unrecognized_import_status_cannot_leak_content(monkeypatch):
    row = saved_row()
    row[8] = "PRIVATE_CUSTOM_STATUS"
    result = report(monkeypatch, rows=[row])
    assert result["transactions"][0]["existing_status"] == "unrecognized_status"
    assert "PRIVATE_CUSTOM_STATUS" not in json.dumps(result)
