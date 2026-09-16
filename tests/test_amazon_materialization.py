import pandas as pd

from app.amazon_pipeline import AmazonPipeline
from app.auto_expense import expense_id
from app.models import ProductClassification, ProductClassificationBatch


class FakeAI:
    def classify_products(self, products, categories):
        return ProductClassificationBatch(products=[
            ProductClassification(
                asin=item["asin"], major_category="その他", minor_category="未分類"
            ) for item in products
        ])


class FakeDB:
    def __init__(self, baseline_keys=None, expenses=None, expense_index=None):
        self.baseline_keys = set(baseline_keys or [])
        self.expenses = list(expenses or [])
        self.appended = {}
        self.updated = {}
        self._expense_index = dict(expense_index or {})

    def amazon_index(self): return {}
    def amazon_baseline_keys(self): return self.baseline_keys
    def product_master(self): return {}
    def categories(self): return [("その他", "未分類"), ("食費", "食品")]
    def expense_index(self): return self._expense_index
    def expense_rows_for_import(self, import_id):
        return [(index,row) for index,row in enumerate(self.expenses,start=2)
                if len(row)>10 and row[10]==import_id]
    def import_index(self): return {}
    def ensure_expense_status_column(self): pass
    def append(self, sheet, rows): self.appended.setdefault(sheet, []).extend(rows)
    def update_rows(self, sheet, rows): self.updated.setdefault(sheet, []).extend(rows)


def write_csv(path):
    pd.DataFrame([{
        "Order ID": "ORDER-1",
        "ASIN": "ASIN-1",
        "Order Date": "2026-08-16T01:00:00Z",
        "Product Name": "テスト商品",
        "Original Quantity": 1,
        "Total Amount": "1,200",
        "Payment Method Type": "カード",
        "Ship Date": "2026-08-18T01:00:00Z",
        "Carrier Name & Tracking Number": "not-stored",
    }]).to_csv(path, index=False)


def test_incremental_amazon_creates_item_expense_and_order_import(tmp_path):
    path = tmp_path / "amazon.csv"
    write_csv(path)
    db = FakeDB()
    result = AmazonPipeline(db, FakeAI()).import_csv(str(path))
    assert result["expense_new"] == 1
    assert result["order_import_new"] == 1
    expense = db.appended["支出明細"][0]
    assert expense[4] == 1200
    assert expense[8] == "Amazon"
    assert expense[10] == "amazon:ORDER-1"
    assert expense[12] == "active"
    imported = db.appended["取込データ"][0]
    assert imported[0] == "amazon:ORDER-1"
    assert imported[6] == 1200
    assert imported[8] == "canonical_amazon"
    amazon = db.appended["Amazon注文"][0]
    assert amazon[13:] == ["2026-08-18", 1]
    assert "not-stored" not in amazon


def test_item_materialization_supersedes_gmail_order_total(tmp_path):
    path = tmp_path / "amazon.csv"
    write_csv(path)
    aggregate = [
        expense_id("amazon:ORDER-1"), "2026-08-16", "Amazon.co.jp", "Amazon注文",
        1200, "その他", "未分類", "カード", "Amazon", "", "amazon:ORDER-1",
        "Amazon Gmail注文合計", "active",
    ]
    db = FakeDB(expenses=[aggregate])
    AmazonPipeline(db, FakeAI()).import_csv(str(path))
    row_num,row = db.updated["支出明細"][-1]
    assert row_num == 2
    assert row[0] == aggregate[0]
    assert row[12] == "superseded_amazon_items"


def test_amazon_replay_preserves_existing_human_product_category(tmp_path):
    path = tmp_path / "amazon.csv"
    write_csv(path)
    expense_id = AmazonPipeline._expense_id("ORDER-1|ASIN-1")
    existing = [
        expense_id, "2026-08-16", "Amazon.co.jp", "テスト商品", 1200,
        "食費", "食品", "カード", "Amazon", "", "amazon:ORDER-1", "human", "active",
    ]
    db = FakeDB(expenses=[existing], expense_index={expense_id: 2})
    AmazonPipeline(db, FakeAI()).import_csv(str(path))
    updated = next(row for _,row in db.updated["支出明細"] if row[0] == expense_id)
    assert updated[5:7] == ["食費", "食品"]
    assert updated[11] == "human"
