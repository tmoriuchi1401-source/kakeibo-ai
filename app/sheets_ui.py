"""Opt-in presentation only. No schema, transaction, or review writers here."""
from __future__ import annotations

from hashlib import sha256
from datetime import date, timedelta
import json
import re

from .amazon_review import AMAZON_REVIEW_HEADERS
from .sheets import HEADERS

SPREADSHEET_ID = "1G44cDDUryVpZazTDwuCT4eZrir5KJb2WVm9baHTRPow"
HOME_ID = 1909140001
CHART_ID = 1909140002
CATEGORY_UI_ID = 1909140003
CATEGORY_UI_TITLE = "カテゴリ対応"
CATEGORY_UI_MARKER = "kakeibo_category_ui"
CATEGORY_UI_ROWS = 5005
CATEGORY_UI_COLUMNS = 17
EXPENSE_CATEGORY_HELPER_ID = 1909140004
EXPENSE_CATEGORY_HELPER_TITLE = "_支出明細カテゴリ候補"
EXPENSE_CATEGORY_HELPER_MARKER = "kakeibo_expense_category_validation"
# The category sheet currently has 1,000 rows (including its header), so a
# helper row can represent every possible minor category without a second
# category master.  This is deliberately a sheet-local support grid, not a
# user-editable list.
EXPENSE_CATEGORY_HELPER_COLUMNS = 1000
MARKER = "kakeibo_daily_ui"
VERSION = "1"
HOME_COLUMNS = 9
AUTO_MONTH = "当月（自動）"
CAP = 5000  # Data rows; overflow is visible, never silently omitted.
IDS = {
    "支出明細": 0, "レシート": 620056485, "カテゴリ": 1571056330,
    "店舗": 987268279, "Amazon注文": 847076207, "商品マスタ": 1674154424,
    "取込データ": 1833120072, "要確認": 1682462052, "支出一覧": 1889438451,
    "_要確認カテゴリ候補": 754328221, "Amazon照合候補": 1149502063,
    "Amazonイベント": 312392508, "Amazon注文ヘッダ": 1878689512,
    "給与明細ヘッダ": 1356414173, "給与明細項目": 467192626,
    "給与標準項目": 59038810, "給与項目別名": 1059447273,
    "勤務先マスタ": 166495142, "Amazon要確認": 1248165085, "Coverage確認": 81236831,
}
DAILY = ["支出一覧", "要確認", "Amazon要確認"]
RIGHT = [CATEGORY_UI_TITLE, "カテゴリ", "商品マスタ", "店舗", "給与明細ヘッダ", "給与明細項目",
         "給与標準項目", "給与項目別名", "勤務先マスタ", "Coverage確認", "レシート", "取込データ", "支出明細"]
HIDDEN = ["Amazon注文", "Amazon照合候補",
          "Amazonイベント", "Amazon注文ヘッダ", "_要確認カテゴリ候補",
          EXPENSE_CATEGORY_HELPER_TITLE]
FORMAT_KEYS = ["backgroundColorStyle", "textFormat", "verticalAlignment",
               "wrapStrategy", "numberFormat", "horizontalAlignment"]
TEXT_KEYS = ["fontFamily", "fontSize", "bold", "foregroundColorStyle"]
FORMAT_MASK = ",".join("userEnteredFormat." + key for key in FORMAT_KEYS if key != "textFormat") + "," + ",".join(
    "userEnteredFormat.textFormat." + key for key in TEXT_KEYS)


def color(hex_value):
    return {"rgbColor": {k: int(hex_value[i:i+2], 16) / 255
                         for k, i in zip(("red", "green", "blue"), (0, 2, 4))}}


def grid(sid, r0, r1, c0, c1):
    return dict(sheetId=sid, startRowIndex=r0, endRowIndex=r1,
                startColumnIndex=c0, endColumnIndex=c1)


def dimension(sid, kind, start, end, **properties):
    return {"updateDimensionProperties": {
        "range": dict(sheetId=sid, dimension=kind, startIndex=start, endIndex=end),
        "properties": properties, "fields": ",".join(properties)}}


def style(rng, **fmt):
    fields = []
    for key, value in fmt.items():
        fields.extend("userEnteredFormat.textFormat." + k for k in value) if key == "textFormat" else fields.append("userEnteredFormat." + key)
    return {"repeatCell": {"range": rng, "cell": {"userEnteredFormat": fmt},
                           "fields": ",".join(fields)}}


def cell(row, col, value, sid=HOME_ID):
    key = "formulaValue" if isinstance(value, str) and value.startswith("=") else "stringValue"
    if isinstance(value, (float, int)):
        key = "numberValue"
    return {"updateCells": {"range": grid(sid, row-1, row, col-1, col),
                            "rows": [{"values": [{"userEnteredValue": {key: value}}]}],
                            "fields": "userEnteredValue"}}


def installed(meta):
    return any(s["properties"].get("sheetId") == HOME_ID and any(
        m.get("metadataKey") == MARKER and m.get("metadataValue") == VERSION
        for m in s.get("developerMetadata", [])) for s in meta.get("sheets", []))


def owned(home):
    return home["properties"]["sheetId"] == HOME_ID and any(
        m.get("metadataKey") == MARKER and m.get("metadataValue") in {VERSION, "restored:" + VERSION}
        for m in home.get("developerMetadata", []))


def layout_requests(sheet):
    """No values, validations, notes, filters, or physical column order changes."""
    p = sheet["properties"]
    title, sid = p["title"], p["sheetId"]
    if title not in DAILY or IDS[title] != sid:
        return []
    n = min(p["gridProperties"]["rowCount"], CAP+1)
    count = 10 if title == "支出一覧" else 20 if title == "要確認" else 14
    req = [
        {"updateSheetProperties": {"properties": {"sheetId": sid,
            "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}},
        style(grid(sid, 0, n, 0, count),
              textFormat={"fontFamily": "Arial", "fontSize": 10, "bold": False,
                          "foregroundColorStyle": color("28343B")},
              backgroundColorStyle=color("FFFFFF"), verticalAlignment="MIDDLE", wrapStrategy="WRAP"),
        style(grid(sid, 0, 1, 0, count), backgroundColorStyle=color("E9EEF0"),
              textFormat={"fontFamily": "Arial", "fontSize": 10, "bold": True,
                          "foregroundColorStyle": color("28343B")}),
        dimension(sid, "ROWS", 0, 1, pixelSize=48),
        dimension(sid, "ROWS", 1, n, pixelSize=56),
    ]
    if title == "支出一覧":
        # Keep A:D within 390px including Sheets' row header and scrollbar.
        widths = [70, 86, 90, 85, 105, 105, 100, 90, 180, 150]
        date_col, money_col, inputs = 0, 3, []
        req.append(dimension(sid, "COLUMNS", 9, 10, hiddenByUser=True))
    elif title == "要確認":
        widths = [145, 48, 78, 85, 110, 90, 120, 180, 180, 150,
                  160, 165, 120, 180, 170, 220, 75, 220, 160, 180]
        date_col, money_col, inputs = 2, 5, [(9, 14), (17, 18)]
    else:
        widths = [145, 90, 100, 78, 170, 110, 100, 200, 160, 140, 160, 160, 130, 130]
        date_col, money_col, inputs = 3, None, [(7, 8)]
    req += [dimension(sid, "COLUMNS", i, i+1, pixelSize=width) for i, width in enumerate(widths)]
    req.append(style(grid(sid, 1, n, date_col, date_col+1),
                     numberFormat={"type": "DATE", "pattern": "yyyy/mm/dd"}))
    if money_col is not None:
        req.append(style(grid(sid, 1, n, money_col, money_col+1), horizontalAlignment="RIGHT",
                         numberFormat={"type": "NUMBER", "pattern": '#,##0"円";[Red]-#,##0"円";0"円"'}))
    for start, end in inputs:
        req.append(style(grid(sid, 1, n, start, end), backgroundColorStyle=color("FFF4D8")))
        req.append(style(grid(sid, 0, 1, start, end), backgroundColorStyle=color("F7DFA2")))
    return req


def refresh_layout_requests(meta, title):
    if not installed(meta):
        return []
    return [r for s in meta["sheets"] if s["properties"]["title"] == title
            for r in layout_requests(s)]


def source_range(title, cols):
    """INDIRECT avoids spill growth when existing writers INSERT_ROWS on append."""
    start, end = cols.split(":")
    return f'INDIRECT("\'{title}\'!{start}2:{end}"&MIN(ROWS(\'{title}\'!A:A),{CAP+1}))'


def initial_month_selection(home):
    """Preserve an existing selector, or migrate the former date input safely."""
    if home is None:
        return AUTO_MONTH
    if "monthState" not in home:
        raise ValueError("Home month selection was not read; refresh metadata")
    state = home["monthState"]
    selected = state.get("B4", "")
    if selected:
        if selected == AUTO_MONTH or isinstance(selected, str) and re.fullmatch(r"[1-9][0-9]{3}-(0[1-9]|1[0-2])", selected):
            return selected
        raise ValueError("Unexpected Home month selection; inspect before applying UI")
    legacy = state.get("B3", "")
    if legacy == "" or isinstance(legacy, str) and legacy.startswith("=") and "TODAY()" in legacy:
        return AUTO_MONTH
    try:
        if isinstance(legacy, (int, float)) and not isinstance(legacy, bool):
            chosen = date(1899, 12, 30) + timedelta(days=legacy)
        else:
            parts = str(legacy).strip().replace("/", "-").split("-")
            chosen = date(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts)>2 else 1)
        return chosen.strftime("%Y-%m")
    except (ValueError, TypeError, OverflowError, IndexError) as exc:
        raise ValueError("Unexpected legacy Home month; inspect before applying UI") from exc


def home_cells():
    src = source_range("支出一覧", "A:J")
    money = 'VALUE(REGEXREPLACE(TO_TEXT(raw_amount),"[,¥￥円\\s]",""))'
    normalized = (
        f'=ARRAYFORMULA(LET(src,{src},dates,INDEX(src,,1),raw_amount,INDEX(src,,4),'
        'major,TRIM(INDEX(src,,5)),minor,TRIM(INDEX(src,,6)),ids,INDEX(src,,10),'
        'month_key,IF(dates="","",IFERROR(TEXT(IF(ISNUMBER(dates),dates,'
        'DATEVALUE(LEFT(SUBSTITUTE(TRIM(dates),"-","/"),10))),"yyyy-mm"),"")),'
        f'amount,IFERROR({money},0),'
        'category,IF((major="")+(minor="")+(major="未分類")+(minor="未分類"),"未分類",major),'
        f'problems,IF(ids="",0,IF((month_key="")+(raw_amount="")+(IFERROR({money},"")=""),1,0)),'
        '{IF(ids="","",month_key),amount,category,ids,problems}))'
    )
    ids = source_range("取込データ", "A:A")
    states = source_range("取込データ", "I:I")
    review = (f'=SUMPRODUCT(({ids}<>"")*REGEXMATCH({states},'
              '"^(要確認|needs_review.*|.*_needs_review|amazon_unmatched)$"))')
    amz_ids = source_range("Amazon要確認", "A:A")
    amz_states = source_range("Amazon要確認", "B:B")
    overflow = '+'.join(
        f'MAX(0,COUNTIF(\'{title}\'!{col}2:{col},"<>")-COUNTIF({source_range(title, col+":"+col)},"<>"))'
        for title, col in [("支出一覧", "J"), ("取込データ", "A"), ("Amazon要確認", "A")])
    link = lambda title, label, target="A1": f'=HYPERLINK("#gid={CATEGORY_UI_ID if title == CATEGORY_UI_TITLE else IDS[title]}&range={target}","{label}")'
    month = 'TEXT($B$3,"yyyy-mm")'
    end = CAP+1
    cells = {
        (1, 1): "家計簿AI", (2, 1): "支出と要対応を、ひと目で。",
        (3, 1): "対象月", (3, 2): f'=IF(OR($B$4="",$B$4="{AUTO_MONTH}"),DATE(YEAR(TODAY()),MONTH(TODAY()),1),DATE(VALUE(LEFT($B$4,4)),VALUE(RIGHT($B$4,2)),1))',
        (4, 1): "月を選ぶ ▼", (4, 2): AUTO_MONTH,
        (5, 1): '=TEXT($B$3,"yyyy年m月")&"の計上済み支出"',
        (6, 1): f'=IF($B$17>0,"要データ確認",SUMIF(D2:D{end},{month},E2:E{end}))',
        (7, 1): "要対応",
        (8, 1): link(CATEGORY_UI_TITLE, "カテゴリ未分類"), (8, 2): f'=COUNTIFS(D2:D{end},{month},F2:F{end},"未分類",G2:G{end},"<>")',
        (9, 1): "未分類の金額（対象月）", (9, 2): f'=SUMIFS(E2:E{end},D2:D{end},{month},F2:F{end},"未分類",G2:G{end},"<>")',
        (10, 1): "計上済みで、カテゴリだけ未確定の支出。",
        (11, 1): "取込内容の確認（全期間）",
        (12, 1): link("要確認", "通常review →", "J1"), (12, 2): review,
        (13, 1): link("Amazon要確認", "Amazon review →", "H1"),
        (13, 2): f'=COUNTIFS({amz_ids},"<>",{amz_states},"<>反映済み")',
        (14, 1): "保留・反映待ちを含みます。",
        (15, 1): "未取込のデータは含みません。",
        (17, 1): "集計データの確認", (17, 2): f'=SUM(H2:H{end})+{overflow}',
        (18, 1): '=IF(B17=0,"支出一覧の更新に合わせて集計します。","日付・金額または参照上限を確認してください。")',
        (20, 1): link("支出一覧", "支出一覧を開く →"),
        (21, 1): link("要確認", "要確認の取引情報を開く →", "C1"),
        (22, 1): link("Amazon要確認", "Amazon要確認を開く →"),
        (23, 1): link("レシート", "元画像 →", "F1"),
        (23, 2): link("取込データ", "統合先ID →", "A1"),
        (34, 1): (f'=IFERROR(QUERY(D2:G{end},"select F,sum(E) where D = \'"&{month}&'
                  '"\' and G is not null group by F order by sum(E) desc '
                  'label F \'カテゴリ\',sum(E) \'金額\'",0),{"カテゴリ","金額";"該当なし",0})'),
        (1, 4): "対象月", (1, 5): "金額", (1, 6): "集計カテゴリ", (1, 7): "支出ID", (1, 8): "入力確認",
        (2, 4): normalized,
        (1, 9): "対象月候補", (2, 9): AUTO_MONTH,
        (3, 9): (f'=LET(months,{{ARRAYFORMULA(TEXT(EDATE(TODAY(),SEQUENCE(36,1,0,-1)),"yyyy-mm"));'
                 f'IFERROR(FILTER($D$2:$D${end},$D$2:$D${end}<>""),TEXT(TODAY(),"yyyy-mm"));'
                 'IF(REGEXMATCH(TO_TEXT($B$4),"^[1-9][0-9]{3}-(0[1-9]|1[0-2])$"),$B$4,TEXT(TODAY(),"yyyy-mm"))},'
                 'SORT(UNIQUE(months),1,FALSE))'),
    }
    # A numeric HYPERLINK label retains the count and its number format. A
    # TextFormat link alone is ignored on formula cells by the native UI.
    for row, sid, target in [(8, CATEGORY_UI_ID, "A1"), (12, IDS["要確認"], "J1"), (13, IDS["Amazon要確認"], "H1")]:
        cells[row, 2] = f'=HYPERLINK("#gid={sid}&range={target}",{cells[row, 2][1:]})'
    return cells


def home_requests(home):
    req = []
    cells = home_cells()
    selected = initial_month_selection(home)
    if home is not None and home["monthState"].get("B4"):
        cells.pop((4, 2))  # Never rewrite a user's existing selector.
    else:
        cells[4, 2] = selected
    req += [cell(r, c, value) for (r, c), value in cells.items()]
    req += [{"updateSheetProperties": {"properties": {"sheetId": HOME_ID,
                  "gridProperties": {"frozenRowCount": 4}}, "fields": "gridProperties.frozenRowCount"}},
            style(grid(HOME_ID, 0, CAP+1, 0, 2),
                  textFormat={"fontFamily": "Arial", "fontSize": 11, "foregroundColorStyle": color("28343B")},
                  verticalAlignment="MIDDLE", wrapStrategy="WRAP", backgroundColorStyle=color("FFFFFF")),
            dimension(HOME_ID, "COLUMNS", 0, 1, pixelSize=170),
            dimension(HOME_ID, "COLUMNS", 1, 2, pixelSize=150),
            dimension(HOME_ID, "COLUMNS", 2, HOME_COLUMNS, hiddenByUser=True),
            dimension(HOME_ID, "ROWS", 0, 70, pixelSize=32),
            dimension(HOME_ID, "ROWS", 5, 6, pixelSize=55),
            dimension(HOME_ID, "ROWS", 14, 15, pixelSize=32),
            dimension(HOME_ID, "ROWS", 17, 18, pixelSize=48),
            style(grid(HOME_ID, 0, 1, 0, 2), textFormat={"fontSize": 18, "bold": True}),
            style(grid(HOME_ID, 5, 6, 0, 2), textFormat={"fontSize": 26, "bold": True,
                  "foregroundColorStyle": color("226C60")}, numberFormat={"type": "NUMBER", "pattern": '#,##0"円"'}),
            style(grid(HOME_ID, 2, 3, 1, 2), backgroundColorStyle=color("FFFFFF"), textFormat={"bold": True},
                  numberFormat={"type": "DATE", "pattern": "yyyy年m月"}),
            style(grid(HOME_ID, 3, 4, 1, 2), backgroundColorStyle=color("FFF4D8"), textFormat={"bold": True},
                  horizontalAlignment="LEFT", numberFormat={"type": "TEXT", "pattern": "@"}),
            style(grid(HOME_ID, 33, CAP+1, 1, 2), numberFormat={"type": "NUMBER", "pattern": '#,##0"円"'}),
            {"setDataValidation": {"range": grid(HOME_ID, 2, 3, 1, 2),
                }},  # B3 is now the calculated month, not an input.
            {"setDataValidation": {"range": grid(HOME_ID, 3, 4, 1, 2),
                "rule": {"condition": {"type": "ONE_OF_RANGE", "values": [
                    {"userEnteredValue": f"='ホーム'!$I$2:$I${CAP+1}"}]}, "strict": True, "showCustomUi": True,
                    "inputMessage": f"表示したい月を選択。「{AUTO_MONTH}」で当月表示に戻せます。"}}},
    ]
    for row, note in [(3, "表示中の月です。変更は下のB4から行ってください。"),
                      (4, f"過去月の選択を保持します。「{AUTO_MONTH}」へ戻すと当月に追従します。")]:
        req.append({"updateCells": {"range": grid(HOME_ID, row-1, row, 1, 2),
                    "rows": [{"values": [{"note": note}]}], "fields": "note"}})
    for row in [5, 7, 34]:
        req.append(style(grid(HOME_ID, row-1, row, 0, 2), backgroundColorStyle=color("E9EEF0")))
    req.append(style(grid(HOME_ID, 6, 7, 0, 2), textFormat={"bold": True, "fontSize": 12}))
    for row in [10, 11, 14, 15]:
        req.append(style(grid(HOME_ID, row-1, row, 0, 2), backgroundColorStyle=color("FFFFFF"),
                         textFormat={"fontSize": 10, "foregroundColorStyle": color("53646D")}))
    for row, sid, target in [(8, CATEGORY_UI_ID, "A1"), (12, IDS["要確認"], "J1"), (13, IDS["Amazon要確認"], "H1")]:
        req.append(dimension(HOME_ID, "ROWS", row-1, row, pixelSize=44))
        action_style = style(grid(HOME_ID, row-1, row, 1, 2),
            numberFormat={"type": "NUMBER", "pattern": '0"件　対応する →"'},
            backgroundColorStyle=color("FFF4D8"),
            textFormat={"foregroundColorStyle": color("226C60"), "underline": True})
        action_style["repeatCell"]["fields"] += ",userEnteredFormat.textFormat.link"
        req.append(action_style)
    for row in [17]:
        req.append(style(grid(HOME_ID, row-1, row, 1, 2),
                         numberFormat={"type": "NUMBER", "pattern": '0"件"'}))
    req.append(style(grid(HOME_ID, 8, 9, 1, 2), numberFormat={"type": "NUMBER", "pattern": '#,##0"円"'}))
    existing_merges = home.get("merges", []) if home else []
    for row in [1, 2, 5, 6, 7, 10, 11, 14, 15, 18, 20, 21, 22]:
        rng = grid(HOME_ID, row-1, row, 0, 2)
        if rng not in existing_merges:
            req.append({"mergeCells": {"range": rng, "mergeType": "MERGE_ALL"}})
    spec = {
        "title": "カテゴリ別支出（上位12）", "fontName": "Arial",
        "titleTextFormat": {"fontSize": 12}, "backgroundColorStyle": color("FFFFFF"),
        "basicChart": {"chartType": "BAR", "legendPosition": "NO_LEGEND", "headerCount": 1,
            "axis": [{"position": "BOTTOM_AXIS", "title": "円"}],
            "domains": [{"domain": {"sourceRange": {"sources": [grid(HOME_ID, 33, 46, 0, 1)]}}}],
            "series": [{"series": {"sourceRange": {"sources": [grid(HOME_ID, 33, 46, 1, 2)]}},
                        "targetAxis": "BOTTOM_AXIS", "colorStyle": color("4B897C")}]},
    }
    position = {"overlayPosition": {"anchorCell": {"sheetId": HOME_ID, "rowIndex": 23, "columnIndex": 0},
                "offsetXPixels": 0, "offsetYPixels": 0, "widthPixels": 320, "heightPixels": 300}}
    existing_charts = home.get("charts", []) if home else []
    if any(c["chartId"] == CHART_ID for c in existing_charts):
        req += [{"updateChartSpec": {"chartId": CHART_ID, "spec": spec}},
                {"updateEmbeddedObjectPosition": {"objectId": CHART_ID, "newPosition": position,
                                                   "fields": "anchorCell,offsetXPixels,offsetYPixels,widthPixels,heightPixels"}}]
    else:
        req.append({"addChart": {"chart": {"chartId": CHART_ID, "spec": spec, "position": position}}})
    return req


def build_plan(meta):
    if meta.get("spreadsheetId") != SPREADSHEET_ID:
        raise ValueError("UI target spreadsheet mismatch")
    sheets = sorted(meta["sheets"], key=lambda s: s["properties"]["index"])
    by_title = {s["properties"]["title"]: s for s in sheets}
    # Formula source contracts. Never call ensure_schema to repair a mismatch.
    for title, headers in [("支出一覧", HEADERS["支出一覧"]), ("取込データ", HEADERS["取込データ"]),
                           ("Amazon要確認", AMAZON_REVIEW_HEADERS), ("支出明細", HEADERS["支出明細"]),
                           ("商品マスタ", HEADERS["商品マスタ"]), ("カテゴリ", HEADERS["カテゴリ"])]:
        s = by_title.get(title)
        if not s or s["properties"]["sheetId"] != IDS[title] or s.get("header") != headers:
            raise ValueError(f"UI source contract mismatch: {title}")
    home = by_title.get("ホーム")
    if home and not owned(home):
        raise ValueError("Existing ホーム is not owned by this UI configuration")
    if home and (home["properties"]["gridProperties"]["rowCount"] < CAP+1 or
                 home["properties"]["gridProperties"]["columnCount"] < 8):
        raise ValueError("Home grid changed; inspect before resizing the UI")
    if not home and any(s["properties"]["sheetId"] == HOME_ID for s in sheets):
        raise ValueError("Home sheet ID collision")
    if any(c["chartId"] == CHART_ID for s in sheets if s != home for c in s.get("charts", [])):
        raise ValueError("Home chart ID collision")
    req, skipped = [], []
    from .sheets_ui_actions import (
        category_ui_requests,
        expense_category_validation_requests,
        ledger_input_requests,
    )
    category_ui = by_title.get(CATEGORY_UI_TITLE)
    if category_ui:
        if category_ui["properties"]["sheetId"] != CATEGORY_UI_ID or not any(
            m.get("metadataKey") == CATEGORY_UI_MARKER and m.get("metadataValue") in {VERSION, "restored:"+VERSION}
            for m in category_ui.get("developerMetadata", [])):
            raise ValueError("Existing category UI is not owned by this configuration")
        gp = category_ui["properties"]["gridProperties"]
        if gp["rowCount"] < CATEGORY_UI_ROWS or gp["columnCount"] < CATEGORY_UI_COLUMNS:
            raise ValueError("Category UI grid changed; inspect before applying")
        if not any(m.get("metadataKey") == CATEGORY_UI_MARKER and m.get("metadataValue") == VERSION
                   for m in category_ui.get("developerMetadata", [])):
            req.append({"updateDeveloperMetadata": {"dataFilters": [{"developerMetadataLookup": {
                "metadataKey": CATEGORY_UI_MARKER, "metadataLocation": {"sheetId": CATEGORY_UI_ID}}}],
                "developerMetadata": {"metadataValue": VERSION}, "fields": "metadataValue"}})
    elif any(s["properties"]["sheetId"] == CATEGORY_UI_ID for s in sheets):
        raise ValueError("Category UI sheet ID collision")
    else:
        req += [{"addSheet": {"properties": {"sheetId": CATEGORY_UI_ID, "title": CATEGORY_UI_TITLE,
            "gridProperties": {"rowCount": CATEGORY_UI_ROWS, "columnCount": CATEGORY_UI_COLUMNS,
                               "frozenRowCount": 5, "hideGridlines": True}}}},
            {"createDeveloperMetadata": {"developerMetadata": {"metadataKey": CATEGORY_UI_MARKER,
                "metadataValue": VERSION, "visibility": "DOCUMENT", "location": {"sheetId": CATEGORY_UI_ID}}}}]
    expense_helper = by_title.get(EXPENSE_CATEGORY_HELPER_TITLE)
    ledger_sheet = by_title['支出明細']
    used_rows = ledger_sheet.get('usedRowCount', ledger_sheet['properties']['gridProperties']['rowCount'])
    helper_rows = min(CAP+1, max(1001, ((max(2,used_rows)-2)//1000+1)*1000+1))
    if expense_helper:
        if expense_helper["properties"]["sheetId"] != EXPENSE_CATEGORY_HELPER_ID or not any(
            m.get("metadataKey") == EXPENSE_CATEGORY_HELPER_MARKER
            and m.get("metadataValue") in {VERSION, "restored:" + VERSION}
            for m in expense_helper.get("developerMetadata", [])
        ):
            raise ValueError("Existing expense category helper is not owned by this configuration")
        gp = expense_helper["properties"]["gridProperties"]
        helper_rows = max(helper_rows, min(gp['rowCount'], CAP+1))
        if gp["rowCount"] < helper_rows or gp["columnCount"] < EXPENSE_CATEGORY_HELPER_COLUMNS:
            req.append({"updateSheetProperties": {"properties": {"sheetId": EXPENSE_CATEGORY_HELPER_ID,
                "gridProperties": {"rowCount": max(gp["rowCount"], helper_rows),
                                   "columnCount": max(gp["columnCount"], EXPENSE_CATEGORY_HELPER_COLUMNS)}},
                "fields": "gridProperties.rowCount,gridProperties.columnCount"}})
        if not any(m.get("metadataKey") == EXPENSE_CATEGORY_HELPER_MARKER and m.get("metadataValue") == VERSION
                   for m in expense_helper.get("developerMetadata", [])):
            req.append({"updateDeveloperMetadata": {"dataFilters": [{"developerMetadataLookup": {
                "metadataKey": EXPENSE_CATEGORY_HELPER_MARKER,
                "metadataLocation": {"sheetId": EXPENSE_CATEGORY_HELPER_ID}}}],
                "developerMetadata": {"metadataValue": VERSION}, "fields": "metadataValue"}})
    elif any(s["properties"]["sheetId"] == EXPENSE_CATEGORY_HELPER_ID for s in sheets):
        raise ValueError("Expense category helper sheet ID collision")
    else:
        req += [{"addSheet": {"properties": {"sheetId": EXPENSE_CATEGORY_HELPER_ID,
            "title": EXPENSE_CATEGORY_HELPER_TITLE, "hidden": True,
            "gridProperties": {"rowCount": helper_rows, "columnCount": EXPENSE_CATEGORY_HELPER_COLUMNS}}}},
            {"createDeveloperMetadata": {"developerMetadata": {
                "metadataKey": EXPENSE_CATEGORY_HELPER_MARKER, "metadataValue": VERSION,
                "visibility": "DOCUMENT", "location": {"sheetId": EXPENSE_CATEGORY_HELPER_ID}}}}]
    ledger_grid = by_title["支出明細"]["properties"]["gridProperties"]
    if ledger_grid["rowCount"] < helper_rows:
        # Pre-provisioned blank rows carry the validation into normal
        # INSERT_ROWS appends; this never changes an existing ledger value.
        req.append({"updateSheetProperties": {"properties": {"sheetId": IDS["支出明細"],
            "gridProperties": {"rowCount": helper_rows}}, "fields": "gridProperties.rowCount"}})
    if not home:
        req += [{"addSheet": {"properties": {"sheetId": HOME_ID, "title": "ホーム",
                    "gridProperties": {"rowCount": CAP+1, "columnCount": HOME_COLUMNS,
                                       "frozenRowCount": 4, "hideGridlines": True}}}},
                {"createDeveloperMetadata": {"developerMetadata": {
                    "metadataKey": MARKER, "metadataValue": VERSION,
                    "visibility": "DOCUMENT", "location": {"sheetId": HOME_ID}}}}]
    elif not installed(meta):
        req.append({"updateDeveloperMetadata": {"dataFilters": [{"developerMetadataLookup": {
            "metadataKey": MARKER, "metadataLocation": {"sheetId": HOME_ID}}}],
            "developerMetadata": {"metadataValue": VERSION}, "fields": "metadataValue"}})
    if home and home["properties"]["gridProperties"]["columnCount"] < HOME_COLUMNS:
        req.append({"updateSheetProperties": {"properties": {"sheetId": HOME_ID,
            "gridProperties": {"columnCount": HOME_COLUMNS}}, "fields": "gridProperties.columnCount"}})
    # Move known tabs to the front in reverse order: index semantics stay correct
    # for both initial layout and arbitrary user rearrangements on subsequent runs.
    ordered = ["ホーム"] + DAILY + RIGHT + HIDDEN
    for title in reversed(ordered):
        sid = (HOME_ID if title == "ホーム" else CATEGORY_UI_ID if title == CATEGORY_UI_TITLE
               else EXPENSE_CATEGORY_HELPER_ID if title == EXPENSE_CATEGORY_HELPER_TITLE else IDS[title])
        if title not in {"ホーム", CATEGORY_UI_TITLE, EXPENSE_CATEGORY_HELPER_TITLE} and (
            title not in by_title or by_title[title]["properties"]["sheetId"] != sid
        ):
            skipped.append(title)
            continue
        req.append({"updateSheetProperties": {"properties": {
            "sheetId": sid, "index": 0, "hidden": title in HIDDEN}, "fields": "index,hidden"}})
    for title in DAILY:
        s = by_title.get(title)
        if not s or s["properties"]["sheetId"] != IDS[title]:
            continue
        expected = AMAZON_REVIEW_HEADERS if title == "Amazon要確認" else HEADERS[title]
        if s.get("header") != expected:
            skipped.append(title + " formatting: header differs")
            continue
        req.extend(layout_requests(s))
    req.extend(home_requests(home))
    req.extend(category_ui_requests(category_ui))
    req.extend(ledger_input_requests(by_title["支出明細"]))
    # Existing ledger rows receive exact row-specific G rules now.  New rows
    # are covered by SheetsDB.append after a normal import, avoiding a huge
    # one-shot batch of unused per-row rules.
    req.extend(expense_category_validation_requests(
        minor_end_row=min(by_title["支出明細"]["properties"]["gridProperties"]["rowCount"], helper_rows),
        helper_end_row=helper_rows,
    ))
    preconditions = [{"sheetId": s["properties"]["sheetId"], "title": s["properties"]["title"],
        "index": s["properties"]["index"], "hidden": s["properties"].get("hidden", False),
        "rows": s["properties"].get("gridProperties", {}).get("rowCount", 0),
        "columns": s["properties"].get("gridProperties", {}).get("columnCount", 0),
        "frozenRows": s["properties"].get("gridProperties", {}).get("frozenRowCount", 0),
        "header": s.get("header", []), "monthState": s.get("monthState")} for s in sheets]
    return {"spreadsheetId": SPREADSHEET_ID, "version": VERSION, "requests": req,
            "preconditions": preconditions,
            "skipped": skipped, "new_conditional_formats": 0,
            "visible_order": ["ホーム"] + DAILY + RIGHT, "hide": HIDDEN,
            "source_row_limit": CAP}


def plan_digest(plan):
    return sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
