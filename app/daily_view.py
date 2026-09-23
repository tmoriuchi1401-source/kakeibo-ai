"""Small native daily workbook: owned outputs and separately owned inputs.

Only recent month projections are read for history. Long-term views consume the
small saved summary, never historical ledger lines. No renderer submits edits.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import math
from uuid import uuid4

from .monthly_projection import compare_years, history_page, shift_month


SHEETS={"ホーム":(260919001,50,3),"履歴":(260919002,120,8),
        "確認":(260919003,140,10),"推移":(260919004,320,6),
        "設定":(260919005,700,6),"_候補":(260919006,1000,4)}
OWNED_MARKER="kakeibo_daily_view_v1"
PAGE_SIZE=50
HOME_MONTH_COUNT=13
OUTPUT_FIELDS="userEnteredValue,userEnteredFormat.textFormat.link"
# Reuse the last 14 rows of the existing hidden grid. Choice pages use at
# most 501 entries, so reserving these cells does not reduce available choices.
HOME_LOOKUP_ROW=SHEETS["_候補"][1]-HOME_MONTH_COUNT
STATE_LABELS={"queued":"反映待ち","pending":"反映待ち（保存確認中）","applied":"反映済み","failed":"失敗"}


def grid(title,first,last,left=0,right=3):
    return {"sheetId":SHEETS[title][0],"startRowIndex":first-1,"endRowIndex":last,
            "startColumnIndex":left,"endColumnIndex":right}


def literal(value):
    if value is None or value=="":return {}
    key="boolValue" if isinstance(value,bool) else "numberValue" if isinstance(value,(int,float)) else "stringValue"
    return {"userEnteredValue":{key:value}}


def link(url,label):
    return {"userEnteredValue":{"stringValue":str(label)},
            "userEnteredFormat":{"textFormat":{"link":{"uri":str(url)}}}}


def cells(title,start,rows,left=0,width=None):
    if not rows:return None
    width=width or max(len(row) for row in rows)
    if any(len(row)>width for row in rows):raise ValueError("daily_cell_width_invalid")
    return {"updateCells":{"range":grid(title,start,start+len(rows)-1,left,left+width),
        "rows":[{"values":[v if isinstance(v,dict) else literal(v) for v in list(row)+[""]*(width-len(row))]}
                for row in rows],"fields":OUTPUT_FIELDS}}


def dropdown(title,row,col,options=None,source=None,strict=True):
    condition=({"type":"ONE_OF_RANGE","values":[{"userEnteredValue":source}]} if source else
               {"type":"ONE_OF_LIST","values":[{"userEnteredValue":str(x)} for x in options]})
    return {"setDataValidation":{"range":grid(title,row,row,col,col+1),"rule":{
        "condition":condition,"strict":strict,"showCustomUi":True}}}


def install_copy_requests(source_id,target_id,old_sheet_ids,*,current_month):
    if source_id==target_id or not source_id or not target_id:raise ValueError("daily_copy_required")
    new_ids={spec[0] for spec in SHEETS.values()}
    if new_ids & set(old_sheet_ids):raise ValueError("daily_sheet_id_collision")
    requests=[]
    # Add before removing copied tabs, so the workbook is never sheetless.
    for title,(sid,rows,cols) in SHEETS.items():
        requests.append({"addSheet":{"properties":{"sheetId":sid,"title":"_daily_new_"+title,
            "gridProperties":{"rowCount":rows,"columnCount":cols}}}})
    requests.extend({"deleteSheet":{"sheetId":sid}} for sid in old_sheet_ids)
    for index,(title,(sid,rows,cols)) in enumerate(SHEETS.items()):
        requests.append({"updateSheetProperties":{"properties":{"sheetId":sid,"title":title,"index":index,
            "hidden":title.startswith("_"),"gridProperties":{"hideGridlines":True,"frozenRowCount":0 if title.startswith("_") else 2}},
            "fields":"title,index,hidden,gridProperties.hideGridlines,gridProperties.frozenRowCount"}})
        if title.startswith("_"):continue
        widths=[100,140,85] if title!="確認" else [90,120,75,40]
        for col,width in enumerate(widths):
            requests.append({"updateDimensionProperties":{"range":{"sheetId":sid,"dimension":"COLUMNS",
                "startIndex":col,"endIndex":col+1},"properties":{"pixelSize":width},"fields":"pixelSize"}})
        if cols>len(widths):
            requests.append({"updateDimensionProperties":{"range":{"sheetId":sid,"dimension":"COLUMNS",
                "startIndex":len(widths),"endIndex":cols},"properties":{"hiddenByUser":True},"fields":"hiddenByUser"}})
        requests.append({"repeatCell":{"range":grid(title,1,10,0,len(widths)),"cell":{"userEnteredFormat":{
            "textFormat":{"fontFamily":"Arial","fontSize":11},"wrapStrategy":"WRAP","verticalAlignment":"MIDDLE"}},
            "fields":"userEnteredFormat(textFormat,wrapStrategy,verticalAlignment)"}})
        requests.append({"repeatCell":{"range":grid(title,1,1,0,len(widths)),"cell":{"userEnteredFormat":{
            "textFormat":{"bold":True,"fontSize":16},"backgroundColor":{"red":0.94,"green":0.95,"blue":0.96}}},
            "fields":"userEnteredFormat(textFormat,backgroundColor)"}})
        requests.append({"updateDimensionProperties":{"range":{"sheetId":sid,"dimension":"ROWS","startIndex":0,"endIndex":10},
            "properties":{"pixelSize":36},"fields":"pixelSize"}})
    initial={
        "ホーム":[["家計簿AI","日常画面"],["状態","準備中・本番未切替"],["対象月",current_month],
                  ["記録済み支出","未集計"],["買い物件数","未集計"],["確認が必要","未集計"],
                  ["最終更新","未反映"],["履歴",link(f"#gid={SHEETS['履歴'][0]}","買い物を探す →")],
                  ["確認",link(f"#gid={SHEETS['確認'][0]}","確認・修正 →")],
                  ["推移",link(f"#gid={SHEETS['推移'][0]}","10年の記録 →")],
                  ["正式台帳",link(f"https://docs.google.com/spreadsheets/d/{source_id}/edit","過去の詳細・原本 →")],
                  ["案内","選択・送信は既存の定期処理で反映されます。未取込はゼロではありません。"]],
        "履歴":[["履歴","買い物単位"],["状態","準備中・本番未切替"],["年月","直近13か月"],
                ["カテゴリ","すべて"],["店舗・商品",""],["ページ",1],["全件数","未集計"],
                ["案内","商品明細・原本は台帳で開きます。古い明細も正式台帳に残ります。"],[],
                ["日付・店舗","商品・分類","金額"]],
        "確認":[["確認","全期間の未解決"],["状態","準備中・本番未切替"],["ページ",1],
                ["全未解決","未集計"],["案内","保留も含みます。表示更新では修正入力を消しません。"],
                ["対象","確認する内容","対応","状態"]],
        "推移":[["推移","10年の記録"],["状態","準備中・本番未切替"],["内訳の年",current_month[:4]],
                ["カテゴリ","すべて"],["比較","両年の完了月同士。当月は別表示。"],
                ["年","記録済み合計","月平均・前年比"]],
        "設定":[["設定","補助"],["正本",link(f"https://docs.google.com/spreadsheets/d/{source_id}/edit","正式台帳を開く")],
                ["取込状況","経路・口座別。未確認を完了として扱いません。"],
                ["分類","既存の承認済みルールを引き継ぎます。"],
                ["運用","本番の切替準備中です。ここに入力しただけでは正式台帳は変わりません。"]],
        "_候補":[["カテゴリ表示","カテゴリID","年月","修正対象ID"],["すべて","","直近13か月",""]],
    }
    for title,rows in initial.items():requests.append(cells(title,1,rows,width=4 if title in {"_候補","確認"} else 3))
    form=[["支出の修正","空欄は変更しません"],["対象（固定ID）",""],["日付",""],["金額",""],
          ["カテゴリ",""],["店舗",""],["商品名",""],["メモ",""],["送信",False],
          ["反映状況","未送信"],["最終更新",""],
          ["案内","既存明細の修正です。金額は円単位で入力し、送信にチェックしてください。"]]
    requests.append(cells("確認",60,form,width=3))
    requests.append(cells("確認",61,[["REQ-"+uuid4().hex]],left=9,width=1))
    requests.append({"repeatCell":{"range":grid("確認",60,71),"cell":{"userEnteredFormat":{
        "wrapStrategy":"WRAP","verticalAlignment":"MIDDLE","textFormat":{"fontSize":11}}},
        "fields":"userEnteredFormat(wrapStrategy,verticalAlignment,textFormat)"}})
    requests.append({"updateDimensionProperties":{"range":{"sheetId":SHEETS["確認"][0],"dimension":"ROWS",
        "startIndex":59,"endIndex":71},"properties":{"pixelSize":48},"fields":"pixelSize"}})
    months=["直近13か月",*[shift_month(current_month,-i) for i in range(13)]]
    requests.extend([dropdown("履歴",3,1,months),dropdown("ホーム",3,1,months[1:]),
                     dropdown("履歴",4,1,["すべて"]),dropdown("推移",4,1,["すべて"]),
                     dropdown("推移",3,1,[str(int(current_month[:4])-i) for i in range(10)]),
                     dropdown("確認",3,1,[1]),dropdown("履歴",6,1,[1])])
    requests.append({"setDataValidation":{"range":grid("確認",68,68,1,2),"rule":{
        "condition":{"type":"BOOLEAN"},"strict":True,"showCustomUi":True}}})
    requests.append({"repeatCell":{"range":grid("確認",61,68,1,2),"cell":{"userEnteredFormat":{
        "backgroundColor":{"red":1,"green":0.98,"blue":0.85},"wrapStrategy":"WRAP"}},
        "fields":"userEnteredFormat(backgroundColor,wrapStrategy)"}})
    requests.append({"createDeveloperMetadata":{"developerMetadata":{"metadataKey":OWNED_MARKER,
        "metadataValue":sha256(source_id.encode()).hexdigest(),"visibility":"DOCUMENT","location":{"spreadsheet":True}}}})
    from .daily_money_review import install_requests
    from .daily_coverage import install_requests as coverage_install
    from .daily_category_management import install_requests as category_install
    from .daily_choices import install_requests as choices_install
    return [r for r in requests if r]+layout_requests()+install_requests(source_id)+coverage_install(source_id,current_month=current_month)+category_install(source_id)+choices_install(source_id)


def layout_requests():
    """Format only authored blocks, including wrapped explanations."""
    requests=[]
    blocks={"ホーム":(1,12,3),"履歴":(1,10,3),"確認":(1,6,4),
            "推移":(1,299,3),"設定":(1,5,3)}
    for title,(first,last,width) in blocks.items():
        requests.append({"repeatCell":{"range":grid(title,first,last,0,width),"cell":{"userEnteredFormat":{
            "wrapStrategy":"WRAP","verticalAlignment":"MIDDLE"}},
            "fields":"userEnteredFormat(wrapStrategy,verticalAlignment)"}})
    for title,rows in {"ホーム":[12],"履歴":[8],"確認":[5,71],"推移":[5],"設定":[3,4,5]}.items():
        for row in rows:
            requests.append({"mergeCells":{"range":grid(title,row,row,1,3),"mergeType":"MERGE_ALL"}})
            requests.append({"updateDimensionProperties":{"range":{"sheetId":SHEETS[title][0],
                "dimension":"ROWS","startIndex":row-1,"endIndex":row},
                "properties":{"pixelSize":80},"fields":"pixelSize"}})
    for first,last,height in [(7,16,110),(20,139,54),(150,299,48)]:
        requests.append({"updateDimensionProperties":{"range":{"sheetId":SHEETS["推移"][0],
            "dimension":"ROWS","startIndex":first-1,"endIndex":last},
            "properties":{"pixelSize":height},"fields":"pixelSize"}})
    return requests+header_form_layout_requests()+number_format_requests()


def number_format_requests():
    requests=[]
    money=[("ホーム",4,4,1),("履歴",11,60,2),
           ("推移",7,16,1),("推移",20,139,1),("推移",150,299,1)]
    counts=[("ホーム",5,6,1),("推移",20,139,2)]
    for ranges,pattern in [(money,'#,##0" 円";-#,##0" 円";0" 円"'),(counts,'#,##0" 件"')]:
        for title,first,last,col in ranges:
            requests.append({"repeatCell":{"range":grid(title,first,last,col,col+1),
                "cell":{"userEnteredFormat":{"numberFormat":{"type":"NUMBER","pattern":pattern}}},
                "fields":"userEnteredFormat.numberFormat"}})
    return requests


def header_form_layout_requests():
    requests=[]
    for title in ("ホーム","履歴","確認","推移","設定"):
        width=4 if title=="確認" else 3
        requests.append({"mergeCells":{"range":grid(title,1,1,1,width),"mergeType":"MERGE_ALL"}})
        if title!="設定":
            requests.append({"mergeCells":{"range":grid(title,2,2,1,width),"mergeType":"MERGE_ALL"}})
    for row in range(60,71):
        requests.append({"mergeCells":{"range":grid("確認",row,row,1,4),"mergeType":"MERGE_ALL"}})
    return requests


@dataclass(frozen=True)
class ReviewItem:
    fixed_id:str
    title:str
    detail:str
    status:str
    url:str


def review_page(items, page=1):
    unique={}
    for item in items:
        if item.fixed_id in unique:raise ValueError("duplicate_review_id")
        unique[item.fixed_id]=item
    if page<1:raise ValueError("invalid_review_page")
    return list(unique.values())[(page-1)*PAGE_SIZE:page*PAGE_SIZE],len(unique),max(1,math.ceil(len(unique)/PAGE_SIZE))


def coverage_status(summary,month):
    routes=summary.get("required_routes",[])
    states=summary.get("coverage",{}).get(month,{})
    return "完了" if routes and all(states.get(route) in {"complete","not_applicable"} for route in routes) else "取込状況未確認"


def home_requests(summary,current_month,total,updated_at):
    """Publish saved monthly totals; Sheets alone recalculates month selection."""
    months=[shift_month(current_month,-i) for i in range(HOME_MONTH_COUNT)]
    rows=[["ホーム対象月","記録済み支出","買い物件数","取込状況"]]
    for month in months:
        item=summary.get("months",{}).get(month)
        state=coverage_status(summary,month)
        known=item is not None and (item["purchase_count"]>0 or state=="完了")
        rows.append([month,item["amount"] if known else "未集計",
                     item["purchase_count"] if known else "未集計",state])
    # Dropdown edits may be stored as a date serial by Sheets. Normalize only
    # the lookup key, leaving the owner's typed value and number format intact.
    key='IF(ISNUMBER($B$3),TEXT($B$3,"yyyy-mm"),$B$3)'
    def lookup(column,missing):
        first,last=HOME_LOOKUP_ROW+1,HOME_LOOKUP_ROW+HOME_MONTH_COUNT
        return {"userEnteredValue":{"formulaValue":
            f'=XLOOKUP({key},\'_候補\'!$A${first}:$A${last},'
            f'\'_候補\'!${column}${first}:${column}${last},"{missing}",0)'}}
    return [cells("_候補",HOME_LOOKUP_ROW,rows,width=4),
            cells("ホーム",2,[["取込状況",lookup("D","取込状況未確認")]],width=3),
            cells("ホーム",4,[["記録済み支出",lookup("B","未集計")],
                ["買い物件数",lookup("C","未集計")],
                ["確認が必要",{"userEnteredValue":{"formulaValue":"='確認'!$B$4"}}],
                ["最終更新",updated_at]],width=3),
            cells("ホーム",12,[["対象月の表示はすぐ切り替わります。新しい取込・送信の反映は定期処理です。未取込はゼロではありません。"]],left=1,width=1),
            dropdown("ホーム",3,1,months)]


def render_requests(*,read_month,summary,catalog,current_month,source_id,controls,reviews,updated_at,ledger_sheet_id=None):
    from .daily_choices import category_id, page_dropdown, render_requests as choice_requests
    labels={c.category_id:c.label for c in catalog.categories}
    selected=controls.get("month","直近13か月")
    selected="" if selected=="直近13か月" else selected
    category_label=controls.get("category","すべて")
    category="" if category_label=="すべて" else category_id(catalog,category_label)
    page=history_page(read_month,current_month=current_month,selected_month=selected,category_id=category,
                      search=str(controls.get("search","")),page=int(controls.get("page",1)),page_size=PAGE_SIZE)
    requests=[cells("履歴",7,[[page.total,f"{page.page}/{page.pages}ページ"]],width=3),
              cells("履歴",2,[["最終更新",updated_at]],width=3)]
    rows=[];choices=[]
    for p in page.rows:
        url=f"https://docs.google.com/spreadsheets/d/{source_id}/edit"
        if ledger_sheet_id is not None:url+=f"#gid={ledger_sheet_id}"
        if p.ledger_row:url+=("&" if ledger_sheet_id is not None else "#")+f"range=A{p.ledger_row}:M{p.ledger_row}"
        description=" / ".join(labels.get(c,"未分類") for c in p.categories)
        rows.append([p.day+"\n"+p.merchant[:40],link(url,f"{description}\n商品{p.item_count}点・詳細 →"),p.amount,p.purchase_id])
        for expense_id in p.expense_ids:
            choices.append(f"{expense_id}｜{p.day} {p.merchant[:24]}")
    requests.append(cells("履歴",11,rows+[[""]*4 for _ in range(PAGE_SIZE-len(rows))],width=4))
    requests.append(page_dropdown("履歴",6,1,page.page,page.pages))
    shown,total,pages=review_page(reviews,int(controls.get("review_page",1)))
    requests.extend([cells("確認",2,[["最終更新",updated_at]],width=3),cells("確認",4,[["全未解決",total,f"{pages}ページ"]],width=3),
                     page_dropdown("確認",3,1,controls.get("review_page",1),pages)])
    rows=[[r.title[:70],r.detail[:180],link(r.url,"開く →"),r.status,r.fixed_id] for r in shown]
    requests.append(cells("確認",7,rows+[[""]*5 for _ in range(PAGE_SIZE-len(rows))],width=5))
    monetary=[r.fixed_id for r in shown if r.title in {"Amazon金銭","Amazon通知の確認"} and r.fixed_id.startswith(("AM-","MN-"))]
    monetary += [r.fixed_id for r in shown if r.fixed_id.startswith("RQ-") and r.status=="失敗"]
    if monetary:requests.append(dropdown("確認",81,1,monetary,strict=False))
    # Renderer never touches B61:B68 or J61, even when filters/page change.
    requests.extend(home_requests(summary,current_month,total,updated_at))
    from .daily_charts import data_requests as chart_data, layout_requests as chart_layout
    requests.extend(chart_data(summary,catalog,current_month))
    requests.extend(chart_layout())
    requests.extend(choice_requests(catalog,choices,controls))
    requests.extend(trend_requests(summary,catalog,current_month,controls,updated_at))
    from .daily_coverage import render_requests as coverage_render
    if controls.get("coverage_enabled"):requests.extend(coverage_render(summary,current_month,controls))
    if controls.get("category_management_enabled"):
        from .daily_category_management import render_requests as category_render
        requests.extend(category_render(catalog,controls))
    for title,start,count,width in [("履歴",11,len(page.rows),3),("確認",7,len(shown),4)]:
        if count:
            requests.append({"repeatCell":{"range":grid(title,start,start+count-1,0,width),"cell":{"userEnteredFormat":{
                "wrapStrategy":"WRAP","verticalAlignment":"MIDDLE","textFormat":{"fontSize":10}}},
                "fields":"userEnteredFormat(wrapStrategy,verticalAlignment,textFormat.fontSize)"}})
            dimensions={"sheetId":SHEETS[title][0],"dimension":"ROWS",
                "startIndex":start-1,"endIndex":start+count-1}
            if title=="確認":
                requests.append({"autoResizeDimensions":{"dimensions":dimensions}})
            else:
                requests.append({"updateDimensionProperties":{"range":dimensions,
                    "properties":{"pixelSize":70},"fields":"pixelSize"}})
    requests.extend([
        cells("ホーム",8,[[link(f"#gid={SHEETS['履歴'][0]}","買い物を探す →")],
                         [link(f"#gid={SHEETS['確認'][0]}","確認・修正 →")],
                         [link(f"#gid={SHEETS['推移'][0]}","10年の記録 →")],
                         [link(f"https://docs.google.com/spreadsheets/d/{source_id}/edit","過去の詳細・原本 →")]],left=1,width=1),
        cells("設定",2,[[link(f"https://docs.google.com/spreadsheets/d/{source_id}/edit","正式台帳を開く")]],left=1,width=1),
    ])
    return [r for r in requests if r]


def trend_requests(summary,catalog,current_month,controls,updated_at):
    from .daily_choices import category_id,page_dropdown
    label=controls.get("trend_category","すべて")
    category=None if label=="すべて" else category_id(catalog,label)
    months=summary.get("months",{})
    amounts={m:(v["amount"] if category is None else dict(v["category_amounts"]).get(category,0)) for m,v in months.items()}
    coverage={(m,route):status for m,states in summary.get("coverage",{}).items() for route,status in states.items()}
    year=int(current_month[:4]);annual=[]
    for y in range(year,year-10,-1):
        recorded=[m for m in amounts if m.startswith(str(y)+"-") and m<=current_month]
        complete=[m for m in recorded if m<current_month and coverage_status(summary,m)=="完了"]
        comparison=compare_years(y,current_month,amounts,coverage,summary.get("required_routes",[]))
        average=f"平均 {sum(amounts[m] for m in complete)/len(complete):,.0f}円 / {len(complete)}か月" if complete else "平均 未確認 / 0か月"
        change=f"{comparison.change_percent:+.1f}%" if comparison.change_percent is not None else "未比較"
        annual.append([f"{y}年\n記録 {len(recorded)}か月",sum(amounts[m] for m in recorded) if recorded else "未集計",
                       f"{average}\n前年比 {change} / 共通{len(comparison.months)}か月"])
    monthly=[]
    for offset in range(120):
        month=shift_month(f"{year}-12",-offset)
        state="進行月" if month==current_month else "未来" if month>current_month else coverage_status(summary,month)
        monthly.append([month+"\n"+state,amounts.get(month,"未集計"),months[month]["purchase_count"] if month in months else ""])
    selected_year=str(controls.get("trend_year",year))
    category_amounts={}
    for month,item in months.items():
        if month.startswith(selected_year+"-") and month<=current_month:
            for key,amount in item["category_amounts"]:category_amounts[key]=category_amounts.get(key,0)+amount
    labels={c.category_id:c.label for c in catalog.categories}
    breakdown=[[labels.get(key,"未分類"),value] for key,value in sorted(category_amounts.items(),key=lambda x:-x[1])]
    total=len(breakdown);pages=max(1,math.ceil(total/150))
    page=min(pages,max(1,int(controls.get("breakdown_page",1))))
    breakdown=breakdown[(page-1)*150:page*150]
    return [cells("推移",2,[["最終更新",updated_at]],width=3),cells("推移",7,annual,width=3),
            cells("推移",145,[["内訳ページ"]],width=1),page_dropdown("推移",145,1,page,pages),
            cells("推移",146,[["全カテゴリ",total,f"{page}/{pages}ページ"]],width=3),
            cells("推移",19,[["年月・取込状況","記録済み金額","買い物件数"]],width=3),
            cells("推移",20,monthly,width=3),cells("推移",149,[[selected_year+"年の内訳","記録済み金額"]],width=3),
            cells("推移",150,breakdown+[[""]*3 for _ in range(150-len(breakdown))],width=3)]
