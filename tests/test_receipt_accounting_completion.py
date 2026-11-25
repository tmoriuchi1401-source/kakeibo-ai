from copy import deepcopy

import pytest

from app.receipt_pipeline import ReceiptPipeline
from app.drive_receipts import should_archive_result
from test_receipt_pipeline import FakeDB, FakeAI
from app.models import ReceiptResult, ReceiptItem


class ReadbackDB(FakeDB):
    def __init__(self, corrupt):
        super().__init__()
        self.rows = {}
        self.corrupt = corrupt

    def append(self, title, rows):
        super().append(title, rows)
        self.rows.setdefault(title, []).extend(deepcopy(rows))

    def get_raw(self, region):
        title = region.split('!')[0].strip("'")
        rows = deepcopy(self.rows.get(title, []))
        if title == self.corrupt and rows:
            rows[0][1] = 'wrong-readback'
        return rows


@pytest.mark.parametrize('corrupt', [None, 'レシート', '支出明細', '取込データ'])
def test_materializer_requires_exact_accounting_before_original_completion(corrupt):
    db = ReadbackDB(corrupt)
    pipeline = ReceiptPipeline(db, FakeAI())
    pipeline.verify_materialization = True
    parsed = ReceiptResult(date='2026-01-01', merchant='Synthetic shop', total=100,
        items=[ReceiptItem(name='Synthetic item', amount=100, major_category='食費', minor_category='食品')])
    if corrupt:
        with pytest.raises(RuntimeError, match='accounting_readback_mismatch'):
            pipeline._materialize_result(parsed, 'synthetic-source', '', [])
    else:
        result = pipeline._materialize_result(parsed, 'synthetic-source', '', [])
        assert should_archive_result(result)

