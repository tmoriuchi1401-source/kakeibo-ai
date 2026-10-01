from test_bank_income import bank
from test_bank_income_recurring import Rig
from app.auto_expense import expense_id
from app.canonical_import import materialize_import_row
from app.bank_income import income_id
from test_bank_income import imported
from datetime import datetime, timezone
from dataclasses import replace
from app.bank_pdf_pipeline import BankParseIssue
import pytest


class ApprovedMeanings:
    def __init__(self, identities):
        self.identities = set(identities)
        self.reads = 0
        self.before_guard = None
    def __call__(self, tx):
        if tx.source_row_identity not in self.identities:
            return {"state": "no_sheet_rule"}
        return {"state": "matched", "classification": "income", "income_category": "その他確認済収入"}
    def require_unchanged(self):
        self.reads += 1
        if self.before_guard:
            self.before_guard()


def setup(tmp_path, monkeypatch, *, two=False):
    rig = Rig(tmp_path, monkeypatch)
    first = bank("振込 未確定特典", seed="first")
    rig.add("first", first)
    identities = [first.source_row_identity]
    if two:
        second = bank("振込 未確定特典", seed="second")
        rig.add("second", second)
        identities.append(second.source_row_identity)
    for file in rig.drive._files.response["files"]:
        file["parents"] = ["A" * 20]
    return rig, ApprovedMeanings(identities)


def test_approved_income_uses_recurring_authority_posts_once_then_moves_only_after_readback(tmp_path, monkeypatch):
    rig, meanings = setup(tmp_path, monkeypatch, two=True)
    first = rig.run(meaning_resolver=meanings, processed_folder_id="B" * 20)
    assert first["status"] == "complete", first
    assert first["income_created"] == first["deposit_imports_created"] == first["files_processed"] == 2
    assert meanings.reads >= 4
    assert all(file["parents"] == ["B" * 20] for file in rig.drive._files.response["files"])
    before = list(rig.db.writes)
    # The actual inbox query excludes files moved to processed.
    rig.drive._files.response["files"] = []
    second = rig.run(meaning_resolver=meanings, processed_folder_id="B" * 20)
    assert second["status"] == "noop", second
    assert second["income_created"] == second["deposit_imports_created"] == second["files_processed"] == 0
    assert rig.db.writes == before and not rig.expense_calls


def test_original_changes_after_preview_stop_before_any_financial_append_or_move(tmp_path, monkeypatch):
    rig, meanings = setup(tmp_path, monkeypatch)
    def changed():
        rig.drive._files.response["files"][0]["modifiedTime"] = "2026-09-14T02:01:00Z"
    meanings.before_guard = changed
    result = rig.run(meaning_resolver=meanings, processed_folder_id="B" * 20)
    assert result["status"] == "failed", result
    assert result["failure_reason"] == "bank_meaning_original_changed_before_apply"
    assert not rig.db.writes and not rig.drive._files.updated and not rig.expense_calls


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("outside", [True, False])
@pytest.mark.parametrize("saved", ["missing", "mismatched", "exact"])
def test_approved_duplicate_expense_requires_exact_ledger_in_preview_and_archive(tmp_path, monkeypatch, dry_run, outside, saved):
    rig = Rig(tmp_path, monkeypatch)
    tx = bank("振込 用途確認済み", seed="expense", amount=-500, transaction_kind="withdrawal")
    rig.add("expense", tx)
    file = rig.drive._files.response["files"][0]
    file["parents"] = ["A" * 20]
    if outside:
        file["modifiedTime"] = "2026-09-01T00:00:00Z"
    row = materialize_import_row(tx.to_canonical(), imported_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
                                status="auto_expense", target_id=expense_id(tx.source_row_identity))
    rig.db.tables["取込データ"] = [row]
    expense = [expense_id(tx.source_row_identity), tx.transaction_date, tx.description,
               "", 500 if saved == "exact" else 501, "その他", "未分類", "銀行口座", tx.source,
               "", tx.source_row_identity, "", "active"]
    rig.db.tables["支出明細"] = [] if saved == "missing" else [expense]
    prior_get = rig.db.get
    rig.db.get = lambda rng: rig.db.tables["支出明細"] if rng == "支出明細!A2:M" else prior_get(rng)
    meanings = ApprovedMeanings([tx.source_row_identity])
    def resolve(current):
        return {"state": "matched", "classification": "expense", "income_category": ""}
    class ExpenseMeanings:
        __call__ = staticmethod(resolve)
        require_unchanged = meanings.require_unchanged
    result = rig.run(meaning_resolver=ExpenseMeanings(), processed_folder_id="B" * 20, dry_run=dry_run)
    assert result["status"] in {"noop", "dry_run_noop"}, result
    assert result["duplicate"] == 1 and result["new_eligible"] == 0
    assert result["unresolved_expense"] == int(saved != "exact")
    assert result["files_processed"] == int(saved == "exact")
    assert result["files_withheld"] == int(saved != "exact")
    if saved != "exact":
        assert "unresolved_expense" in result["file_statuses"][0]["reasons"]
    assert not rig.db.writes and not rig.expense_calls
    assert len(rig.drive._files.updated) == int(saved == "exact" and not dry_run)


@pytest.mark.parametrize("purpose", ["transfer", "reimbursement", "other_nonwrite"])
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("history", ["none", "nonposting", "review", "income_label", "orphan_income", "orphan_expense", "mismatch"])
def test_nonposting_approval_cannot_hide_prior_review_or_financial_history(tmp_path, monkeypatch, purpose, dry_run, history):
    rig = Rig(tmp_path, monkeypatch)
    tx = bank("振込 用途確認済み", seed="nonposting")
    rig.add("nonposting", tx)
    rig.drive._files.response["files"][0]["parents"] = ["A" * 20]
    status = {"review": "needs_review", "income_label": "bank_income"}.get(history, "bank_non_expense")
    imported = materialize_import_row(tx.to_canonical(), imported_at=datetime(2026, 9, 1, tzinfo=timezone.utc), status=status)
    if history == "mismatch":
        imported[6] += 1
    rig.db.tables["取込データ"] = [] if history in {"none", "orphan_income", "orphan_expense"} else [imported]
    if history == "orphan_income":
        rig.db.tables["収入明細"].append([income_id(tx.source_row_identity), tx.transaction_date,
            tx.signed_amount, "その他確認済収入", tx.description, tx.account_alias,
            tx.source_row_identity, tx.source, "operator_confirmed_income", tx.source_row_hash])
    rig.db.tables["支出明細"] = [[expense_id(tx.source_row_identity), tx.transaction_date,
        tx.description, "", 1000, "その他", "未分類", "銀行口座", tx.source, "",
        tx.source_row_identity, "", "active"]] if history == "orphan_expense" else []
    prior_get = rig.db.get
    rig.db.get = lambda rng: rig.db.tables["支出明細"] if rng == "支出明細!A2:M" else prior_get(rng)
    class Nonposting:
        def __call__(self, current):
            return {"state": "matched", "classification": purpose, "income_category": ""}
        def require_unchanged(self): pass
    before = {key: list(value) for key, value in rig.db.tables.items()}
    result = rig.run(meaning_resolver=Nonposting(), processed_folder_id="B" * 20, dry_run=dry_run)
    ready = history in {"none", "nonposting"}
    assert result["unresolved_nonposting"] == int(not ready), result
    assert result["files_withheld"] == int(not ready) and result["files_processed"] == int(ready), result
    if history == "none" and not dry_run:
        assert [sheet for sheet, _ in rig.db.writes] == ["取込データ"]
        assert rig.db.tables["取込データ"][0][8] == "bank_non_expense"
        assert rig.db.tables["収入明細"] == before["収入明細"]
    else:
        assert not rig.db.writes and rig.db.tables == before
    assert not rig.expense_calls and result.get("income_created", 0) == result["written"] == 0
    assert len(rig.drive._files.updated) == int(ready and not dry_run)
    if not ready:
        assert "unresolved_nonposting" in result["file_statuses"][0]["reasons"]
    if ready and not dry_run:
        writes = list(rig.db.writes)
        rig.drive._files.response["files"] = []
        again = rig.run(meaning_resolver=Nonposting(), processed_folder_id="B" * 20)
        assert again["written"] == again["income_created"] == again["files_processed"] == 0
        assert rig.db.writes == writes


def test_financial_history_appearing_after_preview_stops_final_archive(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    tx = bank("振込 用途確認済み", seed="late-financial-history")
    rig.add("nonposting", tx)
    rig.drive._files.response["files"][0]["parents"] = ["A" * 20]
    rig.db.tables["取込データ"] = [materialize_import_row(tx.to_canonical(),
        imported_at=datetime(2026, 9, 1, tzinfo=timezone.utc), status="bank_non_expense")]
    rig.db.tables["支出明細"] = []
    prior_get = rig.db.get
    rig.db.get = lambda rng: [] if rng == "支出明細!A2:M" else prior_get(rng)
    class Nonposting:
        def __call__(self, current):
            return {"state": "matched", "classification": "transfer", "income_category": ""}
        def require_unchanged(self):
            if len(rig.db.tables["収入明細"]) == 1:
                rig.db.tables["収入明細"].append([income_id(tx.source_row_identity), tx.transaction_date,
                    tx.signed_amount, "その他確認済収入", tx.description, tx.account_alias,
                    tx.source_row_identity, tx.source, "operator_confirmed_income", tx.source_row_hash])
    result = rig.run(meaning_resolver=Nonposting(), processed_folder_id="B" * 20)
    assert result["files_processed"] == 0 and result["files_withheld"] == 1
    assert not rig.db.writes and not rig.drive._files.updated and not rig.expense_calls
    assert "archive_readback_failed" in result["file_statuses"][0]["reasons"]


@pytest.mark.parametrize("dry_run", [True, False])
def test_saved_bank_income_missing_ledger_cannot_bypass_outside_write_window(tmp_path, monkeypatch, dry_run):
    rig, meanings = setup(tmp_path, monkeypatch)
    tx = rig.documents["first"][0]
    rig.db.tables["取込データ"] = [imported(tx)]
    rig.drive._files.response["files"][0]["modifiedTime"] = "2026-09-01T00:00:00Z"
    result = rig.run(meaning_resolver=meanings, processed_folder_id="B" * 20, dry_run=dry_run)
    assert result["files_withheld"] == result["outside_write_window"] == 1, result
    assert result.get("income_created", 0) == result["written"] == 0, result
    assert result["planned_income_writes"] == 0 and result["income_scope_withheld"] == 1
    assert not rig.db.writes and not rig.drive._files.updated


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("fault", ["review", "parse_issue", "balance", "collision", "mismatch", "unseen"])
def test_saved_income_settlement_obeys_original_holds(tmp_path, monkeypatch, dry_run, fault):
    rig, meanings = setup(tmp_path, monkeypatch)
    tx = rig.documents["first"][0]
    rig.db.tables["取込データ"] = [imported(tx)]
    expected_reason = {
        "review": "transaction_review", "parse_issue": "parse_issue", "balance": "balance_consistency_failure",
        "collision": "parse_issue", "mismatch": "existing_content_mismatch",
    }.get(fault)
    if fault == "review":
        rig.documents["first"] += (bank("振込 本当の未確定出金", seed="unknown-outgoing",
            amount=-800, transaction_kind="withdrawal"),)
    if fault == "mismatch":
        rig.db.tables["取込データ"][0][6] += 1
    if fault == "unseen":
        rig.drive._files.response["files"] = []
    if fault in {"parse_issue", "balance", "collision"}:
        from app.bank_pdf_pipeline import BankPdfPipeline
        parser = BankPdfPipeline.parse
        def faulty(parser_self, path, **kwargs):
            result = parser(parser_self, path, **kwargs)
            return replace(result,
                issues=(BankParseIssue(1, 1, "identity_collision" if fault == "collision" else "amount_invalid"),)
                    if fault != "balance" else (),
                balance_consistency_failures=int(fault == "balance"))
        monkeypatch.setattr("app.bank_steady_state.BankPdfPipeline.parse", faulty)
    result = rig.run(meaning_resolver=meanings, processed_folder_id="B" * 20, dry_run=dry_run)
    assert result["planned_income_writes"] == result["written"] == 0, result
    assert result["income_scope_withheld"] >= 1 and result["files_processed"] == 0, result
    assert result["files_withheld"] == int(fault != "unseen"), result
    if expected_reason:
        assert expected_reason in result["file_statuses"][0]["reasons"], result
    assert not rig.db.writes and not rig.drive._files.updated and not rig.expense_calls


def test_saved_approved_income_inside_window_is_completed_once_without_reimport(tmp_path, monkeypatch):
    rig, meanings = setup(tmp_path, monkeypatch)
    tx = rig.documents["first"][0]
    rig.db.tables["取込データ"] = [imported(tx)]
    original_imports = [list(row) for row in rig.db.tables["取込データ"]]
    result = rig.run(meaning_resolver=meanings, processed_folder_id="B" * 20)
    assert result["income_created"] == result["files_processed"] == result["duplicate"] == 1, result
    assert result["deposit_imports_created"] == result["income_scope_withheld"] == 0
    assert rig.db.tables["取込データ"] == original_imports
    assert [sheet for sheet, _ in rig.db.writes] == ["収入明細"]
    writes = list(rig.db.writes)
    rig.drive._files.response["files"] = []
    again = rig.run(meaning_resolver=meanings, processed_folder_id="B" * 20)
    assert again["income_created"] == again["written"] == again["files_processed"] == 0
    assert rig.db.writes == writes


def test_mixed_window_income_only_eligible_original_settles_and_replay_is_zero(tmp_path, monkeypatch):
    rig, meanings = setup(tmp_path, monkeypatch, two=True)
    held_tx, eligible_tx = rig.documents["first"][0], rig.documents["second"][0]
    rig.db.tables["取込データ"] = [imported(held_tx), imported(eligible_tx)]
    rig.drive._files.response["files"][0]["modifiedTime"] = "2026-09-01T00:00:00Z"
    result = rig.run(meaning_resolver=meanings, processed_folder_id="B" * 20)
    assert result["income_created"] == result["income_scope_withheld"] == 1, result
    assert result["files_processed"] == result["files_withheld"] == 1, result
    assert result["duplicate"] == 2 and result["deposit_imports_created"] == 0
    assert [row[6] for row in rig.db.tables["収入明細"][1:]] == [eligible_tx.source_row_identity]
    assert rig.drive._files.response["files"][0]["parents"] == ["A" * 20]
    assert rig.drive._files.response["files"][1]["parents"] == ["B" * 20]
    writes = list(rig.db.writes)
    rig.drive._files.response["files"] = [rig.drive._files.response["files"][0]]
    again = rig.run(meaning_resolver=meanings, processed_folder_id="B" * 20)
    assert again["income_created"] == again["written"] == again["files_processed"] == 0
    assert again["income_scope_withheld"] == again["files_withheld"] == 1
    assert rig.db.writes == writes and len(rig.drive._files.updated) == 1


def test_partial_meaning_answer_does_not_persist_unknown_income_as_review_import(tmp_path, monkeypatch):
    rig, meanings = setup(tmp_path, monkeypatch)
    tx = rig.documents["first"][0]
    unknown = bank("給与振込特典", seed="still-unknown-reward")
    rig.documents["first"] += (unknown,)
    result = rig.run(meaning_resolver=meanings, processed_folder_id="B" * 20)
    assert result["income_created"] == result["deposit_imports_created"] == result["income_scope_withheld"] == 1, result
    assert result["files_processed"] == 0 and result["files_withheld"] == 1
    assert [row[0] for row in rig.db.tables["取込データ"]] == [tx.source_row_identity]
    assert [row[6] for row in rig.db.tables["収入明細"][1:]] == [tx.source_row_identity]
    writes = list(rig.db.writes)
    again = rig.run(meaning_resolver=meanings, processed_folder_id="B" * 20)
    assert again["income_created"] == again["deposit_imports_created"] == again["files_processed"] == 0
    assert rig.db.writes == writes and not rig.drive._files.updated
