from datetime import datetime, timezone

import pytest

from app.bank_canary import preview_bank_loan_repayments
from app.bank_pdf_pipeline import BankPdfResult, NormalizedBankTransaction
from app.bank_reconciliation import build_bank_preview_plan, build_bank_shadow_result
from app.canonical_import import materialize_import_row
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
        raise AssertionError("loan preview must not invoke writer")


def bank(description, identity, amount=-1000):
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


def loan_context(*extra):
    transactions = tuple(
        bank("約定返済", f"bank:loan:{number}") for number in range(4)
    ) + tuple(extra)
    shadow = build_bank_shadow_result(parsed_for(*transactions), [])
    return shadow, build_bank_preview_plan(shadow, [])


def test_four_loans_are_expense_preview_with_live_housing_category_and_no_write():
    shadow, preview = loan_context()
    db = ReadOnlyDB()

    result = preview_bank_loan_repayments(
        shadow, preview, db, imported_at=NOW,
    ).summary()

    assert result == {
        "selected": 4,
        "planned": 4,
        "write_eligible": 4,
        "canonical_expense_rows": 4,
        "existing_duplicate": 0,
        "ambiguous_collision": 0,
        "target_binding_valid": True,
        "target_header_valid": True,
        "expense_category": {"major": "住まい", "minor": "住宅ローン"},
        "category_authority_valid": True,
        "write_attempted": 0,
        "external_write_count": 0,
    }
    assert not db.writer_invoked


def test_atm_and_receipt_same_amount_never_enter_loan_or_expense_preview():
    atm = bank("ATM 現金引出", "bank:atm")
    loans = tuple(
        bank("約定返済", f"bank:loan:{number}") for number in range(4)
    )
    receipt = [
        "receipt:same-amount", "", "receipt", "receipt:same-amount",
        "2026-09-01", "ATM近隣店舗", 1000, "現金", "canonical_receipt",
        "", "receipt-hash", "",
    ]
    existing = parse_import_rows([receipt])
    parsed = parsed_for(*loans, atm)
    shadow = build_bank_shadow_result(parsed, existing)
    preview = build_bank_preview_plan(shadow, existing)

    assert "bank:atm" not in preview.candidate_identities
    result = preview_bank_loan_repayments(
        shadow, preview, ReadOnlyDB(), imported_at=NOW,
    )
    assert result.selected == 4


def test_loan_preview_fails_closed_without_existing_housing_category():
    shadow, preview = loan_context()

    with pytest.raises(RuntimeError, match="housing_category_unavailable"):
        preview_bank_loan_repayments(
            shadow, preview, ReadOnlyDB(categories=(("その他", "未分類"),)),
            imported_at=NOW,
        )


def test_loan_preview_requires_exact_expected_count():
    shadow, preview = loan_context()

    with pytest.raises(RuntimeError, match="expected_row_count_changed"):
        preview_bank_loan_repayments(
            shadow, preview, ReadOnlyDB(), imported_at=NOW, expected_rows=3,
        )


def test_existing_57_bank_rows_remain_duplicates_while_four_loans_are_new():
    existing_bank = tuple(
        bank("口座振替 公共サービス", f"bank:existing:{number}")
        for number in range(57)
    )
    loans = tuple(
        bank("約定返済", f"bank:loan:{number}") for number in range(4)
    )
    existing_rows = [
        materialize_import_row(
            transaction.to_canonical(), imported_at=NOW, status="bank_expense",
        )
        for transaction in existing_bank
    ]
    existing = parse_import_rows(existing_rows)
    parsed = parsed_for(*existing_bank, *loans)
    parsed = BankPdfResult(
        pages=parsed.pages,
        candidate_rows=parsed.candidate_rows,
        transactions=parsed.transactions,
        issues=parsed.issues,
        balance_consistency_failures=0,
        duplicate_candidates=57,
        canonical_transactions=tuple(item.to_canonical() for item in loans),
    )

    shadow = build_bank_shadow_result(parsed, existing)
    preview = build_bank_preview_plan(shadow, existing)

    assert preview.existing_duplicate == 57
    assert preview.new_plan_candidates == 4
    assert preview.withheld_by_classification == 0
