from app.reconciliation import parse_import_rows
from app.review_pipeline import ReviewApprovalPipeline, receipt_original_link
from app.sheets import HEADERS, SheetsDB
from googleapiclient.errors import HttpError
from httplib2 import Response

TEST_SOURCE_ID = "a" * 33


def receipt(source_id, *, source="receipt", import_id=None):
    raw = [import_id or "receipt:" + source_id, "", source, source_id,
           "2026-08-06", "店舗", 1368, "", "要確認", "", "", ""]
    return parse_import_rows([raw])[0]


class Drive:
    def __init__(self, file):
        self.file = file
        self.requested = []

    def files(self):
        return self

    def get(self, **kwargs):
        self.requested.append(kwargs)
        return self

    def execute(self):
        return self.file


def test_original_link_uses_stable_file_id_for_image_and_pdf():
    for mime in ("image/png", "application/pdf"):
        source_id = TEST_SOURCE_ID
        drive = Drive({"id": source_id, "mimeType": mime, "trashed": False})
        link = receipt_original_link(receipt(source_id), drive)
        assert link == (f'=HYPERLINK("https://drive.google.com/file/d/{source_id}/view",'
                        '"画像を開く")')
        assert drive.requested[0]["fileId"] == source_id


def test_missing_mismatched_or_trashed_source_is_visible():
    source_id = TEST_SOURCE_ID
    assert receipt_original_link(receipt("missing")) == "原本リンクなし"
    assert receipt_original_link(receipt(source_id, import_id="receipt:other")) == "原本リンクなし"
    assert receipt_original_link(receipt(source_id), Drive({
        "id": source_id, "mimeType": "image/png", "trashed": True})) == "原本リンクなし"
    assert receipt_original_link(receipt(source_id, source="au PAY")) == "対象外"

    class Denied(Drive):
        def execute(self):
            raise HttpError(Response({"status": 403}), b"denied")

    assert receipt_original_link(receipt(source_id), Denied({})) == "原本リンクなし"


def test_legacy_schema_inserts_g_without_rewriting_review_inputs():
    class Request:
        def __init__(self, result=None): self.result = result or {}
        def execute(self): return self.result

    class Service:
        def __init__(self): self.requests = []
        def spreadsheets(self): return self
        def batchUpdate(self, **kwargs):
            self.requests.extend(kwargs["body"]["requests"])
            return Request()

    db = SheetsDB.__new__(SheetsDB)
    db.sid = "sheet"
    db.svc = Service()
    db._sheet_metadata_cache = {"sheets": [{"properties": {
        "title": "要確認", "sheetId": 123}}]}
    old_header = HEADERS["要確認"][:6] + HEADERS["要確認"][7:]
    db.get = lambda _: [old_header]
    db._ensure_review_original_column()
    assert db.svc.requests == [{"insertDimension": {"range": {
        "sheetId": 123, "dimension": "COLUMNS", "startIndex": 6, "endIndex": 7},
        "inheritFromBefore": True}}]
    # A retry after the insert committed must not shift the columns again.
    db.get = lambda _: [old_header[:6] + [""] + old_header[6:]]
    db._ensure_review_original_column()
    assert len(db.svc.requests) == 1


def test_approval_updates_do_not_replace_original_formula():
    class Request:
        def execute(self): return {}

    class Service:
        def __init__(self): self.body = None
        def spreadsheets(self): return self
        def values(self): return self
        def batchUpdate(self, **kwargs):
            self.body = kwargs["body"]
            return Request()

    db = SheetsDB.__new__(SheetsDB)
    db.sid = "sheet"
    db.svc = Service()
    db.update_rows("要確認", [(2, ["id"] + [""] * 14 + ["保留"] + [""] * 4 + ["選択済み"])])
    assert [item["range"] for item in db.svc.body["data"]] == ["'要確認'!P2", "'要確認'!U2"]
    assert all("G2" not in item["range"] for item in db.svc.body["data"])


def test_approval_migrates_before_apply_and_preview_reads_legacy_safely():
    db = SheetsDB.__new__(SheetsDB)
    legacy = HEADERS["要確認"][:6] + HEADERS["要確認"][7:]
    old_row = ["receipt:synthetic", "高", "2026-08-06", "receipt", "店", 1368,
               "要確認", "確認", "", "保留"]
    migrated = False

    def get(rng):
        if rng == "要確認!1:1":
            return [HEADERS["要確認"] if migrated else legacy]
        if rng == "要確認!A2:T":
            return [old_row]
        if rng == "要確認!A2:U":
            return [old_row[:6] + ["原本リンクなし"] + old_row[6:]]
        raise AssertionError(rng)

    def ensure_sheet(title, header):
        nonlocal migrated
        assert title == "要確認" and header == HEADERS["要確認"]
        migrated = True

    db.get = get
    db.ensure_sheet = ensure_sheet
    approval = ReviewApprovalPipeline(db)
    assert approval._review_rows(migrate=False)[0][10] == "保留"
    assert not migrated
    assert approval._review_rows(migrate=True)[0][10] == "保留"
    assert migrated
