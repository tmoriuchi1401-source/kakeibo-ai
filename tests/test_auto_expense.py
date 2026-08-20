from app.auto_expense import AutoExpensePipeline, expense_id


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
