from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.bank_canary import (
    BankCanaryAuthority,
    BankCanaryPreparationPipeline,
    build_bank_canary_plan,
    dry_run_bank_canary,
    validate_bank_canary_plan,
)
from app.bank_pdf_pipeline import BankPdfResult, NormalizedBankTransaction
from app.bank_reconciliation import build_bank_preview_plan, build_bank_shadow_result
from app.reconciliation import parse_import_rows
from app.sheets import HEADERS


NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def bank(description, amount=-1000, identity="bank:1"):
    return NormalizedBankTransaction(
        source="auじぶん銀行PDF",
        account_alias="test-account",
        transaction_date="2026-09-01",
        description=description,
        signed_amount=amount,
        source_page=1,
        source_row=1,
        source_row_identity=identity,
        source_row_hash="a" * 64,
        transaction_kind="withdrawal" if amount < 0 else "deposit",
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


def import_row(transaction, *, merchant=None):
    canonical = transaction.to_canonical()
    return [
        canonical.identity, "", canonical.source, canonical.source_record_id,
        canonical.transaction_date,
        canonical.merchant if merchant is None else merchant,
        canonical.amount_yen, canonical.payment_method, "bank_expense", "",
        canonical.source_hash, canonical.memo,
    ]


class ReadOnlyDB:
    sid = "sheet-id"

    def __init__(self, rows=(), header=None):
        self.rows = list(rows)
        self.header = list(HEADERS["取込データ"] if header is None else header)
        self.reads = []

    def get(self, range_name):
        self.reads.append(range_name)
        if range_name == "取込データ!A1:L1":
            return [self.header]
        if range_name == "取込データ!A2:L":
            return self.rows
        raise AssertionError(range_name)

    def append(self, *_args, **_kwargs):
        raise AssertionError("dry run invoked writer")

    def update_rows(self, *_args, **_kwargs):
        raise AssertionError("dry run invoked writer")


def context(*transactions, existing=()):
    existing_transactions = parse_import_rows(list(existing))
    shadow = build_bank_shadow_result(parsed_for(*transactions), existing_transactions)
    return shadow, build_bank_preview_plan(shadow, existing_transactions)


def authority(identity, **changes):
    values = {
        "selected_source_identity": identity,
        "target_spreadsheet_id": "sheet-id",
    }
    values.update(changes)
    return BankCanaryAuthority(**values)


@pytest.mark.parametrize(("transaction", "classification"), [
    (bank("給与 匿名勤務先", 1000, "bank:income"), "income"),
    (bank("口座振替 公共サービス", -1000, "bank:expense"), "expense"),
])
def test_eligible_income_and_direct_expense_make_one_row_plan(
    transaction, classification,
):
    shadow, preview = context(transaction)
    plan = build_bank_canary_plan(
        shadow, preview, authority(transaction.source_row_identity),
    )
    result = dry_run_bank_canary(plan, ReadOnlyDB(), imported_at=NOW)

    assert plan.classification == classification
    assert plan.write_eligibility == "eligible"
    assert len(plan.materialize(imported_at=NOW)) == 1
    assert result.summary() == {
        "authority_mode": "bank_canary_preparation",
        "classification": classification,
        "write_eligibility": "eligible",
        "target_worksheet": "取込データ",
        "planned": 1,
        "authorized": 1,
        "withheld": 0,
        "existing_duplicate": 0,
        "ambiguous_collision": 0,
        "target_binding_valid": True,
        "target_header_valid": True,
        "selected_identity_absent": True,
        "max_writes": 1,
        "external_write_count": 0,
    }


@pytest.mark.parametrize("transaction", [
    bank("口座振替 AU PAY カード", identity="bank:card"),
    bank("定額自動入金", 1000, "bank:transfer"),
    bank("約定返済", identity="bank:loan"),
    bank("ATM 現金引出", identity="bank:atm"),
    bank("不明摘要", identity="bank:review"),
])
def test_withheld_classifications_cannot_be_selected(transaction):
    shadow, preview = context(transaction)

    with pytest.raises(RuntimeError, match="classification_withheld"):
        build_bank_canary_plan(
            shadow, preview, authority(transaction.source_row_identity),
        )


def test_selector_zero_matches_fails_closed():
    transaction = bank("給与 匿名勤務先", 1000, "bank:income")
    shadow, preview = context(transaction)

    with pytest.raises(RuntimeError, match="selector_zero_matches"):
        build_bank_canary_plan(shadow, preview, authority("bank:missing"))


def test_selector_multiple_matches_and_identity_collision_fail_closed():
    first = bank("口座振替 サービスA", identity="bank:collision")
    second = bank("口座振替 サービスB", identity="bank:collision")
    shadow, preview = context(first, second)

    assert preview.ambiguous_collision == 1
    with pytest.raises(RuntimeError, match="selector_multiple_matches"):
        build_bank_canary_plan(shadow, preview, authority("bank:collision"))


def test_existing_identity_duplicate_selected_fails_before_plan():
    transaction = bank("口座振替 公共サービス", identity="bank:existing")
    existing = import_row(transaction)
    shadow, preview = context(transaction, existing=(existing,))

    with pytest.raises(RuntimeError, match="candidate_duplicate_or_collision"):
        build_bank_canary_plan(shadow, preview, authority("bank:existing"))


def test_pre_read_rejects_identity_that_appeared_after_planning():
    transaction = bank("口座振替 公共サービス", identity="bank:race")
    shadow, preview = context(transaction)
    plan = build_bank_canary_plan(shadow, preview, authority("bank:race"))

    with pytest.raises(RuntimeError, match="existing_identity_duplicate"):
        dry_run_bank_canary(
            plan, ReadOnlyDB(rows=(import_row(transaction),)), imported_at=NOW,
        )


def test_pre_read_rejects_conflicting_existing_identity():
    transaction = bank("口座振替 公共サービス", identity="bank:conflict")
    shadow, preview = context(transaction)
    plan = build_bank_canary_plan(shadow, preview, authority("bank:conflict"))

    with pytest.raises(RuntimeError, match="existing_identity_collision"):
        dry_run_bank_canary(
            plan,
            ReadOnlyDB(rows=(import_row(transaction, merchant="別内容"),)),
            imported_at=NOW,
        )


def test_target_header_mismatch_fails_closed():
    transaction = bank("給与 匿名勤務先", 1000)
    shadow, preview = context(transaction)
    plan = build_bank_canary_plan(shadow, preview, authority("bank:1"))

    with pytest.raises(RuntimeError, match="target_schema_mismatch"):
        dry_run_bank_canary(plan, ReadOnlyDB(header=["wrong"]), imported_at=NOW)


def test_source_identity_change_invalidates_plan():
    transaction = bank("給与 匿名勤務先", 1000, "bank:original")
    shadow, preview = context(transaction)
    plan = build_bank_canary_plan(shadow, preview, authority("bank:original"))
    changed = replace(
        plan,
        transaction=replace(plan.transaction, identity="bank:changed"),
    )

    with pytest.raises(RuntimeError, match="source_identity_changed"):
        validate_bank_canary_plan(changed)


def test_batch_greater_than_one_is_rejected_by_authority_and_plan():
    with pytest.raises(RuntimeError, match="max_rows_must_be_one"):
        authority("bank:1", max_rows=2).validate()

    transaction = bank("給与 匿名勤務先", 1000)
    shadow, preview = context(transaction)
    plan = build_bank_canary_plan(shadow, preview, authority("bank:1"))
    with pytest.raises(RuntimeError, match="exactly_one_row_required"):
        validate_bank_canary_plan(replace(plan, planned_rows=2))


def test_dry_run_replay_is_deterministic_and_writer_is_not_invoked():
    transaction = bank("給与 匿名勤務先", 1000)
    shadow, preview = context(transaction)
    plan = build_bank_canary_plan(shadow, preview, authority("bank:1"))
    db = ReadOnlyDB()

    first = dry_run_bank_canary(plan, db, imported_at=NOW).summary()
    second = dry_run_bank_canary(plan, db, imported_at=NOW).summary()

    assert first == second
    assert first["external_write_count"] == 0
    assert set(db.reads) == {"取込データ!A1:L1", "取込データ!A2:L"}


def test_pipeline_candidate_listing_exposes_only_stable_identities(monkeypatch):
    transactions = (
        bank("給与 匿名勤務先", 1000, "bank:income"),
        bank("ATM 現金引出", -1000, "bank:atm"),
    )
    db = ReadOnlyDB()
    monkeypatch.setattr(
        "app.bank_canary.BankPdfPipeline.parse",
        lambda *args, **kwargs: parsed_for(*transactions),
    )

    result = BankCanaryPreparationPipeline(db).candidate_identities("statement.pdf")

    assert result == {
        "selector": "stable_source_identity",
        "candidate_identity_count": 1,
        "candidate_identities": ["bank:income"],
        "external_write_count": 0,
    }
    assert "匿名勤務先" not in repr(result)


def test_pipeline_dry_run_uses_read_only_db_and_returns_counts(monkeypatch):
    transaction = bank("給与 匿名勤務先", 1000, "bank:income")
    db = ReadOnlyDB()
    monkeypatch.setattr(
        "app.bank_canary.BankPdfPipeline.parse",
        lambda *args, **kwargs: parsed_for(transaction),
    )

    result = BankCanaryPreparationPipeline(db).dry_run(
        "statement.pdf",
        selected_source_identity="bank:income",
        imported_at=NOW,
    )

    assert result["parsed"] == 1
    assert result["eligible_before_dedupe"] == 1
    assert result["new_plan_candidates"] == 1
    assert result["withheld_by_classification"] == 0
    assert result["planned"] == result["authorized"] == 1
    assert result["external_write_count"] == 0
