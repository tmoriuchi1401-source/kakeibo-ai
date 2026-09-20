from copy import deepcopy

import pytest

from app.daily_charts import category_rows, chart_requests, charts, data_requests
from app.daily_sheets import DailySheets, entered
from app.daily_view import SHEETS
from app.monthly_projection import Category, CategoryCatalog, ProjectionError
from test_daily_sheets import Grid
from test_projection_refresh import initialized, row


def test_categories_group_major_keep_refunds_and_remainder_conserves_total():
    catalog=CategoryCatalog([Category(str(i),f"major{i//2}",f"minor{i}") for i in range(16)])
    amounts=[(str(i),(-1 if i==15 else 1)*(i+1)*100) for i in range(16)]
    rows=category_rows({"category_amounts":amounts},catalog)
    assert len(rows)==6 and rows[-1][0]=="その他（残り）"
    assert sum(v for _,v in rows)==sum(v for _,v in amounts)
    assert category_rows({"category_amounts":[("missing",-100)]},catalog)[0]==["未分類",-100]


def test_sources_are_bounded_unknown_is_blank_and_zero_with_records_is_retained():
    catalog=CategoryCatalog([])
    summary={"months":{"2026-09":{"amount":0,"purchase_count":0},
                       "2026-08":{"amount":0,"purchase_count":2,"category_amounts":[("x",0)]}}}
    reqs=data_requests(summary,catalog,"2026-09")
    monthly=[[entered(c) for c in r["values"]] for r in reqs[0]["updateCells"]["rows"]]
    assert monthly[1]==["25/09",""] and monthly[-1]==["26/09",""] and monthly[-2]==["26/08",0]
    cache=[[entered(c) for c in r["values"]] for r in reqs[1]["updateCells"]["rows"]]
    assert cache[-1]==["2026-09#6","（表示なし）",""]
    for req in reqs:
        rect=req["updateCells"]["range"]
        assert rect["sheetId"] in {SHEETS["推移"][0],SHEETS["ホーム"][0]}
        if rect["sheetId"]==SHEETS["推移"][0]:assert rect["startColumnIndex"]==3 and rect["endRowIndex"]<=108
    assert sum(r*c for _,r,c in SHEETS.values())==12630


def test_chart_reconciliation_is_idempotent_and_preserves_unrelated_charts():
    existing=charts()+[{"chartId":99,"spec":{"title":"owner chart"}}]
    meta={"sheets":[{"charts":deepcopy(existing)}]}
    assert chart_requests(meta)==[]
    meta["sheets"][0]["charts"][0]["spec"]["title"]="old"
    changes=chart_requests(meta)
    assert len(changes)==1 and "updateChartSpec" in changes[0]
    del meta["sheets"][0]["charts"][0]
    assert len(chart_requests(meta))==1 and "addChart" in chart_requests(meta)[0]


def test_native_chart_defaults_and_rgb_rounding_do_not_trigger_rewrites():
    native=deepcopy(charts())
    for chart in native:
        del chart["position"]["overlayPosition"]["anchorCell"]["columnIndex"]
        chart["spec"]["titleTextFormat"]["fontFamily"]="Arial"
        chart["spec"]["basicChart"]["axis"]=[{"position":"LEFT_AXIS"}]
    rgb=native[0]["spec"]["basicChart"]["series"][0]["colorStyle"]["rgbColor"]
    for channel,value in rgb.items():rgb[channel]=round(value,8)
    assert chart_requests({"sheets":[{"charts":native}]})==[]


def test_refresh_lost_chart_response_recovers_and_checks_readback():
    store,reader,_=initialized([row("a")])
    grid=Grid();daily=DailySheets(grid,"source",store)
    daily.verify=lambda:{"sheets":[{"charts":list(grid.charts.values())}]}
    grid.fail_after=True
    with pytest.raises(RuntimeError):daily.refresh(current_month="2026-09",updated_at="now",reviews=[])
    daily.refresh(current_month="2026-09",updated_at="now",reviews=[])
    assert len(grid.charts)==2 and reader.reads==[]
    before=len([r for w in grid.writes for r in w["requests"] if "addChart" in r])
    daily.refresh(current_month="2026-09",updated_at="now",reviews=[])
    assert len([r for w in grid.writes for r in w["requests"] if "addChart" in r])==before
    grid.charts.clear();grid.on_write=grid.charts.clear
    with pytest.raises(ProjectionError,match="daily_chart_readback_failed"):
        daily.refresh(current_month="2026-09",updated_at="now",reviews=[])
