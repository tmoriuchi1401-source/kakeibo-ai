from app.amazon_installment import AmazonInstallmentPipeline
from app.models import ProductClassification, ProductClassificationBatch


KEY = "249-1078090-9955016|B00R10Y9GS"
ORDER_ID = "249-1078090-9955016"


def amazon_row(key=KEY, order_id=ORDER_ID, amount=24000, note="baseline", asin="B00R10Y9GS"):
    return [key, order_id, asin, "2026-06-02", "家庭用金庫", 1, amount,
            "MasterCard - 1071", "", "", note, "hash", ""]


def installment_row(import_id, date, amount, status="needs_review_amazon_installment",
                    target="", member="本会員"):
    return [import_id, "", "au PAYカード", import_id, date, "アマゾンブンカツバライ",
            amount, "通常払い", status, target, "hash", f"会員={member}"]


class FakeAI:
    def __init__(self, category=("住まい", "その他")):
        self.category = category
        self.calls = 0

    def classify_products(self, products, categories):
        self.calls += 1
        return ProductClassificationBatch(products=[
            ProductClassification(
                asin=item["asin"], major_category=self.category[0],
                minor_category=self.category[1], note="AI分類",
            ) for item in products
        ])


class FailingAI:
    def classify_products(self, products, categories):
        raise RuntimeError("classification unavailable")


class FakeDB:
    def __init__(self, orders=None, imports=None, expenses=None, products=None):
        self.orders = list(orders or [amazon_row()])
        self.imports = list(imports or [
            installment_row("card:1", "2026-06-05", 4000),
            installment_row("card:2", "2026-07-05", 4000),
            installment_row("card:3", "2026-07-10", 16000),
        ])
        self.expenses = list(expenses or [])
        self.products = list(products or [])

    def get(self, rng):
        if rng == "取込データ!A2:L": return self.imports
        if rng == "Amazon注文!A2:M": return self.orders
        raise AssertionError(rng)

    def categories(self):
        return [("その他", "未分類"), ("住まい", "その他"), ("日用品", "その他")]

    def product_master(self):
        return {r[0]: (r[2], r[3], r[1]) for r in self.products}

    def expense_index(self):
        return {r[0]: i for i, r in enumerate(self.expenses, start=2)}

    def import_index(self):
        return {r[0]: (i, r[10] if len(r) > 10 else "")
                for i, r in enumerate(self.imports, start=2)}

    def ensure_expense_status_column(self): pass

    def append(self, sheet, rows):
        target = {"商品マスタ": self.products, "支出明細": self.expenses,
                  "取込データ": self.imports}[sheet]
        target.extend([list(row) for row in rows])

    def update_rows(self, sheet, rows):
        target = {"取込データ": self.imports, "支出明細": self.expenses}[sheet]
        for row_num, row in rows:
            target[row_num - 2] = list(row)


def test_three_installments_uniquely_match_and_apply_once():
    db = FakeDB()
    preview = AmazonInstallmentPipeline(db).preview()
    assert preview == {
        "installment_rows": 3, "candidate_groups": 1, "matched_groups": 1,
        "unmatched_groups": 0, "amazon_orders_to_materialize": 1,
        "amazon_expenses_to_create": 1,
    }

    result = AmazonInstallmentPipeline(db, FakeAI()).apply()
    assert result["expenses_created"] == 1
    assert len(db.expenses) == 1
    assert db.expenses[0][4] == 24000
    assert db.expenses[0][8] == "Amazon"
    assert db.expenses[0][11] == f"Amazonキー={KEY}"
    assert db.expenses[0][12] == "active"
    card_rows = db.imports[:3]
    assert {row[8] for row in card_rows} == {"matched_amazon_installment"}
    assert len({row[9] for row in card_rows}) == 1
    canonical = [row for row in db.imports if row[0] == f"amazon:{ORDER_ID}"]
    assert len(canonical) == 1
    assert canonical[0][8] == "canonical_amazon"

    again = AmazonInstallmentPipeline(db, FakeAI()).apply()
    assert again["expenses_created"] == 0
    assert len(db.expenses) == 1
    assert len([row for row in db.imports if row[0] == f"amazon:{ORDER_ID}"]) == 1


def test_product_master_category_is_reused_without_ai():
    db = FakeDB(products=[["B00R10Y9GS", "家庭用金庫", "日用品", "その他", "", ""]])
    ai = FakeAI()
    AmazonInstallmentPipeline(db, ai).apply()
    assert ai.calls == 0
    assert db.expenses[0][5:7] == ["日用品", "その他"]


def test_missing_product_uses_gemini_and_caches_result():
    db = FakeDB()
    ai = FakeAI(("住まい", "その他"))
    result = AmazonInstallmentPipeline(db, ai).apply()
    assert ai.calls == 1
    assert result["products_cached"] == 1
    assert db.products[0][0] == "B00R10Y9GS"
    assert db.expenses[0][5:7] == ["住まい", "その他"]


def test_gemini_failure_uses_and_caches_fallback_category():
    db = FakeDB()
    result = AmazonInstallmentPipeline(db, FailingAI()).apply()
    assert result["products_cached"] == 1
    assert db.products[0][2:4] == ["その他", "未分類"]
    assert db.expenses[0][5:7] == ["その他", "未分類"]


def test_multiple_order_candidates_are_not_applied():
    second = amazon_row("OTHER|ASIN-2", "OTHER", asin="ASIN-2")
    db = FakeDB(orders=[amazon_row(), second])
    preview = AmazonInstallmentPipeline(db).preview()
    assert preview["candidate_groups"] == 2
    assert preview["matched_groups"] == 0
    assert preview["unmatched_groups"] == 1
    result = AmazonInstallmentPipeline(db, FakeAI()).apply()
    assert result["expenses_created"] == 0
    assert all(row[8] == "needs_review_amazon_installment" for row in db.imports)


def test_amount_mismatch_and_non_baseline_are_not_candidates():
    mismatch = FakeDB(orders=[amazon_row(amount=25000)])
    assert AmazonInstallmentPipeline(mismatch).preview()["matched_groups"] == 0

    incremental = FakeDB(orders=[amazon_row(note="incremental")])
    assert AmazonInstallmentPipeline(incremental).preview()["candidate_groups"] == 0
    assert AmazonInstallmentPipeline(incremental).preview()["matched_groups"] == 0


def test_payment_before_order_and_partial_bucket_are_not_candidates():
    before = FakeDB(imports=[
        installment_row("card:1", "2026-05-05", 4000),
        installment_row("card:2", "2026-06-05", 20000),
    ])
    assert AmazonInstallmentPipeline(before).preview()["matched_groups"] == 0

    partial = FakeDB(imports=[
        installment_row("card:1", "2026-06-05", 4000),
        installment_row("card:2", "2026-07-05", 4000),
        installment_row("card:3", "2026-07-10", 16000),
        installment_row("card:4", "2026-08-05", 4000),
    ])
    assert AmazonInstallmentPipeline(partial).preview()["matched_groups"] == 0


def test_linked_or_refunded_installments_are_not_candidates():
    linked = FakeDB(imports=[
        installment_row("card:1", "2026-06-05", 4000, target="M-existing"),
        installment_row("card:2", "2026-07-05", 20000),
    ])
    assert AmazonInstallmentPipeline(linked).preview()["candidate_groups"] == 0

    refunded = FakeDB(imports=[
        installment_row("card:1", "2026-06-05", 4000),
        installment_row("card:2", "2026-07-05", 20000, member="本会員; 返金"),
    ])
    assert AmazonInstallmentPipeline(refunded).preview()["candidate_groups"] == 0
