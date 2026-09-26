"""Small, value-only category choices shared by the existing input surfaces.

The version marker is installed only by the isolated migration. Normal writers
never convert a live input surface implicitly. The canonical F/G values remain
unchanged; category changes use the fixed-ID request path after cutover.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from .monthly_projection import ProjectionError


TITLE = "_支出明細カテゴリ候補"
MARKER = "kakeibo_expense_category_validation"
VERSION = "2:combined"
SEPARATOR = "｜"
HEADERS = ["カテゴリ（大｜小）", "カテゴリID", "大カテゴリ", "小カテゴリ"]


def compact_helper(meta):
    found = [s for s in meta.get("sheets", []) if s["properties"].get("title") == TITLE
             and any(m.get("metadataKey") == MARKER and m.get("metadataValue") == VERSION
                     for m in s.get("developerMetadata", []))]
    if len(found) > 1:
        raise ProjectionError("compact_category_helper_ambiguous")
    if found:
        grid = found[0]["properties"]["gridProperties"]
        if grid["columnCount"] != 4 or grid["rowCount"] < 2:
            raise ProjectionError("compact_category_helper_invalid")
    return found[0] if found else None


def category_rows(catalog):
    rows = [HEADERS]
    labels = set()
    for item in catalog.categories:
        if not item.active:
            continue
        if SEPARATOR in item.major or SEPARATOR in item.minor:
            raise ProjectionError("category_separator_in_name")
        label = item.major + SEPARATOR + item.minor
        if label in labels:
            raise ProjectionError("category_label_ambiguous")
        labels.add(label)
        rows.append([label, item.category_id, item.major, item.minor])
    if len(rows) == 1:
        raise ProjectionError("compact_category_choices_empty")
    return rows


def category_condition(helper):
    end = helper["properties"]["gridProperties"]["rowCount"]
    return {"type": "ONE_OF_RANGE", "values": [
        {"userEnteredValue": f"='{TITLE}'!$A$2:$A${end}"}]}


def controls(sheet_id, helper, start_row, count):
    if not count:
        return []
    base = {"sheetId": sheet_id, "startRowIndex": start_row - 1,
            "endRowIndex": start_row + count - 1}
    return [
        {"setDataValidation": {"range": {**base, "startColumnIndex": 2, "endColumnIndex": 3},
            "rule": {"condition": category_condition(helper), "strict": True, "showCustomUi": True,
                     "inputMessage": "大カテゴリと小カテゴリを一緒に選びます。"}}},
        {"setDataValidation": {"range": {**base, "startColumnIndex": 3, "endColumnIndex": 4}}},
        {"setDataValidation": {"range": {**base, "startColumnIndex": 4, "endColumnIndex": 6},
            "rule": {"condition": {"type": "BOOLEAN"}, "strict": True, "showCustomUi": True}}},
    ]


def logical_rule_row(physical):
    cells = list(physical) + [""] * max(0, 12 - len(physical))
    if SEPARATOR not in str(cells[2]) or cells[3] != "":
        # Nonempty retired D is a conflicting manual edit, not a second input.
        if cells[2] or cells[3]:
            raise ProjectionError("compact_category_input_invalid")
        return cells[:12]
    if str(cells[2]).count(SEPARATOR) != 1:
        raise ProjectionError("compact_category_input_invalid")
    cells[2:4] = str(cells[2]).split(SEPARATOR)
    return cells[:12]


def physical_rule_row(logical, *, header=False):
    cells = list(logical) + [""] * max(0, 12 - len(logical))
    if header:
        cells[2:4] = [HEADERS[0], ""]
    else:
        if any(SEPARATOR in str(x) for x in cells[2:4]):
            raise ProjectionError("category_separator_in_name")
        cells[2:4] = [(str(cells[2]) + SEPARATOR + str(cells[3]))
                      if cells[2] or cells[3] else "", ""]
    return cells[:12]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def _cells(sheet_id, first, left, values, width):
    def literal(value):
        if value == "" or value is None:
            return {}
        kind = "boolValue" if isinstance(value, bool) else "numberValue" if isinstance(value, (int, float)) else "stringValue"
        return {"userEnteredValue": {kind: value}}
    return {"updateCells": {"range": {"sheetId": sheet_id, "startRowIndex": first - 1,
        "endRowIndex": first + len(values) - 1, "startColumnIndex": left, "endColumnIndex": left + width},
        "rows": [{"values": [literal(x) for x in row]} for row in values], "fields": "userEnteredValue"}}


def migration_plan(snapshot, catalog):
    """One native atomic batch; caller must hold the existing writer lock.

    snapshot includes fresh metadata, all external helper references from the
    bounded audit, and complete workflow values. Unknown consumers stop the
    migration before the helper is cleared or shrunk. No ledger values are
    included in any request. A native backup is a separate prerequisite.
    """
    from .sheets import CATEGORY_WORKFLOW_MARKERS, CATEGORY_WORKFLOW_SHEET
    from .sheets_ui import EXPENSE_CATEGORY_HELPER_ID
    meta = snapshot["metadata"]
    by_title = {s["properties"]["title"]: s for s in meta["sheets"]}
    helper = by_title.get(TITLE)
    if not helper or helper["properties"]["sheetId"] != EXPENSE_CATEGORY_HELPER_ID or not any(
            m.get("metadataKey") == MARKER and m.get("metadataValue") in {"1", VERSION}
            for m in helper.get("developerMetadata", [])):
        raise ProjectionError("compact_category_helper_not_owned")
    if snapshot.get("metadata_references"):
        raise ProjectionError("compact_category_metadata_reference_requires_mapping")
    if not snapshot.get("audit_complete"):
        raise ProjectionError("compact_category_reference_audit_required")
    workflow = by_title.get(CATEGORY_WORKFLOW_SHEET)
    if workflow is None:
        raise ProjectionError("compact_category_workflow_missing")
    values = snapshot["workflow_values"]
    markers = [next((i for i, row in enumerate(values) if row and row[0] == text), None)
               for text in CATEGORY_WORKFLOW_MARKERS.values()]
    if (any(i is None for i in markers) or sorted(set(markers)) != markers or
            any(sum(bool(row) and row[0] == text for row in values) != 1
                for text in CATEGORY_WORKFLOW_MARKERS.values())):
        raise ProjectionError("compact_category_workflow_markers_invalid")
    first, end = markers[0] + 2, markers[1]
    rule_rows = {i for i in range(first, end) if any(str(x).strip() for x in values[i])}
    for ref in snapshot.get("references", []):
        title, row, col, kind = ref
        allowed = kind == "validation" and (
            (title == "支出明細" and row >= 1 and col in (5, 6)) or
            (title == CATEGORY_WORKFLOW_SHEET and row in rule_rows and col in (2, 3)) or
            (title == "要確認" and row >= 1 and col == 12))
        if not allowed:
            raise ProjectionError("compact_category_reference_requires_mapping")
    rows = category_rows(catalog)
    helper_id = helper["properties"]["sheetId"]
    new_helper = deepcopy(helper)
    new_helper["properties"]["gridProperties"] = {"rowCount": len(rows), "columnCount": 4}
    requests = []
    ledger_id = by_title["支出明細"]["properties"]["sheetId"]
    requests.append({"setDataValidation": {"range": {"sheetId": ledger_id,
        "startRowIndex": 1, "startColumnIndex": 5, "endColumnIndex": 7}}})
    workflow_id = workflow["properties"]["sheetId"]
    converted = []
    already = compact_helper(meta) is not None
    for i in range(first, end):
        row = list(values[i]) + [""] * max(0, 12 - len(values[i]))
        if already:
            logical_rule_row(row)  # Detect edits in the retired column.
            converted.append(row[2:4])
        else:
            converted.append(physical_rule_row(row)[2:4])
    requests.append(_cells(workflow_id, first, 2, [[HEADERS[0], ""]], 2))
    if converted:
        requests.append(_cells(workflow_id, first + 1, 2, converted, 2))
    # Clear only old C/D validations; inputs/checkboxes/snapshots are untouched.
    if end > first:
        requests.append({"setDataValidation": {"range": {"sheetId": workflow_id,
            "startRowIndex": first, "endRowIndex": end, "startColumnIndex": 2, "endColumnIndex": 4}}})
    runs = []
    for i in sorted(rule_rows):
        if runs and runs[-1][1] == i:
            runs[-1][1] = i + 1
        else:
            runs.append([i, i + 1])
    for start, stop in runs:
        requests.extend(controls(workflow_id, new_helper, start + 1, stop - start))
    review = by_title.get("要確認")
    if review:
        review_id = review["properties"]["sheetId"]
        requests.append({"setDataValidation": {"range": {"sheetId": review_id,
            "startRowIndex": 1, "startColumnIndex": 12, "endColumnIndex": 14}}})
        last = snapshot.get("review_last_row", 1)
        if last > 1:
            requests.append({"setDataValidation": {"range": {"sheetId": review_id,
                "startRowIndex": 1, "endRowIndex": last, "startColumnIndex": 12, "endColumnIndex": 13},
                "rule": {"condition": category_condition(new_helper), "strict": True, "showCustomUi": True}}})
    # Clear old spills before shrinking, then write literal stable-ID choices.
    requests.extend([
        {"updateCells": {"range": {"sheetId": helper_id}, "fields": "userEnteredValue,dataValidation"}},
        {"updateSheetProperties": {"properties": {"sheetId": helper_id, "hidden": True,
            "gridProperties": {"rowCount": len(rows), "columnCount": 4}},
            "fields": "hidden,gridProperties.rowCount,gridProperties.columnCount"}},
        _cells(helper_id, 1, 0, rows, 4),
        {"updateDeveloperMetadata": {"dataFilters": [{"developerMetadataLookup": {
            "metadataKey": MARKER, "metadataLocation": {"sheetId": helper_id}}}],
            "developerMetadata": {"metadataValue": VERSION}, "fields": "metadataValue"}},
    ])
    # Source ranges must exist before Google validates a growing choice list.
    helper_requests = requests[-4:]
    requests = helper_requests + requests[:-4]
    return {"snapshot_digest": digest(snapshot), "requests": requests,
            "helper_cells_before": helper["properties"]["gridProperties"]["rowCount"] *
                                   helper["properties"]["gridProperties"]["columnCount"],
            "helper_cells_after": len(rows) * 4}


class CompactCategoryMigration:
    """Explicit migration transport, run only inside the existing writer lock.

    All-cell auditing requests formulas and validation definitions only, never
    ledger, Medical or Payroll scalar values. Results contain counts only.
    No auth scopes, flags, backup files or credentials are created here.
    """

    def __init__(self, db):
        self.db = db
        self.read_requests = 0

    def _get(self, **kwargs):
        self.read_requests += 1
        return self.db._execute_sheet_read(lambda: self.db.svc.spreadsheets().get(
            spreadsheetId=self.db.sid, **kwargs))

    def _values(self, title, extent, right, *, page_size=1000):
        values = []
        quoted = "'" + title.replace("'", "''") + "'"
        for first in range(1, extent + 1, page_size):
            last = min(extent, first + page_size - 1)
            self.read_requests += 1
            page = self.db.get_raw(f"{quoted}!A{first}:{right}{last}")
            values.extend(page + [[] for _ in range(last - first + 1 - len(page))])
        while values and not values[-1]:
            values.pop()
        return values

    def snapshot(self):
        from .sheets import CATEGORY_WORKFLOW_SHEET
        # No includeGridData and no scalar values in the all-sheet audit.
        meta = self._get(includeGridData=False)
        helper = next((s for s in meta["sheets"] if s["properties"]["title"] == TITLE), None)
        if helper is None:
            raise ProjectionError("compact_category_helper_not_owned")
        helper_id = helper["properties"]["sheetId"]
        def references(value):
            if isinstance(value, str):
                return TITLE in value
            if isinstance(value, dict):
                return value.get("sheetId") == helper_id or any(references(v) for v in value.values())
            return isinstance(value, list) and any(references(v) for v in value)
        outside = {key: value for key, value in meta.items() if key != "sheets"}
        outside["sheets"] = [s for s in meta["sheets"] if s["properties"]["sheetId"] != helper_id]
        result = {"metadata": meta, "metadata_references": references(outside), "references": [],
                  "validation_sources": []}
        for sheet in meta["sheets"]:
            props = sheet["properties"]
            title = props["title"]
            if props["sheetId"] == helper_id:
                continue
            grid = props.get("gridProperties")
            if not grid:
                raise ProjectionError("compact_category_sheet_type_unsupported")
            rows, cols = grid["rowCount"], grid["columnCount"]
            # Audit every allocated row, including consumers beyond row 5,000.
            number, right = cols, ""
            while number:
                number, remainder = divmod(number - 1, 26)
                right = chr(65 + remainder) + right
            page_size = max(1, min(1000, 26000 // cols))
            quoted = "'" + title.replace("'", "''") + "'"
            for first in range(1, rows + 1, page_size):
                response = self._get(ranges=[f"{quoted}!A{first}:{right}{min(rows, first+page_size-1)}"],
                    fields="sheets(data(startRow,startColumn,rowData(values(userEnteredValue(formulaValue),dataValidation))))")
                for returned in response.get("sheets", []):
                    for data in returned.get("data", []):
                        for ri, row in enumerate(data.get("rowData", []), data.get("startRow", 0)):
                            for ci, cell in enumerate(row.get("values", []), data.get("startColumn", 0)):
                                if references(cell.get("userEnteredValue", {})):
                                    result["references"].append([title, ri, ci, "formula"])
                                if references(cell.get("dataValidation", {})):
                                    result["references"].append([title, ri, ci, "validation"])
                                    result["validation_sources"].append([title, ri, ci,
                                        cell["dataValidation"].get("condition")])
            if title == CATEGORY_WORKFLOW_SHEET:
                result["workflow_values"] = self._values(title, rows, "L")
            elif title == "要確認":
                result["review_last_row"] = len(self._values(title, rows, "A"))
        if compact_helper(meta):
            result["helper_values"] = self._values(TITLE, helper["properties"]["gridProperties"]["rowCount"], "D")
        result["audit_complete"] = True
        return result

    def plan(self, catalog):
        return migration_plan(self.snapshot(), catalog)

    def apply(self, plan, catalog):
        """Re-read before one atomic write, then verify values and all consumers.

        The caller owns the writer lock/frozen user-input window and native
        backup. Sheets has no cell compare-and-swap; a digest alone is not a
        substitute for that exclusion. Unknown write outcomes are not retried.
        """
        before = self.snapshot()
        fresh = migration_plan(before, catalog)
        if fresh != plan:
            raise ProjectionError("compact_category_snapshot_changed")
        self.db.svc.spreadsheets().batchUpdate(spreadsheetId=self.db.sid,
            body={"requests": plan["requests"]}).execute(num_retries=0)
        self.db._invalidate_sheet_metadata()
        after = self.snapshot()
        if not compact_helper(after["metadata"]) or after.get("helper_values") != category_rows(catalog):
            raise ProjectionError("compact_category_readback_failed")
        # Only C/D and their header change. Check every other input, fixed ID,
        # approval snapshot, and backfill/confirmation block after the write.
        from .sheets import CATEGORY_WORKFLOW_MARKERS
        expected = deepcopy(before["workflow_values"])
        marker = next(i for i, row in enumerate(expected) if row and row[0] == CATEGORY_WORKFLOW_MARKERS["rule"])
        end = next(i for i, row in enumerate(expected) if row and row[0] == CATEGORY_WORKFLOW_MARKERS["backfill"])
        expected[marker + 1] = physical_rule_row(expected[marker + 1], header=True)
        if not compact_helper(before["metadata"]):
            for i in range(marker + 2, end):
                expected[i] = physical_rule_row(expected[i])
        def normalized(values):
            result = []
            for row in values:
                row = list(row)
                while row and row[-1] == "":
                    row.pop()
                result.append(row)
            while result and not result[-1]:
                result.pop()
            return result
        if normalized(expected) != normalized(after["workflow_values"]):
            raise ProjectionError("compact_category_input_readback_changed")
        migration_plan(after, catalog)  # No unexpected surviving consumers.
        if any(ref[0] == "支出明細" for ref in after["references"]):
            raise ProjectionError("compact_category_ledger_validation_remaining")
        titles = {s["properties"]["sheetId"]: s["properties"]["title"] for s in after["metadata"]["sheets"]}
        expected_sources = []
        for request in plan["requests"]:
            body = request.get("setDataValidation", {})
            condition = body.get("rule", {}).get("condition", {})
            if condition.get("type") != "ONE_OF_RANGE":
                continue
            grid = body["range"]
            expected_sources.extend([titles[grid["sheetId"]], i, grid["startColumnIndex"], condition]
                                    for i in range(grid["startRowIndex"], grid["endRowIndex"]))
        if sorted(expected_sources, key=str) != sorted(after.get("validation_sources", []), key=str):
            raise ProjectionError("compact_category_validation_readback_failed")
        return {"category_helper_cells_before": plan["helper_cells_before"],
                "category_helper_cells_after": plan["helper_cells_after"],
                "category_migration_write_requests": 1,
                "category_migration_read_requests": self.read_requests}
