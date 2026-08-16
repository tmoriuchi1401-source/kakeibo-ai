import pandas as pd

from app.amazon_pipeline import AmazonPipeline
from app.models import ProductClassification, ProductClassificationBatch


class FakeAI:
    def classify_products(self, products, categories):
        return ProductClassificationBatch(products=[
            ProductClassification(
                asin=item["asin"], major_category="その他", minor_category="未分類"
            ) for item in products
        ])


class FakeDB:
    def __init__(self, baseline_keys=None):
        self.baseline_keys = set(baseline_keys or [])
        self.appended = {}
        self.updated = {}

    def amazon_index(self): return {}
    def amazon_baseline_keys(self): return self.baseline_keys
    def product_master(self): return {}
    def categories(self): return [("その他", "未分類")]
    def expense_index(self): return {}
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
