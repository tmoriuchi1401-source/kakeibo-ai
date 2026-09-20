"""Two bounded native charts; only saved summaries feed their hidden sources."""
from __future__ import annotations

from .daily_view import HOME_MONTH_COUNT, SHEETS, cells, coverage_status, grid
from .monthly_projection import shift_month


CHART_IDS=(260920101,260920102)
CATEGORY_FIRST=21
CATEGORY_LAST=CATEGORY_FIRST+HOME_MONTH_COUNT*6-1


def formula(value):
    return {"userEnteredValue":{"formulaValue":value}}


def category_rows(item,catalog):
    """Major-category top five plus remainder; refunds retain their signs."""
    labels={c.category_id:c.major or "未分類" for c in catalog.categories}
    amounts={}
    for key,value in item.get("category_amounts",[]):
        label=labels.get(key,"未分類")
        amounts[label]=amounts.get(label,0)+value
    rows=sorted(amounts.items(),key=lambda pair:(-abs(pair[1]),pair[0]))
    result=[list(pair) for pair in rows[:5]]
    if len(rows)>5:result.append(["その他（残り）",sum(value for _,value in rows[5:])])
    return result+[["",""] for _ in range(6-len(result))]


def data_requests(summary,catalog,current_month):
    months=[shift_month(current_month,-i) for i in range(HOME_MONTH_COUNT)]
    monthly=[["年月","記録済み支出（円）"]]
    cache=[["対象月・順位","大カテゴリ","記録済み支出（円）"]]
    for month in reversed(months):
        item=summary.get("months",{}).get(month)
        known=item is not None and (item["purchase_count"]>0 or coverage_status(summary,month)=="完了")
        monthly.append([month[2:].replace("-","/"),item["amount"] if known else ""])
        rows=category_rows(item,catalog) if known else [["",""]]*6
        cache.extend([[f"{month}#{i}",row[0] or "（表示なし）",row[1]] for i,row in enumerate(rows,1)])
    key='IF(ISNUMBER(\'ホーム\'!$B$3),TEXT(\'ホーム\'!$B$3,"yyyy-mm"),\'ホーム\'!$B$3)'
    selected=[["大カテゴリ","記録済み支出（円）"]]
    for rank in range(1,7):
        def lookup(col,missing):
            return (f'XLOOKUP({key}&"#{rank}",$D${CATEGORY_FIRST}:$D${CATEGORY_LAST},'
                    f'${col}${CATEGORY_FIRST}:${col}${CATEGORY_LAST},"{missing}",0)')
        # XLOOKUP of an empty native cell can return zero. An explicit label
        # sentinel keeps unused categories/unknown months entirely unplotted.
        selected.append([formula(f'=IF({lookup("E","（表示なし）")}="（表示なし）","",{lookup(col,"")})')
                         for col in ("E","F")])
    return [cells("推移",1,monthly,left=3,width=2),
            cells("推移",20,cache,left=3,width=3),
            cells("推移",102,selected,left=3,width=2),
            cells("ホーム",13,[["記録済み分のグラフです。未集計月は空白です。"]],width=3),
            cells("ホーム",30,[[formula(f'={key}&" の内訳 / "&\'ホーム\'!B2')]],width=3)]


def layout_requests():
    requests=[]
    for row in (13,30):
        requests.extend([
            {"mergeCells":{"range":grid("ホーム",row,row),"mergeType":"MERGE_ALL"}},
            {"repeatCell":{"range":grid("ホーム",row,row),"cell":{"userEnteredFormat":{
                "wrapStrategy":"WRAP","verticalAlignment":"MIDDLE","textFormat":{"fontSize":10},
                "backgroundColor":{"red":0.94,"green":0.95,"blue":0.96}}},
                "fields":"userEnteredFormat(wrapStrategy,verticalAlignment,textFormat,backgroundColor)"}},
            {"updateDimensionProperties":{"range":{"sheetId":SHEETS["ホーム"][0],"dimension":"ROWS",
                "startIndex":row-1,"endIndex":row},"properties":{"pixelSize":42},"fields":"pixelSize"}}])
    # Existing hidden columns, no new rows/columns or additional transaction data.
    requests.append({"repeatCell":{"range":grid("推移",1,108,4,6),"cell":{"userEnteredFormat":{
        "numberFormat":{"type":"NUMBER","pattern":"#,##0"}}},"fields":"userEnteredFormat.numberFormat"}})
    return requests


def charts():
    def source(first,last,col):
        return {"sourceRange":{"sources":[grid("推移",first,last,col,col+1)]}}
    result=[]
    for chart_id,kind,title,subtitle,first,last,row,height,color in [
        (CHART_IDS[0],"LINE","月別の支出推移","直近13か月・記録済み分（円）",1,14,14,300,{"red":40/255,"green":110/255,"blue":165/255}),
        (CHART_IDS[1],"BAR","対象月のカテゴリ内訳","大カテゴリ上位5件＋その他（円）",102,108,31,340,{"red":30/255,"green":130/255,"blue":117/255})]:
        series={"series":source(first,last,4),"targetAxis":"BOTTOM_AXIS" if kind=="BAR" else "LEFT_AXIS"}
        # Native BAR round-trips discard series color/labels. Keep its default
        # styling instead of reapplying unsupported fields on every refresh.
        if kind=="LINE":series.update({"pointStyle":{"size":4},"colorStyle":{"rgbColor":color}})
        spec={"title":title,"subtitle":subtitle,"fontName":"Arial",
              "titleTextFormat":{"fontSize":14,"bold":True},"subtitleTextFormat":{"fontSize":10},
              "hiddenDimensionStrategy":"SHOW_ALL",
              "basicChart":{"chartType":kind,"legendPosition":"NO_LEGEND","headerCount":1,
                  "domains":[{"domain":source(first,last,3)}],"series":[series]}}
        result.append({"chartId":chart_id,"spec":spec,"position":{"overlayPosition":{
            "anchorCell":{"sheetId":SHEETS["ホーム"][0],"rowIndex":row-1,"columnIndex":0},
            "widthPixels":325,"heightPixels":height}}})
    return result


def contains(actual,expected):
    """Ignore native API defaults while comparing every managed field."""
    if isinstance(expected,dict):
        return isinstance(actual,dict) and all(contains(actual.get(k,0 if v==0 else None),v) for k,v in expected.items())
    if isinstance(expected,list):
        return isinstance(actual,list) and len(actual)==len(expected) and all(contains(a,b) for a,b in zip(actual,expected))
    if isinstance(expected,float) and isinstance(actual,(int,float)):return abs(actual-expected)<1e-6
    return actual==expected


def chart_requests(metadata):
    existing={c["chartId"]:c for s in metadata.get("sheets",[]) for c in s.get("charts",[])}
    requests=[]
    for chart in charts():
        old=existing.get(chart["chartId"])
        if old is None:requests.append({"addChart":{"chart":chart}});continue
        if not contains(old.get("spec"),chart["spec"]):
            requests.append({"updateChartSpec":{"chartId":chart["chartId"],"spec":chart["spec"]}})
        if not contains(old.get("position"),chart["position"]):
            requests.append({"updateEmbeddedObjectPosition":{"objectId":chart["chartId"],
                "newPosition":chart["position"],"fields":"anchorCell,widthPixels,heightPixels"}})
    return requests
