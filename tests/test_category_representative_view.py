from collections import Counter
from copy import deepcopy

import pytest

from app.category_navigation import LEDGER, WORKFLOW, representative_transaction_formula, sheet_url, source_rows
from app.category_representative_view import (VIEW, VIEW_MARKER, CANARY_MARKER, ORIGIN_LABEL,
    LIST_LABEL, VIEW_VERSION, ViewMaintenance, ledger_value_formula, view_return_formula)
from tests.test_category_navigation import workflow
from tests.test_category_navigation_maintenance import Rig, entered


class ViewRig(ViewMaintenance):
    def __init__(self, corrupt=None):
        legacy = Rig()
        rows = workflow(("EXP-2", "EXP-1", "EXP-3", "EXP-4"))
        legacy.state["cells"][WORKFLOW] = {(r, c): {"userEnteredValue": entered(v), "effectiveValue": entered(v)}
            for r, row in enumerate(rows) for c, v in enumerate(row) if v != ""}
        for r, identity in enumerate(("EXP-1", "EXP-2", "EXP-3", "EXP-4", "OTHER"), 1):
            values = [identity, 46296, "synthetic shop", "synthetic item", 0 if r == 1 else r*100,
                      "food", "groceries", "card", "synthetic", "", "synthetic memo", "", "active"]
            for c, value in enumerate(values):
                legacy.state["cells"][LEDGER][r, c] = {"userEnteredValue": entered(value), "effectiveValue": entered(value)}
        legacy.apply("canary")
        self.state = deepcopy(legacy.state)
        self.db = self
        self.sid = "synthetic"
        self.svc = self
        self.calls = 0
        self.corrupt = corrupt

    def snapshot(self): return deepcopy(self.state)
    def _invalidate_sheet_metadata(self): pass
    def _sheet_metadata(self): return {"sheets": list(self.state["sheets"].values())}
    def spreadsheets(self): return self
    def batchUpdate(self, **kw): self.requests = kw["body"]["requests"]; return self

    def execute(self, **kw):
        self.calls += 1
        for request in self.requests:
            if "addSheet" in request:
                props = deepcopy(request["addSheet"]["properties"])
                self.state["sheets"][props["title"]] = {"properties": props}
                self.state["cells"][props["title"]] = {}
            elif "createDeveloperMetadata" in request:
                marker = deepcopy(request["createDeveloperMetadata"]["developerMetadata"])
                marker["metadataId"] = 100+sum(len(s.get("developerMetadata", [])) for s in self.state["sheets"].values())
                title = self.title(marker["location"]["sheetId"])
                self.state["sheets"][title].setdefault("developerMetadata", []).append(marker)
            elif "updateDeveloperMetadata" in request:
                body = request["updateDeveloperMetadata"]
                mid = body["dataFilters"][0]["developerMetadataLookup"]["metadataId"]
                for sheet in self.state["sheets"].values():
                    for marker in sheet.get("developerMetadata", []):
                        if marker["metadataId"] == mid: marker.update(body["developerMetadata"])
            elif "updateCells" in request:
                body = request["updateCells"]; start = body["start"]
                cells = self.state["cells"][self.title(start["sheetId"])]
                for r, row in enumerate(body["rows"], start["rowIndex"]):
                    for c, change in enumerate(row["values"], start["columnIndex"]):
                        cell = cells.setdefault((r, c), {})
                        for field in body["fields"].split(","):
                            cell.pop(field, None)
                            if field in change: cell[field] = deepcopy(change[field])
            elif "updateDimensionProperties" in request:
                body = request["updateDimensionProperties"]; area = body["range"]
                key = "columnMetadata" if area["dimension"] == "COLUMNS" else "rowMetadata"
                dims = self.state.setdefault("dimensions", {}).setdefault(key, {})
                for i in range(area["startIndex"], area["endIndex"]): dims[i] = body["properties"]["pixelSize"]
            elif "updateSheetProperties" in request:
                props = request["updateSheetProperties"]["properties"]
                self.state["sheets"][self.title(props["sheetId"])]["properties"]["gridProperties"].update(props["gridProperties"])
            elif "appendDimension" in request:
                body = request["appendDimension"]
                self.state["sheets"][self.title(body["sheetId"])]["properties"]["gridProperties"]["rowCount"] += body["length"]
        self.recalculate()
        if self.corrupt == "ledger": self.state["cells"][LEDGER][1, 4]["userEnteredValue"] = entered(999)
        if self.corrupt == "input": self.state["cells"][WORKFLOW][3, 5]["userEnteredValue"] = entered(False)
        if self.corrupt == "freeze": self.state["sheets"][VIEW]["properties"]["gridProperties"]["frozenColumnCount"] = 0
        if self.corrupt == "hyperlink": self.state["cells"][VIEW][2, 0]["hyperlink"] = "https://invalid.example/"
        if self.corrupt == "value": self.state["cells"][VIEW][2, 4]["effectiveValue"] = entered(999)
        return {}

    def title(self, sid):
        return next(t for t, s in self.state["sheets"].items() if s["properties"]["sheetId"] == sid)

    def recalculate(self):
        for cells in self.state["cells"].values():
            for cell in cells.values():
                if "formulaValue" not in cell.get("userEnteredValue", {}):
                    cell["effectiveValue"] = deepcopy(cell.get("userEnteredValue", {}))
                    cell.pop("hyperlink", None)
        sources = source_rows(self.rows(self.state, WORKFLOW, 27, formulas=True))
        counts = Counter(i for _, i in sources)
        ledger = self.rows(self.state, LEDGER, 14, formulas=True)
        records = {r[0]: r[:13] for r in ledger[1:] if r[0]}
        positions = {r[0]: i for i, r in enumerate(ledger[1:], 2) if r[0]}
        viewer = self.state["cells"].get(VIEW, {})
        view_positions = {cell["userEnteredValue"]["stringValue"]: r+1
            for (r, c), cell in viewer.items() if c == 13 and r > 1 and cell.get("userEnteredValue")}
        for row, identity in sources:
            cell = self.state["cells"][WORKFLOW][row-1, 0]
            formula = cell.get("userEnteredValue", {}).get("formulaValue", "")
            if not formula: continue
            viewed = VIEW in formula and identity in view_positions
            sid = self.state["sheets"][VIEW]["properties"]["sheetId"] if viewed else 0
            area = f"A{view_positions[identity]}" if viewed else f"N{positions[identity]}"
            cell["hyperlink"] = sheet_url(self.sid, sid) + "&range=" + area
            cell["effectiveValue"] = entered("代表取引を見る")
        for r in range(1, len(ledger)):
            cell = self.state["cells"][LEDGER].get((r, 13), {})
            if "formulaValue" not in cell.get("userEnteredValue", {}): continue
            identity = ledger[r][0]
            target = next(n for n, i in sources if i == identity) if counts[identity] == 1 else 1
            cell["hyperlink"] = sheet_url(self.sid, 20) + f"&range=A{target}"
            cell["effectiveValue"] = entered("↩ カテゴリ操作に戻る")
        if not viewer: return
        viewer[0, 0]["hyperlink"] = sheet_url(self.sid, 20) + "&range=A1"
        viewer[0, 0]["effectiveValue"] = entered(LIST_LABEL)
        for identity, row in view_positions.items():
            r = row-1
            target = next(n for n, i in sources if i == identity) if counts[identity] == 1 else 1
            label = ORIGIN_LABEL if counts[identity] == 1 else LIST_LABEL + ("（複数候補）" if counts[identity] else "（対象なし）")
            viewer[r, 0]["hyperlink"] = sheet_url(self.sid, 20) + f"&range=A{target}"
            viewer[r, 0]["effectiveValue"] = entered(label)
            for c, value in enumerate(records[identity][1:], 1): viewer[r, c]["effectiveValue"] = entered(value)


def test_existing_three_canary_frozen_links_inputs_and_replay():
    rig = ViewRig()
    before = rig.snapshot()
    assert rig.apply("preview")["viewer_transactions"] == 3 and rig.calls == 0
    result = rig.apply("canary")
    assert result["verified_transactions"] == 3 and result["phase"] == "verified-v2"
    assert result["frozen_rows"] == 2 and result["frozen_columns"] == 1
    assert rig.state["cells"][LEDGER] == before["cells"][LEDGER]
    for (r, c), cell in before["cells"][WORKFLOW].items():
        if c != 0: assert rig.state["cells"][WORKFLOW][r, c] == cell
    calls = rig.calls
    assert rig.apply("canary")["write_requests"] == 0 and rig.calls == calls
    assert rig.state["cells"][VIEW][2, 4]["effectiveValue"] == entered(0)
    assert rig.state["cells"][VIEW][2, 9]["effectiveValue"] == entered("")


def test_expand_requires_phone_evidence_and_full_refresh_follows_sources():
    rig = ViewRig()
    with pytest.raises(ValueError, match="iphone_verified_canary_required"): rig.apply("expand", iphone_sheets_verified=True)
    rig.apply("canary")
    with pytest.raises(ValueError, match="iphone_verified_canary_required"): rig.apply("expand")
    result = rig.apply("expand", iphone_sheets_verified=True)
    assert result["phase"] == VIEW_VERSION and result["verified_transactions"] == 4
    assert result["safari_verification"] == "unverified"
    calls = rig.calls
    assert rig.apply("expand", iphone_sheets_verified=True)["write_requests"] == 0 and calls == rig.calls
    rig.state["cells"][WORKFLOW][3, 7]["userEnteredValue"] = entered("OTHER")
    rig.recalculate()
    result = rig.apply("refresh")
    assert result["verified_transactions"] == 4
    assert [r[13] for r in rig.rows(rig.snapshot(), VIEW, 14, formulas=True)[2:]] == ["EXP-1", "EXP-3", "EXP-4", "OTHER"]


def test_canary_refresh_restores_freeze_and_regeneration_without_expansion(monkeypatch):
    rig = ViewRig(); rig.apply("canary")
    rig.state["sheets"][VIEW]["properties"]["gridProperties"].update(frozenRowCount=0, frozenColumnCount=0)
    for row, _ in source_rows(rig.rows(rig.snapshot(), WORKFLOW, 27)):
        rig.state["cells"][WORKFLOW][row-1, 0] = {"userEnteredValue": entered("regenerated label"), "effectiveValue": entered("regenerated label")}
    import app.category_representative_view as view_module
    monkeypatch.setattr(view_module, "ViewMaintenance", lambda db: rig)
    from app.category_navigation import refresh_navigation
    result = refresh_navigation(rig)
    assert result["verified_transactions"] == 3
    assert rig.state["sheets"][VIEW]["properties"]["gridProperties"]["frozenColumnCount"] == 1
    assert "hyperlink" not in rig.state["cells"][WORKFLOW][6, 0]  # Fourth source stays outside canary.


def test_source_duplicate_deletion_and_view_sort_are_literal_and_safe():
    rig = ViewRig(); rig.apply("canary")
    rig.state["cells"][WORKFLOW][3, 7]["userEnteredValue"] = entered("EXP-1")
    rig.recalculate()
    assert rig.apply("refresh")["ambiguous"] == 1
    assert rig.state["cells"][VIEW][2, 0]["hyperlink"].endswith("&range=A1")
    assert rig.state["cells"][VIEW][2, 0]["effectiveValue"]["stringValue"] == LIST_LABEL + "（複数候補）"
    assert rig.state["cells"][VIEW][3, 0]["effectiveValue"]["stringValue"] == LIST_LABEL + "（対象なし）"
    # A remains fixed while B:N is sorted; identical formulas still bind to N/ROW.
    cells = rig.state["cells"][VIEW]
    for c in range(1, 14): cells[2, c], cells[4, c] = cells[4, c], cells[2, c]
    rig.recalculate()
    assert rig.apply("refresh")["write_requests"] == 0


def test_view_inserted_blank_row_and_deleted_row_do_not_capture_row_numbers():
    rig = ViewRig(); rig.apply("canary")
    cells = rig.state["cells"][VIEW]
    rig.state["cells"][VIEW] = {(r+1 if r >= 3 else r, c): v for (r, c), v in cells.items()}
    rig.recalculate()
    assert rig.apply("refresh")["verified_transactions"] == 3
    # A deleted derived row is restored from the fixed canary identity scope.
    rig.state["cells"][VIEW] = {(r, c): v for (r, c), v in rig.state["cells"][VIEW].items() if r != 3}
    assert rig.apply("refresh")["verified_transactions"] == 3
    assert {r[13] for r in rig.rows(rig.snapshot(), VIEW, 14, formulas=True)[2:]} == {"EXP-1", "EXP-2", "EXP-3"}


@pytest.mark.parametrize("corrupt,error", [("ledger", "unrelated_readback_changed"),
    ("input", "unrelated_readback_changed"), ("freeze", "view_readback_failed"),
    ("hyperlink", "origin_target_mismatch"), ("value", "transaction_mismatch")])
def test_native_readback_mismatch_stops_before_verified_phase(corrupt, error):
    rig = ViewRig(corrupt)
    with pytest.raises(ValueError, match=error): rig.apply("canary")
    assert next(m["metadataValue"] for m in rig.state["sheets"][VIEW]["developerMetadata"] if m["metadataKey"] == VIEW_MARKER) == "canary-v2"


def test_foreign_view_values_and_scope_stop_before_writes():
    rig = ViewRig(); rig.apply("canary")
    rig.state["cells"][VIEW][2, 2]["userEnteredValue"] = entered("manual input")
    calls = rig.calls
    with pytest.raises(ValueError, match="foreign_cell"): rig.apply("refresh")
    assert rig.calls == calls


def test_formula_balance_literal_ids_no_shared_last_return_state():
    formulas = [view_return_formula("synthetic", 20), ledger_value_formula("B"),
                representative_transaction_formula("synthetic", 0, 999, viewer_sheet_id=30)]
    for formula in formulas:
        depth = 0; quoted = False
        for char in formula:
            if char == '"': quoted = not quoted
            if not quoted and char == '(': depth += 1
            if not quoted and char == ')': depth -= 1
            assert depth >= 0
        assert depth == 0 and not quoted
        assert "COUNTIF" not in formula and "999" not in formula
    assert "INDEX($N:$N,ROW())" in formulas[0] and ORIGIN_LABEL in formulas[0] and LIST_LABEL in formulas[0]
    assert "MATCH(TRUE,ARRAYFORMULA('代表取引確認'!N:N=INDEX($H:$H,ROW())),0)" in formulas[2]


def test_native_color_rounding_and_computed_links_do_not_create_replay_writes():
    rig = ViewRig(); rig.apply("canary")
    for cells in rig.state["cells"].values():
        for cell in cells.values():
            fmt = cell.get("userEnteredFormat", {})
            color = fmt.get("textFormat", {}).get("foregroundColor", {})
            for channel, value in color.items(): color[channel] = round(value, 8)
            if cell.get("hyperlink"):
                fmt.setdefault("textFormat", {})["link"] = {"uri": cell["hyperlink"]}
                cell["userEnteredFormat"] = fmt
    # Actual Sheets readback mirrors computed HYPERLINK URIs into textFormat.
    # Sorting changes that cache while entered formulas and inputs stay intact.
    cells = rig.state["cells"][VIEW]
    for c in range(1, 14): cells[2, c], cells[4, c] = cells[4, c], cells[2, c]
    rig.recalculate()
    assert rig.apply("refresh")["write_requests"] == 0
