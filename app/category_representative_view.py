"""Derived representative transactions with persistent, frozen navigation."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import math
import json

from .category_navigation import (LEDGER, WORKFLOW, MARKER as LEGACY_MARKER,
    HEADER as LEGACY_HEADER, RULE_MARKER, NEXT_MARKER, return_formula,
    representative_transaction_formula, sheet_url, source_rows)
from .category_navigation_maintenance import NavigationMaintenance

VIEW = "代表取引確認"
VIEW_MARKER = "kakeibo.category.representative.view"
CANARY_MARKER = "kakeibo.category.representative.canary"
VIEW_VERSION = "v2"
PHASES = {"canary-v2", "verified-v2", VIEW_VERSION}
ORIGIN_LABEL = "↩ 元のカテゴリ操作行へ戻る"
LIST_LABEL = "↩ カテゴリ操作の一覧へ"
VIEW_NOTE = "閲覧用表示です。戻り先は各取引の固定支出IDから判定します。"


def viewer_sheet(meta):
    sheets = [s for s in meta.get("sheets", []) if s["properties"]["title"] == VIEW]
    if len(sheets) > 1:
        raise ValueError("category_navigation_view_ambiguous")
    if not sheets:
        return None
    sheet = sheets[0]
    markers = [m for m in sheet.get("developerMetadata", []) if m.get("metadataKey") == VIEW_MARKER]
    if len(markers) != 1 or markers[0].get("metadataValue") not in PHASES:
        raise ValueError("category_navigation_view_not_owned")
    return sheet


def viewer_sheet_id(meta):
    sheet = viewer_sheet(meta)
    return sheet["properties"]["sheetId"] if sheet else None


def view_return_formula(spreadsheet_id, workflow_sheet_id):
    base = sheet_url(spreadsheet_id, workflow_sheet_id)
    listing = f'HYPERLINK("{base}&range=A1","{LIST_LABEL}'
    first = f'IFNA(MATCH("{RULE_MARKER}",\'{WORKFLOW}\'!A:A,0),0)'
    last = f'IFNA(MATCH("{NEXT_MARKER}",\'{WORKFLOW}\'!A:A,0),0)'
    valid = (f'SUMPRODUCT(N(\'{WORKFLOW}\'!A:A="{RULE_MARKER}"))=1,'
             f'SUMPRODUCT(N(\'{WORKFLOW}\'!A:A="{NEXT_MARKER}"))=1')
    return (f'=LET(id,INDEX($N:$N,ROW()),first,{first},last,{last},'
        f'IF(id="","",IF(OR(first=0,last<=first+2,NOT(AND({valid}))),'
        f'{listing}（再確認）"),LET(ids,INDEX(\'{WORKFLOW}\'!H:H,first+2):'
        f'INDEX(\'{WORKFLOW}\'!H:H,last-1),n,SUMPRODUCT(N(ids=id)),'
        f'IF(n=0,{listing}（対象なし）"),IF(n=1,'
        f'HYPERLINK("{base}&range=A"&(MATCH(TRUE,ARRAYFORMULA(ids=id),0)+first+1),'
        f'"{ORIGIN_LABEL}"),{listing}（複数候補）")))))))')


def ledger_value_formula(column):
    identity = "INDEX($N:$N,ROW())"
    count = f"SUMPRODUCT(N('{LEDGER}'!A:A={identity}))"
    match = f"MATCH(TRUE,ARRAYFORMULA('{LEDGER}'!A:A={identity}),0)"
    value = f"INDEX('{LEDGER}'!{column}:{column},{match})"
    return (f'=IF({identity}="","",IF({count}<>1,"取引を確認（ID不一致）",'
            f'IF(ISBLANK({value}),"",{value})))')


def _cell(value, fmt=None, note=None):
    field = "formulaValue" if value.startswith("=") else "stringValue"
    result = {"userEnteredValue": {field: value}}
    if fmt: result["userEnteredFormat"] = fmt
    if note: result["note"] = note
    return result


def view_rows(spreadsheet_id, workflow_sheet_id, identities, headers):
    body = {"wrapStrategy": "WRAP", "verticalAlignment": "TOP",
            "textFormat": {"fontFamily": "Arial", "fontSize": 11}}
    link = {**body, "textFormat": {**body["textFormat"],
            "foregroundColor": {"red": 25/255, "green": 76/255, "blue": 178/255}}}
    # Sheets stores RGB channels as 8-bit colors. Use exact channels so native
    # readback does not schedule an identical formatting write on every replay.
    header = {**body, "backgroundColor": {"red": 234/255, "green": 239/255, "blue": 242/255},
              "textFormat": {**body["textFormat"], "bold": True}}
    rows = [[_cell(f'=HYPERLINK("{sheet_url(spreadsheet_id, workflow_sheet_id)}&range=A1","{LIST_LABEL}")',
                    {**link, "textFormat": {**link["textFormat"], "bold": True}}, VIEW_NOTE),
             _cell("代表取引の確認", header)],
            [_cell("元の行へ戻る", header), *[_cell(str(x), header) for x in headers[1:13]],
             _cell("固定支出ID", header)]]
    for identity in identities:
        cells = [_cell(view_return_formula(spreadsheet_id, workflow_sheet_id), link)]
        for index, column in enumerate("BCDEFGHIJKLM", 1):
            fmt = dict(body)
            if index == 1: fmt["numberFormat"] = {"type": "DATE", "pattern": "yyyy-mm-dd"}
            if index == 4: fmt["numberFormat"] = {"type": "NUMBER", "pattern": "#,##0"}
            cells.append(_cell(ledger_value_formula(column), fmt))
        cells.append(_cell(identity, body))
        rows.append(cells)
    return rows


def _contains(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(_contains(actual.get(k), v) for k, v in expected.items())
    if isinstance(expected, float) and isinstance(actual, (int, float)):
        return math.isclose(actual, expected, abs_tol=1e-6)
    return actual == expected


def _entered_cell(cell):
    result = {f: deepcopy(v) for f, v in cell.items() if f not in ("effectiveValue", "hyperlink")}
    if "formulaValue" in cell.get("userEnteredValue", {}):
        # Google also mirrors a HYPERLINK result into textFormat.link. That
        # computed URI follows rows; it is not an edit to the cell's format.
        result.get("userEnteredFormat", {}).get("textFormat", {}).pop("link", None)
    return result


class ViewMaintenance(NavigationMaintenance):
    def snapshot(self):
        snapshot = super().snapshot()
        sheet = snapshot["sheets"].get(VIEW)
        if sheet:
            props = sheet["properties"]
            if props["gridProperties"]["columnCount"] < 14:
                raise ValueError("category_navigation_view_grid_invalid")
            result = self.db._execute_sheet_read(lambda: self.db.svc.spreadsheets().get(
                spreadsheetId=self.db.sid, ranges=[f"'{VIEW}'!A1:N{props['gridProperties']['rowCount']}"],
                includeGridData=True, fields="sheets(data(startRow,startColumn,rowData(values("
                    "userEnteredValue,userEnteredFormat,dataValidation,note,effectiveValue,hyperlink)),"
                    "columnMetadata(pixelSize),rowMetadata(pixelSize)))"))
            cells = snapshot["cells"].setdefault(VIEW, {})
            dimensions = snapshot.setdefault("dimensions", {})
            for block in result["sheets"][0].get("data", []):
                for r, row in enumerate(block.get("rowData", []), block.get("startRow", 0)):
                    for c, cell in enumerate(row.get("values", []), block.get("startColumn", 0)):
                        if cell: cells[r, c] = cell
                for key, offset in (("columnMetadata", "startColumn"), ("rowMetadata", "startRow")):
                    dimensions[key] = {i: v.get("pixelSize") for i, v in
                        enumerate(block.get(key, []), block.get(offset, 0))}
        return snapshot

    def plan(self, snapshot, mode, *, iphone_sheets_verified=False):
        meta = {"sheets": list(snapshot["sheets"].values())}
        sheet = viewer_sheet(meta)
        phase = next((m["metadataValue"] for m in sheet.get("developerMetadata", [])
                      if m.get("metadataKey") == VIEW_MARKER), None) if sheet else None
        if mode == "expand" and (phase not in ("verified-v2", VIEW_VERSION) or not iphone_sheets_verified):
            raise ValueError("category_navigation_view_iphone_verified_canary_required")
        if mode == "canary" and phase == VIEW_VERSION:
            raise ValueError("category_navigation_view_already_expanded")
        if mode == "refresh" and not sheet:
            return {"requests": [], "identities": [], "installed": False}
        ledger = self.rows(snapshot, LEDGER, 14, formulas=True)
        workflow = self.rows(snapshot, WORKFLOW, 27, formulas=True)
        sources = source_rows(workflow)
        ids = [str(r[0]) for r in ledger[1:] if r[0]]
        if any(n != 1 for n in Counter(ids).values()):
            raise ValueError("category_navigation_ledger_duplicate_id")
        if any(identity not in ids for _, identity in sources):
            raise ValueError("category_navigation_representative_missing")
        old = self.rows(snapshot, VIEW, 14, formulas=True) if sheet else []
        existing_ids = [str(r[13]) for r in old[2:] if r[13]]
        if len(set(existing_ids)) != len(existing_ids):
            raise ValueError("category_navigation_view_duplicate_id")
        if sheet:
            expected = view_rows(self.db.sid, snapshot["sheets"][WORKFLOW]["properties"]["sheetId"],
                                 [], ledger[0][:13])
            # A derived viewer contains no editable inputs or shared last-return state.
            for (r, c), cell in snapshot["cells"].get(VIEW, {}).items():
                if r >= 2:
                    identity = str(old[r][13]) if r < len(old) and old[r][13] else ""
                    owned = view_rows(self.db.sid, snapshot["sheets"][WORKFLOW]["properties"]["sheetId"],
                                      [identity], ledger[0][:13])[2]
                    wanted = owned[c]
                else:
                    wanted = expected[r][c] if c < len(expected[r]) else {}
                if cell.get("dataValidation") or cell.get("note") not in (None, "", VIEW_NOTE):
                    raise ValueError("category_navigation_view_foreign_cell")
                if cell.get("userEnteredValue") and cell["userEnteredValue"] != wanted.get("userEnteredValue", {}):
                    raise ValueError("category_navigation_view_foreign_cell")
        full = mode == "expand" or phase == VIEW_VERSION
        if full:
            live = list(dict.fromkeys(identity for _, identity in sources))
            identities = [i for i in existing_ids if i in live] + [i for i in live if i not in existing_ids]
        elif sheet:
            scopes = [m for m in sheet.get("developerMetadata", []) if m.get("metadataKey") == CANARY_MARKER]
            if len(scopes) != 1:
                raise ValueError("category_navigation_view_canary_scope_changed")
            scope = json.loads(scopes[0]["metadataValue"])
            if not isinstance(scope, list) or not 1 <= len(scope) <= 3 or len(set(scope)) != len(scope) or not set(existing_ids) <= set(scope):
                raise ValueError("category_navigation_view_canary_scope_changed")
            identities = existing_ids + [i for i in scope if i not in existing_ids]
        else:
            legacy = self.marker(snapshot)
            if not legacy or legacy.get("metadataValue") != "verified-v1" or ledger[0][13] != LEGACY_HEADER:
                raise ValueError("category_navigation_existing_canary_required")
            formula = return_formula(self.db.sid, snapshot["sheets"][WORKFLOW]["properties"]["sheetId"])
            identities = [str(r[0]) for r in ledger[1:] if r[13] == formula]
            if not 1 <= len(identities) <= 3 or any(i not in dict(Counter(i for _, i in sources)) for i in identities):
                raise ValueError("category_navigation_existing_canary_invalid")
        if not full and not 1 <= len(identities) <= 3:
            raise ValueError("category_navigation_view_canary_invalid")
        if any(i not in ids for i in identities):
            raise ValueError("category_navigation_representative_missing")
        used_ids = {s["properties"]["sheetId"] for s in snapshot["sheets"].values()}
        vid = sheet["properties"]["sheetId"] if sheet else 261011117
        while not sheet and vid in used_ids: vid += 1
        wid = snapshot["sheets"][WORKFLOW]["properties"]["sheetId"]
        lid = snapshot["sheets"][LEDGER]["properties"]["sheetId"]
        wanted = view_rows(self.db.sid, wid, identities, ledger[0][:13])
        requests = []
        if not sheet:
            requests += [{"addSheet": {"properties": {"sheetId": vid, "title": VIEW,
                "gridProperties": {"rowCount": max(1000, len(wanted)), "columnCount": 14,
                    "frozenRowCount": 2, "frozenColumnCount": 1, "hideGridlines": True}}}},
                {"createDeveloperMetadata": {"developerMetadata": {"metadataKey": VIEW_MARKER,
                    "metadataValue": "canary-v2", "location": {"sheetId": vid}, "visibility": "DOCUMENT"}}},
                {"createDeveloperMetadata": {"developerMetadata": {"metadataKey": CANARY_MARKER,
                    "metadataValue": json.dumps(identities), "location": {"sheetId": vid}, "visibility": "DOCUMENT"}}}]
        else:
            grid = sheet["properties"]["gridProperties"]
            if len(wanted) > grid["rowCount"]:
                requests.append({"appendDimension": {"sheetId": vid, "dimension": "ROWS",
                    "length": len(wanted) - grid["rowCount"]}})
            if grid.get("frozenRowCount", 0) != 2 or grid.get("frozenColumnCount", 0) != 1 or not grid.get("hideGridlines"):
                requests.append({"updateSheetProperties": {"properties": {"sheetId": vid,
                    "gridProperties": {"frozenRowCount": 2, "frozenColumnCount": 1, "hideGridlines": True}},
                    "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount,gridProperties.hideGridlines"}})
        changed = not sheet or len(old) != len(wanted)
        for r, row in enumerate(wanted):
            for c, value in enumerate(row):
                current = snapshot["cells"].get(VIEW, {}).get((r, c), {})
                if not _contains(current, value): changed = True
        if changed:
            rows = [{"values": row + [{}] * (14-len(row))} for row in wanted]
            rows += [{"values": [{}] * 14} for _ in range(max(0, len(old)-len(wanted)))]
            requests.append({"updateCells": {"start": {"sheetId": vid, "rowIndex": 0, "columnIndex": 0},
                "rows": rows, "fields": "userEnteredValue,userEnteredFormat,note"}})
        widths = [172, 112, 190, 280, 100, 130, 130, 150, 150, 180, 260, 220, 100, 210]
        dimensions = snapshot.get("dimensions", {})
        for index, width in enumerate(widths):
            if dimensions.get("columnMetadata", {}).get(index) != width:
                requests.append({"updateDimensionProperties": {"range": {"sheetId": vid, "dimension": "COLUMNS",
                    "startIndex": index, "endIndex": index+1}, "properties": {"pixelSize": width}, "fields": "pixelSize"}})
        for first, last, height in ((0, 1, 48), (1, 2, 36), (2, len(wanted), 72)):
            if last > first and any(dimensions.get("rowMetadata", {}).get(i) != height for i in range(first, last)):
                requests.append({"updateDimensionProperties": {"range": {"sheetId": vid, "dimension": "ROWS",
                    "startIndex": first, "endIndex": last}, "properties": {"pixelSize": height}, "fields": "pixelSize"}})
        selected = set(identities)
        for row, identity in sources:
            if identity not in selected: continue
            formula = representative_transaction_formula(self.db.sid, lid, row, viewer_sheet_id=vid)
            if workflow[row-1][0] != formula:
                requests.append({"updateCells": {"start": {"sheetId": wid, "rowIndex": row-1, "columnIndex": 0},
                    "rows": [{"values": [{"userEnteredValue": {"formulaValue": formula}}]}], "fields": "userEnteredValue"}})
        return {"requests": requests, "identities": identities, "viewer_sheet_id": vid,
                "installed": True, "viewer_transactions": len(identities), "representatives": len(sources),
                "ambiguous": sum(n>1 for n in Counter(i for _, i in sources).values()), "phase": phase}

    def apply(self, mode, *, iphone_sheets_verified=False):
        before = self.snapshot()
        plan = self.plan(before, mode, iphone_sheets_verified=iphone_sheets_verified)
        report = {k: v for k, v in plan.items() if k not in ("requests", "identities", "viewer_sheet_id")}
        if mode == "preview":
            return {**report, "write_requests": 0, "planned_requests": len(plan["requests"])}
        if not plan["installed"]: return report
        if self.snapshot() != before:
            raise ValueError("category_navigation_input_changed")
        if plan["requests"]:
            self.db.svc.spreadsheets().batchUpdate(spreadsheetId=self.db.sid,
                body={"requests": plan["requests"]}).execute(num_retries=0)
            self.db._invalidate_sheet_metadata()
        after = self.snapshot()
        for title in (LEDGER, WORKFLOW):
            left = {k: _entered_cell(cell)
                    for k, cell in before["cells"][title].items() if title == LEDGER or k[1] != 0}
            right = {k: _entered_cell(cell)
                    for k, cell in after["cells"][title].items() if title == LEDGER or k[1] != 0}
            if left != right: raise ValueError("category_navigation_unrelated_readback_changed")
            if before["sheets"][title] != after["sheets"][title]:
                raise ValueError("category_navigation_existing_sheet_changed")
        wid = before["sheets"][WORKFLOW]["properties"]["sheetId"]
        allowed = {r["updateCells"]["start"]["rowIndex"] for r in plan["requests"]
            if "updateCells" in r and r["updateCells"]["start"]["sheetId"] == wid}
        for key in set(before["cells"][WORKFLOW]) | set(after["cells"][WORKFLOW]):
            if key[1] == 0 and key[0] not in allowed:
                # Owned formulas may recalculate when viewer rows move, without
                # changing any category cell. Verify the resolved targets below.
                left = _entered_cell(before["cells"][WORKFLOW].get(key, {}))
                right = _entered_cell(after["cells"][WORKFLOW].get(key, {}))
                if left != right: raise ValueError("category_navigation_unrelated_link_changed")
        if self.plan(after, "expand" if mode == "expand" else "refresh",
                     iphone_sheets_verified=iphone_sheets_verified)["requests"]:
            raise ValueError("category_navigation_view_readback_failed")
        grid = after["sheets"][VIEW]["properties"]["gridProperties"]
        if grid.get("frozenRowCount") != 2 or grid.get("frozenColumnCount") != 1:
            raise ValueError("category_navigation_view_freeze_mismatch")
        sources = source_rows(self.rows(after, WORKFLOW, 27))
        counts = Counter(i for _, i in sources)
        listing = sheet_url(self.db.sid, wid) + "&range=A1"
        if after["cells"][VIEW][0, 0].get("hyperlink") != listing:
            raise ValueError("category_navigation_view_list_target_mismatch")
        identities = plan["identities"]
        ledger = self.rows(after, LEDGER, 14)
        records = {str(r[0]): r[:13] for r in ledger[1:] if r[0]}
        for row, identity in enumerate(identities, 3):
            target = next(r for r, i in sources if i == identity) if counts[identity] == 1 else 1
            cell = after["cells"][VIEW][row-1, 0]
            if cell.get("hyperlink") != sheet_url(self.db.sid, wid) + f"&range=A{target}":
                raise ValueError("category_navigation_view_origin_target_mismatch")
            label = cell.get("effectiveValue", {}).get("stringValue", "")
            if counts[identity] == 1 and label != ORIGIN_LABEL or counts[identity] != 1 and not label.startswith(LIST_LABEL):
                raise ValueError("category_navigation_view_label_mismatch")
            for col, value in enumerate(records.get(identity, [""]*13)[1:], 1):
                actual = after["cells"][VIEW].get((row-1, col), {}).get("effectiveValue", {})
                actual = next(iter(actual.values()), "")
                if actual != value:
                    raise ValueError("category_navigation_view_transaction_mismatch")
        for row, identity in sources:
            if identity in identities:
                expected = sheet_url(self.db.sid, plan["viewer_sheet_id"]) + f"&range=A{identities.index(identity)+3}"
                if after["cells"][WORKFLOW][row-1, 0].get("hyperlink") != expected:
                    raise ValueError("category_navigation_view_forward_target_mismatch")
        marker = next(m for m in after["sheets"][VIEW]["developerMetadata"] if m["metadataKey"] == VIEW_MARKER)
        phase = VIEW_VERSION if mode == "expand" else marker["metadataValue"] if mode == "refresh" else "verified-v2"
        if marker["metadataValue"] != phase:
            self.db.svc.spreadsheets().batchUpdate(spreadsheetId=self.db.sid, body={"requests": [{
                "updateDeveloperMetadata": {"dataFilters": [{"developerMetadataLookup": {"metadataId": marker["metadataId"]}}],
                    "developerMetadata": {"metadataValue": phase}, "fields": "metadataValue"}}]}).execute(num_retries=0)
            confirmed = viewer_sheet({"sheets": list(self.snapshot()["sheets"].values())})
            if not any(m.get("metadataKey") == VIEW_MARKER and m.get("metadataValue") == phase for m in confirmed["developerMetadata"]):
                raise ValueError("category_navigation_view_phase_readback_failed")
        return {**report, "phase": phase, "write_requests": int(bool(plan["requests"])),
            "verified_transactions": len(identities), "ledger_and_inputs_unchanged": True,
            "frozen_rows": 2, "frozen_columns": 1, "safari_verification": "unverified"}
