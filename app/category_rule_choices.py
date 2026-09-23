"""Future-rule decisions, independent of historical-preview approval."""
REGISTER = "登録する"
DECLINE = "登録しない"
UNSELECTED = "未選択"


def checked(value):
    return str(value).strip().upper() in {"TRUE", "1", "YES", "ON", REGISTER}


def physical_choice(value):
    if value == DECLINE:
        return DECLINE
    return REGISTER if checked(value) else UNSELECTED


def logical_choice(value):
    return DECLINE if value == DECLINE else checked(value)


def choice_validation(sheet_id, start_row, count):
    return {"setDataValidation": {"range": {"sheetId": sheet_id,
        "startRowIndex": start_row - 1, "endRowIndex": start_row + count - 1,
        "startColumnIndex": 4, "endColumnIndex": 5}, "rule": {
        "condition": {"type": "ONE_OF_LIST", "values": [
            {"userEnteredValue": value} for value in (UNSELECTED, REGISTER, DECLINE)]},
        "strict": True, "showCustomUi": True,
        "inputMessage": "登録する＝次回処理で保存。登録しない＝この候補の追加登録を見送ります。既存ルールと過去分は変更しません。"}}}
