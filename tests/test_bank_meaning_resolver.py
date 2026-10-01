from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.bank_income import BankIncomePipeline, INCOME_SHEET, classify_deposit, income_row_matches
from app.bank_meaning_resolver import BankMeaningGuardedDB, from_environment, load
from app.bank_meaning_rules import RULE_SHEET
from app.bank_pdf_recurring import _existing_income_settled
from app.bank_reconciliation import classify_bank_transaction, normalize_bank_description
from app.bank_review_decisions import DECISION_SHEET
from test_bank_income import DB, bank, imported
from test_bank_review_requests import prepared, processor, REQUEST


def approved(*, future=False, purpose="収入", category="その他確認済収入"):
    db, rows, source = prepared(future=future)
    rows[0][2:4] = [purpose, category]
    assert processor(db, source).process(rows, REQUEST)[0]["bank_groups_confirmed"] == 1
    return db, source, load(db, legacy_rules={})


def test_saved_once_meaning_used_by_both_classifiers_but_not_future_transaction():
    _, source, resolver = approved()
    tx = next(iter(source.transactions.values()))
    assert classify_bank_transaction(tx, meaning_resolver=resolver).classification == "income"
    assert classify_deposit(tx, meaning_resolver=resolver).category == "その他確認済収入"
    future = replace(tx, source_row_identity=tx.source_row_identity[:-24] + "b" * 24,
                     source_row_hash="b" * 64, transaction_date="2026-10-02")
    assert resolver(future)["state"] == "no_sheet_rule"
    assert classify_deposit(future, meaning_resolver=resolver).outcome == "needs_review"


def test_future_rule_reuses_exact_conditions_without_changing_identity():
    _, source, resolver = approved(future=True)
    tx = next(iter(source.transactions.values()))
    future = replace(tx, source_row_identity=tx.source_row_identity[:-24] + "b" * 24,
                     source_row_hash="b" * 64, transaction_date="2026-10-02")
    assert resolver(future)["state"] == "matched"
    assert resolver(future)["rule_ids"]
    assert future.source_row_identity.endswith("b" * 24)
    assert resolver(replace(future, description=future.description + " 別用途"))["state"] == "no_sheet_rule"


@pytest.mark.parametrize("change", ["amount", "date", "hash", "description", "source"])
def test_once_only_full_source_proof_cannot_match_altered_transaction(change):
    _, source, resolver = approved()
    tx = next(iter(source.transactions.values()))
    changes = {"amount": dict(signed_amount=tx.signed_amount + 1),
               "date": dict(transaction_date="2026-10-02"), "hash": dict(source_row_hash="b" * 64),
               "description": dict(description=tx.description + "変更"), "source": dict(source="bank:chiba")}
    assert resolver(replace(tx, **changes[change]))["state"] == "no_sheet_rule"


def test_legacy_conflict_holds_both_income_and_bank_review():
    db, source, _ = approved(future=True)
    tx = next(iter(source.transactions.values()))
    key = (normalize_bank_description(tx.description), "incoming", tx.account_alias)
    resolver = load(db, legacy_rules={"confirmed_internal_transfers": {key}})
    assert resolver(tx)["state"] == "held"
    assert classify_deposit(tx, meaning_resolver=resolver).outcome == "needs_review"
    assert classify_bank_transaction(tx, meaning_resolver=resolver).classification == "needs_review"


def test_transfer_approval_is_non_income_and_has_explicit_source_relation():
    db, rows, source = prepared(future=True)
    rows[0][2:4] = ["自己口座間振替", ""]
    rows[1][2:4] = ["千葉銀行", "chiba-primary"]
    assert processor(db, source).process(rows, REQUEST)[0]["bank_groups_confirmed"] == 1
    resolver = load(db, legacy_rules={})
    tx = next(iter(source.transactions.values()))
    assert resolver(tx)["source_alias"] == "chiba-primary"
    assert classify_deposit(tx, meaning_resolver=resolver).outcome == "transfer"
    assert classify_bank_transaction(tx, meaning_resolver=resolver).classification == "transfer"


def test_rule_without_live_group_approval_fails_closed():
    db, _, _ = approved(future=True)
    db.tables[DECISION_SHEET] = db.tables[DECISION_SHEET][:1]
    with pytest.raises(ValueError, match="approval_missing"):
        load(db, legacy_rules={})


def test_income_reason_change_preserves_valid_saved_posting_with_exact_financial_content():
    tx = bank(description="普通預金利息")
    historical = classify_deposit(tx).row()
    approved_row = historical.copy()
    approved_row[8] = "operator_confirmed_income"
    assert not income_row_matches(historical, approved_row)
    assert income_row_matches(historical, approved_row, approved_meaning=True)
    for column, value in {1: "2026-08-26", 2: 1001, 3: "その他確認済収入",
                          4: "別の摘要", 5: "other-primary", 9: "b" * 64}.items():
        changed = historical.copy()
        changed[column] = value
        try:
            assert not income_row_matches(changed, approved_row, approved_meaning=True)
        except RuntimeError:
            pass  # Invalid provenance is also fail-closed, never an exact match.


def test_saved_income_reason_change_is_no_write_and_archive_income_settled():
    _, source, resolver = approved(category="利息")
    tx = next(iter(source.transactions.values()))
    saved = classify_deposit(tx, meaning_resolver=resolver).row()
    saved[8] = "bank_interest_type"
    db = DB([imported(tx)])
    db.tables[INCOME_SHEET].append(saved)
    pipeline = BankIncomePipeline(db, meaning_resolver=resolver)
    assert pipeline.preview()["existing_income"] == 1
    for _ in range(2):
        assert pipeline.apply((tx.source_row_identity,), income_write_enabled=True,
                              approved_spreadsheet_id=db.sid)["incomes_created"] == 0
    daily = SimpleNamespace(parsed_result=SimpleNamespace(transactions=(tx,)))
    assert _existing_income_settled(db, daily, db.tables["取込データ"],
        confirmed_internal_transfers=(), confirmed_non_own_classifications=(), meaning_resolver=resolver)
    assert not db.writes and db.tables[INCOME_SHEET][-1] == saved


def test_unposted_approved_income_posts_once_then_replays_zero():
    _, source, resolver = approved()
    tx = next(iter(source.transactions.values()))
    db = DB([imported(tx)])
    pipeline = BankIncomePipeline(db, meaning_resolver=resolver)
    counts = [pipeline.apply((tx.source_row_identity,), income_write_enabled=True,
                            approved_spreadsheet_id=db.sid)["incomes_created"] for _ in range(2)]
    assert counts == [1, 0] and len(db.writes) == 1


def test_master_change_before_income_append_writes_zero():
    master_db, source, resolver = approved()
    tx = next(iter(source.transactions.values()))
    db = DB([imported(tx)])
    master_db.tables[DECISION_SHEET][-1][-1] = False
    with pytest.raises(ValueError, match="master_changed_before_apply"):
        BankIncomePipeline(db, meaning_resolver=resolver).apply((tx.source_row_identity,),
            income_write_enabled=True, approved_spreadsheet_id=db.sid)
    assert not db.writes


def test_final_canonical_request_rechecks_master_at_execute_not_request_creation():
    db, _, resolver = approved(future=True)
    requests = []
    class Request:
        def execute(self):
            requests.append("executed")
    class Service:
        def spreadsheets(self): return self
        def values(self): return self
        def append(self, **kwargs): return Request()
    db.svc = Service()
    guarded = BankMeaningGuardedDB(db, resolver)
    request = guarded.svc.spreadsheets().values().append(
        spreadsheetId=db.sid, range="取込データ!A:L", body={"values": [["synthetic"]]})
    db.tables[RULE_SHEET][-1][-2] = False
    with pytest.raises(ValueError, match="master_changed_before_apply"):
        request.execute()
    assert not requests


def test_feature_off_does_not_read_bank_master_and_invalid_flag_fails():
    assert from_environment(object(), legacy_rules={}, env={}) is None
    assert from_environment(object(), legacy_rules={}, env={"BANK_REVIEW_ENABLED": "false"}) is None
    with pytest.raises(ValueError, match="flag_invalid"):
        from_environment(object(), legacy_rules={}, env={"BANK_REVIEW_ENABLED": "True"})
