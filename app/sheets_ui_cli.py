"""python -m app.sheets_ui_cli: a read-only preview unless --apply is explicit.

Uses the existing service account connection. No ensure_schema or business
pipeline is run. Snapshot/restore contains UI state only and stays in .private.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from .sheets_ui import (
    CATEGORY_UI_COLUMNS, CATEGORY_UI_ID, CATEGORY_UI_MARKER, CATEGORY_UI_ROWS,
    CAP, CHART_ID, DAILY, EXPENSE_CATEGORY_HELPER_ID, EXPENSE_CATEGORY_HELPER_MARKER,
    FORMAT_KEYS, FORMAT_MASK, HOME_COLUMNS, HOME_ID, IDS, MARKER, TEXT_KEYS, VERSION,
    SPREADSHEET_ID, build_plan, dimension, grid, plan_digest,
)


def read_metadata(service):
    meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    grid_sheets = [s for s in meta["sheets"] if s["properties"].get("gridProperties")]
    ranges = [f"'{s['properties']['title'].replace(chr(39), chr(39)*2)}'!A1:{chr(64+min(26,s['properties']['gridProperties']['columnCount']))}1"
              for s in grid_sheets]
    result = service.spreadsheets().values().batchGet(
        spreadsheetId=SPREADSHEET_ID, ranges=ranges, valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    if len(result.get("valueRanges", [])) != len(grid_sheets):
        raise ValueError("UI header read incomplete")
    for sheet, values in zip(grid_sheets, result["valueRanges"]):
        sheet["header"] = (values.get("values") or [[]])[0]
    ledger = next((s for s in grid_sheets if s['properties']['title'] == '支出明細'), None)
    if ledger:
        last = ledger['properties']['gridProperties']['rowCount']
        used = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID,
            range=f"'支出明細'!A1:A{last}", valueRenderOption='UNFORMATTED_VALUE').execute().get('values', [])
        ledger['usedRowCount'] = len(used)
    home = next((s for s in grid_sheets if s["properties"]["sheetId"] == HOME_ID), None)
    if home:
        inputs = service.spreadsheets().values().batchGet(
            spreadsheetId=SPREADSHEET_ID, ranges=["'ホーム'!B3:B4"], valueRenderOption="FORMULA",
        ).execute().get("valueRanges", [])
        if len(inputs) != 1:
            raise ValueError("Home month selection read incomplete")
        rows = inputs[0].get("values", [])
        home["monthState"] = {f"B{i+3}": rows[i][0] if i < len(rows) and rows[i] else "" for i in range(2)}
    return meta


def private_path(path):
    root = Path(__file__).resolve().parents[1] / ".private"
    result = Path(path).resolve()
    if not result.is_relative_to(root.resolve()) or result == root.resolve():
        raise ValueError("UI state must be saved under this checkout's ignored .private directory")
    return result


def write_private(path, value):
    target = private_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create: a rerun must not destroy the pre-apply backup.
    with target.open("x", encoding="utf-8") as out:
        json.dump(value, out, ensure_ascii=False, separators=(",", ":"))


def execute_backup_read(request):
    """Read a UI backup without gzip amplification on repetitive grid data."""
    # A blank but formatted ledger compresses extremely well.  Google serves
    # that response with a ratio above httplib2's safety ceiling, even though
    # the request itself is small.  Request an identity response for backups
    # only; this does not change spreadsheet state or normal API requests.
    headers = getattr(request, "headers", None)
    if headers is not None:
        headers["accept-encoding"] = "identity"
    return request.execute()


def capture_restore(service, meta, plan):
    """Capture only fields this presentation changes, not business cell values."""
    restore = []
    for s in sorted(meta["sheets"], key=lambda s: s["properties"]["index"], reverse=True):
        p = s["properties"]
        if p["sheetId"] not in set(IDS.values()) | {HOME_ID, CATEGORY_UI_ID, EXPENSE_CATEGORY_HELPER_ID}:
            continue
        restore.append({"updateSheetProperties": {"properties": {
            "sheetId": p["sheetId"], "index": 0, "hidden": p.get("hidden", False)},
            "fields": "index,hidden"}})
    targets = [s for s in meta["sheets"] if s["properties"]["title"] in DAILY + ["支出明細"] and
               s["properties"]["title"] + " formatting: header differs" not in plan.get("skipped", [])]
    for s in targets:
        p = s["properties"]
        title, sid, n = p["title"], p["sheetId"], min(p["gridProperties"]["rowCount"], CAP+1)
        if sid != IDS[title]:
            continue
        width = {"支出一覧": 10, "要確認": 22, "Amazon要確認": 14, "支出明細": 7}[title]
        first_col = 5 if title == "支出明細" else 0
        raw = execute_backup_read(service.spreadsheets().get(
            spreadsheetId=SPREADSHEET_ID, ranges=[f"'{title}'!A1:{chr(64+width)}{n}"],
            fields="sheets(properties(sheetId),data(startRow,startColumn,rowData(values(userEnteredFormat,dataValidation)),rowMetadata(pixelSize),columnMetadata(pixelSize,hiddenByUser)))",
        ))["sheets"][0]
        formats, row_sizes, column_sizes = {}, {}, {}
        for block in raw.get("data", []):
            r0, c0 = block.get("startRow", 0), block.get("startColumn", 0)
            for i, row in enumerate(block.get("rowData", []), r0):
                for j, c in enumerate(row.get("values", []), c0):
                    fmt = c.get("userEnteredFormat", {})
                    formats[i, j] = {k: fmt[k] for k in FORMAT_KEYS if k in fmt}
                    if "textFormat" in formats[i, j]:
                        formats[i, j]["textFormat"] = {k: fmt["textFormat"][k] for k in TEXT_KEYS if k in fmt["textFormat"]}
            row_sizes.update({i: v.get("pixelSize", 21) for i, v in enumerate(block.get("rowMetadata", []), r0)})
            column_sizes.update({i: v for i, v in enumerate(block.get("columnMetadata", []), c0)})
        # Consecutive identical formatting is restored as one request per run.
        for col in range(first_col, width):
            start, prior = 0, formats.get((0, col), {})
            for row in range(1, n+1):
                current = formats.get((row, col), {}) if row < n else None
                if current != prior:
                    restore.append({"repeatCell": {"range": grid(sid, start, row, col, col+1),
                                    "cell": {"userEnteredFormat": prior}, "fields": FORMAT_MASK}})
                    start, prior = row, current
        start, prior = 0, row_sizes.get(0, 21)
        for row in range(1, n+1):
            current = row_sizes.get(row, 21) if row < n else None
            if current != prior:
                restore.append(dimension(sid, "ROWS", start, row, pixelSize=prior))
                start, prior = row, current
        for col in range(first_col, width):
            props = {"pixelSize": column_sizes.get(col, {}).get("pixelSize", 100)}
            if title == "支出一覧" and col == 9:
                props["hiddenByUser"] = column_sizes.get(col, {}).get("hiddenByUser", False)
            restore.append(dimension(sid, "COLUMNS", col, col+1, **props))
        restore.append({"updateSheetProperties": {"properties": {"sheetId": sid,
            "gridProperties": {"frozenRowCount": p["gridProperties"].get("frozenRowCount", 0)}},
            "fields": "gridProperties.frozenRowCount"}})
        if title == "支出明細":
            observed = raw.get("data", [{}])[0].get("rowData", [])
            # The install plan may grow the ledger. Clear the
            # rules in its newly-created blank rows too; otherwise a restore
            # would leave category validation behind beyond the original grid.
            validation_n = max([n] + [r['updateSheetProperties']['properties'].get('gridProperties', {}).get('rowCount', n)
                for r in plan['requests'] if r.get('updateSheetProperties', {}).get('properties', {}).get('sheetId') == sid])
            validation_rows = []
            for row in range(validation_n):
                cells = observed[row].get("values", []) if row < len(observed) else []
                validation_rows.append({"values": [
                    ({"dataValidation": cells[col]["dataValidation"]}
                     if col < len(cells) and "dataValidation" in cells[col] else {})
                    for col in (5, 6)
                ]})
            # Only restore the prior rules.  Values, formulas, notes, and
            # formats in the editable ledger are deliberately untouched.
            restore.append({"updateCells": {"range": grid(sid, 0, validation_n, 5, 7),
                "rows": validation_rows, "fields": "dataValidation"}})
    # A new Home is hidden, never deleted. Keep ownership while disabling hooks.
    if not any(s["properties"]["sheetId"] == HOME_ID for s in meta["sheets"]):
        restore += [{"updateSheetProperties": {"properties": {"sheetId": HOME_ID,
                    "hidden": True, "index": len(meta["sheets"])}, "fields": "hidden,index"}},
                    {"updateDeveloperMetadata": {"dataFilters": [{"developerMetadataLookup": {
                        "metadataKey": MARKER, "metadataLocation": {"sheetId": HOME_ID}}}],
                        "developerMetadata": {"metadataValue": "restored:" + VERSION}, "fields": "metadataValue"}}]
    else:
        # For later runs, capture the existing home including its month, chart,
        # merges, and formatting. It is the sole UI-owned content surface.
        old_home = next(s for s in meta["sheets"] if s["properties"]["sheetId"] == HOME_ID)
        width = min(HOME_COLUMNS, old_home["properties"]["gridProperties"]["columnCount"])
        home = execute_backup_read(service.spreadsheets().get(
            spreadsheetId=SPREADSHEET_ID, ranges=[f"'ホーム'!A1:{chr(64+width)}{CAP+1}"],
            fields="sheets(properties,merges,charts,data(startRow,startColumn,rowData(values(userEnteredValue,userEnteredFormat,dataValidation,note)),rowMetadata(pixelSize),columnMetadata(pixelSize,hiddenByUser)))",
        ))["sheets"][0]
        restore.append({"unmergeCells": {"range": grid(HOME_ID, 0, CAP+1, 0, HOME_COLUMNS)}})
        home_data = home.get("data", [{}])[0]
        source_rows = home_data.get("rowData", [])
        rows = [{"values": [{k: c[k] for k in ("userEnteredValue", "userEnteredFormat", "dataValidation", "note") if k in c}
                            for c in (source_rows[i].get("values", []) if i < len(source_rows) else [])]}
                for i in range(CAP+1)]
        for row in rows:
            row["values"] += [{} for _ in range(HOME_COLUMNS-len(row["values"]))]
        restore.append({"updateCells": {"range": grid(HOME_ID, 0, CAP+1, 0, HOME_COLUMNS),
            "rows": rows,
            "fields": "userEnteredValue,userEnteredFormat,dataValidation,note"}})
        for i in range(HOME_COLUMNS):
            old = home_data.get("columnMetadata", [])
            prior = old[i] if i < len(old) else {}
            restore.append(dimension(HOME_ID, "COLUMNS", i, i+1,
                pixelSize=prior.get("pixelSize", 100), hiddenByUser=prior.get("hiddenByUser", i >= width)))
        for i in range(70):
            old = home_data.get("rowMetadata", [])
            prior = old[i] if i < len(old) else {}
            restore.append(dimension(HOME_ID, "ROWS", i, i+1, pixelSize=prior.get("pixelSize", 21)))
        restore += [{"mergeCells": {"range": r, "mergeType": "MERGE_ALL"}} for r in home.get("merges", [])]
        restore.append({"updateSheetProperties": {"properties": {"sheetId": HOME_ID,
            "gridProperties": {"frozenRowCount": old_home["properties"]["gridProperties"].get("frozenRowCount", 0)}},
            "fields": "gridProperties.frozenRowCount"}})
        for chart in home.get("charts", []):
            if chart["chartId"] == CHART_ID:
                restore += [{"updateChartSpec": {"chartId": CHART_ID, "spec": chart["spec"]}},
                            {"updateEmbeddedObjectPosition": {"objectId": CHART_ID,
                                "newPosition": chart["position"],
                                "fields": "anchorCell,offsetXPixels,offsetYPixels,widthPixels,heightPixels"}}]
        if not any(c["chartId"] == CHART_ID for c in home.get("charts", [])):
            restore.append({"deleteEmbeddedObject": {"objectId": CHART_ID}})
    category_ui = next((s for s in meta["sheets"] if s["properties"]["sheetId"] == CATEGORY_UI_ID), None)
    if category_ui is None:
        restore += [{"updateSheetProperties": {"properties": {"sheetId": CATEGORY_UI_ID,
            "hidden": True, "index": len(meta["sheets"])+(0 if any(s["properties"]["sheetId"] == HOME_ID for s in meta["sheets"]) else 1)},
            "fields": "hidden,index"}},
            {"updateDeveloperMetadata": {"dataFilters": [{"developerMetadataLookup": {
                "metadataKey": CATEGORY_UI_MARKER, "metadataLocation": {"sheetId": CATEGORY_UI_ID}}}],
                "developerMetadata": {"metadataValue": "restored:"+VERSION}, "fields": "metadataValue"}}]
    else:
        restore.extend(capture_category_ui_restore(service, category_ui))
    expense_helper = next((s for s in meta["sheets"]
                           if s["properties"]["sheetId"] == EXPENSE_CATEGORY_HELPER_ID), None)
    if expense_helper is None:
        restore += [{"updateSheetProperties": {"properties": {"sheetId": EXPENSE_CATEGORY_HELPER_ID,
            "hidden": True, "index": len(meta["sheets"])+(0 if any(
                s["properties"]["sheetId"] == HOME_ID for s in meta["sheets"]) else 1)},
            "fields": "hidden,index"}},
            {"updateDeveloperMetadata": {"dataFilters": [{"developerMetadataLookup": {
                "metadataKey": EXPENSE_CATEGORY_HELPER_MARKER,
                "metadataLocation": {"sheetId": EXPENSE_CATEGORY_HELPER_ID}}}],
                "developerMetadata": {"metadataValue": "restored:" + VERSION}, "fields": "metadataValue"}}]
    return {"spreadsheetId": SPREADSHEET_ID, "created_at": datetime.now(timezone.utc).isoformat(),
            "applied_plan_sha256": plan_digest(plan), "requests": restore,
            "sheetIds": {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]},
            "headers": {s["properties"]["title"]: s.get("header", []) for s in meta["sheets"]}}


def capture_category_ui_restore(service, sheet):
    """Snapshot the read-only UI projection; business rows are never copied here."""
    sid, n, width = CATEGORY_UI_ID, CATEGORY_UI_ROWS, CATEGORY_UI_COLUMNS
    raw = execute_backup_read(service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID,
        ranges=[f"'カテゴリ対応'!A1:Q{n}"],
        fields="sheets(merges,data(rowData(values(userEnteredValue,userEnteredFormat,dataValidation,note)),rowMetadata(pixelSize),columnMetadata(pixelSize,hiddenByUser)))",
    ))["sheets"][0]
    data = raw.get("data", [{}])[0]
    observed = data.get("rowData", [])
    keys = ("userEnteredValue", "userEnteredFormat", "dataValidation", "note")
    rows = [{"values": [{k: c[k] for k in keys if k in c}
        for c in observed[i].get("values", [])[:width]] if i < len(observed) else []} for i in range(n)]
    for row in rows: row["values"] += [{} for _ in range(width-len(row["values"]))]
    req = [{"unmergeCells": {"range": grid(sid, 0, n, 0, width)}},
        {"updateCells": {"range": grid(sid, 0, n, 0, width), "rows": rows, "fields": ",".join(keys)}}]
    for i in range(width):
        prior = (data.get("columnMetadata", []) + [{}]*width)[i]
        req.append(dimension(sid, "COLUMNS", i, i+1, pixelSize=prior.get("pixelSize", 100), hiddenByUser=prior.get("hiddenByUser", False)))
    sizes = [r.get("pixelSize", 21) for r in data.get("rowMetadata", [])]
    sizes += [21]*(n-len(sizes))
    start = 0
    for i in range(1, n+1):
        if i == n or sizes[i] != sizes[start]:
            req.append(dimension(sid, "ROWS", start, i, pixelSize=sizes[start]))
            start = i
    req.extend({"mergeCells": {"range": r, "mergeType": "MERGE_ALL"}} for r in raw.get("merges", []))
    gp = sheet["properties"]["gridProperties"]
    req.append({"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {
        "frozenRowCount": gp.get("frozenRowCount", 0), "hideGridlines": gp.get("hideGridlines", False)}},
        "fields": "gridProperties.frozenRowCount,gridProperties.hideGridlines"}})
    return req


def execute_plan(service, plan, *, apply=False, approved_digest=None, backup=None):
    if not apply:
        return {"mode": "dry-run", "request_count": len(plan["requests"]), "sha256": plan_digest(plan)}
    if approved_digest != plan_digest(plan) or not backup:
        raise ValueError("Apply requires the reviewed plan digest and a new private backup path")
    current = read_metadata(service)
    fresh = build_plan(current)
    if plan_digest(fresh) != approved_digest:
        raise ValueError("UI state changed; generate and review a fresh preview")
    saved = capture_restore(service, current, fresh)
    write_private(backup, saved)
    # Capture may take time; do not write if another Work changed the layout/schema.
    if plan_digest(build_plan(read_metadata(service))) != approved_digest:
        raise ValueError("UI state changed while capturing backup; no write performed")
    service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID,
        body={"requests": fresh["requests"]}).execute(num_retries=0)
    after = read_metadata(service)
    for s in current["sheets"]:
        new = next((v for v in after["sheets"] if v["properties"]["sheetId"] == s["properties"]["sheetId"]), None)
        if new is None or new["properties"]["title"] != s["properties"]["title"] or (
                s["properties"]["sheetId"] not in {HOME_ID, CATEGORY_UI_ID, EXPENSE_CATEGORY_HELPER_ID} and new["header"] != s["header"]):
            raise ValueError("UI readback requires investigation; do not rerun business pipelines")
    from .sheets_ui_verify import verify_home
    return {"mode": "applied", "request_count": len(fresh["requests"]),
            "headers_unchanged": True, "backup": str(backup), "verification": verify_home(service, after),
            "native_visual_verification_required": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observed-metadata", help="Offline connector metadata; preview only")
    parser.add_argument("--output", help="Write the exact plan under .private (exclusive create)")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approve-plan", help="SHA-256 of the concrete reviewed plan")
    parser.add_argument("--backup", help="New .private UI snapshot path required with --apply")
    parser.add_argument("--restore", help="Private pre-apply snapshot; preview by default")
    parser.add_argument("--verify", action="store_true", help="Read-only native formula/arithmetic verification")
    args = parser.parse_args(argv)
    if args.observed_metadata and (args.apply or args.restore):
        parser.error("offline metadata is preview only")
    if args.verify and (args.apply or args.restore or args.observed_metadata):
        parser.error("--verify is a separate live read-only operation")
    service = None
    if args.observed_metadata:
        meta = json.loads(private_path(args.observed_metadata).read_text(encoding="utf-8"))
    else:
        from .google_clients import read_only_sheets_service, sheets_service
        service = sheets_service() if args.apply else read_only_sheets_service()
        meta = read_metadata(service)
    if args.verify:
        from .sheets_ui_verify import verify_home
        print(json.dumps(verify_home(service, meta)))
        return
    if args.restore:
        from .compact_categories import compact_helper
        if compact_helper(meta):
            raise ValueError("Legacy UI restore cannot overwrite compact categories; use the isolated native backup")
        plan = json.loads(private_path(args.restore).read_text(encoding="utf-8"))
        if plan.get("spreadsheetId") != SPREADSHEET_ID:
            raise ValueError("Restore target mismatch")
        for title, header in plan["headers"].items():
            live = next((s for s in meta["sheets"] if s["properties"]["title"] == title), None)
            if live is None or live["properties"]["sheetId"] != plan["sheetIds"][title] or (
                live["properties"]["sheetId"] not in {HOME_ID, CATEGORY_UI_ID, EXPENSE_CATEGORY_HELPER_ID} and live["header"] != header):
                raise ValueError("Restore schema changed; inspect before restoring UI")
        if args.apply:
            if args.approve_plan != plan_digest(plan):
                raise ValueError("Restore requires the reviewed restore-plan digest")
            service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID,
                body={"requests": plan["requests"]}).execute(num_retries=0)
        result = {"mode": "restored" if args.apply else "restore-preview", "sha256": plan_digest(plan),
                  "request_count": len(plan["requests"])}
    else:
        plan = build_plan(meta)
        result = execute_plan(service, plan, apply=args.apply, approved_digest=args.approve_plan, backup=args.backup)
        result.update({k: plan[k] for k in ("visible_order", "hide", "skipped", "source_row_limit")})
    if args.output:
        write_private(args.output, plan)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
