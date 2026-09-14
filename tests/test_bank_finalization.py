from app.auto_expense import expense_id
from app.bank_finalization import (
    BANK_DUPLICATE_STATUS,
    BANK_NON_EXPENSE_STATUS,
    BANK_REVIEW_STATUS,
    BankFinalizationPipeline,
    validate_bank_finalization_canary,
)
from app.bank_pdf_pipeline import CHIBA_BANK_SOURCE, DOCOMO_SMTB_SOURCE, SOURCE


def import_row(import_id, source, merchant, amount, status, target=""):
    return [
        import_id, "", source, import_id, "2026-08-27", merchant, amount,
        "銀行口座", status, target, "hash", "",
    ]


def expense_row(expense_id_value, import_id, merchant="既存支出", amount=1000,
                status="active"):
    return [
        expense_id_value, "2026-08-27", merchant, "自動計上", amount,
        "その他", "未分類", "銀行口座", "test", "", import_id, "", status,
    ]


class FakeDB:
    def __init__(self, imports, expenses=()):
        self.imports = imports
        self.expenses = list(expenses)
        self.appended = []
        self.updated = {}

    def get(self, rng):
        if rng == "取込データ!A2:L":
            return self.imports
        if rng == "支出明細!A2:M":
            return self.expenses
        raise AssertionError(rng)

    def categories(self):
        return [("その他", "未分類"), ("住まい", "住宅ローン")]

    def ensure_expense_status_column(self):
        pass

    def append(self, sheet, rows):
        self.appended.extend((sheet, row) for row in rows)
        if sheet == "支出明細":
            self.expenses.extend([list(row) for row in rows])

    def update_rows(self, sheet, rows):
        self.updated.setdefault(sheet, []).extend(rows)
        target = self.imports if sheet == "取込データ" else self.expenses
        for row_num, row in rows:
            target[row_num - 2] = list(row)


def test_preview_routes_all_three_banks_without_posting_income_or_settlements():
    db = FakeDB([
        import_row("expense", SOURCE, "公共サービス", -1000, "bank_expense"),
        import_row("income", DOCOMO_SMTB_SOURCE, "給与", 2000, "bank_income"),
        import_row("loan", SOURCE, "約定返済(住宅)", -3000, "bank_loan_repayment"),
        import_row("settlement", DOCOMO_SMTB_SOURCE,
                   "口座振替 イオンフィナンシャルサービス", -4000, "bank_expense"),
        import_row("review", SOURCE, "口座振替 SMBC( ドコモSMTB", -5000,
                   "bank_expense"),
        import_row("done", CHIBA_BANK_SOURCE, "給食費", -6000, "auto_expense",
                   expense_id("done")),
    ], [expense_row(expense_id("done"), "done", "給食費", 6000)])

    result = BankFinalizationPipeline(db).preview()

    assert result["total_bank_rows"] == 6
    assert result["new_expense"] == 2
    assert result["new_income"] == 1
    assert result["non_expense"] == 1
    assert result["review"] == 1
    assert result["duplicate"] == 1
    assert db.appended == []
    assert db.updated == {}


def test_exact_existing_expense_is_linked_instead_of_double_posted():
    db = FakeDB([
        import_row("bank", SOURCE, "保険会社", -1000, "bank_expense"),
    ], [expense_row("existing", "card", "保険会社", 1000)])

    result = BankFinalizationPipeline(db).preview()

    assert result["excluded_link"] == 1
    assert result["new_expense"] == 0


def test_apply_exact_selection_posts_expense_and_preserves_income():
    db = FakeDB([
        import_row("expense", SOURCE, "公共サービス", -1000, "bank_expense"),
        import_row("income", SOURCE, "給与", 2000, "bank_income"),
    ])

    result = BankFinalizationPipeline(db).apply(("expense",))

    assert result["expenses_created"] == 1
    assert db.appended[0][1][0] == expense_id("expense")
    assert db.appended[0][1][4] == 1000
    assert db.updated["取込データ"][0][1][8] == "auto_expense"
    assert all(update[1][0] != "income" for update in db.updated["取込データ"])
    assert result["read_back_verified"] is True


def test_apply_routes_nonexpense_and_review_without_expense_rows():
    db = FakeDB([
        import_row("settlement", SOURCE, "口座振替 dカード", -1000, "bank_expense"),
        import_row("review", SOURCE, "口座振替 DF AUジブン", -2000, "bank_expense"),
    ])

    result = BankFinalizationPipeline(db).apply(
        ("settlement", "review"), max_rows=2,
    )

    assert result["expenses_created"] == 0
    statuses = {row[1][0]: row[1][8] for row in db.updated["取込データ"]}
    assert statuses == {
        "settlement": BANK_NON_EXPENSE_STATUS,
        "review": BANK_REVIEW_STATUS,
    }


def test_partial_write_replay_repairs_import_without_readding_expense():
    stable = expense_id("expense")
    db = FakeDB([
        import_row("expense", SOURCE, "公共サービス", -1000, "bank_expense"),
    ], [expense_row(stable, "expense", "公共サービス", 1000)])

    preview = BankFinalizationPipeline(db).preview()
    result = BankFinalizationPipeline(db).apply(("expense",))

    assert preview["duplicate"] == 1
    assert result["expenses_created"] == 0
    assert result["imports_updated"] == 1
    assert db.updated["取込データ"][0][1][8:10] == ["auto_expense", stable]


def test_ambiguous_duplicate_is_reviewed_not_linked():
    db = FakeDB([
        import_row("bank", SOURCE, "保険会社", -1000, "bank_expense"),
    ], [
        expense_row("existing-1", "card-1", "保険会社", 1000),
        expense_row("existing-2", "card-2", "保険会社", 1000),
    ])

    result = BankFinalizationPipeline(db).apply(("bank",))

    assert result["review"] == 1
    assert db.appended == []
    assert db.updated["取込データ"][0][1][8] == BANK_REVIEW_STATUS


def test_existing_link_status_replays_without_mutation():
    db = FakeDB([
        import_row("bank", SOURCE, "保険会社", -1000, BANK_DUPLICATE_STATUS,
                   "existing"),
    ])

    result = BankFinalizationPipeline(db).apply(("bank",))

    assert result["excluded_link"] == 1
    assert result["imports_updated"] == 0


def test_card_settlement_exact_variants_are_nonexpense():
    db = FakeDB([
        import_row("aeon", SOURCE, "口座振替 イオンフイナンシヤルサ-ビス",
                   -1000, "bank_expense"),
        import_row("dcard", SOURCE, "口座振替 dカード", -2000, "bank_expense"),
    ])

    result = BankFinalizationPipeline(db).preview()

    assert result["non_expense"] == 2
    assert result["new_expense"] == 0


def test_selected_preview_is_exact_and_read_only():
    db = FakeDB([
        import_row("expense", SOURCE, "公共サービス", -1000, "bank_expense"),
        import_row("income", SOURCE, "給与", 2000, "bank_income"),
    ])

    result = BankFinalizationPipeline(db).preview(("expense",))

    assert result["selected"] == 1
    assert result["total_bank_rows"] == 1
    assert result["new_expense"] == 1
    assert result["new_income"] == 0
    assert db.appended == []
    assert db.updated == {}


def test_apply_defaults_to_exact_one_canary_row():
    db = FakeDB([
        import_row("one", SOURCE, "公共サービス", -1000, "bank_expense"),
        import_row("two", SOURCE, "公共サービス2", -2000, "bank_expense"),
    ])

    import pytest
    with pytest.raises(RuntimeError, match="selection_bound_invalid"):
        BankFinalizationPipeline(db).apply(("one", "two"))


def test_canary_binding_requires_exact_identity_target_and_head():
    validate_bank_finalization_canary(
        ("bank:one",),
        target_spreadsheet_id="sheet-id",
        approved_target="sheet-id",
        current_head="a" * 40,
        expected_head="a" * 40,
    )

    import pytest
    with pytest.raises(RuntimeError, match="exactly_one_identity"):
        validate_bank_finalization_canary(
            ("bank:one", "bank:two"),
            target_spreadsheet_id="sheet-id", approved_target="sheet-id",
            current_head="a" * 40, expected_head="a" * 40,
        )
    with pytest.raises(RuntimeError, match="approved_target_mismatch"):
        validate_bank_finalization_canary(
            ("bank:one",),
            target_spreadsheet_id="sheet-id", approved_target="other",
            current_head="a" * 40, expected_head="a" * 40,
        )
    with pytest.raises(RuntimeError, match="expected_head_mismatch"):
        validate_bank_finalization_canary(
            ("bank:one",),
            target_spreadsheet_id="sheet-id", approved_target="sheet-id",
            current_head="a" * 40, expected_head="b" * 40,
        )
