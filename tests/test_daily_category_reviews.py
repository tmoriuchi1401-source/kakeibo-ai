from copy import deepcopy
import re

import pytest

from app.daily_sheets import read_existing_reviews
from app.daily_view import review_page
from app.monthly_projection import ProjectionError
from app.sheets import CATEGORY_WORKFLOW_MARKERS as MARKERS


class Queues:
    sid = "source"

    def __init__(self, tables, extents=None):
        self.tables = tables
        self.reads = []
        self.metadata = {"sheets": [{"properties": {"title": title, "sheetId": 400 + i,
            "gridProperties": {"rowCount": (extents or {}).get(title, max(rows, default=1))}}}
            for i, (title, rows) in enumerate(tables.items())]}

    def _sheet_metadata(self):
        return self.metadata

    def get_raw(self, a1):
        self.reads.append(a1)
        title, first, column, last = re.fullmatch(r"'(.+)'!A(\d+):([A-Z])(\d+)", a1).groups()
        rows = [self.tables[title].get(n, [])[:ord(column) - 64] for n in range(int(first), int(last) + 1)]
        while rows and not rows[-1]:
            rows.pop()
        return deepcopy(rows)


def rule(key, state="登録待ち", future=False, past=False):
    return ["店舗・条件", "条件の説明\n" + state, "食費", "食品", future, past, key, "expense-id",
            "2010-01-01", "card", "service", "private-approval-snapshot"]


def test_three_stages_use_distinct_fixed_ids_and_keep_source_inputs():
    rows = {1: [MARKERS["rule"]], 2: ["header"],
            3: rule("group:choice", "カテゴリを選択"),
            4: rule("optional", "未登録"), 5: rule("registered", "登録済み"),
            6: rule("both", future=True, past=True),
            7: rule("held", "held: stale_source"),
            8: rule("changed", "再承認が必要（条件またはカテゴリが変更）"),
            9: [MARKERS["backfill"]], 10: ["header"],
            11: ["条件", "食費", "2010-01", "2010-02", True, "", "service", "both"],
            12: ["任意プレビュー", "食費", "", "", False, "", "service", "optional"],
            13: ["条件\nheld: invalid_period", "食費", "不正", "", False, "", "service", "bad"],
            14: [MARKERS["confirm"]], 15: ["header"],
            16: ["2010年の固定プレビュー", "2件 / 200円", False, "previewed", "", "", "request"],
            17: ["2010年の明細", "100円", "", "内訳"],
            18: ["済", "2件", False, "complete", "", "", "done"]}
    db = Queues({"カテゴリ操作": rows})
    before = deepcopy(db.tables)
    reviews = read_existing_reviews(db)
    assert len(reviews) == 7
    indexed = {item.fixed_id: item for item in reviews}
    assert indexed["分類:rule:group:choice"].status == "要確認"
    assert indexed["分類:rule:both"].status == "処理待ち"
    assert "別承認" in indexed["分類:rule:both"].detail
    assert indexed["分類:rule:held"].status == "保留"
    assert indexed["分類:backfill:bad"].status == "保留"
    assert indexed["分類:confirm:request"].url.endswith("gid=400&range=A16")
    assert all("snapshot" not in item.detail for item in reviews)
    assert db.tables == before and db.reads == ["'カテゴリ操作'!A1:H18"]


def test_sparse_pages_and_marker_at_boundary_preserve_real_row_links_and_all_years():
    rows = {999: [MARKERS["rule"]], 1000: ["header"], 1001: rule("group:old")}
    rows.update({2000: [MARKERS["confirm"]], 2001: ["header"]})
    rows.update({2002 + n: ["2010年の要求", "1件", False, "previewed", "", "", f"old-{n}"] for n in range(123)})
    db = Queues({"カテゴリ操作": rows}, {"カテゴリ操作": 4001})
    reviews = read_existing_reviews(db)
    last, total, pages = review_page(reviews, 3)
    assert (len(last), total, pages) == (24, 124, 3)
    assert reviews[0].url.endswith("range=A1001") and last[-1].url.endswith("range=A2124")
    assert db.reads == [f"'カテゴリ操作'!A{a}:H{b}" for a, b in [(1,1000),(1001,2000),(2001,3000),(3001,4000),(4001,4001)]]
    # Sorting/inserting a row changes only the location, never the review ID.
    db.tables["カテゴリ操作"][1010] = db.tables["カテゴリ操作"].pop(1001)
    after = read_existing_reviews(db)
    assert after[0].fixed_id == reviews[0].fixed_id and after[0].url.endswith("range=A1010")


@pytest.mark.parametrize("compact", [False, True])
def test_category_layout_does_not_change_identity_or_read_approval_payload(compact):
    row = rule("group:stable")
    if compact:
        row[2:4] = ["食費｜食品", ""]
    db = Queues({"カテゴリ操作": {1:[MARKERS["rule"]], 2:["header"], 3:row}})
    assert read_existing_reviews(db)[0].fixed_id == "分類:rule:group:stable"
    assert db.reads == ["'カテゴリ操作'!A1:H3"]


def test_durable_states_override_stale_ui_and_missing_confirmation_remains_visible():
    db = Queues({"カテゴリ操作": {1:[MARKERS["confirm"]], 2:["header"],
        3:["stale", "2件", False, "previewed", "", "", "done"],
        4:["partial", "2件", False, "previewed", "", "", "partial"],
        5:["held", "2件", False, "held", "", "", "held"],
        6:["inconsistent", "2件", False, "complete", "", "", "inconsistent"]},
        "カテゴリ過去反映要求": {2:["done", "old", "complete"], 3:["partial", "old", "partial"],
            4:["held", "old", "previewed"], 5:["missing", "old", "previewed"],
            6:["inconsistent", "old", "previewed"], 1002:["missing-partial", "old", "partial"]},
        "支出明細": {1:["must not read"]}, "カテゴリ過去反映対象": {1:["must not read"]}})
    items = {r.fixed_id:r for r in read_existing_reviews(db)}
    assert len(items) == 5 and "分類:confirm:done" not in items
    assert items["分類:confirm:partial"].status == "一部未反映"
    assert items["分類:confirm:held"].status == "保留"
    assert items["分類:confirm:inconsistent"].status == "保留"
    assert "再表示" in items["分類:confirm:missing"].detail
    assert items["分類:confirm:missing-partial"].status == "一部未反映"
    assert db.reads[:2] == ["'カテゴリ過去反映要求'!A2:C1001", "'カテゴリ過去反映要求'!A1002:C1002"]
    assert len(db.reads) == 3


def test_legacy_tabs_work_but_are_not_double_counted_after_unification():
    db = Queues({"カテゴリ自動分類": {2:rule("pending", future=True)},
        "カテゴリ過去反映": {2:["条件", "食費", "2010-01", "2010-02", True, "service", "preview"]},
        "カテゴリ過去反映確認": {2:["要求", "1件", True, "previewed", "request"]}})
    reviews = read_existing_reviews(db)
    assert [r.fixed_id for r in reviews] == ["分類:rule:pending", "分類:backfill:preview", "分類:confirm:request"]
    assert all(r.status == "処理待ち" for r in reviews)
    db = Queues({**db.tables, "カテゴリ操作": {1:[MARKERS["rule"]], 2:["header"], 3:rule("pending", future=True)}})
    assert len(read_existing_reviews(db)) == 1 and len(db.reads) == 1


def test_completed_past_preview_suffix_does_not_hide_unresolved_future_approval():
    db = Queues({"カテゴリ操作": {1:[MARKERS["rule"]], 2:["header"],
        3:rule("held", "held: stale\n過去プレビュー: previewed / 要求=abc"),
        4:rule("done", "registered\n過去プレビュー: previewed / 要求=def")}})
    reviews = read_existing_reviews(db)
    assert len(reviews) == 1 and reviews[0].fixed_id == "分類:rule:held"


def test_duplicate_durable_request_is_not_silently_overwritten():
    db = Queues({"カテゴリ過去反映要求": {2:["same", "old", "previewed"], 3:["same", "old", "complete"]}})
    with pytest.raises(ProjectionError, match="duplicate_category_review_request"):
        read_existing_reviews(db)
