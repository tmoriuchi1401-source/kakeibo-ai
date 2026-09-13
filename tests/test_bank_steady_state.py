from datetime import datetime, timezone
import json

import pytest

from app.bank_canary import BankBatchAuthority, build_bank_batch_plan
from app.canonical_one_row_production import (
    project_bank_bounded_batch,
    validate_canonical_five_row_batch,
)
from app.bank_pdf_pipeline import BankPdfResult, NormalizedBankTransaction
from app.bank_reconciliation import build_bank_preview_plan, build_bank_shadow_result
from app.bank_steady_state import (
    BankDailyPreview,
    BankDailySelectionManifest,
    build_bank_daily_preview,
    freeze_manifest,
    load_manifest,
)
from app.canonical_import import materialize_import_row


NOW = datetime(2026, 9, 13, tzinfo=timezone.utc)


def bank(description, amount, identity):
    return NormalizedBankTransaction(
        source="auじぶん銀行PDF",
        account_alias="test-account",
        transaction_date="2026-09-01",
        description=description,
        signed_amount=amount,
        source_page=1,
        source_row=1,
        source_row_identity=identity,
        source_row_hash=(identity.encode().hex() * 8)[:64],
        transaction_kind="deposit" if amount > 0 else "withdrawal",
    )


def parsed_for(*transactions):
    return BankPdfResult(
        pages=1,
        candidate_rows=len(transactions),
        transactions=tuple(transactions),
        issues=(),
        balance_consistency_failures=0,
        duplicate_candidates=0,
        canonical_transactions=tuple(item.to_canonical() for item in transactions),
    )


class DB:
    sid = "sheet-id"

    def __init__(self, rows=()):
        self.rows = list(rows)

    def get(self, range_name):
        assert range_name == "取込データ!A2:L"
        return self.rows


def test_daily_preview_separates_write_and_suppressed_taxonomy(monkeypatch):
    transactions = (
        bank("給与", 1000, "bank:income"),
        bank("口座振替 公共", -1000, "bank:expense"),
        bank("約定返済", -2000, "bank:loan"),
        bank("口座振替 AU PAY カード", -3000, "bank:card"),
        bank("振込 自口座", 4000, "bank:transfer"),
        bank("ATM 現金", -500, "bank:cash"),
        bank("振込 勤務先立替", 600, "bank:reimbursement"),
        bank("振込 例外", -700, "bank:review"),
    )
    monkeypatch.setattr(
        "app.bank_steady_state.BankPdfPipeline.parse",
        lambda *args, **kwargs: parsed_for(*transactions),
    )
    monkeypatch.setattr("app.bank_steady_state.pdf_digest", lambda path: "d" * 64)

    result = build_bank_daily_preview(
        DB(), "statement.pdf",
        target_spreadsheet_id="sheet-id",
        expected_git_head="a" * 40,
        confirmed_internal_transfers=frozenset({
            ("振込自口座", "incoming", "test-account"),
        }),
        confirmed_non_own_classifications=frozenset({
            ("振込勤務先立替", "incoming", "test-account", "reimbursement"),
            ("振込例外", "outgoing", "test-account", "needs_review"),
        }),
    )

    assert result.summary["new_income"] == 1
    assert result.summary["new_expense"] == 1
    assert result.summary["new_loan_repayment"] == 1
    assert result.summary["card_settlement_suppressed"] == 1
    assert result.summary["own_transfer_suppressed"] == 1
    assert result.summary["cash_withdrawal_suppressed"] == 1
    assert result.summary["reimbursement_suppressed"] == 1
    assert result.summary["operator_confirmed_non_own_review"] == 1
    assert result.summary["true_unknown"] == 0
    assert result.summary["total_proposed_writes"] == 3
    assert result.summary["write_attempted"] == 0


def test_daily_selection_manifest_is_external_exact_and_replayable(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    destination = tmp_path / "runtime" / "bank-selection.json"
    preview = BankDailyPreview(
        summary={},
        candidate_identities=("bank:1", "bank:2"),
        pdf_sha256="b" * 64,
        target_spreadsheet_id="sheet-id",
        expected_git_head="a" * 40,
    )

    created = freeze_manifest(destination, preview, repository_root=repository)
    assert load_manifest(destination) == created
    assert created.source_identities == preview.candidate_identities
    with pytest.raises(RuntimeError, match="manifest_changed"):
        freeze_manifest(
            destination,
            BankDailyPreview(
                summary={}, candidate_identities=("bank:3",),
                pdf_sha256="b" * 64, target_spreadsheet_id="sheet-id",
                expected_git_head="a" * 40,
            ),
            repository_root=repository,
        )


def test_all_duplicate_daily_preview_is_safe_noop(monkeypatch):
    transaction = bank("口座振替 公共", -1000, "bank:existing")
    monkeypatch.setattr(
        "app.bank_steady_state.BankPdfPipeline.parse",
        lambda *args, **kwargs: parsed_for(transaction),
    )
    monkeypatch.setattr("app.bank_steady_state.pdf_digest", lambda path: "e" * 64)
    existing = materialize_import_row(
        transaction.to_canonical(), imported_at=NOW, status="bank_expense",
    )

    result = build_bank_daily_preview(
        DB([existing]), "statement.pdf",
        target_spreadsheet_id="sheet-id",
        expected_git_head="a" * 40,
    )

    assert result.summary["existing_duplicate"] == 1
    assert result.summary["total_proposed_writes"] == 0
    assert result.summary["write_attempted"] == 0


def test_steady_state_authority_accepts_two_rows_and_rejects_over_100():
    transactions = (
        bank("給与", 1000, "bank:income"),
        bank("約定返済", -2000, "bank:loan"),
    )
    authority = BankBatchAuthority(
        selected_source_identities=tuple(item.source_row_identity for item in transactions),
        target_spreadsheet_id="sheet-id",
        min_rows=2,
        max_rows=2,
        authority_mode="bank_steady_state",
    )
    authority.validate()

    parsed = parsed_for(*transactions)
    shadow = build_bank_shadow_result(parsed, [])
    preview = build_bank_preview_plan(shadow, [])
    plan = build_bank_batch_plan(shadow, preview, authority)
    batch = project_bank_bounded_batch(plan)
    validate_canonical_five_row_batch(batch)
    assert batch.max_rows == 2
    assert {item.transaction_kind for item in batch.candidates} == {"income", "expense"}

    with pytest.raises(RuntimeError, match="row_bound_invalid"):
        BankBatchAuthority(
            selected_source_identities=tuple(f"bank:{i}" for i in range(101)),
            target_spreadsheet_id="sheet-id",
            min_rows=101,
            max_rows=101,
            authority_mode="bank_steady_state",
        ).validate()


def test_steady_state_manifest_rejects_duplicate_identity():
    with pytest.raises(RuntimeError, match="duplicate_identity"):
        BankDailySelectionManifest(
            pdf_sha256="c" * 64,
            source_identities=("bank:1", "bank:1"),
            target_spreadsheet_id="sheet-id",
            expected_git_head="a" * 40,
        ).validate()
