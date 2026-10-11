"""UI-only, fixed-ID navigation. No accounting or approval writes."""
from __future__ import annotations

from collections import Counter

WORKFLOW = "カテゴリ操作"
LEDGER = "支出明細"
HEADER = "カテゴリ操作へ戻る"
MARKER = "kakeibo.category.navigation"
VERSION = "v1"
NOTE = ("固定支出IDで元のカテゴリ操作行へ戻ります。複数候補は一覧を開きます。"
        "Sheetsアプリで移動しない場合は、リンクをSafariで開いてください。")
RULE_MARKER = "■ 1. カテゴリを選ぶ・今後の自動分類"
NEXT_MARKER = "■ 2. 過去分の固定プレビュー"


def sheet_url(spreadsheet_id, sheet_id):
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid={sheet_id}"


def representative_transaction_formula(spreadsheet_id, expense_sheet_id, row_num, *, viewer_sheet_id=None):
    # ROW binds to the current row even after an A-only sort or row insertion.
    identity = "INDEX($H:$H,ROW())"
    match = f"MATCH(TRUE,ARRAYFORMULA('支出明細'!A:A={identity}),0)"
    count = f"SUMPRODUCT(N('支出明細'!A:A={identity}))"
    # Selecting a wide A:N range leaves N offscreen in Sheets. Select the
    # return cell on the same transaction row so navigation is immediately visible.
    base = sheet_url(spreadsheet_id, expense_sheet_id) + "&range=N"
    legacy = (f'=IF({identity}="","",IF({count}<>1,"代表取引を確認（ID不一致）",'
            f'IFNA(HYPERLINK("{base}"&{match},'
            f'"代表取引を見る｜"&TEXT(INDEX(\'支出明細\'!B:B,{match}),"yyyy-mm-dd")'
            f'&"｜"&TEXT(INDEX(\'支出明細\'!E:E,{match}),"#,##0")&"円"),'
            '"代表取引が見つかりません")))')
    if viewer_sheet_id is None:
        return legacy
    from .category_representative_view import VIEW
    view_count = f"SUMPRODUCT(N('{VIEW}'!N:N={identity}))"
    view_match = f"MATCH(TRUE,ARRAYFORMULA('{VIEW}'!N:N={identity}),0)"
    target = sheet_url(spreadsheet_id, viewer_sheet_id) + "&range=A"
    viewed = legacy.replace(f'"{base}"&{match}', f'"{target}"&{view_match}', 1)
    # During the three-ID canary other representatives retain the legacy route.
    # Duplicate viewer identities never choose the first matching row.
    return (f'=IF({view_count}=0,{legacy[1:]},IF({view_count}<>1,'
            f'"代表取引の表示を確認（ID不一致）",{viewed[1:]}))')


def return_formula(spreadsheet_id, workflow_sheet_id):
    # One identical formula per occupied ledger row, no captured physical row.
    # Empty outputs on non-representatives also survive A:M-only sorting.
    # Scope to the rule block; bank group IDs and preview payloads are not sources.
    base = sheet_url(spreadsheet_id, workflow_sheet_id)
    first = f'IFNA(MATCH("{RULE_MARKER}",\'{WORKFLOW}\'!A:A,0),0)'
    last = f'IFNA(MATCH("{NEXT_MARKER}",\'{WORKFLOW}\'!A:A,0),0)'
    markers_valid = (f'SUMPRODUCT(N(\'{WORKFLOW}\'!A:A="{RULE_MARKER}"))=1,'
                     f'SUMPRODUCT(N(\'{WORKFLOW}\'!A:A="{NEXT_MARKER}"))=1')
    return (f'=LET(id,INDEX($A:$A,ROW()),first,{first},last,{last},'
            f'IF(OR(id="",first=0,last<=first+2,NOT(AND({markers_valid}))),"",'
            f'LET(ids,INDEX(\'{WORKFLOW}\'!H:H,first+2):INDEX(\'{WORKFLOW}\'!H:H,last-1),'
            'n,SUMPRODUCT(N(ids=id)),IF(n=0,"",IF(n=1,'
            f'HYPERLINK("{base}&range=A"&(MATCH(TRUE,ARRAYFORMULA(ids=id),0)+first+1),'
            '"↩ カテゴリ操作に戻る"),'
            f'HYPERLINK("{base}&range=A1","↩ カテゴリ操作の一覧（複数候補）"))))))')


def source_rows(rows):
    """Read actual marker positions, never recompute positions from row counts."""
    markers = {}
    for marker in (RULE_MARKER, NEXT_MARKER):
        positions = [i for i, row in enumerate(rows) if row and row[0] == marker]
        if len(positions) != 1:
            raise ValueError("category_navigation_markers_invalid")
        markers[marker] = positions[0]
    first, last = markers[RULE_MARKER], markers[NEXT_MARKER]
    if last <= first + 1:
        raise ValueError("category_navigation_markers_invalid")
    return [(i + 1, str(rows[i][7])) for i in range(first + 2, last)
            if len(rows[i]) > 7 and rows[i][7]]


def navigation_plan(*, spreadsheet_id, ledger_sheet_id, workflow_sheet_id,
                    ledger_ids, workflow_rows, current, install=False, canary_ids=None):
    """Only N and rule A may change; validate before emitting any request."""
    header = current[0][0] if current and current[0] else ""
    if header != HEADER:
        if not install:
            return {"installed": False, "requests": []}
        if any(any(row) for row in current):
            raise ValueError("category_navigation_column_occupied")
    formula = return_formula(spreadsheet_id, workflow_sheet_id)
    if header == HEADER and any(row and row[0] not in ("", formula) for row in current[1:]):
        raise ValueError("category_navigation_column_changed")
    ids = [str(row[0]) if row and row[0] else "" for row in ledger_ids]
    counts = Counter(identity for identity in ids if identity)
    if any(n != 1 for n in counts.values()):
        raise ValueError("category_navigation_ledger_duplicate_id")
    sources = source_rows(workflow_rows)
    missing = [identity for _, identity in sources if counts[identity] != 1]
    if missing:
        raise ValueError("category_navigation_representative_missing")
    representatives = Counter(identity for _, identity in sources)
    selected = None if canary_ids is None else set(canary_ids)
    if selected is not None and (not selected or not selected <= representatives.keys()):
        raise ValueError("category_navigation_canary_unknown")
    requests = []
    def cell(sheet, row, column, value):
        requests.append({"updateCells": {"start": {"sheetId": sheet,
            "rowIndex": row - 1, "columnIndex": column},
            "rows": [{"values": [{"userEnteredValue": value} if value else {}]}], "fields": "userEnteredValue"}})
    if header != HEADER:
        cell(ledger_sheet_id, 1, 13, {"stringValue": HEADER})
    # Install on existing records only, never on thousands of empty grid rows.
    # Formulas on other records display nothing and make partial sorts safe.
    for number, identity in enumerate(ids, 2):
        wanted = formula if identity and (selected is None or identity in selected) else ""
        present = current[number-1][0] if number <= len(current) and current[number-1] else ""
        if present != wanted:
            cell(ledger_sheet_id, number, 13,
                 {"formulaValue": wanted} if wanted else {})
    for number, identity in sources:
        if selected is None or identity in selected:
            wanted = representative_transaction_formula(spreadsheet_id, ledger_sheet_id, number)
            if workflow_rows[number-1][0] != wanted:
                cell(workflow_sheet_id, number, 0, {"formulaValue": wanted})
    return {"installed": True, "requests": requests, "representatives": len(sources),
            "return_links": len(representatives), "ambiguous": sum(n > 1 for n in representatives.values()),
            "formula_rows": sum(bool(i) for i in ids),
            "canary_links": len(selected) if selected is not None else None}


def installed(meta):
    return any(s.get("properties", {}).get("title") == LEDGER and any(
        m.get("metadataKey") == MARKER and m.get("metadataValue") == VERSION
        for m in s.get("developerMetadata", [])) for s in meta.get("sheets", []))


def refresh_navigation(db):
    """Maintain an explicitly installed UI column during existing refreshes."""
    meta = db._sheet_metadata()
    from .category_representative_view import viewer_sheet, ViewMaintenance
    if viewer_sheet(meta):
        return ViewMaintenance(db).apply("refresh")
    if not installed(meta):
        return {"installed": False}
    if db.get_raw("'支出明細'!N1:N1") != [[HEADER]]:
        raise ValueError("category_navigation_header_changed")
    sheets = {s["properties"]["title"]: s["properties"] for s in meta["sheets"]}
    ledger, workflow = sheets[LEDGER], sheets[WORKFLOW]
    extent = ledger["gridProperties"]["rowCount"]
    workflow_extent = workflow["gridProperties"]["rowCount"]
    # FORMULA read avoids turning existing formulas into their displayed labels.
    current = db._execute_sheet_read(lambda: db.svc.spreadsheets().values().get(
        spreadsheetId=db.sid, range=f"'{LEDGER}'!N1:N{extent}",
        valueRenderOption="FORMULA")).get("values", [])
    workflow_rows = db._execute_sheet_read(lambda: db.svc.spreadsheets().values().get(
        spreadsheetId=db.sid, range=f"'{WORKFLOW}'!A1:L{workflow_extent}",
        valueRenderOption="FORMULA")).get("values", [])
    plan = navigation_plan(spreadsheet_id=db.sid, ledger_sheet_id=ledger["sheetId"],
        workflow_sheet_id=workflow["sheetId"], ledger_ids=db.get_raw(f"'{LEDGER}'!A2:A{extent}"),
        workflow_rows=workflow_rows, current=current)
    if plan["requests"]:
        db.svc.spreadsheets().batchUpdate(spreadsheetId=db.sid,
            body={"requests": plan["requests"]}).execute(num_retries=0)
    return {key: value for key, value in plan.items() if key != "requests"}
