from pathlib import Path

from app.drive_paypay import DrivePayPayPipeline, PROCESSED_PROPERTY
from app.settings import Settings


HEADER = (
    "取引日,出金金額（円）,入金金額（円）,海外出金金額,通貨,変換レート（円）,利用国,"
    "取引内容,取引先,取引方法,支払い区分,利用者,取引番号\n"
)
VALID = (HEADER +
         '2026/08/01 12:34,"1,200",,,,,,支払い,テスト食堂,PayPay残高,一回払い,本人,TX-001\n').encode("utf-8-sig")


class Request:
    def __init__(self, result): self.result = result
    def execute(self): return self.result


class FakeFiles:
    def __init__(self, files):
        self.items = files
        self.updates = []

    def list(self, **kwargs):
        return Request({"files": self.items})

    def update(self, **kwargs):
        self.updates.append(kwargs)
        target = next(item for item in self.items if item["id"] == kwargs["fileId"])
        target["appProperties"] = kwargs["body"]["appProperties"]
        if kwargs.get("addParents"):
            target["parents"] = [kwargs["addParents"]]
        return Request(target)


class FakeDrive:
    def __init__(self, files): self.resource = FakeFiles(files)
    def files(self): return self.resource


class FakeDB:
    def __init__(self): self.rows = []
    def import_ids(self): return {row[0] for row in self.rows}
    def append(self, sheet, rows):
        assert sheet == "取込データ"
        self.rows.extend(rows)


def drive_file(file_id="f1", name="paypay.csv", processed=False):
    properties = {PROCESSED_PROPERTY: "2026-08-20T00:00:00Z"} if processed else {}
    return {"id": file_id, "name": name, "mimeType": "text/csv",
            "parents": ["1234567890source"], "appProperties": properties}


def pipeline(files, payloads, db=None, processed_folder="1234567890processed"):
    return DrivePayPayPipeline(
        "1234567890source", db, processed_folder,
        service=FakeDrive(files), downloader=lambda file_id: payloads[file_id],
    )


def test_preview_is_read_only_and_reports_csv_summary():
    file = drive_file()
    p = pipeline([file], {"f1": VALID})
    result = p.preview()
    assert result["target_csvs"] == 1
    assert result["processable_csvs"] == 1
    assert result["files"][0] == {
        "name": "paypay.csv", "rows": 1, "payments": 1,
        "payment_total": 1200, "processable": True, "skip_reason": "",
    }
    assert p.service.resource.updates == []


def test_drive_csv_is_imported_and_moved_after_success():
    db = FakeDB(); file = drive_file()
    p = pipeline([file], {"f1": VALID}, db)
    result = p.apply()
    assert result["imported_files"] == 1
    assert len(db.rows) == 1
    assert db.rows[0][0] == "paypay:TX-001"
    assert file["parents"] == ["1234567890processed"]
    assert file["appProperties"][PROCESSED_PROPERTY]


def test_same_csv_reprocessing_does_not_duplicate_transactions():
    db = FakeDB()
    first = pipeline([drive_file("f1")], {"f1": VALID}, db)
    first.apply()
    second = pipeline([drive_file("f2")], {"f2": VALID}, db)
    result = second.apply()
    assert len(db.rows) == 1
    assert result["files"][0]["import"]["unchanged"] == 1


def test_non_csv_and_non_paypay_csv_are_skipped():
    files = [drive_file("txt", "memo.txt"), drive_file("csv", "other.csv")]
    p = pipeline(files, {"txt": b"ignored", "csv": b"name,amount\nfoo,100\n"})
    result = p.preview()
    assert result["target_csvs"] == 1
    assert result["processable_csvs"] == 0
    assert result["files"][0]["skip_reason"] == "CSV以外"
    assert "PayPay CSV" in result["files"][1]["skip_reason"]


def test_invalid_csv_does_not_stop_other_files():
    files = [drive_file("bad", "bad.csv"), drive_file("good", "good.csv")]
    db = FakeDB()
    p = pipeline(files, {"bad": b"\x81", "good": VALID}, db)
    result = p.apply()
    assert result["imported_files"] == 1
    assert result["failed_files"] == 1
    assert len(db.rows) == 1


def test_processed_property_skips_file_without_deleting_it():
    file = drive_file(processed=True)
    p = pipeline([file], {"f1": VALID})
    result = p.preview()
    assert result["files"][0]["skip_reason"] == "処理済み"
    assert file in p.service.resource.items


def test_success_without_processed_folder_marks_file_in_place():
    db = FakeDB(); file = drive_file()
    p = pipeline([file], {"f1": VALID}, db, processed_folder="")
    p.apply()
    assert file["parents"] == ["1234567890source"]
    assert file["appProperties"][PROCESSED_PROPERTY]
    assert p.preview()["files"][0]["skip_reason"] == "処理済み"


def test_missing_paypay_folder_does_not_affect_other_settings_validation():
    settings = Settings(spreadsheet_id="sheet", paypay_drive_folder_id="")
    settings.validate(need_sheet=True)


def test_action_only_runs_drive_paypay_when_folder_secret_is_set():
    workflow = Path(".github/workflows/process-receipts.yml").read_text(encoding="utf-8")
    assert "PAYPAY_DRIVE_FOLDER_ID: ${{ secrets.PAYPAY_DRIVE_FOLDER_ID }}" in workflow
    assert "env.PAYPAY_DRIVE_FOLDER_ID != ''" in workflow
    assert workflow.index("python -m app.cli drive-paypay") < workflow.index("python -m app.cli reconcile")
