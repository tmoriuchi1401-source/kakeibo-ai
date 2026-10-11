from copy import deepcopy
import pytest
import yaml
from pathlib import Path

from app.category_navigation import LEDGER, WORKFLOW, MARKER, HEADER, VERSION, sheet_url, source_rows
from app.category_navigation_maintenance import NavigationMaintenance
from tests.test_category_navigation import workflow


def entered(value):
    field = "boolValue" if isinstance(value, bool) else "numberValue" if isinstance(value, (int, float)) else "formulaValue" if str(value).startswith("=") else "stringValue"
    return {field: value}


class Rig(NavigationMaintenance):
    def __init__(self, *, corrupt=None):
        self.db = self
        self.sid = "synthetic"
        self.svc = self
        self.calls = 0
        self.corrupt = corrupt
        self.state = {"sheets": {
            LEDGER: {"properties": {"title": LEDGER, "sheetId": 0,
                       "gridProperties": {"rowCount": 20, "columnCount": 26}},
                     "protectedRanges": [{"range": {"sheetId": 0, "endColumnIndex": 13}}]},
            WORKFLOW: {"properties": {"title": WORKFLOW, "sheetId": 20,
                         "gridProperties": {"rowCount": 30, "columnCount": 27}}}}, "cells": {}}
        for title, rows in ((LEDGER, [["ID"] + ["header"] * 12,
                                ["EXP-1", "date", "shop", "item", 123] + ["accounting"] * 8,
                                ["EXP-2", "date", "shop", "item", 456] + ["accounting"] * 8,
                                ["OTHER", "date", "shop", "item", 789] + ["accounting"] * 8]),
                            (WORKFLOW, workflow())):
            self.state["cells"][title] = {(r, c): {"userEnteredValue": entered(v), "effectiveValue": entered(v)}
                for r, row in enumerate(rows) for c, v in enumerate(row) if v != ""}

    def snapshot(self): return deepcopy(self.state)
    def spreadsheets(self): return self
    def batchUpdate(self, **kw):
        self.requests = kw["body"]["requests"]
        return self
    def execute(self, **kw):
        self.calls += 1
        for request in self.requests:
            if "createDeveloperMetadata" in request:
                value = deepcopy(request["createDeveloperMetadata"]["developerMetadata"])
                value["metadataId"] = 77
                self.state["sheets"][LEDGER].setdefault("developerMetadata", []).append(value)
            elif "updateDeveloperMetadata" in request:
                self.state["sheets"][LEDGER]["developerMetadata"][0]["metadataValue"] = request["updateDeveloperMetadata"]["developerMetadata"]["metadataValue"]
            elif "updateCells" in request:
                body = request["updateCells"]
                start = body["start"]
                title = LEDGER if start["sheetId"] == 0 else WORKFLOW
                cell = self.state["cells"][title].setdefault((start["rowIndex"], start["columnIndex"]), {})
                change = body["rows"][0]["values"][0]
                if body["fields"] == "userEnteredValue":
                    cell.pop("userEnteredValue", None)
                cell.update(deepcopy(change))
        if self.corrupt == "ledger":
            self.state["cells"][LEDGER][1, 4]["userEnteredValue"] = entered(999)
        elif self.corrupt == "input":
            self.state["cells"][WORKFLOW][3, 5]["userEnteredValue"] = entered(False)
        elif self.corrupt == "protection":
            self.state["sheets"][LEDGER]["protectedRanges"] = []
        self.recalculate()
        return {}

    def recalculate(self):
        sources = source_rows(self.rows(self.state, WORKFLOW, 27, formulas=True))
        positions = {row[0]: i for i, row in enumerate(self.rows(self.state, LEDGER, 14)[1:], 2)}
        for title, cells in self.state["cells"].items():
            for (r, c), cell in cells.items():
                value = cell.get("userEnteredValue", {})
                if "formulaValue" not in value:
                    cell["effectiveValue"] = deepcopy(value)
                    continue
                if title == WORKFLOW and c == 0:
                    identity = next(i for row, i in sources if row == r+1)
                    url = sheet_url(self.sid, 0) + f"&range=A{positions[identity]}:N{positions[identity]}"
                    cell["effectiveValue"] = entered("代表取引を見る")
                else:
                    identity = cells[r, 0]["userEnteredValue"]["stringValue"]
                    matching = [row for row, i in sources if i == identity]
                    if not matching:
                        cell["effectiveValue"] = entered("")
                        cell.pop("hyperlink", None)
                        continue
                    url = sheet_url(self.sid, 20) + f"&range=A{matching[0] if len(matching)==1 else 1}"
                    cell["effectiveValue"] = entered("↩ カテゴリ操作に戻る")
                cell["hyperlink"] = url if self.corrupt != "hyperlink" else "https://invalid.example/"


def test_canary_readback_expand_and_replay_zero_writes():
    rig = Rig()
    baseline = rig.invariant(rig.snapshot())
    assert rig.apply("preview")["write_requests"] == 0 and rig.calls == 0
    canary = rig.apply("canary")
    assert canary["verified_links"] == 2 and canary["phase"] == "verified-v1"
    assert rig.invariant(rig.snapshot()) == baseline
    assert rig.apply("expand")["phase"] == VERSION
    calls = rig.calls
    assert rig.apply("expand")["write_requests"] == 0
    assert rig.calls == calls


def test_expand_requires_verified_canary_and_phase_is_not_normal_refresh():
    rig = Rig()
    with pytest.raises(ValueError, match="verified_canary_required"):
        rig.apply("expand")
    assert rig.calls == 0
    from app.category_navigation import installed
    rig.state["sheets"][LEDGER]["developerMetadata"] = [{"metadataKey": MARKER,
        "metadataValue": "canary-v1", "metadataId": 77}]
    assert not installed({"sheets": list(rig.state["sheets"].values())})


@pytest.mark.parametrize("corrupt,error", [("ledger", "unrelated_readback_changed"),
    ("input", "unrelated_readback_changed"), ("protection", "protection_changed"),
    ("hyperlink", "forward_target_mismatch")])
def test_mismatch_stops_and_never_records_verified_phase(corrupt, error):
    rig = Rig(corrupt=corrupt)
    with pytest.raises(ValueError, match=error):
        rig.apply("canary")
    assert rig.marker(rig.snapshot())["metadataValue"] == "canary-v1"
    assert rig.calls == 1


def test_foreign_n_constraints_and_duplicate_sources():
    rig = Rig()
    rig.state["cells"][LEDGER][0, 13] = {"note": "someone's note"}
    with pytest.raises(ValueError, match="column_has_constraints"):
        rig.apply("canary")
    assert rig.calls == 0
    rig.state["cells"][LEDGER].pop((0, 13))
    rig.state["cells"][WORKFLOW][4, 7] = {"userEnteredValue": entered("EXP-2"), "effectiveValue": entered("EXP-2")}
    assert rig.apply("canary")["ambiguous"] == 1
    assert rig.state["cells"][LEDGER][2, 13]["hyperlink"].endswith("&range=A1")


def test_workflow_has_same_lock_main_gate_and_only_sheet_credentials():
    data = yaml.safe_load(Path(".github/workflows/category-navigation.yml").read_text())
    assert data["concurrency"]["group"] == "kakeibo-production"
    assert "schedule" not in data.get("on", data.get(True))
    job = data["jobs"]["navigation"]
    assert "github.sha == vars.KAKEIBO_VALIDATED_MAIN_SHA" in job["if"]
    assert "github.ref == 'refs/heads/main'" in job["if"]
    assert set(job["env"]) == {"GITHUB_SHA", "KAKEIBO_PRODUCTION_ENABLED", "KAKEIBO_VALIDATED_MAIN_SHA",
                               "SPREADSHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON"}
    assert job["steps"][-1]["run"].startswith("python -m app.category_navigation_maintenance ")
