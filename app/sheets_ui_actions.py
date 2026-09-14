"""Read-only category queue and links into existing expense ledger cells.

No classifications, proposed values, approvals, or business writers are added.
"""
from __future__ import annotations

from .sheets_ui import (
    CAP, CATEGORY_UI_COLUMNS, CATEGORY_UI_ID, CATEGORY_UI_ROWS, HOME_ID, IDS,
    cell, color, dimension, grid, source_range, style,
)


def category_ui_cells():
    end = CAP+1
    view = source_range("支出一覧", "A:J")
    ledger = source_range("支出明細", "A:M")
    ledger_ids = source_range("支出明細", "A:A")
    master = source_range("商品マスタ", "A:F")
    categories = source_range("カテゴリ", "A:B")
    date_value = lambda name: f'IF(ISNUMBER({name}),{name},DATEVALUE(LEFT(SUBSTITUTE(TRIM({name}),"-","/"),10)))'
    shortened = lambda name, n: f'LEFT({name},{n})&IF(LEN({name})>{n},"…","")'
    escape_id = 'SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(key,"~","~~"),"*","~*"),"?","~?")'
    # Filter the same normalized IDs/month/categories used by Home's count.
    # Sort the projection only; underlying business rows never move.
    pending = (f'=IFERROR(SORT(FILTER({view},\'ホーム\'!D2:D{end}=TEXT(\'ホーム\'!$B$3,"yyyy-mm"),'
               f'\'ホーム\'!F2:F{end}="未分類",\'ホーム\'!G2:G{end}<>""),2,TRUE,3,TRUE,1,TRUE),"")')
    positions = (f'=MAP(M2:M{end},LAMBDA(key,IF(key="","",LET(escaped,{escape_id},'
                 f'IF(COUNTIF({ledger_ids},"="&escaped)=1,MATCH(escaped,{ledger_ids},0)+1,0)))))')
    current = (f'=MAP(M2:M{end},N2:N{end},LAMBDA(key,source_row,IF(key="","",'
               f'IF(source_row=0,"元データ確認",LET(src,{ledger},'
               'TRIM(INDEX(src,source_row-1,6))&"｜"&TRIM(INDEX(src,source_row-1,7)))))))')
    state = (f'=MAP(M2:M{end},N2:N{end},LAMBDA(key,source_row,IF(key="","",'
             f'IF(source_row=0,"元データ確認",LET(src,{ledger},'
             'day,INDEX(src,source_row-1,2),status,INDEX(src,source_row-1,13),'
             'major,TRIM(INDEX(src,source_row-1,6)),minor,TRIM(INDEX(src,source_row-1,7)),'
             f'IF(OR(AND(status<>"",status<>"active"),IFERROR(TEXT({date_value("day")},"yyyy-mm"),"")<>TEXT(\'ホーム\'!$B$3,"yyyy-mm")),'
             '"一覧更新待ち",IF(OR(major="",minor="",major="未分類",minor="未分類"),"修正可","分類済・更新待ち")))))))')
    # Only one distinct, catalog-valid pair can be shown. Ambiguity => no suggestion.
    suggestion = (f'=MAP(F2:F{end},E2:E{end},M2:M{end},LAMBDA(item,merchant,key,IF(key="","",'
        f'LET(products,{master},history,{view},catalog,{categories},'
        'allowed,ARRAYFORMULA(INDEX(catalog,,1)&"｜"&INDEX(catalog,,2)),'
        'product_pairs,IFERROR(UNIQUE(FILTER(ARRAYFORMULA(INDEX(products,,3)&"｜"&INDEX(products,,4)),'
        'INDEX(products,,2)=item,INDEX(products,,3)<>"",INDEX(products,,4)<>"",'
        'INDEX(products,,3)<>"未分類",INDEX(products,,4)<>"未分類")),""),'
        'product_pair,INDEX(product_pairs,1),'
        'past_pairs,IFERROR(UNIQUE(FILTER(ARRAYFORMULA(TRIM(INDEX(history,,5))&"｜"&TRIM(INDEX(history,,6))),'
        'INDEX(history,,2)=merchant,INDEX(history,,10)<>"",TRIM(INDEX(history,,5))<>"",TRIM(INDEX(history,,6))<>"",'
        'TRIM(INDEX(history,,5))<>"未分類",TRIM(INDEX(history,,6))<>"未分類")),""),'
        'past_pair,INDEX(past_pairs,1),'
        'IF(AND(item<>"",item<>"自動計上",item<>"手動計上",ROWS(product_pairs)=1,SUM(ARRAYFORMULA(N(allowed=product_pair)))>0),'
        'product_pair&CHAR(10)&"（商品マスタ）",'
        'IF(AND(merchant<>"",ROWS(past_pairs)=1,SUM(ARRAYFORMULA(N(allowed=past_pair)))>0),'
        'past_pair&CHAR(10)&"（同じ店舗）","候補なし"))))))')
    info = (f'=MAP(D2:D{end},E2:E{end},M2:M{end},'
            f'LAMBDA(day,merchant,key,IF(key="","",IFERROR(TEXT({date_value("day")},"yyyy/mm/dd"),TO_TEXT(day))'
            f'&CHAR(10)&{shortened("merchant",32)})))')
    detail = (f'=MAP(F2:F{end},O2:O{end},M2:M{end},LAMBDA(item,pair,key,IF(key="","",'
              f'{shortened("item",44)}&CHAR(10)&CHAR(10)&"現在カテゴリ"&CHAR(10)&pair)))')
    action = (f'=MAP(G2:G{end},N2:N{end},P2:P{end},Q2:Q{end},M2:M{end},'
        'LAMBDA(amount,source_row,status,suggestion,key,IF(key="","",LET('
        'money,IFERROR(TEXT(VALUE(REGEXREPLACE(TO_TEXT(amount),"[,¥￥円\\s]","")),"#,##0円"),"金額確認"),'
        f'IF(status="修正可",HYPERLINK("#gid={IDS["支出明細"]}&range=F"&source_row&":G"&source_row,'
        'money&CHAR(10)&"修正する →"&CHAR(10)&CHAR(10)&IF(suggestion="候補なし",suggestion,"候補: "&suggestion)),'
        'money&CHAR(10)&status)))))')
    return {
        (1, 1): "未分類のカテゴリ修正",
        (2, 1): '=TEXT(\'ホーム\'!B3,"yyyy年m月")&"　"&TEXT(\'ホーム\'!B8,"0")&"件（対象月）"',
        (3, 1): f'=HYPERLINK("#gid={HOME_ID}&range=A1","ホーム・月の選択へ戻る →")',
        (4, 1): '=IF(\'ホーム\'!B8=0,"この月の未分類はありません。","候補は参考。修正は次の支出一覧更新で反映。")',
        (5, 1): "日付・店舗", (5, 2): "商品名\n現在カテゴリ", (5, 3): "金額\n修正用カテゴリ",
        (6, 1): info, (6, 2): detail, (6, 3): action,
        (1, 4): "対象月の未分類（支出一覧）", (2, 4): pending,
        (1, 14): "支出明細の行", (2, 14): positions,
        (1, 15): "現在カテゴリ", (2, 15): current,
        (1, 16): "入力先の状態", (2, 16): state,
        (1, 17): "推奨（未採用）", (2, 17): suggestion,
    }


def category_ui_requests(sheet):
    sid = CATEGORY_UI_ID
    req = [cell(r, c, value, sid) for (r, c), value in category_ui_cells().items()]
    req += [style(grid(sid, 0, CATEGORY_UI_ROWS, 0, 3),
        textFormat={"fontFamily": "Arial", "fontSize": 10, "bold": False,
                    "foregroundColorStyle": color("28343B")},
        verticalAlignment="TOP", wrapStrategy="WRAP", backgroundColorStyle=color("FFFFFF")),
        dimension(sid, "COLUMNS", 0, 1, pixelSize=90),
        dimension(sid, "COLUMNS", 1, 2, pixelSize=125),
        dimension(sid, "COLUMNS", 2, 3, pixelSize=105),
        dimension(sid, "COLUMNS", 3, CATEGORY_UI_COLUMNS, hiddenByUser=True),
        dimension(sid, "ROWS", 0, 5, pixelSize=36),
        dimension(sid, "ROWS", 3, 5, pixelSize=44),
        dimension(sid, "ROWS", 5, CATEGORY_UI_ROWS, pixelSize=180),
        style(grid(sid, 0, 1, 0, 3), textFormat={"fontSize": 17, "bold": True}),
        style(grid(sid, 1, 2, 0, 3), textFormat={"bold": True}),
        style(grid(sid, 4, 5, 0, 3), backgroundColorStyle=color("E9EEF0"), textFormat={"bold": True}),
        style(grid(sid, 5, CATEGORY_UI_ROWS, 2, 3), backgroundColorStyle=color("FFF4D8")),
        {"updateSheetProperties": {"properties": {"sheetId": sid,
            "gridProperties": {"frozenRowCount": 5, "hideGridlines": True}},
            "fields": "gridProperties.frozenRowCount,gridProperties.hideGridlines"}},
    ]
    for row in range(4):
        rng = grid(sid, row, row+1, 0, 3)
        if not sheet or rng not in sheet.get("merges", []):
            req.append({"mergeCells": {"range": rng, "mergeType": "MERGE_ALL"}})
    return req


def ledger_input_requests(sheet):
    """Style existing F:G cells; never seed values, validations, or classifications."""
    sid, n = IDS["支出明細"], min(sheet["properties"]["gridProperties"]["rowCount"], CAP+1)
    return [dimension(sid, "COLUMNS", 5, 6, pixelSize=150),
        dimension(sid, "COLUMNS", 6, 7, pixelSize=165),
        dimension(sid, "ROWS", 0, n, pixelSize=44),
        style(grid(sid, 0, n, 5, 7), backgroundColorStyle=color("FFF4D8"),
            textFormat={"fontFamily": "Arial", "fontSize": 11}, wrapStrategy="WRAP", verticalAlignment="MIDDLE"),
        style(grid(sid, 0, 1, 5, 7), backgroundColorStyle=color("F7DFA2"), textFormat={"bold": True}),
        {"updateSheetProperties": {"properties": {"sheetId": sid,
            "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}}]
