from app.daily_view import SHEETS, ReviewItem, install_copy_requests, render_requests, review_page
from app.projection_refresh import load_catalog
from test_projection_refresh import initialized, row


def render(store,refresh,**controls):
    return render_requests(read_month=refresh.read_month,summary=store.data["summary"],
        catalog=load_catalog(store.data["catalog"]),current_month="2026-09",source_id="source",
        controls=controls,reviews=[],updated_at="2026-09-19 22:00")


def test_daily_copy_is_small_and_cannot_replace_authoritative_ledger():
    import pytest
    assert sum(rows*cols for _,rows,cols in SHEETS.values())==12_630
    with pytest.raises(ValueError,match="copy_required"):
        install_copy_requests("source","source",[0],current_month="2026-09")
    requests=install_copy_requests("source","copy",[0,2,3],current_month="2026-09")
    assert [r["deleteSheet"]["sheetId"] for r in requests if "deleteSheet" in r]==[0,2,3]
    assert all(len(r)==1 for r in requests)
    for request in requests:
        if "updateCells" in request:
            spec=request["updateCells"];rng=spec["range"]
            assert len(spec["rows"])==rng["endRowIndex"]-rng["startRowIndex"]
            assert all(len(r["values"])==rng["endColumnIndex"]-rng["startColumnIndex"] for r in spec["rows"])


def test_display_filters_do_not_write_any_form_or_selection_value():
    store,reader,refresh=initialized([row("a"),row("b","2020-01-01")])
    requests=render(store,refresh,month="2026-09")
    assert store.reads.count("month-2026-09")==1
    assert "month-2020-01" not in store.reads
    for request in requests:
        if "updateCells" not in request:continue
        rng=request["updateCells"]["range"]
        if rng["sheetId"]==SHEETS["確認"][0]:
            assert rng["endRowIndex"]<=56  # B61:B68 and J61 are never outputs.
        if rng["sheetId"]==SHEETS["履歴"][0]:
            assert not(rng["startRowIndex"]<6 and rng["endRowIndex"]>2)
    assert reader.reads==[]


def test_review_pagination_keeps_year_crossing_unresolved_items_and_total():
    items=[ReviewItem(str(i),"2020年の確認" if i<5 else "確認","判断が必要","保留","#source") for i in range(123)]
    first,total,pages=review_page(items,1)
    last,_,_=review_page(items,3)
    assert len(first)==50 and first[0].title=="2020年の確認"
    assert total==123 and pages==3 and len(last)==23


def test_trends_show_missing_as_unknown_and_include_10_years():
    store,reader,refresh=initialized([row("a")])
    requests=render(store,refresh)
    monthly=next(r["updateCells"] for r in requests if r.get("updateCells",{}).get("range",{}).get("sheetId")==SHEETS["推移"][0]
                 and r["updateCells"]["range"]["startRowIndex"]==19)
    values=[[next(iter(c.get("userEnteredValue",{}).values()),"") for c in r["values"]] for r in monthly["rows"]]
    assert len(values)==120
    assert values[-1][0].startswith("2017-01")
    assert values[-1][1]=="未集計"
    assert any(r[0].startswith("2026-09\n進行月") and r[1]==100 for r in values)


def test_category_breakdown_pages_all_categories_without_changing_totals_or_selection():
    from app.daily_view import trend_requests
    from app.monthly_projection import CategoryCatalog
    catalog=CategoryCatalog.bootstrap(("分類",str(i)) for i in range(301))
    summary={"months":{"2026-08":{"amount":301,"purchase_count":301,
        "category_amounts":[[c.category_id,1] for c in catalog.categories]}}}
    labels=[]
    for page in (1,2,3):
        requests=trend_requests(summary,catalog,"2026-09",{"breakdown_page":page},"now")
        block=next(r["updateCells"] for r in requests if r.get("updateCells",{}).get("range",{}).get("startRowIndex")==149)
        labels.extend(r["values"][0]["userEnteredValue"]["stringValue"] for r in block["rows"] if r["values"][0])
        for r in requests:
            if "updateCells" not in r:continue
            target=r["updateCells"]["range"]
            assert not(target["startRowIndex"]<=144<target["endRowIndex"] and target["startColumnIndex"]<=1<target["endColumnIndex"])
    assert len(set(labels))==len(labels)==301 and summary["months"]["2026-08"]["amount"]==301


def test_merchant_formula_text_is_literal_and_category_filter_is_not_silently_lost():
    import pytest
    data=row("a");data[2]='=IMPORTXML("https://invalid","x")'
    store,reader,refresh=initialized([data])
    requests=render(store,refresh)
    hist=next(r["updateCells"] for r in requests if r.get("updateCells",{}).get("range",{}).get("sheetId")==SHEETS["履歴"][0]
              and r["updateCells"]["range"]["startRowIndex"]==10)
    assert "stringValue" in hist["rows"][0]["values"][0]["userEnteredValue"]
    with pytest.raises(ValueError,match="category_selection_changed"):
        render(store,refresh,category="なくなったカテゴリ")
