from app.reconciliation import parse_import_rows
from app.review_pipeline import review_items


def row(import_id,source,date,status):
    return [import_id,"",source,import_id,date,"店舗",100,"",status,"","","備考"]


def test_review_extracts_only_actionable_states():
    items=review_items(parse_import_rows([
        row("r1","receipt","2026-08-10","要確認"),
        row("a1","au PAY","2026-08-16","unclassified_aupay"),
        row("c1","au PAYカード","2026-08-15","unclassified_card"),
        row("a2","au PAY","2026-08-14","matched_receipt"),
        row("c2","au PAYカード","2026-08-14","transfer_aupay_charge"),
        row("m1","Amazon","2026-08-14","canonical_amazon"),
    ]))
    assert [x.transaction.import_id for x in items] == ["r1","a1","c1"]
    assert [x.priority for x in items] == ["高","中","中"]


def test_ambiguous_duplicate_is_high_priority():
    items=review_items(parse_import_rows([
        row("a1","au PAY","2026-08-16","needs_review_duplicate"),
    ]))
    assert items[0].priority == "高"
    assert "統合先" in items[0].recommendation
