from app.reconciliation import parse_import_rows
from app.review_pipeline import ReviewApprovalPipeline, is_reviewable_status, review_items
from app.sheets import review_category_helper_formulas


def row(import_id,source,date,status):
    return [import_id,"",source,import_id,date,"店舗",100,"",status,"","","備考"]


def test_review_extracts_only_actionable_states():
    items=review_items(parse_import_rows([
        row("r1","receipt","2026-08-10","要確認"),
        row("a1","au PAY","2026-08-16","unclassified_aupay"),
        row("c1","au PAYカード","2026-08-15","unclassified_card"),
        row("c3","au PAYカード","2026-08-13","amazon_needs_review"),
        row("a2","au PAY","2026-08-14","matched_receipt"),
        row("c2","au PAYカード","2026-08-14","transfer_aupay_charge"),
        row("m1","Amazon","2026-08-14","canonical_amazon"),
    ]))
    assert [x.transaction.import_id for x in items] == ["c3","r1","a1","c1"]
    assert [x.priority for x in items] == ["高","高","中","中"]
    assert "Amazon" in items[0].recommendation


def test_ambiguous_duplicate_is_high_priority():
    items=review_items(parse_import_rows([
        row("a1","au PAY","2026-08-16","needs_review_duplicate"),
    ]))
    assert items[0].priority == "高"
    assert "統合先" in items[0].recommendation


def test_review_category_helper_filters_each_review_row():
    formulas=review_category_helper_formulas(2)
    assert len(formulas)==2
    assert "要確認!L2" in formulas[0][0]
    assert "要確認!L3" in formulas[1][0]
    assert "FILTER(カテゴリ!$B$2:$B" in formulas[0][0]


def test_all_extracted_status_patterns_can_be_manually_applied():
    assert is_reviewable_status("needs_review_aupay_csv")
    assert is_reviewable_status("amazon_needs_review")
    assert is_reviewable_status("unclassified_aupay")
    assert not is_reviewable_status("matched_receipt")


class FakeDB:
    def __init__(self,category=("食費","食料品")):
        self.category=category
        self.appended=[]; self.updated={}

    def get(self,rng):
        if rng=="取込データ!A2:L":
            return [row("a1","au PAY","2026-08-16","unclassified_aupay")]
        if rng=="要確認!A2:O":
            return [["a1","中","2026-08-16","au PAY","店舗",100,"unclassified_aupay",
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
