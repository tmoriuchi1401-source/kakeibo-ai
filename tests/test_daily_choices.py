from copy import deepcopy
from hashlib import sha256

import pytest

from app.daily_choices import (MARKER, category_id,
    installed, render_requests, page_options, upgrade_requests)
from app.daily_view import SHEETS, HOME_LOOKUP_ROW
from app.monthly_projection import Category, CategoryCatalog, ProjectionError
from app.projection_refresh import catalog_document, load_catalog
from test_daily_sheets import daily_setup, Grid
from test_projection_refresh import initialized, row


def catalog():return CategoryCatalog(Category(f"CAT-{i:032x}","分類",str(i),active=i%2==0) for i in range(1205))


def helper(requests):
    block=next(q["updateCells"] for q in requests if q.get("updateCells",{}).get("range",{}).get("sheetId")==SHEETS["_候補"][0]
               and q["updateCells"]["range"]["startRowIndex"]==1)
    return [[next(iter(c.get("userEnteredValue",{}).values()),"") for c in r["values"]] for r in block["rows"]]


def test_every_category_and_expense_is_reachable_with_constant_grid_and_small_dropdowns():
    cat=catalog();expenses=[f"EXP-{i}｜2026-09-01 店舗" for i in range(1201)]
    columns=[set() for _ in range(4)]
    for page in range(1,8):
        controls=dict(choice_pages_enabled=True,history_choice_page=page,trend_choice_page=page,
                      correction_category_page=page,expense_choice_page=page)
        requests=render_requests(cat,expenses,controls)
        rows=helper(requests)
        assert len(rows)==HOME_LOOKUP_ROW-2 and all(len(r)==4 for r in rows)
        for column in range(4):columns[column].update(r[column] for r in rows if r[column])
        assert all(len(q["setDataValidation"]["rule"]["condition"].get("values",[]))<=43
                   for q in requests if "setDataValidation" in q and "rule" in q["setDataValidation"])
        assert not any("updateSheetProperties" in q or "appendDimension" in q for q in requests)
    assert columns[0]==columns[1]=={"すべて",*(c.label for c in cat.categories)}
    assert columns[2]=={c.label for c in cat.categories if c.active}
    assert columns[3]==set(expenses)
    assert sum(rows*cols for _,rows,cols in SHEETS.values())==12_630


def test_off_page_selections_and_alias_survive_without_writing_controls_or_form():
    cat=catalog().rename("CAT-"+"0"*32,"分類","改名後")
    controls=dict(choice_pages_enabled=True,history_choice_page=2,trend_choice_page=3,
        correction_category_page=2,expense_choice_page=2,category="分類｜0",trend_category="分類｜1000",
        correction_category="分類｜0",correction_choice="EXP-old｜昔の選択")
    requests=render_requests(cat,["EXP-new"],controls);rows=helper(requests)
    for column,selection in enumerate(["分類｜0","分類｜1000","分類｜0","EXP-old｜昔の選択"]):
        assert selection in [r[column] for r in rows]
    assert category_id(cat,"分類｜0")==category_id(cat,"分類｜改名後")
    forbidden={(SHEETS[title][0],r-1,1) for title,r in [("履歴",4),("履歴",9),("推移",4),("推移",17),
        ("確認",61),("確認",64),("確認",73),("確認",75)]}
    for q in requests:
        if "updateCells" not in q:continue
        rect=q["updateCells"]["range"]
        assert not any(s==rect["sheetId"] and rect["startRowIndex"]<=r<rect["endRowIndex"] and
                       rect["startColumnIndex"]<=c<rect["endColumnIndex"] for s,r,c in forbidden)


def test_empty_candidates_remove_stale_validation_and_typing_invalid_category_does_not_clear_input():
    requests=render_requests(CategoryCatalog([]),[],{"choice_pages_enabled":True,"correction_category":"編集中"})
    cleared=[r["setDataValidation"]["range"]["startRowIndex"] for r in requests if "setDataValidation" in r and "rule" not in r["setDataValidation"]]
    assert cleared==[63,60]
    assert all("編集中" not in r for r in helper(requests))


def test_large_catalog_requires_page_controls_before_live_activation():
    with pytest.raises(ProjectionError,match="paging_required"):render_requests(catalog(),[],{})


def test_large_page_counts_keep_a_small_menu_and_allow_arbitrary_page_entry():
    assert len(page_options(1000,100_000))<=43
    assert {1,980,1000,1020,100_000}<=set(page_options(1000,100_000))


def test_upgrade_preserves_existing_reserved_text_and_rejects_wrong_binding():
    daily,grid,_,_=daily_setup();assert upgrade_requests(daily)
    grid.put(75,2,"本人メモ")
    with pytest.raises(ProjectionError,match="occupied"):upgrade_requests(daily)
    daily.verify=lambda:{"developerMetadata":[{"metadataKey":MARKER,"metadataValue":"wrong"}]}
    with pytest.raises(ProjectionError,match="binding_invalid"):installed(daily)
    daily.verify=lambda:{"developerMetadata":[{"metadataKey":MARKER,"metadataValue":sha256(b"source").hexdigest()}]}
    assert upgrade_requests(daily)==[]


def test_real_daily_refresh_reads_controls_and_all_items_after_the_old_500_limit():
    from app.daily_sheets import DailySheets
    rows=[row(f"exp-{i}") for i in range(1201)]
    for r in rows:r[10]="one-purchase"
    store,reader,refresh=initialized(rows);original=deepcopy(reader.rows)
    cat=load_catalog(store.data["catalog"])
    store.data["catalog"]=catalog_document(CategoryCatalog([*cat.categories,*catalog().categories]))
    grid=Grid();daily=DailySheets(grid,"source",store)
    daily.verify=lambda:{"developerMetadata":[{"metadataKey":MARKER,"metadataValue":sha256(b"source").hexdigest()}],
                         "sheets":[{"charts":list(grid.charts.values())}]}
    grid.put(75,2,3);grid.put(61,2,"original-selection");grid.put(64,2,"食費｜食料品")
    grid.put(9,2,7,title="履歴");grid.put(17,2,6,title="推移");grid.put(73,2,4)
    before=deepcopy(store.data)
    daily.refresh(current_month="2026-09",updated_at="now",reviews=[])
    displayed=[v for (sid,r,c),v in grid.data.items() if sid==SHEETS["_候補"][0] and c==3 and 0<r<HOME_LOOKUP_ROW-1 and v]
    assert len(displayed)==202 and "original-selection" in displayed
    expected=[r[0] for r in original[1000:]]
    assert set(v.split("｜")[0] for v in displayed if v!="original-selection")==set(expected)
    assert daily.form()[0][0]=="original-selection" and daily.form()[0][3]=="食費｜食料品"
    assert daily.controls("2026-09")["expense_choice_page"]==3
    assert store.data==before and reader.rows==original and reader.reads==[]
    assert daily.refresh(current_month="2026-09",updated_at="now",reviews=[])["daily_changed_blocks"]==0


def test_rename_preserves_active_correction_choice_through_submission():
    daily,grid,ledger,reader=daily_setup();cat=load_catalog(daily.store.data["catalog"])
    identity=cat.resolve("食費","食料品")
    daily.store.data["catalog"]=catalog_document(cat.rename(identity,"食費","食品"))
    grid.put(64,2,"食費｜食料品")
    assert daily.submit(ledger)["corrections_applied"]==1
    assert reader.rows[0][5:7]==["食費","食品"]
