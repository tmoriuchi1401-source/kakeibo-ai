from app.aupay_csv_pipeline import AuPayCsvPipeline, parse_aupay_csv


class FakeDB:
    def __init__(self, existing=None):
        self.existing = existing or []
        self.appended = []
        self.updated = []

    def get(self, rng):
        assert rng == "取込データ!A2:L"
        return self.existing

    def append(self, sheet, rows):
        assert sheet == "取込データ"
        self.appended.extend(rows)

    def update_rows(self, sheet, rows):
        assert sheet == "取込データ"
        self.updated.extend(rows)


def test_parse_aupay_csv(tmp_path):
    path = tmp_path / "aupay.csv"
    path.write_text(
        "■ご利用明細\n"
        ",利用日時,利用店舗,種別,利用額（円）,キャンペーン,外貨,レート,備考\n"
        "1,2026/08/01 12:34,テスト店,支払い,\"1,100\",,,,\n",
        encoding="cp932",
    )
    rows = parse_aupay_csv(str(path))
    assert rows[0]["date"] == "2026-08-01"
    assert rows[0]["amount"] == 1100
    assert rows[0]["import_id"].startswith("aupaycsv:")


def test_existing_mail_payment_is_not_imported_twice(tmp_path):
    path = tmp_path / "aupay.csv"
    path.write_text(
        ",利用日時,利用店舗,種別,利用額（円）,キャンペーン,外貨,レート,備考\n"
        "1,2026/08/01 12:34,テスト 店,支払い,1100,,,,\n",
        encoding="cp932",
    )
    existing = [["mail:1", "", "au PAY", "", "2026-08-01", "テスト店",
                 1100, "au PAY", "unclassified_aupay", "", "", ""]]
    db = FakeDB(existing)
    result = AuPayCsvPipeline(db).import_csv(str(path))
    assert result["covered_by_existing"] == 1
    assert result["confirmed_existing"] == 1
    assert result["new"] == 0
    assert db.appended == []
    assert "CSV確認済=202608" in db.updated[0][1][11]


def test_autocharge_is_kept_but_not_classified_as_expense(tmp_path):
    path = tmp_path / "aupay.csv"
    path.write_text(
        ",利用日時,利用店舗,種別,利用額（円）,キャンペーン,外貨,レート,備考\n"
        "1,2026/08/01 12:34,オートチャージ,オートチャージ,3000,,,,\n",
        encoding="cp932",
    )
    db = FakeDB()
    result = AuPayCsvPipeline(db).import_csv(str(path))
    assert result["autocharges"] == 1
    assert db.appended[0][8] == "transfer_aupay_charge"


def test_duplicate_payments_are_matched_by_occurrence_count(tmp_path):
    path = tmp_path / "aupay.csv"
    path.write_text(
        ",利用日時,利用店舗,種別,利用額（円）,キャンペーン,外貨,レート,備考\n"
        "1,2026/08/01 12:34,テスト店,支払い,1100,,,,\n"
        "2,2026/08/01 18:34,テスト店,支払い,1100,,,,\n",
        encoding="cp932",
    )
    existing = [["mail:1", "", "au PAY", "", "2026-08-01", "テスト店",
                 1100, "au PAY", "unclassified_aupay", "", "", ""]]
    db = FakeDB(existing)

    result = AuPayCsvPipeline(db).import_csv(str(path))

    assert result["covered_by_existing"] == 1
    assert result["new"] == 1
    assert len(db.appended) == 1


def test_existing_csv_row_is_unchanged_on_reimport(tmp_path):
    path = tmp_path / "aupay.csv"
    path.write_text(
        ",利用日時,利用店舗,種別,利用額（円）,キャンペーン,外貨,レート,備考\n"
        "1,2026/08/01 12:34,テスト店,支払い,1100,,,,\n",
        encoding="cp932",
    )
    parsed = parse_aupay_csv(str(path))[0]
    existing = [[parsed["import_id"], "", "au PAY", "", "2026-08-01", "テスト店",
                 1100, "au PAY", "unclassified_aupay", "", "", ""]]
    db = FakeDB(existing)

    result = AuPayCsvPipeline(db).import_csv(str(path))

    assert result["unchanged"] == 1
    assert result["new"] == 0
    assert db.appended == []
