from copy import deepcopy

from app.daily_view import HOME_LOOKUP_ROW, SHEETS, home_requests, render_requests
from app.daily_sheets import DailySheets
from app.monthly_projection import shift_month
from app.projection_refresh import load_catalog
from test_daily_sheets import Grid
from test_projection_refresh import initialized, row


def test_home_uses_all_13_saved_months_without_category_filter_or_ledger_reads():
    store,reader,refresh=initialized([row("a"),row("b","2026-08-01")])
    summary=store.data["summary"]
    before=deepcopy(store.data)
    outputs=render_requests(read_month=refresh.read_month,summary=summary,
        catalog=load_catalog(store.data["catalog"]),current_month="2026-09",source_id="source",
        controls={"home_month":46235},reviews=[],updated_at="now")
    grid=Grid();daily=DailySheets(grid,"source",store)
    grid.put(3,2,46235,title="ホーム")
    daily.update_outputs(outputs)
    assert grid.data[SHEETS["ホーム"][0],2,1]==46235
    for i in range(13):
        month=shift_month("2026-09",-i)
        record=[grid.data[SHEETS["_候補"][0],HOME_LOOKUP_ROW+i,c] for c in range(4)]
        assert record[0]==month
        expected=summary["months"].get(month)
        assert record[1:3]==([expected["amount"],expected["purchase_count"]] if expected else ["未集計","未集計"])
    assert store.data==before and reader.reads==[]
    # Re-render never converts formulas to values, or overwrites the selection.
    grid.put(3,2,"2026-09",title="ホーム")
    assert daily.update_outputs(outputs)["daily_changed_blocks"]==0
    for r,col in [(1,"D"),(3,"B"),(4,"C")]:
        formula=grid.data[SHEETS["ホーム"][0],r,1]
        assert formula.startswith('=XLOOKUP(IF(ISNUMBER($B$3),TEXT($B$3,"yyyy-mm"),$B$3),')
        assert f"'_候補'!${col}$988:${col}$1000" in formula
        assert formula.endswith(',0)')  # Explicit exact match, no default zero.
    assert sum(r*c for _,r,c in SHEETS.values())==12630


def test_unknown_empty_month_is_not_zero_but_recorded_net_zero_is_preserved():
    summary={"required_routes":["card"],"coverage":{"2026-08":{"card":"complete"}},
        "months":{"2026-09":{"amount":0,"purchase_count":0},
                  "2026-08":{"amount":0,"purchase_count":0},
                  "2026-07":{"amount":0,"purchase_count":2}}}
    grid=Grid();daily=DailySheets(grid,"source",None)
    daily.update_outputs(home_requests(summary,"2026-09",123,"now"))
    records=[[grid.data[SHEETS["_候補"][0],HOME_LOOKUP_ROW+i,c] for c in range(4)] for i in range(3)]
    assert records==[["2026-09","未集計","未集計","取込状況未確認"],
                     ["2026-08",0,0,"完了"],["2026-07",0,2,"取込状況未確認"]]
    assert grid.data[SHEETS["ホーム"][0],5,1]=="='確認'!$B$4"  # All-period unresolved, across every page.
    # Month rollover replaces the fixed lookup table and refreshes candidates.
    requests=home_requests(summary,"2026-10",123,"now")
    daily.update_outputs(requests)
    assert grid.data[SHEETS["_候補"][0],HOME_LOOKUP_ROW,0]=="2026-10"
    assert grid.data[SHEETS["_候補"][0],999,0]=="2025-10"
    values=requests[-1]["setDataValidation"]["rule"]["condition"]["values"]
    assert len(values)==13 and values[0]["userEnteredValue"]=="2026-10"
