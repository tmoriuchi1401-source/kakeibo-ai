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
        if sheet in self.sheets:
            for row_num, row in rows:
                index = row_num - 2
                if 0 <= index < len(self.sheets[sheet]):
                    self.sheets[sheet][index] = list(row)

    def expense_index(self):
        return {row[0]:index for index,row in enumerate(self.sheets.get("支出明細",[]),start=2)}
    def expense_rows_for_import(self, import_id):
        return [(index,row) for index,row in enumerate(self.sheets.get("支出明細",[]),start=2)
                if len(row)>10 and row[10]==import_id]
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


def test_amazon_action_without_selection_does_not_update_import_or_expense():
    db = MemoryDB()
    ReviewPipeline(db).refresh()
    row = review_row(db, "card:1")
    row[9] = "Amazon注文と照合"
    result = ReviewApprovalPipeline(db).apply()
    assert "Amazon注文と照合" in ReviewApprovalPipeline.ACTIONS
    assert result["applied"] == 0
    assert result["errors"] == 1
    assert db.sheets.get("updates:取込データ", []) == []
    assert db.sheets.get("支出明細", []) == []


def select_candidate(db, card_id="card:1", order_id="ORDER-1"):
    if not db.sheets["Amazon照合候補"]:
        ReviewPipeline(db).refresh()
    candidate = next(row for row in db.sheets["Amazon照合候補"]
                     if row[1] == card_id and row[2] == order_id)
    row = review_row(db, card_id)
    row[9] = "Amazon注文と照合"
    row[17] = candidate[17]
    ReviewPipeline(db).refresh()
    return candidate


def test_phase_three_valid_candidate_updates_only_card_and_review():
    db = MemoryDB()
    original_orders = [list(row) for row in db.sheets["Amazon注文"]]
    candidate = select_candidate(db)
    preview = ReviewApprovalPipeline(db).preview()
    assert preview == {
        "amazon_manual_selected": 1, "amazon_manual_valid": 1,
        "amazon_manual_invalid": 0, "amazon_manual_conflicts": 0,
        "amazon_manual_would_match": 1,
    }

    result = ReviewApprovalPipeline(db).apply()
    card_row = db.sheets["取込データ"][0]
    reviewed = review_row(db, "card:1")
    assert result["amazon_manual_matched"] == 1
    assert card_row[8] == "matched_amazon"
    assert card_row[9] == "amazon:ORDER-1"
    assert f"手動照合={candidate[0]}" in card_row[11]
    assert "Amazonキー=amazon:ORDER-1" in card_row[11]
    assert "カード側は支出計上しない" in card_row[11]
    assert "カード額:" in card_row[11] and "差額率:" in card_row[11]
    assert "商品数:" in card_row[11] and "データ種別:" in card_row[11]
    assert reviewed[14] == "反映済み" and reviewed[19] == "反映済み"
    assert db.sheets["Amazon注文"] == original_orders
    assert db.sheets.get("支出明細", []) == []

    ReviewPipeline(db).refresh()
    assert all(row[0] != "card:1" for row in db.sheets["要確認"])


def test_existing_card_expense_is_excluded_but_no_expense_is_created():
    db = MemoryDB()
    db.sheets["支出明細"] = [[
        "E-card", "2026-08-20", "Amazon", "", 1000, "", "", "Visa",
        "au PAYカード", "", "card:1", "", "active",
    ]]
    select_candidate(db)
    result = ReviewApprovalPipeline(db).apply()
    assert result["expenses_created"] == 0
    assert result["expenses_excluded"] == 1
    assert len(db.sheets["支出明細"]) == 1
    assert db.sheets["支出明細"][0][12] == "duplicate_excluded"


def test_invalid_deleted_and_changed_candidates_do_not_update_import():
    for mutation in ("invalid_id", "deleted", "changed"):
        db = MemoryDB(); candidate = select_candidate(db)
        if mutation == "invalid_id":
            row = review_row(db, "card:1"); row[18] = "amcand:invalid"; row[19] = "選択済み"
        elif mutation == "deleted":
            db.sheets["Amazon注文"] = [row for row in db.sheets["Amazon注文"]
                                           if row[1] != candidate[2]]
        else:
            next(row for row in db.sheets["Amazon注文"] if row[1] == candidate[2])[11] = "changed"
        result = ReviewApprovalPipeline(db).apply()
        assert result["amazon_manual_invalid"] == 1
        assert db.sheets["取込データ"][0][8] == "amazon_unmatched"
        assert review_row(db, "card:1")[14].startswith("未反映:")


def test_resolved_manual_refund_and_installment_cards_are_rejected_at_apply():
    mutations = [
        lambda row: row.__setitem__(8, "matched_amazon"),
        lambda row: row.__setitem__(11, "手動照合=legacy"),
        lambda row: row.__setitem__(11, "返金"),
        lambda row: row.__setitem__(7, "分割払い"),
    ]
    for mutate in mutations:
        db = MemoryDB(); select_candidate(db)
        mutate(db.sheets["取込データ"][0])
        result = ReviewApprovalPipeline(db).apply()
        assert result["amazon_manual_invalid"] == 1
        assert "updates:取込データ" not in db.sheets or not db.sheets["updates:取込データ"]


def test_used_order_and_batch_conflicts_reject_all_rows():
    db = MemoryDB()
    db.sheets["取込データ"].append(import_row(
        "old", "au PAYカード", "2026-08-10", 1000, "matched_amazon",
        note="手動照合=old; Amazonキー=amazon:ORDER-1",
    ))
    select_candidate(db)
    assert ReviewApprovalPipeline(db).apply()["amazon_manual_invalid"] == 1
    assert db.sheets["取込データ"][0][8] == "amazon_unmatched"

    db = MemoryDB()
    db.sheets["取込データ"].append(import_row(
        "card:2", "au PAYカード", "2026-08-20", 1000, "amazon_unmatched",
    ))
    ReviewPipeline(db).refresh()
    select_candidate(db, "card:1", "ORDER-1")
    select_candidate(db, "card:2", "ORDER-1")
    preview = ReviewApprovalPipeline(db).preview()
    assert preview["amazon_manual_conflicts"] == 2
    result = ReviewApprovalPipeline(db).apply()
    assert result["amazon_manual_invalid"] == 2
    assert db.sheets["取込データ"][0][8] == "amazon_unmatched"
    assert db.sheets["取込データ"][2][8] == "amazon_unmatched"


def test_amazon_unmatched_cannot_be_manually_created_as_expense():
    db = MemoryDB(); ReviewPipeline(db).refresh()
    row = review_row(db, "card:1")
    row[9] = "支出として計上"; row[11] = "日用品｜雑貨"
    result = ReviewApprovalPipeline(db).apply()
    assert result["errors"] == 1
    assert db.sheets["取込データ"][0][8] == "amazon_unmatched"
    assert db.sheets.get("支出明細", []) == []
    assert "Amazon注文と照合" in review_row(db, "card:1")[14]
