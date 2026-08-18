from app.aupay_card_pipeline import AuPayCardPipeline


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
