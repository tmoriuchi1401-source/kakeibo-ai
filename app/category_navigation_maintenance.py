"""Locked, UI-only canary/expansion; does not invoke accounting pipelines."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import json
import os

from .category_navigation import (
    HEADER, LEDGER, MARKER, NOTE, VERSION, WORKFLOW, navigation_plan,
    representative_transaction_formula, return_formula, sheet_url, source_rows,
)


class NavigationMaintenance:
    def __init__(self, db):
        self.db = db

    def snapshot(self):
        meta = self.db._execute_sheet_read(lambda: self.db.svc.spreadsheets().get(
            spreadsheetId=self.db.sid,
            fields="sheets(properties,protectedRanges,developerMetadata)"))
        sheets = {s["properties"]["title"]: s for s in meta["sheets"]}
        if not all(title in sheets for title in (LEDGER, WORKFLOW)):
            raise ValueError("category_navigation_sheet_missing")
        if sheets[LEDGER]["properties"]["gridProperties"]["columnCount"] < 14:
            raise ValueError("category_navigation_column_missing")
        ranges = [f"'{title}'!A1:{end}{sheets[title]['properties']['gridProperties']['rowCount']}"
                  for title, end in ((LEDGER, "N"), (WORKFLOW, "AA"))]
        result = self.db._execute_sheet_read(lambda: self.db.svc.spreadsheets().get(
            spreadsheetId=self.db.sid, ranges=ranges, includeGridData=True,
            fields="sheets(properties(sheetId,title),data(startRow,startColumn,rowData(values("
                   "userEnteredValue,userEnteredFormat,dataValidation,note,effectiveValue,hyperlink))))"))
        cells = {}
        for sheet in result.get("sheets", []):
            table = cells.setdefault(sheet["properties"]["title"], {})
            for block in sheet.get("data", []):
                for i, row in enumerate(block.get("rowData", []), block.get("startRow", 0)):
                    for j, cell in enumerate(row.get("values", []), block.get("startColumn", 0)):
                        if cell:
                            table[i, j] = cell
        return {"sheets": sheets, "cells": cells}

    @staticmethod
    def rows(snapshot, title, width, *, formulas=False):
        cells = snapshot["cells"].get(title, {})
        last = max((row for (row, _), cell in cells.items() if cell.get("userEnteredValue")), default=0)
        result = []
        for row in range(last + 1):
            values = []
            for col in range(width):
                cell = cells.get((row, col), {})
                value = cell.get("userEnteredValue" if formulas else "effectiveValue", {})
                if not value:
                    value = cell.get("userEnteredValue", {})
                values.append(next(iter(value.values()), ""))
            result.append(values)
        return result

    @staticmethod
    def marker(snapshot):
        matches = [m for m in snapshot["sheets"][LEDGER].get("developerMetadata", [])
                   if m.get("metadataKey") == MARKER]
        if len(matches) > 1:
            raise ValueError("category_navigation_metadata_ambiguous")
        return matches[0] if matches else None

    def plan(self, snapshot, mode):
        if "代表取引確認" in snapshot["sheets"]:
            raise ValueError("category_navigation_view_mode_required")
        ledger = self.rows(snapshot, LEDGER, 14, formulas=True)
        workflow = self.rows(snapshot, WORKFLOW, 27, formulas=True)
        sources = source_rows(workflow)
        marker = self.marker(snapshot)
        if mode == "expand" and (not marker or marker.get("metadataValue") not in ("verified-v1", VERSION)):
            raise ValueError("category_navigation_verified_canary_required")
        if mode == "canary" and marker and marker.get("metadataValue") == VERSION:
            raise ValueError("category_navigation_already_expanded")
        if marker and marker.get("metadataValue") not in ("canary-v1", "verified-v1", VERSION):
            raise ValueError("category_navigation_metadata_changed")
        for (_, col), cell in snapshot["cells"].get(LEDGER, {}).items():
            if col == 13 and (cell.get("dataValidation") or cell.get("note") not in (None, "", NOTE)):
                raise ValueError("category_navigation_column_has_constraints")
        selected = list(dict.fromkeys(identity for _, identity in sources))[:3] if mode == "canary" else None
        result = navigation_plan(spreadsheet_id=self.db.sid,
            ledger_sheet_id=snapshot["sheets"][LEDGER]["properties"]["sheetId"],
            workflow_sheet_id=snapshot["sheets"][WORKFLOW]["properties"]["sheetId"],
            ledger_ids=[[row[0]] for row in ledger[1:]], workflow_rows=workflow,
            current=[[row[13]] for row in ledger], install=True, canary_ids=selected)
        result["selected"] = selected
        return result

    @staticmethod
    def invariant(snapshot):
        # Every user-entered value, validation, note and format outside the two
        # link surfaces remains byte-for-byte equivalent after normalization.
        return {title: {key: {field: value for field, value in cell.items()
                            if field not in ("effectiveValue", "hyperlink")}
                        for key, cell in cells.items()
                        if not (title == LEDGER and key[1] == 13)
                        and not (title == WORKFLOW and key[1] == 0)}
                for title, cells in snapshot["cells"].items()}

    def apply(self, mode):
        before = self.snapshot()
        plan = self.plan(before, mode)
        report = {k: v for k, v in plan.items() if k not in ("requests", "selected")}
        if mode == "preview":
            return {**report, "write_requests": 0, "planned_cells": len(plan["requests"])}
        # Verify the snapshot once more immediately before mutation.
        if self.snapshot() != before:
            raise ValueError("category_navigation_input_changed")
        requests = deepcopy(plan["requests"])
        marker = self.marker(before)
        sid = before["sheets"][LEDGER]["properties"]["sheetId"]
        if not marker:
            requests.append({"createDeveloperMetadata": {"developerMetadata": {
                "metadataKey": MARKER, "metadataValue": "canary-v1",
                "location": {"sheetId": sid}, "visibility": "DOCUMENT"}}})
            requests.extend([
                {"updateCells": {"start": {"sheetId": sid, "rowIndex": 0, "columnIndex": 13},
                    "rows": [{"values": [{"note": NOTE, "userEnteredFormat": {
                        "textFormat": {"bold": True}, "wrapStrategy": "WRAP"}}]}],
                    "fields": "note,userEnteredFormat(textFormat,wrapStrategy)"}},
                {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS",
                    "startIndex": 13, "endIndex": 14}, "properties": {"pixelSize": 260}, "fields": "pixelSize"}},
            ])
        if requests:
            self.db.svc.spreadsheets().batchUpdate(spreadsheetId=self.db.sid,
                body={"requests": requests}).execute(num_retries=0)
        after = self.snapshot()
        if self.invariant(before) != self.invariant(after):
            raise ValueError("category_navigation_unrelated_readback_changed")
        for title in (LEDGER, WORKFLOW):
            if before["sheets"][title].get("protectedRanges", []) != after["sheets"][title].get("protectedRanges", []):
                raise ValueError("category_navigation_protection_changed")
        # A contains unrelated controls, markers and bank links. Only the
        # exact planned representative cells may change there.
        allowed = {r["updateCells"]["start"]["rowIndex"] for r in plan["requests"]
                   if "updateCells" in r and r["updateCells"]["start"]["sheetId"] != sid}
        for key in set(before["cells"][WORKFLOW]) | set(after["cells"][WORKFLOW]):
            if key[1] == 0 and key[0] not in allowed:
                if before["cells"][WORKFLOW].get(key) != after["cells"][WORKFLOW].get(key):
                    raise ValueError("category_navigation_unrelated_link_changed")
        if self.plan(after, "canary" if mode == "canary" else "preview")["requests"]:
            raise ValueError("category_navigation_formula_readback_failed")
        workflow = self.rows(after, WORKFLOW, 27)
        sources = source_rows(workflow)
        counts = Counter(identity for _, identity in sources)
        selected = set(plan["selected"]) if plan["selected"] is not None else set(counts)
        ledger = self.rows(after, LEDGER, 14)
        positions = {row[0]: i for i, row in enumerate(ledger[1:], 2) if row[0]}
        wid = after["sheets"][WORKFLOW]["properties"]["sheetId"]
        for row_num, identity in sources:
            if identity not in selected:
                continue
            forward = after["cells"][WORKFLOW].get((row_num-1, 0), {})
            expected = sheet_url(self.db.sid, sid) + f"&range=N{positions[identity]}"
            if forward.get("hyperlink") != expected:
                raise ValueError("category_navigation_forward_target_mismatch")
        for number, row in enumerate(ledger[1:], 2):
            identity = row[0]
            backlink = after["cells"][LEDGER].get((number-1, 13), {})
            if identity in selected:
                target = next(r for r, i in sources if i == identity) if counts[identity] == 1 else 1
                expected = sheet_url(self.db.sid, wid) + f"&range=A{target}"
                if backlink.get("hyperlink") != expected:
                    raise ValueError("category_navigation_return_target_mismatch")
            elif backlink.get("hyperlink") or backlink.get("effectiveValue", {}).get("errorValue"):
                raise ValueError("category_navigation_unexpected_return_link")
        marker = self.marker(after)
        phase = "verified-v1" if mode == "canary" else VERSION
        if marker["metadataValue"] != phase:
            self.db.svc.spreadsheets().batchUpdate(spreadsheetId=self.db.sid, body={"requests": [{
                "updateDeveloperMetadata": {"dataFilters": [{"developerMetadataLookup": {
                    "metadataId": marker["metadataId"]}}], "developerMetadata": {"metadataValue": phase},
                    "fields": "metadataValue"}}]}).execute(num_retries=0)
            if self.marker(self.snapshot())["metadataValue"] != phase:
                raise ValueError("category_navigation_phase_readback_failed")
        return {**report, "write_requests": int(bool(requests)), "verified_links": len(selected),
                "ledger_and_inputs_unchanged": True, "phase": phase}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preview", "canary", "expand", "view-preview", "view-canary", "view-expand"), default="view-preview")
    parser.add_argument("--iphone-sheets-verified", action="store_true")
    args = parser.parse_args()
    from .production_flow import verify_execution_boundary
    from .google_clients import read_only_sheets_service, shipping_backfill_sheets_service
    from .sheets import SheetsDB
    verify_execution_boundary(dict(os.environ), os.environ.get("GITHUB_SHA", ""))
    service = read_only_sheets_service() if args.mode in ("preview", "view-preview") else shipping_backfill_sheets_service()
    try:
        db = SheetsDB(os.environ["SPREADSHEET_ID"], service=service)
        if args.mode.startswith("view-"):
            from .category_representative_view import ViewMaintenance
            report = ViewMaintenance(db).apply(args.mode[5:], iphone_sheets_verified=args.iphone_sheets_verified)
        else:
            report = NavigationMaintenance(db).apply(args.mode)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    except Exception as exc:
        # Never print Google error bodies, account data or IDs to Actions logs.
        code = str(exc) if isinstance(exc, ValueError) and str(exc).startswith("category_navigation_") else "category_navigation_failed"
        print(json.dumps({"status": "stopped", "reason": code}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
