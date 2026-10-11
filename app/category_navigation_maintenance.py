"""Maintain installed inline summaries; no accounting or approval pipeline."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import json
import math
import os

from .category_navigation import (LEDGER, WORKFLOW, HEADER,
    INLINE_MARKER, INLINE_VERSION, A_WIDTH,
    representative_summary_formula, representative_summary, summary_height,
    return_formula, sheet_url, source_rows, inline_scope)


def entered(cell):
    result = {k: deepcopy(v) for k, v in cell.items() if k in ("userEnteredValue", "userEnteredFormat", "note", "dataValidation")}
    if "formulaValue" in cell.get("userEnteredValue", {}):
        result.get("userEnteredFormat", {}).get("textFormat", {}).pop("link", None)
    fmt = result.get("userEnteredFormat", {})
    if fmt.get("textFormat") == {}: fmt.pop("textFormat")
    if not fmt: result.pop("userEnteredFormat", None)
    return result


def contains(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(contains(actual.get(k), v) for k, v in expected.items())
    if isinstance(expected, float) and isinstance(actual, (int, float)):
        return math.isclose(actual, expected, abs_tol=1e-6)
    return actual == expected


class NavigationMaintenance:
    def __init__(self, db): self.db = db

    def snapshot(self):
        meta = self.db._execute_sheet_read(lambda: self.db.svc.spreadsheets().get(
            spreadsheetId=self.db.sid,
            fields="sheets(properties,protectedRanges,developerMetadata,merges),namedRanges"))
        sheets = {s["properties"]["title"]: s for s in meta["sheets"]}
        if not all(t in sheets for t in (LEDGER, WORKFLOW)):
            raise ValueError("category_navigation_sheet_missing")
        if sheets[LEDGER]["properties"]["gridProperties"]["columnCount"] < 14:
            raise ValueError("category_navigation_column_missing")
        ranges = [f"'{t}'!A1:{end}{sheets[t]['properties']['gridProperties']['rowCount']}"
                  for t, end in ((LEDGER, "N"), (WORKFLOW, "AA"))]
        result = self.db._execute_sheet_read(lambda: self.db.svc.spreadsheets().get(
            spreadsheetId=self.db.sid, ranges=ranges, includeGridData=True,
            fields="sheets(properties(title),data(startRow,startColumn,rowData(values("
                "userEnteredValue,userEnteredFormat,dataValidation,note,effectiveValue,hyperlink)),"
                "columnMetadata(pixelSize),rowMetadata(pixelSize)))"))
        cells, dimensions = {}, {}
        for s in result.get("sheets", []):
            title = s["properties"]["title"]; table = cells.setdefault(title, {})
            dims = dimensions.setdefault(title, {})
            for b in s.get("data", []):
                for r, row in enumerate(b.get("rowData", []), b.get("startRow", 0)):
                    for c, cell in enumerate(row.get("values", []), b.get("startColumn", 0)):
                        if cell: table[r, c] = cell
                for key, start in (("rowMetadata", "startRow"), ("columnMetadata", "startColumn")):
                    dims.setdefault(key, {}).update({i: v.get("pixelSize") for i, v in
                        enumerate(b.get(key, []), b.get(start, 0))})
        return {"sheets": sheets, "cells": cells, "dimensions": dimensions}

    @staticmethod
    def rows(snapshot, title, width, *, formulas=False):
        cells = snapshot["cells"].get(title, {})
        last = max((r for (r, _), cell in cells.items() if cell.get("userEnteredValue")), default=0)
        return [[next(iter(cells.get((r, c), {}).get("userEnteredValue" if formulas else "effectiveValue", {}).values()), "")
                 for c in range(width)] for r in range(last+1)]

    @staticmethod
    def phase(snapshot):
        markers = [m for m in snapshot["sheets"][WORKFLOW].get("developerMetadata", [])
                   if m.get("metadataKey") == INLINE_MARKER]
        if len(markers) > 1: raise ValueError("category_navigation_metadata_ambiguous")
        return markers[0] if markers else None

    def plan(self, snapshot, mode):
        if mode not in ("preview", "refresh"):
            raise ValueError("category_navigation_mode_invalid")
        workflow = self.rows(snapshot, WORKFLOW, 27, formulas=True)
        ledger = self.rows(snapshot, LEDGER, 14, formulas=True)
        sources = source_rows(workflow)
        marker = self.phase(snapshot)
        phase = marker["metadataValue"] if marker else None
        if not marker: return {"requests": [], "selected": [], "installed": False}
        full = phase == INLINE_VERSION
        if full: selected = set(i for _, i in sources)
        else:
            selected = inline_scope({"sheets": list(snapshot["sheets"].values())})
        if not full and not selected <= {i for _, i in sources}:
            raise ValueError("category_navigation_canary_source_missing")
        counts = Counter(str(r[0]) for r in ledger[1:] if r[0])
        if any(counts[i] != 1 for i in selected):
            raise ValueError("category_navigation_representative_id_mismatch")
        records = {str(r[0]): r[:13] for r in self.rows(snapshot, LEDGER, 14)[1:] if r[0]}
        sid = snapshot["sheets"][WORKFLOW]["properties"]["sheetId"]
        lid = snapshot["sheets"][LEDGER]["properties"]["sheetId"]
        requests, allowed = [], set()
        def cell(r, c, value, fields):
            requests.append({"updateCells": {"start": {"sheetId": sid, "rowIndex": r, "columnIndex": c},
                "rows": [{"values": [value]}], "fields": fields}})
            allowed.add((r, c))
        # Two legacy top merges cross the requested frozen-column boundary.
        # Move only the static instruction into B4:F4; preserve controls/results.
        for merge in snapshot["sheets"][WORKFLOW].get("merges", []):
            if merge.get("startColumnIndex", 0) == 0 and merge.get("endColumnIndex", 0) > 1:
                r, end = merge.get("startRowIndex", 0), merge.get("endRowIndex", 0)
                if (r, end, merge["endColumnIndex"]) not in ((3, 4, 6), (4, 5, 3)):
                    raise ValueError("category_navigation_frozen_column_merge_conflict")
                requests.append({"unmergeCells": {"range": merge}})
                if r == 4:
                    # Sheets copies the merged anchor format into B5:C5 on
                    # unmerge. Restore their original entered formats explicitly.
                    for c in (1, 2):
                        requests.append({"updateCells": {"start": {"sheetId": sid, "rowIndex": 4, "columnIndex": c},
                            "rows": [{"values": [{"userEnteredFormat": deepcopy(snapshot["cells"][WORKFLOW].get((4, c), {}).get("userEnteredFormat", {}))}]}],
                            "fields": "userEnteredFormat"}})
                if r == 3:
                    if snapshot["cells"][WORKFLOW].get((3, 1), {}).get("userEnteredValue"):
                        raise ValueError("category_navigation_instruction_destination_occupied")
                    original = entered(snapshot["cells"][WORKFLOW].get((3, 0), {}))
                    if not original.get("userEnteredValue", {}).get("stringValue"):
                        raise ValueError("category_navigation_instruction_changed")
                    cell(3, 1, original, "userEnteredValue,userEnteredFormat,note")
                    cell(3, 0, {"userEnteredValue": {"stringValue": "操作の説明"}}, "userEnteredValue")
                    requests.append({"mergeCells": {"range": {**merge, "startColumnIndex": 1}, "mergeType": "MERGE_ALL"}})
        for r, identity in sources:
            if identity not in selected: continue
            value = {"userEnteredValue": {"formulaValue": representative_summary_formula()},
                "userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP",
                    "textFormat": {"fontFamily": "Arial", "fontSize": 11, "bold": False,
                        "underline": False, "foregroundColor": {"red": 41/255, "green": 51/255, "blue": 59/255}}}}
            current = snapshot["cells"][WORKFLOW].get((r-1, 0), {})
            if current.get("dataValidation"): raise ValueError("category_navigation_summary_constraints")
            fields = "userEnteredValue,userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment,userEnteredFormat.textFormat"
            if not contains(current, value) or current.get("userEnteredFormat", {}).get("textFormat", {}).get("link"):
                cell(r-1, 0, value, fields)
            height = summary_height(representative_summary(records[identity]))
            if snapshot.get("dimensions", {}).get(WORKFLOW, {}).get("rowMetadata", {}).get(r-1) != height:
                requests.append({"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS",
                    "startIndex": r-1, "endIndex": r}, "properties": {"pixelSize": height}, "fields": "pixelSize"}})
        if snapshot.get("dimensions", {}).get(WORKFLOW, {}).get("columnMetadata", {}).get(0) != A_WIDTH:
            requests.append({"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS",
                "startIndex": 0, "endIndex": 1}, "properties": {"pixelSize": A_WIDTH}, "fields": "pixelSize"}})
        if (3, 1) in allowed or workflow[3][0] == "操作の説明":
            if snapshot.get("dimensions", {}).get(WORKFLOW, {}).get("rowMetadata", {}).get(3) != 72:
                requests.append({"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS",
                    "startIndex": 3, "endIndex": 4}, "properties": {"pixelSize": 72}, "fields": "pixelSize"}})
        grid = snapshot["sheets"][WORKFLOW]["properties"]["gridProperties"]
        if grid.get("frozenRowCount") != 4 or grid.get("frozenColumnCount", 0) != 1:
            requests.append({"updateSheetProperties": {"properties": {"sheetId": sid,
                "gridProperties": {"frozenRowCount": 4, "frozenColumnCount": 1}},
                "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"}})
        back = return_formula(self.db.sid, sid)
        if ledger[0][13] != HEADER: raise ValueError("category_navigation_header_changed")
        for r, row in enumerate(ledger[1:], 1):
            if row[13] not in ("", back): raise ValueError("category_navigation_return_cell_changed")
            # Repair missing canary backlinks by ID after partial ledger sorts.
            # Full rollout keeps the ROW-bound formula on every occupied record.
            if (full or row[0] in selected) and row[0] and row[13] != back:
                requests.append({"updateCells": {"start": {"sheetId": lid, "rowIndex": r, "columnIndex": 13},
                    "rows": [{"values": [{"userEnteredValue": {"formulaValue": back}}]}], "fields": "userEnteredValue"}})
        return {"requests": requests, "selected": selected, "allowed": allowed, "installed": True,
            "representatives": len(sources), "target_rows": sum(i in selected for _, i in sources), "phase": phase}

    def verify(self, before, after, plan):
        allowed = plan["allowed"]
        merges = deepcopy(before["sheets"][WORKFLOW].get("merges", []))
        for request in plan["requests"]:
            if "unmergeCells" in request: merges.remove(request["unmergeCells"]["range"])
            if "mergeCells" in request: merges.append(request["mergeCells"]["range"])
        if sorted(map(lambda x: json.dumps(x, sort_keys=True), merges)) != sorted(map(lambda x: json.dumps(x, sort_keys=True), after["sheets"][WORKFLOW].get("merges", []))):
            raise ValueError("category_navigation_layout_changed")
        for title in (LEDGER, WORKFLOW):
            for key in set(before["cells"][title]) | set(after["cells"][title]):
                if title == WORKFLOW and key in allowed or title == LEDGER and key[1] == 13: continue
                if entered(before["cells"][title].get(key, {})) != entered(after["cells"][title].get(key, {})):
                    raise ValueError("category_navigation_unrelated_readback_changed")
            if before["sheets"][title].get("protectedRanges", []) != after["sheets"][title].get("protectedRanges", []):
                raise ValueError("category_navigation_protection_changed")
            for field in ("rowCount", "columnCount"):
                if before["sheets"][title]["properties"]["gridProperties"][field] != after["sheets"][title]["properties"]["gridProperties"][field]:
                    raise ValueError("category_navigation_grid_changed")
        if self.plan(after, "refresh")["requests"]: raise ValueError("category_navigation_readback_failed")
        if (3, 1) in allowed and after["cells"][WORKFLOW][3, 1].get("userEnteredValue") != before["cells"][WORKFLOW][3, 0].get("userEnteredValue"):
            raise ValueError("category_navigation_instruction_readback_changed")
        sources = source_rows(self.rows(after, WORKFLOW, 27, formulas=True))
        counts = Counter(str(r[0]) for r in self.rows(after, LEDGER, 14)[1:] if r[0])
        records = {str(r[0]): r[:13] for r in self.rows(after, LEDGER, 14)[1:] if r[0]}
        matched = 0
        for r, identity in sources:
            if identity not in plan["selected"]: continue
            cell = after["cells"][WORKFLOW][r-1, 0]
            if counts[identity] != 1: raise ValueError("category_navigation_representative_id_mismatch")
            if cell.get("effectiveValue", {}).get("errorValue"): raise ValueError("category_navigation_formula_error")
            if cell.get("hyperlink") or cell.get("effectiveValue", {}).get("stringValue") != representative_summary(records[identity]):
                raise ValueError("category_navigation_summary_value_mismatch")
            matched += 1
        wid = after["sheets"][WORKFLOW]["properties"]["sheetId"]
        groups = Counter(i for _, i in sources)
        for r, row in enumerate(self.rows(after, LEDGER, 14, formulas=True)[1:], 1):
            if not row[13]: continue
            cell = after["cells"][LEDGER][r, 13]
            target = next(n for n, i in sources if i == row[0]) if groups[row[0]] == 1 else 1
            expected = sheet_url(self.db.sid, wid) + f"&range=A{target}" if groups[row[0]] else None
            if cell.get("hyperlink") != expected or cell.get("effectiveValue", {}).get("errorValue"):
                raise ValueError("category_navigation_return_target_mismatch")
        return matched

    def apply(self, mode):
        before = self.snapshot()
        plan = self.plan(before, mode)
        report = {k: v for k, v in plan.items() if k not in ("requests", "selected", "allowed")}
        if mode == "preview": return {**report, "planned_requests": len(plan["requests"]), "write_requests": 0}
        if not plan["installed"]: return report
        if self.snapshot() != before: raise ValueError("category_navigation_input_changed")
        if plan["requests"]:
            self.db.svc.spreadsheets().batchUpdate(spreadsheetId=self.db.sid,
                body={"requests": plan["requests"]}).execute(num_retries=0)
            self.db._invalidate_sheet_metadata()
        after = self.snapshot()
        matched = self.verify(before, after, plan)
        return {**report, "write_requests": int(bool(plan["requests"])),
            "id_matches": matched, "formula_errors": 0,
            "ledger_and_inputs_unchanged": True, "frozen_rows": 4, "frozen_columns": 1}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("inline-preview", "inline-refresh"), default="inline-preview")
    args = parser.parse_args()
    from .production_flow import verify_execution_boundary
    from .google_clients import read_only_sheets_service, shipping_backfill_sheets_service
    from .sheets import SheetsDB
    verify_execution_boundary(dict(os.environ), os.environ.get("GITHUB_SHA", ""))
    service = read_only_sheets_service() if args.mode == "inline-preview" else shipping_backfill_sheets_service()
    try:
        report = NavigationMaintenance(SheetsDB(os.environ["SPREADSHEET_ID"], service=service)).apply(
            args.mode[7:])
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) and str(exc).startswith("category_navigation_") else "category_navigation_failed"
        print(json.dumps({"status": "stopped", "reason": code}))
        raise SystemExit(1)


if __name__ == "__main__": main()
