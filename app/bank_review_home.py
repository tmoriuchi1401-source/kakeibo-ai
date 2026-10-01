"""Narrow bank navigation in the existing owned management home."""
from .bank_review_ui import BANK_MARKER
from .sheets import CATEGORY_WORKFLOW_SHEET
from .sheets_ui import HOME_ID, SPREADSHEET_ID, owned


def _read_literals(db):
    # UNFORMATTED_VALUE still evaluates formulas. Ownership/idempotence and
    # read-back compare the authored formula, not today's displayed count.
    return db._execute_sheet_read(lambda: db.svc.spreadsheets().values().get(
        spreadsheetId=db.sid, range="'ホーム'!A11:B17", valueRenderOption="FORMULA"
    )).get("values", [])


def home_cells(metadata):
    if metadata.get("spreadsheetId") != SPREADSHEET_ID:
        raise ValueError("bank_home_binding_invalid")
    sheets = {sheet["properties"]["title"]: sheet for sheet in metadata["sheets"]}
    home = sheets.get("ホーム")
    workflow = sheets.get(CATEGORY_WORKFLOW_SHEET)
    if home is None or workflow is None or not owned(home) or home["properties"]["sheetId"] != HOME_ID:
        return {}
    sid = workflow["properties"]["sheetId"]
    if type(sid) is not int:
        raise ValueError("bank_home_workflow_binding_invalid")
    section = (f'IFERROR(INDEX(\'{CATEGORY_WORKFLOW_SHEET}\'!B1:B10000,'
               f'MATCH("{BANK_MARKER}",\'{CATEGORY_WORKFLOW_SHEET}\'!A1:A10000,0)+1),"")')
    def count(pattern):
        return f'IFERROR(VALUE(REGEXEXTRACT({section},"{pattern}")),0)'
    groups = count("用途確認 ([0-9]+)グループ")
    pending = "+".join(count(pattern) for pattern in (
        "記帳待ち ([0-9]+)グループ", "収入未記帳 ([0-9]+)件", "不一致 ([0-9]+)件"))
    target = f'"#gid={sid}&range=A"&MATCH("{BANK_MARKER}",\'{CATEGORY_WORKFLOW_SHEET}\'!A1:A10000,0)'
    formula = (f'=IF({groups}>0,HYPERLINK({target},"銀行確認 "&{groups}&"グループ →"),'
               f'IF(({pending})>0,HYPERLINK({target},"銀行の記帳状況を確認 →"),""))')
    return {11: {"stringValue": "分類・ルールを整える"},
            13: {"formulaValue": f'=HYPERLINK("#gid={sid}&range=A1","支出カテゴリのルールを登録・変更　"&C8&"件 →")'},
            17: {"formulaValue": formula}}


def update_home(db):
    db._invalidate_sheet_metadata()
    values = home_cells(db._sheet_metadata())
    if not values:
        return {"bank_home_write_requests": 0}
    prior = _read_literals(db)
    if not prior or not prior[0] or prior[0][0] not in {"整理・改善", "分類・ルールを整える"}:
        return {"bank_home_write_requests": 0, "bank_home_held": 1}
    expected = {row: next(iter(value.values())) for row, value in values.items()}
    actual = {row: prior[row - 11][0] if len(prior) > row - 11 and prior[row - 11] else "" for row in values}
    if actual == expected:
        return {"bank_home_write_requests": 0}
    metadata = db._sheet_metadata()
    sid = next(sheet["properties"]["sheetId"] for sheet in metadata["sheets"]
               if sheet["properties"]["title"] == CATEGORY_WORKFLOW_SHEET)
    old_category = f'=HYPERLINK("#gid={sid}&range=A1","分類ルールを登録　"&C8&"件 →")'
    if actual[13] not in {old_category, expected[13]} or actual[17] not in {
            "対象月は「カテゴリ操作」画面上部で選べます。", expected[17]}:
        return {"bank_home_write_requests": 0, "bank_home_held": 1}
    requests = [{"updateCells": {"start": {"sheetId": HOME_ID, "rowIndex": row - 1, "columnIndex": 0},
        "rows": [{"values": [{"userEnteredValue": value}]}], "fields": "userEnteredValue"}}
        for row, value in values.items()]
    requests.extend([
        {"repeatCell": {"range": {"sheetId": HOME_ID, "startRowIndex": 16, "endRowIndex": 17,
            "startColumnIndex": 0, "endColumnIndex": 2}, "cell": {"userEnteredFormat": {"textFormat": {
                "fontSize": 11, "underline": True, "foregroundColorStyle": {
                    "rgbColor": {"red": 17 / 255, "green": 85 / 255, "blue": 204 / 255}}}}},
            "fields": "userEnteredFormat.textFormat(fontSize,underline,foregroundColorStyle)"}},
        {"updateDimensionProperties": {"range": {"sheetId": HOME_ID, "dimension": "ROWS",
            "startIndex": 16, "endIndex": 17}, "properties": {"pixelSize": 44}, "fields": "pixelSize"}},
    ])
    # Preserve B3/B4, D:I, every other home cell, and all other sheets.
    if _read_literals(db) != prior:
        raise ValueError("bank_home_changed_before_apply")
    db.svc.spreadsheets().batchUpdate(spreadsheetId=db.sid, body={"requests": requests}).execute(num_retries=0)
    after = _read_literals(db)
    if any(len(after) <= row - 11 or not after[row - 11] or after[row - 11][0] != value for row, value in expected.items()):
        raise ValueError("bank_home_readback_failed")
    return {"bank_home_write_requests": 1}
