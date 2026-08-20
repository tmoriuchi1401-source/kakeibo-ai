from app.aupay_card_pipeline import AuPayCardPipeline, _amazon_extended_eligible


class FakeDB:
    def __init__(self, amazon_rows, import_rows=None):
        self.amazon_rows = amazon_rows
        self.import_rows = import_rows or []
        self.updated = []

    def get(self, rng):
        if rng == "Amazon注文!A2:M":
            return self.amazon_rows
        if rng == "取込データ!A2:L":
            return self.import_rows
        raise AssertionError(rng)

    def update_rows(self, sheet, rows):
        assert sheet == "取込データ"
        self.updated.extend(rows)


def amazon_row(key, order_id, date, amount):
    return [key, order_id, "ASIN", date, "商品", 1, amount]


def test_amazon_candidates_are_grouped_by_order_id():
    db = FakeDB([
        amazon_row("key:1", "ORDER-1", "2026-08-01", 700),
        amazon_row("key:2", "ORDER-1", "2026-08-01", 500),
    ])
    pipeline = AuPayCardPipeline(db)

    candidates = pipeline._amazon_candidates()

    assert candidates == [{
        "key": "amazon:ORDER-1",
        "order_id": "ORDER-1",
        "date": "2026-08-01",
        "amount": 1200,
        "items": 2,
    }]
    assert pipeline._classify_amazon("2026-08-03", 1200, candidates)[0] == "matched_amazon"


def test_zero_and_multiple_amazon_candidates_have_distinct_statuses():
    db = FakeDB([
        amazon_row("key:1", "ORDER-1", "2026-08-01", 780),
        amazon_row("key:2", "ORDER-2", "2026-08-04", 780),
    ])
    pipeline = AuPayCardPipeline(db)
    candidates = pipeline._amazon_candidates()

    assert pipeline._classify_amazon("2026-08-02", 999, candidates)[0] == "amazon_unmatched"
    assert pipeline._classify_amazon("2026-08-02", 780, candidates)[0] == "amazon_needs_review"


def test_existing_amazon_card_row_can_be_reclassified():
    imports = [[
        "card:1", "", "au PAYカード", "card:1", "2026-08-03",
        "AMAZON.CO.JP", 1200, "一括", "amazon_needs_review", "", "",
        "会員=本人; Amazon候補数=0",
    ]]
    db = FakeDB([
        amazon_row("key:1", "ORDER-1", "2026-08-01", 700),
        amazon_row("key:2", "ORDER-1", "2026-08-01", 500),
    ], imports)

    result = AuPayCardPipeline(db).reclassify_amazon()

    assert result["updated"] == 1
    updated = db.updated[0][1]
    assert updated[8] == "matched_amazon"
    assert updated[9] == ""
    assert "Amazon候補数=0" not in updated[11]
    assert "Amazonキー=amazon:ORDER-1" in updated[11]


def test_manual_amazon_match_is_not_reclassified():
    imports = [[
        "card:1", "", "au PAYカード", "card:1", "2026-08-03",
        "AMAZON.CO.JP", 780, "一括", "matched_amazon", "item-key", "",
        "手動照合=2026-08-01注文",
    ]]
    db = FakeDB([
        amazon_row("key:1", "ORDER-1", "2026-08-01", 780),
        amazon_row("key:2", "ORDER-2", "2026-08-02", 780),
    ], imports)

    result = AuPayCardPipeline(db).reclassify_amazon()

    assert result["updated"] == 0
    assert db.updated == []


def test_normal_match_within_seven_days_is_unchanged():
    pipeline = AuPayCardPipeline(FakeDB([]))
    candidates = [{"key": "amazon:o1", "date": "2026-08-01", "amount": 1000}]
    state, matched, match_type, days = pipeline._classify_amazon_details(
        "2026-08-08", 1000, candidates,
    )
    assert (state, match_type, days) == ("matched_amazon", "normal", 7)
    assert matched == candidates


def test_unique_same_amount_order_eight_to_twenty_one_days_before_is_extended_match():
    pipeline = AuPayCardPipeline(FakeDB([]))
    candidates = [{"key": "amazon:o1", "date": "2026-08-01", "amount": 1000}]
    state, matched, match_type, days = pipeline._classify_amazon_details(
        "2026-08-16", 1000, candidates,
    )
    assert (state, match_type, days) == ("matched_amazon", "extended", 15)
    assert matched == candidates


def test_order_more_than_twenty_one_days_before_is_unmatched():
    pipeline = AuPayCardPipeline(FakeDB([]))
    candidates = [{"key": "amazon:o1", "date": "2026-08-01", "amount": 1000}]
    assert pipeline._classify_amazon("2026-08-23", 1000, candidates)[0] == "amazon_unmatched"


def test_order_after_card_date_is_not_extended_match():
    pipeline = AuPayCardPipeline(FakeDB([]))
    candidates = [{"key": "amazon:o1", "date": "2026-08-20", "amount": 1000}]
    assert pipeline._classify_amazon("2026-08-01", 1000, candidates)[0] == "amazon_unmatched"


def test_multiple_extended_candidates_are_never_auto_matched():
    pipeline = AuPayCardPipeline(FakeDB([]))
    candidates = [
        {"key": "amazon:o1", "date": "2026-08-01", "amount": 1000},
        {"key": "amazon:o2", "date": "2026-08-03", "amount": 1000},
    ]
    assert pipeline._classify_amazon("2026-08-16", 1000, candidates)[0] == "amazon_needs_review"


def test_amount_mismatch_is_not_extended_match():
    pipeline = AuPayCardPipeline(FakeDB([]))
    candidates = [{"key": "amazon:o1", "date": "2026-08-01", "amount": 1100}]
    assert pipeline._classify_amazon("2026-08-16", 1000, candidates)[0] == "amazon_unmatched"


def test_refund_and_installment_are_ineligible_for_extended_matching():
    assert not _amazon_extended_eligible("AMAZON.CO.JP", "一括", "返品による返金")
    assert not _amazon_extended_eligible("AMAZON.CO.JP 分割払い", "分割", "")


def test_reclassification_records_extended_match_audit_note():
    imports = [[
        "card:1", "", "au PAYカード", "card:1", "2026-08-16",
        "AMAZON.CO.JP", 1000, "一括", "amazon_unmatched", "", "", "会員=本人",
    ]]
    db = FakeDB([amazon_row("key:1", "ORDER-1", "2026-08-01", 1000)], imports)
    result = AuPayCardPipeline(db).reclassify_amazon()
    updated = db.updated[0][1]
    assert result["updated"] == 1
    assert updated[8] == "matched_amazon"
    assert "Amazon拡張照合=21日以内" in updated[11]
    assert "日付差=15日" in updated[11]
