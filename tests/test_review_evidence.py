from app.reconciliation import parse_import_rows
from app.review_evidence import ReviewResource, recorded_candidate_ids
from app.review_pipeline import ReviewPipeline, review_evidence_cells
from app.sheets import SheetsDB


SID = "s" * 33
FILE = "f" * 33
SHEETS = {"要確認": 11, "取込データ": 22, "支出明細": 33}


def tx(import_id, source, status, target="", source_id=""):
    return parse_import_rows([[import_id, "", source, source_id or import_id,
                               "2026-09-01", "店", 100, "", status, target, "", ""]])[0]


class Drive:
    def __init__(self, parents):
        self.parents = parents
        self.requested = None
    def files(self): return self
    def get(self, **kwargs):
        self.requested = kwargs
        return self
    def execute(self):
        return {"id": FILE, "mimeType": "image/jpeg", "trashed": False,
                "parents": self.parents}


def test_receipt_original_is_the_same_after_folder_move():
    receipt = tx("receipt:" + FILE, "receipt", "要確認", source_id=FILE)
    formulas = []
    for parents in (["inbox"], ["processed"]):
        drive = Drive(parents)
        target, comparison = review_evidence_cells(
            receipt, spreadsheet_id=SID, sheet_ids=SHEETS, review_row=2,
            imports_by_id={}, expense_index={}, has_amazon_candidates=False,
            drive=drive)
        assert comparison == ""
        assert drive.requested["fileId"] == FILE
        formulas.append(target)
    assert formulas[0] == formulas[1]
    assert f"/file/d/{FILE}/view" in formulas[0]
    assert "原本を開く" in formulas[0]


def test_duplicate_links_only_exact_new_and_existing_rows():
    imported = tx("card:new", "au PAYカード", "needs_review_duplicate", "expense:known")
    target, comparison = review_evidence_cells(
        imported, spreadsheet_id=SID, sheet_ids=SHEETS, review_row=4,
        imports_by_id={imported.import_id: imported},
        expense_index={"expense:known": 319}, has_amazon_candidates=False)
    assert "gid=22&range=A2:L2" in target
    assert "gid=33&range=A319:M319" in comparison
    assert "既存明細を見る" in comparison

    _, unknown = review_evidence_cells(
        imported, spreadsheet_id=SID, sheet_ids=SHEETS, review_row=4,
        imports_by_id={}, expense_index={}, has_amazon_candidates=False)
    assert unknown == "比較先を特定できません"


def test_bank_row_does_not_treat_bankpdf_identity_as_drive_id():
    bank = tx("bankpdf:" + FILE, "千葉銀行PDF", "needs_review_transfer")
    target, comparison = review_evidence_cells(
        bank, spreadsheet_id=SID, sheet_ids=SHEETS, review_row=8,
        imports_by_id={}, expense_index={}, has_amazon_candidates=False)
    assert "銀行抽出行を見る" in target and "gid=22&range=A2:L2" in target
    assert "drive.google.com" not in target and comparison == ""


def test_amazon_candidate_link_targets_its_own_review_row():
    card = tx("card:new", "au PAYカード", "amazon_needs_review")
    _, comparison = review_evidence_cells(
        card, spreadsheet_id=SID, sheet_ids=SHEETS, review_row=14,
        imports_by_id={}, expense_index={}, has_amazon_candidates=True)
    assert "gid=11&range=R14:V14" in comparison


def test_reconciliation_candidate_id_is_used_only_if_the_row_exists():
    incoming = tx("card:new", "au PAYカード", "needs_review_duplicate")
    incoming = incoming.__class__(**{**incoming.__dict__,
        "note": "照合=複数候補; 候補=receipt:one,receipt:missing"})
    existing = tx("receipt:one", "receipt", "canonical_receipt")
    assert recorded_candidate_ids(incoming) == ("receipt:one", "receipt:missing")
    _, comparison = review_evidence_cells(
        incoming, spreadsheet_id=SID, sheet_ids=SHEETS, review_row=5,
        imports_by_id={"receipt:one": existing}, expense_index={},
        has_amazon_candidates=False)
    assert "gid=22&range=A2:L2" in comparison
    assert "他1件不明" in comparison


def test_multiple_duplicate_candidates_get_a_scoped_comparison_list():
    def raw(import_id, source, status, note=""):
        return [import_id, "", source, import_id, "2026-09-01", "店", 100,
                "", status, "", "", note]

    class DB:
        sid = SID
        review_sheet_ids = {**SHEETS, "確認材料": 44}
        def __init__(self):
            self.sheets = {
                "取込データ": [
                    raw("card:new", "au PAYカード", "needs_review_duplicate",
                        "照合=複数候補; 候補=receipt:one,receipt:two"),
                    raw("receipt:one", "receipt", "canonical_receipt"),
                    raw("receipt:two", "receipt", "canonical_receipt"),
                ], "要確認": [], "Amazon照合候補": [], "Amazon注文": [],
            }
        def get(self, rng):
            title = rng.split("!")[0]
            return [list(row) for row in self.sheets[title]]
        def categories(self): return []
        def ensure_sheet(self, title, header): self.sheets.setdefault(title, [])
        def clear(self, rng): self.sheets[rng.split("!")[0]] = []
        def append(self, title, rows): self.sheets[title].extend(rows)
        def configure_review_validation(self, *_): pass

    db = DB()
    result = ReviewPipeline(db).refresh()
    assert result["review_rows"] == 1
    assert len(db.sheets["確認材料"]) == 2
    main = db.sheets["要確認"][0]
    assert "gid=44&range=A2:F3" in main[7]
    assert all("候補明細を見る" in row[1] for row in db.sheets["確認材料"])
    assert "gid=22&range=A3:L3" in db.sheets["確認材料"][0][1]
    assert "gid=22&range=A4:L4" in db.sheets["確認材料"][1][1]
    assert all("card:new" not in str(row[0]) for row in db.sheets["確認材料"])
    # A regenerated list keeps the same scoped link and does not duplicate rows.
    ReviewPipeline(db).refresh()
    assert len(db.sheets["確認材料"]) == 2
    assert db.sheets["要確認"][0][7] == main[7]


def test_resource_rejects_unverified_or_malformed_identity():
    try:
        ReviewResource.drive_file(label="原本を開く", role="target", file_id="../bad")
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe file id accepted")


def test_category_rule_representative_uses_exact_ledger_row():
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
    db.sid = SID
    db.svc = Service()
    db._sheet_metadata = lambda: {"sheets": [{"properties": {
        "title": "支出明細", "sheetId": 33}}]}
    db._category_workflow_blocks = lambda: {"rule": ([], [])}
    db._workflow_positions = lambda _: {"rule": {"start": 7}}
    db.link_category_rule_representatives(
        [["条件", "", "", "", "", "", "key", "expense:known"],
         ["条件2", "", "", "", "", "", "key2", "missing"]],
        {"expense:known": (319, ["expense:known", "2026-09-01", "店", "", 100])})
    data = db.svc.body["data"]
    assert len(data) == 1
    assert data[0]["range"].endswith("!A7")
    assert "gid=33&range=A319:M319" in data[0]["values"][0][0]
    assert "代表取引を見る" in data[0]["values"][0][0]
