from datetime import datetime, timezone

import pytest

from app.aupay_card_writer import TargetBinding
from app.bank_canary import (
    BANK_LOAN_ROWS,
    BankBatchAuthority,
    build_bank_batch_plan,
    dry_run_bank_batch,
)
from app.bank_loan_manifest import (
    BankLoanExactManifest,
    load_bank_loan_manifest,
    write_bank_loan_manifest,
)
from app.bank_pdf_pipeline import BankPdfResult, NormalizedBankTransaction
from app.bank_reconciliation import build_bank_preview_plan, build_bank_shadow_result
from app.canonical_import import materialize_import_row
from app.canonical_one_row_production import (
    SealedCanonicalOneRowTransport,
    project_bank_loan_repayment_batch,
)
from app.reconciliation import parse_import_rows
from app.sheets import HEADERS


NOW = datetime(2026, 9, 13, 0, 0, tzinfo=timezone.utc)


class ReadOnlyDB:
    sid = "sheet-id"

    def __init__(self, *, rows=(), categories=(("住まい", "住宅ローン"),)):
        self.rows = [list(row) for row in rows]
        self._categories = list(categories)
        self.writer_invoked = False

    def get(self, range_name):
        if range_name == "取込データ!A1:L1":
            return [HEADERS["取込データ"]]
        if range_name == "取込データ!A2:L":
            return self.rows
        if range_name == "カテゴリ!A2:B":
            return [list(value) for value in self._categories]
        raise AssertionError(range_name)

    def categories(self):
        return list(self._categories)

    def append(self, *_args, **_kwargs):
        self.writer_invoked = True
        raise AssertionError("phase 11 must not invoke writer")


def loan(identity, *, amount=-1000, description="約定返済"):
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
        transaction_kind="withdrawal" if amount < 0 else "deposit",
    )


def context(transactions, existing=()):
    parsed = BankPdfResult(
        pages=1,
        candidate_rows=len(transactions),
        transactions=tuple(transactions),
        issues=(),
        balance_consistency_failures=0,
        duplicate_candidates=0,
        canonical_transactions=tuple(item.to_canonical() for item in transactions),
    )
    existing_transactions = parse_import_rows(list(existing))
    shadow = build_bank_shadow_result(parsed, existing_transactions)
    return shadow, build_bank_preview_plan(shadow, existing_transactions)


def authority(identities):
    return BankBatchAuthority(
        selected_source_identities=tuple(identities),
        target_spreadsheet_id="sheet-id",
        min_rows=BANK_LOAN_ROWS,
        max_rows=BANK_LOAN_ROWS,
        authority_mode="bank_loan_repayment_preparation",
    )


def four_loans():
    return tuple(loan(f"bank:loan:{index}") for index in range(BANK_LOAN_ROWS))


def test_exact_four_loan_manifest_projects_expense_through_transport_without_write():
    transactions = four_loans()
    shadow, preview = context(transactions)
    plan = build_bank_batch_plan(
        shadow, preview,
        authority(item.source_row_identity for item in transactions),
    )
    db = ReadOnlyDB()

    result = dry_run_bank_batch(plan, db, imported_at=NOW)
    projected = project_bank_loan_repayment_batch(plan)
    transport = SealedCanonicalOneRowTransport(
        db,
        binding=TargetBinding(expected_spreadsheet_id="sheet-id"),
        inspector=None,
        key_provider=None,
        journal=None,
        clock=lambda: NOW,
        max_rows=BANK_LOAN_ROWS,
    )

    assert result.summary() == {
        "authority_mode": "bank_loan_repayment_preparation",
        "selected": 4,
        "planned": 4,
        "authorized": 4,
        "withheld": 0,
        "existing_duplicate": 0,
        "ambiguous_collision": 0,
        "target_binding_valid": True,
        "target_header_valid": True,
        "selected_identities_absent": True,
        "max_writes": 4,
        "income": 0,
        "expense": 0,
        "loan_repayment": 4,
        "projected_expense": 4,
        "write_attempted": 0,
        "external_write_count": 0,
    }
    assert {candidate.transaction_kind for candidate in projected.candidates} == {"expense"}
    assert {candidate.import_status for candidate in projected.candidates} == {
        "bank_loan_repayment",
    }
    assert len(transport.prepare_rows(projected.candidates, imported_at=NOW)) == 4
    with pytest.raises(RuntimeError, match="protected_audit_key_provider_required"):
        transport.write_batch_once(projected, None)
    assert not db.writer_invoked


@pytest.mark.parametrize("count", [3, 5])
def test_loan_manifest_three_or_five_rows_is_rejected(count):
    with pytest.raises(
        RuntimeError,
        match="row_bound_invalid|exactly_four|production_authority_forbidden",
    ):
        BankBatchAuthority(
            selected_source_identities=tuple(f"bank:loan:{i}" for i in range(count)),
            target_spreadsheet_id="sheet-id",
            min_rows=count,
            max_rows=count,
            authority_mode="bank_loan_repayment_preparation",
        ).validate()


def test_loan_duplicate_or_collision_rejects_whole_manifest():
    identities = ["bank:loan:0", "bank:loan:1", "bank:loan:1", "bank:loan:3"]
    with pytest.raises(RuntimeError, match="duplicate_identity"):
        authority(identities).validate()

    transactions = four_loans()
    existing = [materialize_import_row(
        transactions[0].to_canonical(), imported_at=NOW,
        status="bank_loan_repayment",
    )]
    shadow, preview = context(transactions, existing=existing)
    with pytest.raises(RuntimeError, match="candidate_duplicate_or_collision"):
        build_bank_batch_plan(
            shadow, preview,
            authority(item.source_row_identity for item in transactions),
        )

    collision_source = (transactions[0], loan("bank:loan:0", amount=-2000), *transactions[1:])
    shadow, preview = context(collision_source)
    with pytest.raises(RuntimeError, match="selector_multiple_matches"):
        build_bank_batch_plan(
            shadow, preview,
            authority(item.source_row_identity for item in transactions),
        )


def test_private_loan_manifest_round_trip_is_external_exact_and_one_shot(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    destination = tmp_path / "private" / "loan-manifest.json"
    manifest = BankLoanExactManifest(
        created_at=NOW,
        source_identities=tuple(f"bank:loan:{i}" for i in range(4)),
        target_spreadsheet_id="sheet-id",
    )

    write_bank_loan_manifest(destination, manifest, repository_root=repository)

    assert load_bank_loan_manifest(destination) == manifest
    with pytest.raises(FileExistsError):
        write_bank_loan_manifest(destination, manifest, repository_root=repository)
    with pytest.raises(RuntimeError, match="outside_repository"):
        write_bank_loan_manifest(
            repository / "loan-manifest.json", manifest,
            repository_root=repository,
        )


def test_loan_manifest_order_controls_payload_without_changing_exact_set_authority():
    transactions = tuple(reversed(four_loans()))
    shadow, preview = context(transactions)
    selected = tuple(reversed(tuple(
        item.source_row_identity for item in transactions
    )))

    plan = build_bank_batch_plan(shadow, preview, authority(selected))

    assert tuple(item.transaction.identity for item in plan.items) == selected
    assert frozenset(preview.candidate_identities) == frozenset(selected)
