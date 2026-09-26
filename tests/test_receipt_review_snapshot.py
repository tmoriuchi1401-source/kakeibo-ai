"""The held receipt's item candidate, header and review link share one identity."""
from copy import deepcopy

import pytest

from app.models import ReceiptItem, ReceiptResult
from app.receipt_validation import validate_receipt_result
from app.receipt_review_snapshot import (SHEET, HEADERS, candidate_rows,
                                         save_candidate, verified_candidate_range, yen)
from app.reconciliation import parse_import_rows
from app.review_pipeline import ReviewPipeline, review_evidence_cells


FILE = "f" * 33
SID = "s" * 33
HASH = "a" * 64
CATEGORIES = [("食費", "食品")]


def result(amounts, total):
    return ReceiptResult(merchant="合成店舗", date="2026-08-06", total=total,
                         items=[ReceiptItem(name=f"品目{i}", amount=amount,
                                            major_category="食費", minor_category="食品")
                                for i, amount in enumerate(amounts, 1)])


@pytest.mark.parametrize("amounts,total,expected", [
    ([100, 268], 368, True),
    ([100, 268], 369, False),
    ([643, -65, 459, 199, 198, 99, 199, 138, -2, -500], 1368, True),
    ([198, 138, -28, 98, 98, 98, 98, 98, 98, 71], 967, True),
])
def test_all_receipt_items_are_summed_including_tax_and_discounts(amounts, total, expected):
    ok, notes = validate_receipt_result(result(amounts, total), CATEGORIES)
    assert ok is expected
    assert bool([note for note in notes if note.startswith("明細合計")]) is not expected


@pytest.mark.parametrize("formatted", ["1,368", "¥1,368", "1,368円", "1368.0", 1368])
def test_equivalent_yen_renderings(formatted):
    assert yen(formatted) == 1368


class CandidateDB:
    def __init__(self):
        self.rows = []
    def ensure_sheet(self, sheet, headers):
        assert (sheet, headers) == (SHEET, HEADERS)
    def get(self, rng):
        assert rng == f"'{SHEET}'!A2:K"
        return deepcopy(self.rows)
    def append_raw(self, sheet, rows):
        assert sheet == SHEET
        self.rows.extend(deepcopy(rows))


def receipt_tx(item_sum=1230, total=1368, source=FILE):
    return parse_import_rows([[
        "receipt:" + source, "", "receipt", source, "2026-08-06", "合成店舗",
        total, "", "要確認", "", HASH,
        f"明細合計{item_sum}≠レシート合計{total}",
    ]])[0]


def evidence(tx, rows):
    return review_evidence_cells(
        tx, spreadsheet_id=SID,
        sheet_ids={"要確認": 11, "取込データ": 22, SHEET: 44},
        review_row=2, imports_by_id={tx.import_id: tx}, expense_index={},
        has_amazon_candidates=False, drive=None,
        receipt_candidate_rows=list(enumerate(rows, 2)),
    )[1]


def test_candidate_snapshot_is_idempotent_and_rejects_changed_replay():
    db = CandidateDB()
    parsed = result([500, 730], 1368)
    save_candidate(db, "receipt:" + FILE, HASH, parsed)
    assert len(db.rows) == 2
    db.rows[0][5] = "1"  # Sheets may format 1.0 as 1.
    db.rows[0][6] = "¥500"
    save_candidate(db, "receipt:" + FILE, HASH, parsed)
    assert len(db.rows) == 2
    with pytest.raises(ValueError, match="replay_conflict"):
        save_candidate(db, "receipt:" + FILE, HASH, result([500, 731], 1368))
    assert len(db.rows) == 2


def test_exact_candidate_lines_and_note_are_required_for_link():
    tx = receipt_tx()
    own = candidate_rows(tx.import_id, HASH, result([500, 730], 1368))
    other = candidate_rows("receipt:" + "g" * 33, HASH, result([999], 999))
    assert verified_candidate_range(tx, list(enumerate(own, 2))) == (2, 3, 1230)
    assert "gid=44&range=A2:K3" in evidence(tx, own + other)
    assert "解析候補1,230円の各行" in evidence(tx, own + other)
    assert evidence(tx, other) == '=HYPERLINK("https://docs.google.com/spreadsheets/d/' + SID + '/edit#gid=22&range=L2","判定時の解析候補1,230円とレシート1,368円")'
    changed = deepcopy(own)
    changed[1][6] = 731
    assert evidence(tx, changed) == "解析候補の保存内容と判定記録が不整合"
    wrong_hash = deepcopy(own)
    wrong_hash[0][2] = "b" * 64
    assert evidence(tx, wrong_hash) == "解析候補の保存内容と判定記録が不整合"
    assert "gid=44" not in evidence(receipt_tx(source="g" * 33), own)


def test_review_refresh_reuses_verified_candidate_and_drops_resolved_status(monkeypatch):
    monkeypatch.setattr("app.review_pipeline.receipt_original_link",
                        lambda _, drive=None: "原本を開く")
    tx = receipt_tx()

    class DB:
        sid = SID
        review_sheet_ids = {"要確認": 11, "取込データ": 22, SHEET: 44}
        def __init__(self):
            self.sheets = {
                "取込データ": [tx.row.copy()],
                "要確認": [], "Amazon照合候補": [], "Amazon注文": [],
                SHEET: candidate_rows(tx.import_id, HASH, result([500, 730], 1368)),
            }
        def get(self, rng):
            return deepcopy(self.sheets[rng.split("!")[0].strip("'")])
        def categories(self): return CATEGORIES
        def ensure_sheet(self, title, header): self.sheets.setdefault(title, [])
        def clear(self, rng): self.sheets[rng.split("!")[0].strip("'")] = []
        def append(self, title, rows): self.sheets[title].extend(deepcopy(rows))
        def configure_review_validation(self, *_): pass

    db = DB()
    ReviewPipeline(db).refresh()
    first = db.sheets["要確認"][0]
    assert "gid=44&range=A2:K3" in first[7]
    first[11] = "保留"
    first[15] = "本人メモ"
    ReviewPipeline(db).refresh()
    second = db.sheets["要確認"][0]
    assert second[7] == first[7]
    assert second[11] == "保留" and second[15] == "本人メモ"

    # A source status change cannot discard an operator's pending input.
    db.sheets["取込データ"][0][8] = "解析済"
    db.sheets["取込データ"][0][11] = ""
    ReviewPipeline(db).refresh()
    assert db.sheets["要確認"][0][11] == "保留"
    assert db.sheets["要確認"][0][15] == "本人メモ"

    # Once the operator has cleared those inputs, refresh drops the resolved
    # generated review; neither the candidate nor ledger amounts are changed.
    db.sheets["要確認"][0][11] = ""
    db.sheets["要確認"][0][15] = ""
    ReviewPipeline(db).refresh()
    assert db.sheets["要確認"] == []
    assert db.sheets[SHEET][0][6] == 500

