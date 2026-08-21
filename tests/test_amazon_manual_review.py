from app.review_pipeline import ReviewApprovalPipeline, ReviewPipeline


def import_row(import_id, source, date, amount, status, *, merchant="AMAZON.CO.JP", note=""):
    return [import_id, "", source, import_id, date, merchant, amount, "Visa", status, "", "", note]


def amazon_row(order_id, date, amount, *, asin="ASIN", name="商品", kind="baseline", data_hash="h"):
    return [f"{order_id}|{asin}", order_id, asin, date, name, 1, amount, "Visa",
            "日用品", "雑貨", kind, data_hash, ""]


class MemoryDB:
    def __init__(self):
        self.sheets = {
            "取込データ": [
                import_row("card:1", "au PAYカード", "2026-08-20", 1000, "amazon_unmatched"),
                import_row("receipt:1", "receipt", "2026-08-19", 500, "要確認", merchant="店舗"),
            ],
            "Amazon注文": [
                amazon_row("ORDER-1", "2026-08-19", 1000, name="候補商品1"),
                amazon_row("ORDER-2", "2026-08-18", 1050, name="候補商品2", kind="incremental"),
                amazon_row("ORDER-3", "2026-08-17", 1100, name="候補商品3"),
                amazon_row("ORDER-4", "2026-08-16", 1150, name="候補商品4"),
            ],
            "要確認": [],
            "Amazon照合候補": [],
        }
        self.validation = None

    def get(self, rng):
        mapping = {
            "取込データ!A2:L": "取込データ",
            "Amazon注文!A2:M": "Amazon注文",
            "要確認!A2:T": "要確認",
            "要確認!A2:O": "要確認",
            "Amazon照合候補!A2:S": "Amazon照合候補",
        }
        if rng not in mapping:
            raise AssertionError(rng)
        return [list(row) for row in self.sheets[mapping[rng]]]

    def categories(self):
        return [("日用品", "雑貨")]

    def ensure_sheet(self, title, header):
        self.sheets.setdefault(title, [])

    def clear(self, rng):
        self.sheets["要確認" if rng.startswith("要確認") else "Amazon照合候補"] = []

    def append(self, sheet, rows):
        self.sheets.setdefault(sheet, []).extend([list(row) for row in rows])

    def configure_review_validation(self, categories, amazon_options_by_row=None):
        self.validation = (categories, amazon_options_by_row or {})

    def update_rows(self, sheet, rows):
        self.sheets.setdefault("updates:" + sheet, []).extend(rows)

    def expense_index(self): return {}
    def expense_rows_for_import(self, import_id): return []
    def ensure_expense_status_column(self): pass


def review_row(db, import_id):
    return next(row for row in db.sheets["要確認"] if row[0] == import_id)


def test_refresh_shows_only_amazon_candidates_with_top_three_and_total_count():
    db = MemoryDB()
    result = ReviewPipeline(db).refresh()
    amazon = review_row(db, "card:1")
    receipt = review_row(db, "receipt:1")

    assert result["amazon_manual_matching_rows"] == 1
    assert result["candidates_generated"] == 3
    assert result["rows_with_candidates"] == 1
    assert amazon[16] == 4
    assert len(amazon[15].splitlines()) == 3
    assert receipt[15:20] == ["", 0, "", "", ""]
    assert len(db.sheets["Amazon照合候補"]) == 3
    assert len({row[0] for row in db.sheets["Amazon照合候補"]}) == 3
    assert all(row[15] in {"baseline", "incremental"} for row in db.sheets["Amazon照合候補"])


def test_selection_label_maps_to_candidate_id_and_survives_refresh():
    db = MemoryDB()
    ReviewPipeline(db).refresh()
    amazon = review_row(db, "card:1")
    chosen_label = db.sheets["Amazon照合候補"][1][17]
    chosen_id = db.sheets["Amazon照合候補"][1][0]
    amazon[9] = "Amazon注文と照合"
    amazon[13] = "ユーザー入力"
    amazon[17] = chosen_label

    ReviewPipeline(db).refresh()
    refreshed = review_row(db, "card:1")
    assert refreshed[9] == "Amazon注文と照合"
    assert refreshed[13] == "ユーザー入力"
    assert refreshed[17] == chosen_label
    assert refreshed[18] == chosen_id
    assert refreshed[19] == "選択済み"
    assert chosen_id not in chosen_label
    assert chosen_label.startswith("#" + chosen_id[-8:])


def test_deleted_candidate_is_not_reassigned_to_another_order():
    db = MemoryDB()
    ReviewPipeline(db).refresh()
    chosen = db.sheets["Amazon照合候補"][0]
    row = review_row(db, "card:1")
    row[17], row[18] = chosen[17], chosen[0]
    db.sheets["Amazon注文"] = [item for item in db.sheets["Amazon注文"] if item[1] != chosen[2]]

    ReviewPipeline(db).refresh()
    refreshed = review_row(db, "card:1")
    assert refreshed[18] == chosen[0]
    assert refreshed[17] == chosen[17]
    assert "候補なし" in refreshed[19]


def test_changed_fingerprint_requires_reselection_and_keeps_original_choice():
    db = MemoryDB()
    ReviewPipeline(db).refresh()
    chosen = db.sheets["Amazon照合候補"][0]
    row = review_row(db, "card:1")
    row[17], row[18] = chosen[17], chosen[0]
    for order in db.sheets["Amazon注文"]:
        if order[1] == chosen[2]:
            order[11] = "changed-hash"

    ReviewPipeline(db).refresh()
    refreshed = review_row(db, "card:1")
    assert refreshed[18] == chosen[0]
    assert "注文内容変更" in refreshed[19]


def test_preview_is_read_only_and_reports_candidate_counts():
    db = MemoryDB()
    result = ReviewPipeline(db).preview()
    assert result["amazon_manual_matching_rows"] == 1
    assert result["candidates_generated"] == 3
    assert db.sheets["要確認"] == []
    assert db.sheets["Amazon照合候補"] == []


def test_amazon_action_is_available_but_phase_two_does_not_update_import_or_expense():
    db = MemoryDB()
    ReviewPipeline(db).refresh()
    row = review_row(db, "card:1")
    row[9] = "Amazon注文と照合"
    result = ReviewApprovalPipeline(db).apply()
    assert "Amazon注文と照合" in ReviewApprovalPipeline.ACTIONS
    assert result["applied"] == 0
    assert result["held"] == 1
    assert db.sheets.get("updates:取込データ", []) == []
    assert db.sheets.get("支出明細", []) == []
