from dataclasses import fields
from datetime import datetime, timezone

import pytest

from app.aupay_card_writer import TargetBinding
from app.bank_canary import (
    BANK_BATCH_ROWS,
    BankBatchAuthority,
    BankCanaryAuthority,
    build_bank_canary_plan,
    build_bank_batch_plan,
    dry_run_bank_batch,
)
from app.bank_pdf_pipeline import BankPdfResult, NormalizedBankTransaction
from app.bank_reconciliation import build_bank_preview_plan, build_bank_shadow_result
from app.canonical_one_row_production import (
    SealedCanonicalOneRowTransport,
    project_bank_canary_candidate,
)
from app.canonical_import import materialize_import_row
from app.reconciliation import parse_import_rows
from app.sheets import HEADERS


NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def corrupt_frozen(value, **changes):
    clone = object.__new__(type(value))
    for item in fields(value):
        object.__setattr__(
            clone, item.name, changes.get(item.name, getattr(value, item.name)),
        )
    return clone


class ReadOnlyDB:
    sid = "sheet-id"

    def __init__(self, rows=(), header=None):
        self.rows = [list(row) for row in rows]
        self.header = list(HEADERS["取込データ"] if header is None else header)
        self.writer_invoked = False

    def get(self, range_name):
        if range_name == "取込データ!A1:L1":
            return [self.header]
        if range_name == "取込データ!A2:L":
            return self.rows
        raise AssertionError(range_name)

    def append(self, *_args, **_kwargs):
        self.writer_invoked = True
        raise AssertionError("writer must not be invoked")


def bank(identity, amount):
    description = (
        f"給与 匿名勤務先 {identity}"
        if amount > 0 else f"口座振替 公共サービス {identity}"
    )
    return NormalizedBankTransaction(
        source="auじぶん銀行PDF",
        account_alias="test-account",
        transaction_date="2026-09-01",
        description=description,
        signed_amount=amount,
        source_page=1,
        source_row=1,
        source_row_identity=identity,
        source_row_hash=(identity.encode("utf-8").hex() * 4)[:64],
        transaction_kind="deposit" if amount > 0 else "withdrawal",
    )


def parsed_for(transactions):
    return BankPdfResult(
        pages=1,
        candidate_rows=len(transactions),
        transactions=tuple(transactions),
        issues=(),
        balance_consistency_failures=0,
        duplicate_candidates=0,
        canonical_transactions=tuple(item.to_canonical() for item in transactions),
    )


def batch_transactions():
    return (
        bank("bank:expense-1", -1000),
        bank("bank:expense-2", -2000),
        bank("bank:expense-3", -3000),
        bank("bank:income-1", 4000),
        bank("bank:income-2", 5000),
    )


def context(transactions, existing=()):
    existing_transactions = parse_import_rows(list(existing))
    shadow = build_bank_shadow_result(
        parsed_for(transactions), existing_transactions,
    )
    return shadow, build_bank_preview_plan(shadow, existing_transactions)


def authority(identities, **changes):
    values = {
        "selected_source_identities": tuple(identities),
        "target_spreadsheet_id": "sheet-id",
    }
    values.update(changes)
    return BankBatchAuthority(**values)


def make_plan(transactions=None, *, existing=(), selected=None):
    transactions = batch_transactions() if transactions is None else tuple(transactions)
    shadow, preview = context(transactions, existing=existing)
    selected = tuple(selected or [item.source_row_identity for item in transactions[:5]])
    return build_bank_batch_plan(shadow, preview, authority(selected))


def make_one_plan(transaction):
    shadow, preview = context((transaction,))
    return build_bank_canary_plan(
        shadow,
        preview,
        BankCanaryAuthority(
            selected_source_identity=transaction.source_row_identity,
            target_spreadsheet_id="sheet-id",
        ),
    )


def import_row(transaction):
    canonical = transaction.to_canonical()
    return materialize_import_row(
        canonical, imported_at=NOW, status="bank_expense",
    )


def test_exactly_five_explicit_identities_are_accepted_and_writer_is_not_called():
    transactions = batch_transactions()
    plan = make_plan(transactions)
    db = ReadOnlyDB()

    result = dry_run_bank_batch(plan, db, imported_at=NOW)

    assert len(plan.materialize(imported_at=NOW)) == BANK_BATCH_ROWS
    assert result.summary() == {
        "authority_mode": "bank_batch_preparation",
        "selected": 5,
        "planned": 5,
        "authorized": 5,
        "withheld": 0,
        "existing_duplicate": 0,
        "ambiguous_collision": 0,
        "target_binding_valid": True,
        "target_header_valid": True,
        "selected_identities_absent": True,
        "max_writes": 5,
        "income": 2,
        "expense": 3,
        "write_attempted": 0,
        "external_write_count": 0,
    }
    assert not db.writer_invoked


@pytest.mark.parametrize("count", [4, 6])
def test_non_five_selector_is_rejected(count):
    identities = [f"bank:{index}" for index in range(count)]
    with pytest.raises(RuntimeError, match="exactly_five_identities"):
        authority(identities).validate()


def test_duplicate_identity_in_selector_is_rejected():
    identities = ["bank:1", "bank:2", "bank:2", "bank:4", "bank:5"]
    with pytest.raises(RuntimeError, match="duplicate_identity"):
        authority(identities).validate()


def test_existing_duplicate_rejects_the_whole_batch():
    transactions = batch_transactions()
    with pytest.raises(RuntimeError, match="candidate_duplicate_or_collision"):
        make_plan(transactions, existing=(import_row(transactions[0]),))


def test_collision_among_selected_five_rejects_the_whole_batch():
    transactions = batch_transactions()
    collision = bank("bank:expense-1", -9999)
    source = (transactions[0], collision, *transactions[1:])
    with pytest.raises(RuntimeError, match="selector_multiple_matches"):
        make_plan(
            source,
            selected=tuple(item.source_row_identity for item in transactions),
        )


def test_withheld_classification_rejects_the_whole_batch():
    transactions = list(batch_transactions())
    transactions[0] = NormalizedBankTransaction(
        source="auじぶん銀行PDF",
        account_alias="test-account",
        transaction_date="2026-09-01",
        description="AU PAY カード",
        signed_amount=-1000,
        source_page=1,
        source_row=1,
        source_row_identity="bank:expense-1",
        source_row_hash="a" * 64,
        transaction_kind="withdrawal",
    )
    with pytest.raises(RuntimeError, match="classification_withheld"):
        make_plan(transactions)


def test_target_and_header_mismatch_reject_before_projection():
    plan = make_plan()
    wrong_target = corrupt_frozen(
        plan.authority, target_spreadsheet_id="other-sheet",
    )
    wrong_plan = corrupt_frozen(plan, authority=wrong_target)
    with pytest.raises(RuntimeError, match="target_spreadsheet_mismatch"):
        dry_run_bank_batch(wrong_plan, ReadOnlyDB(), imported_at=NOW)

    with pytest.raises(RuntimeError, match="target_schema_mismatch"):
        dry_run_bank_batch(
            plan,
            ReadOnlyDB(header=["wrong"]),
            imported_at=NOW,
        )


def test_max_rows_bypass_is_rejected():
    plan = make_plan()
    wrong_authority = corrupt_frozen(plan.authority, max_rows=6)
    wrong_plan = corrupt_frozen(plan, authority=wrong_authority)
    with pytest.raises(RuntimeError, match="rows_bound_must_be_five"):
        from app.bank_canary import validate_bank_batch_plan
        validate_bank_batch_plan(wrong_plan)


def test_replay_is_deterministic_and_transport_capacity_is_bounded():
    transactions = batch_transactions()
    plans = make_plan(transactions)
    first = dry_run_bank_batch(plans, ReadOnlyDB(), imported_at=NOW).summary()
    second = dry_run_bank_batch(plans, ReadOnlyDB(), imported_at=NOW).summary()
    assert first == second

    candidates = tuple(
        project_bank_canary_candidate(make_one_plan(transaction))
        for transaction in transactions
    )
    transport = SealedCanonicalOneRowTransport(
        ReadOnlyDB(),
        binding=TargetBinding(expected_spreadsheet_id="sheet-id"),
        inspector=None,
        key_provider=None,
        journal=None,
        clock=lambda: NOW,
        max_rows=5,
    )
    assert len(transport.prepare_rows(candidates, imported_at=NOW)) == 5
    with pytest.raises(RuntimeError, match="exact_batch_size"):
        transport.prepare_rows(candidates + (candidates[0],), imported_at=NOW)
