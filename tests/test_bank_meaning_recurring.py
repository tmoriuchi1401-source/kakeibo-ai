from test_bank_income import bank
from test_bank_income_recurring import Rig
from app.auto_expense import expense_id
from app.canonical_import import materialize_import_row
from datetime import datetime, timezone
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
