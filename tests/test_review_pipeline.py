from app.reconciliation import parse_import_rows
from app.review_pipeline import ReviewApprovalPipeline, review_items


def row(import_id,source,date,status):
    return [import_id,"",source,import_id,date,"店舗",100,"",status,"","","備考"]


def test_review_extracts_only_actionable_states():
    items=review_items(parse_import_rows([
        row("r1","receipt","2026-08-10","要確認"),
        row("a1","au PAY","2026-08-16","auto_expense"),
        row("c1","au PAYカード","2026-08-15","auto_expense"),
        row("p1","PayPay","2026-08-13","auto_expense"),
        row("amz","au PAYカード","2026-08-12","needs_review_amazon_installment"),
        row("a2","au PAY","2026-08-14","matched_receipt"),
        row("c2","au PAYカード","2026-08-14","transfer_aupay_charge"),
        row("m1","Amazon","2026-08-14","canonical_amazon"),
    ]))
    assert [x.transaction.import_id for x in items] == ["amz","r1"]
    assert [x.priority for x in items] == ["高","高"]


def test_ambiguous_duplicate_is_high_priority():
    items=review_items(parse_import_rows([
        row("a1","au PAY","2026-08-16","needs_review_duplicate"),
    ]))
    assert items[0].priority == "高"
    assert "統合先" in items[0].recommendation


class FakeDB:
    def __init__(self,category=("食費","食料品"),source="au PAY",
                 status="unclassified_aupay",import_id="a1"):
        self.category=category
        self.source=source; self.status=status; self.import_id=import_id
        self.appended=[]; self.updated={}

    def get(self,rng):
        if rng=="取込データ!A2:L":
            return [row(self.import_id,self.source,"2026-08-16",self.status)]
        if rng=="要確認!A2:O":
            return [[self.import_id,"中","2026-08-16",self.source,"店舗",100,self.status,
                     "","","支出として計上","",self.category[0],self.category[1],"メモ",""]]
        raise AssertionError(rng)

    def categories(self): return [("食費","食料品")]
    def expense_index(self): return {}
    def expense_rows_for_import(self,import_id): return []
    def ensure_expense_status_column(self): pass
    def append(self,sheet,rows): self.appended.extend(rows)
    def update_rows(self,sheet,rows): self.updated.setdefault(sheet,[]).extend(rows)


def test_manual_expense_requires_master_category_and_creates_expense():
    db=FakeDB()
    result=ReviewApprovalPipeline(db).apply()
    assert result["applied"]==1
    assert result["errors"]==0
    assert len(db.appended)==1
    assert db.appended[0][5:7]==["食費","食料品"]
    assert db.appended[0][12]=="active"
    assert db.updated["取込データ"][0][1][8]=="manual_expense"


def test_manual_expense_rejects_category_pair_outside_master():
    db=FakeDB(category=("AI新設","勝手なカテゴリ"))
    result=ReviewApprovalPipeline(db).apply()
    assert result["applied"]==0
    assert result["errors"]==1
    assert db.appended==[]
    assert "カテゴリマスタ" in db.updated["要確認"][0][1][14]


def test_paypay_unclassified_can_be_manually_recorded_as_expense():
    db=FakeDB(source="PayPay",status="unclassified_paypay",import_id="paypay:test-1")
    result=ReviewApprovalPipeline(db).apply()

    assert result["applied"]==1
    assert db.appended[0][8]=="PayPay"
    assert db.appended[0][10]=="paypay:test-1"
    assert db.updated["取込データ"][0][1][8]=="manual_expense"
