"""One-time presentation upgrade for the canonical workbook after cutover.

The home is an operations menu. Existing fixed-ID forms and approvals remain
the writers; this plan never changes them or the ledger's transaction cells.
"""
from hashlib import sha256
import re

from .daily_view import SHEETS as DAILY_SHEETS, link, literal
from .daily_edit_cutover import MARKER as CUTOVER_MARKER
from .sheets import CATEGORY_WORKFLOW_MARKERS, CATEGORY_WORKFLOW_SHEET
from .sheets_ui import HOME_ID, CHART_ID, SPREADSHEET_ID, owned, grid, dimension, style, color, cell


def build_plan(metadata,daily_id):
    from .compact_categories import compact_helper
    if metadata.get("spreadsheetId")!=SPREADSHEET_ID or not re.fullmatch(r"[A-Za-z0-9_-]+",daily_id):
        raise ValueError("operations_home_binding_invalid")
    if daily_id==SPREADSHEET_ID or not compact_helper(metadata) or not any(
            m.get("metadataKey")==CUTOVER_MARKER and m.get("metadataValue")==sha256(daily_id.encode()).hexdigest()
            for m in metadata.get("developerMetadata",[])):
        raise ValueError("operations_home_cutover_required")
    sheets={s["properties"]["title"]:s for s in metadata["sheets"]}
    home=sheets.get("ホーム")
    if not home or not owned(home):raise ValueError("operations_home_not_owned")
    required=[CATEGORY_WORKFLOW_SHEET,"要確認","領収書確認","支出一覧","カテゴリ自動分類ルール","レシート","取込データ"]
    if any(t not in sheets for t in required):raise ValueError("operations_home_target_missing")
    if any(c["chartId"]!=CHART_ID for c in home.get("charts",[])):
        raise ValueError("operations_home_unexpected_chart")

    def local(title,label,target="A1"):
        return link(f"#gid={sheets[title]['properties']['sheetId']}&range={target}",label)
    def daily(title,label,target):
        return link(f"https://docs.google.com/spreadsheets/d/{daily_id}/edit#gid={DAILY_SHEETS[title][0]}&range={target}",label)
    def section(kind,label):
        sid=sheets[CATEGORY_WORKFLOW_SHEET]["properties"]["sheetId"]
        # Section rows move when rules grow. Resolve the marker on each click
        # through a formula instead of persisting today's physical row number.
        return {"userEnteredValue":{"formulaValue":
            f'=HYPERLINK("#gid={sid}&range=A"&MATCH("{CATEGORY_WORKFLOW_MARKERS[kind]}",'
            f"'{CATEGORY_WORKFLOW_SHEET}'!A:A,0),\"{label}\")"}}
    values={
        1:"家計簿AI｜編集・管理",2:"修正・分類・確認の入口",
        5:"カテゴリを整える",
        6:local(CATEGORY_WORKFLOW_SHEET,"自動分類ルールを登録・変更 →"),
        7:"カテゴリを選び「今後の自動分類に使う」にチェック。",
        8:daily("確認","個別の支出・未分類を修正 →","A60:D71"),
        9:"対象明細と変更内容を選び、送信します。",
        10:section("backfill","過去の未分類：対象をプレビュー →"),
        11:section("confirm","過去の未分類：確認して反映 →"),
        12:"今後のルール登録と過去分の反映は別の操作です。",
        13:"確認待ちを処理",
        14:local("要確認","一般の取込内容を確認 →","K1"),
        15:daily("確認","Amazon・金銭の確認 →","A80:D88"),
        16:local("領収書確認","領収書の確認画面へ →"),
        17:"参照・設定",
        18:local("支出一覧","支出一覧を確認 →"),
        19:daily("設定","カテゴリ名を管理 →","A89:C98"),
        20:local("カテゴリ自動分類ルール","登録済みの分類ルールを確認 →"),
        21:local("レシート","レシートの原本を確認 →","F1"),
        22:local("取込データ","取込データを確認 →"),
        23:daily("ホーム","日常の記録・グラフを見る →","A1"),
        24:"個別修正は送信後、定期処理で反映されます。修正フォームの「反映状況」を確認してください。",
    }
    requests=[]
    # Clear only the old authored display, including its QUERY spill anchor.
    # B3/B4 and D:I feed existing category workflows and remain unchanged.
    for first,last in [(1,2),(5,34)]:
        rows=[{"values":[values[r] if isinstance(values.get(r),dict) else literal(values.get(r,"")),{}]}
              for r in range(first,last+1)]
        requests.append({"updateCells":{"range":grid(HOME_ID,first-1,last,0,2),"rows":rows,"fields":"userEnteredValue"}})
    requests.extend([cell(3,1,"作業対象月"),cell(4,1,"対象月を選ぶ ▼")])
    for row in values:
        rng=grid(HOME_ID,row-1,row,0,2)
        if rng not in home.get("merges",[]):requests.append({"mergeCells":{"range":rng,"mergeType":"MERGE_ALL"}})
    for first,last in [(1,2),(5,34)]:
        requests.append(style(grid(HOME_ID,first-1,last,0,2),
            textFormat={"fontFamily":"Arial","fontSize":11,"bold":False,"underline":False,
                        "foregroundColorStyle":color("28343B")},
            numberFormat={"type":"TEXT","pattern":"@"},verticalAlignment="MIDDLE",
            horizontalAlignment="LEFT",wrapStrategy="WRAP",backgroundColorStyle=color("FFFFFF")))
    requests.extend([dimension(HOME_ID,"ROWS",0,2,pixelSize=36),
                     dimension(HOME_ID,"ROWS",4,24,pixelSize=44),
                     dimension(HOME_ID,"ROWS",23,24,pixelSize=68)])
    requests.append(style(grid(HOME_ID,0,1,0,2),textFormat={"bold":True,"fontSize":16}))
    for row in (5,13,17):
        requests.append(style(grid(HOME_ID,row-1,row,0,2),backgroundColorStyle=color("E9EEF0"),textFormat={"bold":True}))
        requests.append(dimension(HOME_ID,"ROWS",row-1,row,pixelSize=32))
    for row in (7,9,12,24):
        requests.append(style(grid(HOME_ID,row-1,row,0,2),textFormat={"fontSize":10,"foregroundColorStyle":color("53646D")}))
    for row,value in values.items():
        if isinstance(value,dict):
            requests.append(style(grid(HOME_ID,row-1,row,0,2),
                textFormat={"underline":True,"foregroundColorStyle":color("1155CC")}))
    requests.extend({"deleteEmbeddedObject":{"objectId":c["chartId"]}} for c in home.get("charts",[]))
    return {"spreadsheetId":SPREADSHEET_ID,"requests":requests}
