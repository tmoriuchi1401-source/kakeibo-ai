import pytest

from app.auto_expense import AutoExpensePipeline, expense_id
from app.bank_pdf_pipeline import DOCOMO_SMTB_SOURCE
from app.bank_reconciliation import ASSET_FORMATION_IMPORT_STATUS
from app.expense_view import active_expenses


def import_row(import_id="p1", source="PayPay", merchant="テスト商店", amount=100,
               status="unclassified_paypay", note=""):
    return [import_id, "", source, import_id, "2026-08-16", merchant, amount,
            "通常払い", status, "", "hash", note]


class FakeDB:
    def __init__(self, rows, expenses=None):
        self.rows = rows
        self.expenses = expenses or []
        self.appended = []
        self.updated = {}

    def get(self, rng):
        if rng == "取込データ!A2:L":
            return self.rows
        raise AssertionError(rng)

    def categories(self):
        return [
            ("その他", "未分類"), ("自動車", "高速料金"),
            ("自動車", "ガソリン"), ("食費", "外食"),
        ]

    def expense_index(self):
        return {row[0]: number for number, row in enumerate(self.expenses, start=2)}

    def ensure_expense_status_column(self):
        pass

    def append(self, sheet, rows):
        self.appended.extend((sheet, row) for row in rows)

    def update_rows(self, sheet, rows):
        self.updated.setdefault(sheet, []).extend(rows)


def posted_expenses(db):
    return [row for sheet, row in db.appended if sheet == "支出明細"]


def test_paypay_aupay_and_card_are_auto_posted_with_fallback_category():
    db = FakeDB([
        import_row("p1", "PayPay", status="unclassified_paypay"),
        import_row("a1", "au PAY", status="unclassified_aupay"),
        import_row("c1", "au PAYカード", status="unclassified_card"),
    ])
    result = AutoExpensePipeline(db).apply()

    assert result["expenses_created"] == 3
    assert all(row[5:7] == ["その他", "未分類"] for row in posted_expenses(db))
    assert all(row[12] == "active" for row in posted_expenses(db))
    assert all(row[1][8] == "auto_expense" for row in db.updated["取込データ"])


def test_existing_stable_expense_is_updated_not_appended():
    existing = [expense_id("p1"), "", "", "", 0, "", "", "", "", "", "p1", "", "active"]
    db = FakeDB([import_row()], [existing])
    result = AutoExpensePipeline(db).apply()

    assert result["expenses_created"] == 0
    assert result["expenses_updated"] == 1
    assert posted_expenses(db) == []
    assert db.updated["支出明細"][0][1][0] == expense_id("p1")


def test_high_confidence_categories_and_multi_item_store_fallback():
    db = FakeDB([
        import_row("e1", "au PAYカード", "ETC 千葉西", status="unclassified_card"),
        import_row("g1", "au PAYカード", "ENEOS SS", status="unclassified_card"),
        import_row("m1", "au PAY", "三井リンクラボ新木場", status="unclassified_aupay"),
        import_row("s1", "au PAYカード", "AP/セブンイレブン", status="unclassified_card"),
    ])
    AutoExpensePipeline(db).apply()
    categories = {row[10]: tuple(row[5:7]) for row in posted_expenses(db)}
    assert categories == {
        "e1": ("自動車", "高速料金"),
        "g1": ("自動車", "ガソリン"),
        "m1": ("食費", "外食"),
        "s1": ("その他", "未分類"),
    }


def test_amazon_installment_and_refund_are_reviewed_not_posted():
    db = FakeDB([
        import_row("amz", "au PAYカード", "アマゾンブンカツバライ", status="unclassified_card"),
        import_row("refund", "au PAY", "テスト店 返金", status="unclassified_aupay"),
    ])
    result = AutoExpensePipeline(db).apply()

    assert result["needs_review"] == 2
    assert posted_expenses(db) == []
    statuses = [row[1][8] for row in db.updated["取込データ"]]
    assert statuses == ["needs_review_amazon_installment", "needs_review_refund"]


def test_suica_charge_is_auto_posted_for_now():
    db = FakeDB([
        import_row("suica", "au PAYカード", "AP/スイカ(ケ-タイケツサイ)",
                   5000, "unclassified_card"),
    ])
    result = AutoExpensePipeline(db).apply()
    assert result["auto_expense"] == 1
    assert posted_expenses(db)[0][4] == 5000


def test_existing_receipt_candidate_is_not_auto_posted_first():
    db = FakeDB([
        import_row("p1", "PayPay", "テスト商店", 100, "unclassified_paypay"),
        import_row("receipt:r1", "receipt", "テスト商店", 100, "解析済"),
    ])
    result = AutoExpensePipeline(db).apply()
    assert result["skipped"] == 1
    assert posted_expenses(db) == []


def test_docomo_asset_formation_status_persists_into_expense_category():
    db = FakeDB([
        import_row(
            "docomo:asset", DOCOMO_SMTB_SOURCE, "SBIハイブリッド預金",
            -1000, ASSET_FORMATION_IMPORT_STATUS,
        ),
        import_row(
            "docomo:ordinary", DOCOMO_SMTB_SOURCE, "通常の銀行支出",
            -2000, "bank_expense",
        ),
    ])

    result = AutoExpensePipeline(db).apply()

    assert result["auto_expense"] == 1
    assert result["expenses_created"] == 1
    expense = posted_expenses(db)[0]
    assert expense[4] == 1000
    assert expense[5:7] == ["資産形成", ""]
    assert expense[10] == "docomo:asset"
    assert active_expenses([expense])[0].major_category == "資産形成"
    assert db.updated["取込データ"][0][1][8] == "auto_expense"
    assert db.updated["取込データ"][0][1][9] == expense_id("docomo:asset")


def test_docomo_asset_formation_status_rejects_non_expense_sign():
    db = FakeDB([
        import_row(
            "docomo:bad-sign", DOCOMO_SMTB_SOURCE, "SBIハイブリッド預金",
            1000, ASSET_FORMATION_IMPORT_STATUS,
        ),
    ])

    result = AutoExpensePipeline(db).apply()

    assert result["needs_review"] == 1
    assert posted_expenses(db) == []
    assert db.updated["取込データ"][0][1][8] == "needs_review_asset_formation_sign"


def canonical_card_row(**kwargs):
    row = import_row("aupaycard-mail:" + "a" * 24 + ":001", "au PAYカード",
                     status="auto_expense", **kwargs)
    row[1], row[10] = "2026-09-14 12:00:00", "b" * 64
    return row


@pytest.mark.parametrize("imported_at", ["2026-01-01 12:00:00", "2026-09-15 12:00:00"])
def test_canonical_card_pending_is_excluded_from_preview_and_apply(imported_at):
    row = canonical_card_row()
    row[1] = imported_at
    db = FakeDB([row])
    pipeline = AutoExpensePipeline(db)
    assert pipeline.preview()["candidates"] == 0
    for _ in range(2):
        result = pipeline.apply()
        assert result["candidates"] == result["expenses_created"] == result["imports_updated"] == 0
    assert db.appended == []
    assert not any(db.updated.values())


@pytest.mark.parametrize("column,value", [(0, "legacy-card"), (1, ""), (2, "PayPay"),
    (8, "matched_receipt"), (8, "matched_amazon"), (8, "transfer_aupay_charge"),
    (8, "needs_review_duplicate"), (9, "existing-target"), (10, "invalid-hash")])
def test_canonical_card_posting_does_not_promote_other_states(column, value):
    row = canonical_card_row()
    row[column] = value
    db = FakeDB([row])
    assert AutoExpensePipeline(db).apply()["expenses_created"] == 0
    assert posted_expenses(db) == []


def test_canonical_card_with_receipt_is_still_outside_posting_scope():
    db = FakeDB([canonical_card_row(), import_row("receipt:r", "receipt", status="解析済")])
    result = AutoExpensePipeline(db).apply()
    assert result["candidates"] == 0 and result["expenses_created"] == 0


@pytest.mark.parametrize("parent_enabled", [None, "false"])
def test_legacy_cli_parent_off_preserves_posting_scope_and_replay(monkeypatch, parent_enabled):
    import ast
    import io
    import sys
    from contextlib import redirect_stdout
    from app import cli

    for variable in ("KAKEIBO_PRODUCTION_ENABLED", "KAKEIBO_SCHEDULE_ENABLED"):
        if parent_enabled is None:
            monkeypatch.delenv(variable, raising=False)
        else:
            monkeypatch.setenv(variable, parent_enabled)
    monkeypatch.delenv("KAKEIBO_LEGACY_DISABLED", raising=False)
    canonical = canonical_card_row()
    excluded = []
    for index, status in enumerate(("matched_receipt", "matched_amazon", "needs_review_duplicate",
                                    "transfer_aupay_charge", "refund")):
        row = canonical_card_row()
        row[0] = "aupaycard-mail:" + "a" * 24 + f":{index + 2:03d}"
        row[8] = status
        excluded.append(row)
    db = FakeDB([canonical, *excluded,
                 import_row("paypay", "PayPay", status="unclassified_paypay"),
                 import_row("balance", "au PAY", status="unclassified_aupay"),
                 import_row("card", "au PAYカード", status="unclassified_card"),
                 import_row("refund", "au PAYカード", "テスト店 返品", -100, "unclassified_card"),
                 import_row("transfer", "au PAYカード", "AU PAY 残高チャージ", 100, "unclassified_card")])
    monkeypatch.setattr(cli, "make", lambda *args, **kwargs: (None, db, None))

    def run(command):
        monkeypatch.setattr(sys, "argv", ["app.cli", command])
        output = io.StringIO()
        with redirect_stdout(output):
            cli.main()
        return ast.literal_eval(output.getvalue())

    preview = run("auto-expense-preview")
    result = run("auto-expense")
    assert all(result[key] == value for key, value in preview.items())
    assert result["expenses_created"] == 3 and result["needs_review"] == 2
    expenses = posted_expenses(db)
    assert {(row[8], row[10], row[4]) for row in expenses} == {
        ("PayPay", "paypay", 100), ("au PAY", "balance", 100), ("au PAYカード", "card", 100)}
    assert not {row[0] for row in (canonical, *excluded)} & {row[1][0] for row in db.updated["取込データ"]}
    # Simulate append success / import update loss, then completed rerun.
    db = FakeDB(db.rows, expenses)
    replay = run("auto-expense")
    assert replay["expenses_created"] == 0 and replay["expenses_updated"] == 3
    for number, updated in db.updated["取込データ"]:
        db.rows[number - 2] = updated
    assert run("auto-expense")["candidates"] == 0
    assert posted_expenses(db) == []
