from pathlib import Path

import pytest

import app.drive_paypay as drive_paypay
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

    def get(self, fileId, **kwargs):
        if fileId == "1234567890processed":
            return Request({"id": fileId, "mimeType": "application/vnd.google-apps.folder",
                            "capabilities": {"canAddChildren": True}})
        return Request(next(item for item in self.items if item["id"] == fileId))

    def update(self, **kwargs):
        self.updates.append(kwargs)
        target = next(item for item in self.items if item["id"] == kwargs["fileId"])
        target["appProperties"] = kwargs["body"]["appProperties"]
        if kwargs.get("addParents"):
            target["parents"] = sorted((set(target["parents"]) - {kwargs["removeParents"]}) | {kwargs["addParents"]})
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


def test_empty_paypay_inbox_is_safe_noop():
    db = FakeDB()
    result = pipeline([], {}, db).apply()
    assert result["target_csvs"] == 0
    assert result["imported_files"] == 0
    assert result["failed_files"] == 0
    assert db.rows == []


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


def test_temporary_csv_is_removed_when_parsing_raises(monkeypatch):
    created = []
    original = drive_paypay.tempfile.NamedTemporaryFile

    def tracked_temporary_file(*args, **kwargs):
        handle = original(*args, **kwargs)
        created.append(Path(handle.name))
        return handle

    monkeypatch.setattr(drive_paypay.tempfile, "NamedTemporaryFile", tracked_temporary_file)
    result = pipeline([drive_file()], {"f1": b"not,paypay,csv\n"}).preview()

    assert result["processable_csvs"] == 0
    assert created
    assert all(not path.exists() for path in created)


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


def test_old_processed_csv_moves_without_download_or_append():
    p = pipeline([drive_file(processed=True)], {}, FakeDB())
    assert p.apply()["archived_files"] == 1
    assert p.db.rows == []
    assert p.service.resource.items[0]["parents"] == ["1234567890processed"]


def test_move_preserves_unrelated_parent_and_properties():
    file = drive_file()
    file["parents"].append("unrelated-parent")
    file["appProperties"]["other"] = "keep"
    p = pipeline([file], {"f1": VALID}, FakeDB())
    assert p.apply()["failed_files"] == 0
    assert set(file["parents"]) == {"1234567890processed", "unrelated-parent"}
    assert file["appProperties"]["other"] == "keep"


def test_failed_move_replay_does_not_append_twice(monkeypatch):
    p = pipeline([drive_file()], {"f1": VALID}, FakeDB())
    update = p.service.resource.update
    monkeypatch.setattr(p.service.resource, "update", lambda **kw: (_ for _ in ()).throw(TimeoutError()))
    assert p.apply()["failed_files"] == 1
    assert len(p.db.rows) == 1
    assert p.service.resource.items[0]["parents"] == ["1234567890source"]
    monkeypatch.setattr(p.service.resource, "update", update)
    assert p.apply()["failed_files"] == 0
    assert len(p.db.rows) == 1
    assert p.service.resource.items[0]["parents"] == ["1234567890processed"]


def test_missing_import_readback_never_moves(monkeypatch):
    p = pipeline([drive_file()], {"f1": VALID}, FakeDB())
    monkeypatch.setattr(p.db, "append", lambda *args: None)
    assert p.apply()["failed_files"] == 1
    assert p.service.resource.updates == []


def test_same_folder_is_rejected_before_import():
    p = pipeline([drive_file()], {"f1": VALID}, FakeDB(), processed_folder="1234567890source")
    with pytest.raises(ValueError, match="processed_folder_is_inbox"):
        p.apply()
    assert p.db.rows == []


def test_production_paypay_uses_its_dedicated_folder(monkeypatch):
    from app import production_source
    observed = {}
    class Pipeline:
        def __init__(self, folder, db, processed):
            observed.update(folder=folder, processed=processed)
        def apply(self):
            return dict(imported_files=0, skipped_files=0, failed_files=0)
    monkeypatch.setattr(production_source, "DrivePayPayPipeline", Pipeline)
    monkeypatch.setattr(production_source, "SheetsDB", lambda sid: object())
    settings = Settings(spreadsheet_id="sheet", paypay_drive_folder_id="1234567890source",
                        processed_drive_folder_id="receipt-folder",
                        paypay_processed_drive_folder_id="1234567890processed")
    production_source.paypay(settings, apply=True)
    assert observed["processed"] == "1234567890processed"


def test_move_response_loss_after_commit_is_not_reimported(monkeypatch):
    p = pipeline([drive_file()], {"f1": VALID}, FakeDB())
    update = p.service.resource.update
    def lost(**kwargs):
        update(**kwargs)
        raise TimeoutError()
    monkeypatch.setattr(p.service.resource, "update", lost)
    assert p.apply()["failed_files"] == 1
    monkeypatch.setattr(p.service.resource, "update", update)
    # Even a stale listing must not append or repeat the move.
    assert p.apply()["archived_files"] == 1
    assert len(p.db.rows) == 1
    assert len(p.service.resource.updates) == 1


def test_move_readback_mismatch_is_reported(monkeypatch):
    p = pipeline([drive_file()], {"f1": VALID}, FakeDB())
    monkeypatch.setattr(p.service.resource, "update", lambda **kw: Request({"id": "f1"}))
    assert p.apply()["failed_files"] == 1
    assert "processed_move_readback_failed" in p.apply()["files"][0]["skip_reason"]
    assert len(p.db.rows) == 1


def test_move_uses_current_unrelated_metadata():
    from copy import deepcopy
    p = pipeline([drive_file()], {"f1": VALID}, FakeDB())
    listed = deepcopy(p.service.resource.items[0])
    listed["appProperties"]["other"] = "stale"
    p.service.resource.items[0]["appProperties"]["other"] = "current"
    p._mark_processed(listed)
    assert p.service.resource.items[0]["appProperties"]["other"] == "current"
