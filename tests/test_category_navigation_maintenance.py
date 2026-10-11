from copy import deepcopy
from collections import Counter
from pathlib import Path
import json
import pytest
import yaml

from app.category_navigation import (LEDGER, WORKFLOW, MARKER, HEADER, VERSION,
    INLINE_MARKER, CANARY_SCOPE, INLINE_VERSION, LEGACY_VIEW, RULE_MARKER, NEXT_MARKER,
    representative_summary_formula, representative_summary, return_formula, sheet_url, source_rows)
from app.category_navigation_maintenance import NavigationMaintenance


def entered(value):
    field = "boolValue" if isinstance(value, bool) else "numberValue" if isinstance(value, (int, float)) else "formulaValue" if str(value).startswith("=") else "stringValue"
    return {field: value}


class Call:
    def __init__(self, result): self.result=result
    def execute(self, **kw): return self.result


class Rig(NavigationMaintenance):
    def __init__(self, corrupt=None):
        self.db=self; self.sid="synthetic"; self.svc=self; self.calls=0; self.corrupt=corrupt
        self.state={"sheets": {}, "cells": {}, "dimensions": {WORKFLOW: {"rowMetadata": {}, "columnMetadata": {}}}}
        for title, sid in ((LEDGER,0),(WORKFLOW,20),(LEGACY_VIEW,99)):
            self.state["sheets"][title]={"properties": {"title": title, "sheetId": sid,
                "gridProperties": {"rowCount": 100, "columnCount": 27, "frozenRowCount":4 if sid==20 else 1}}}
        self.state["sheets"][LEDGER]["protectedRanges"]=[{"range":{"sheetId":0,"endColumnIndex":13}}]
        self.state["sheets"][LEDGER]["developerMetadata"]=[{"metadataKey":MARKER,"metadataValue":"verified-v1","metadataId":1}]
        self.state["sheets"][LEGACY_VIEW]["developerMetadata"]=[
            {"metadataKey":"kakeibo.category.representative.view","metadataValue":"verified-v2","metadataId":2},
            {"metadataKey":"kakeibo.category.representative.canary","metadataValue":json.dumps(["EXP-1","EXP-2","EXP-3"]),"metadataId":3}]
        self.state["sheets"][WORKFLOW]["merges"]=[{"sheetId":20,"startRowIndex":3,"endRowIndex":4,"startColumnIndex":0,"endColumnIndex":6},
            {"sheetId":20,"startRowIndex":4,"endRowIndex":5,"startColumnIndex":0,"endColumnIndex":3}]
        ledger=[["ID"]+["header"]*12+[HEADER]]
        for i, identity in enumerate(("EXP-1","EXP-2","EXP-3","EXP-4","OTHER")):
            ledger.append([identity,45977,"よこちゃん" if i==0 else "合成店舗", "自動計上",0 if i==0 else i*100,
                "その他","未分類","PayPay残高","PayPay","",f"import-{identity}","memo","active",
                return_formula(self.sid,20) if i<3 else ""])
        rows=[["入力内容を処理する",False,"処理結果を維持"],["処理結果","未送信16件"],["更新日時","保持"],
              ["チェック後に実行してください"],["設定先を開く →","","","対象月","2026-09"],[RULE_MARKER],["header"]]
        for i,identity in enumerate(("EXP-3","EXP-1","EXP-2","EXP-4")):
            rows.append([f'=HYPERLINK("#gid={99 if i<3 else 0}&range=A3","旧表示")',"承認待ち","食費","食料品","登録しない",True,f"key-{i}",identity,"ignored","ignored","service","approval"])
        rows += [[],[NEXT_MARKER],["header"]]
        for title, table in ((LEDGER,ledger),(WORKFLOW,rows)):
            self.state["cells"][title]={(r,c): {"userEnteredValue":entered(v),"effectiveValue":entered(v)}
                for r,row in enumerate(table) for c,v in enumerate(row) if v!=""}
        self.ref_response={"sheets":[]}
        self.recalculate()

    def snapshot(self): return deepcopy(self.state)
    def _invalidate_sheet_metadata(self): pass
    def _execute_sheet_read(self, fn): return fn().execute()
    def spreadsheets(self): return self
    def get(self, **kw): return Call(self.ref_response)
    def batchUpdate(self, **kw): self.requests=kw["body"]["requests"]; return self
    def title(self, sid): return next(t for t,s in self.state["sheets"].items() if s["properties"]["sheetId"]==sid)
    def execute(self, **kw):
        self.calls+=1
        for request in self.requests:
            if "updateCells" in request:
                body=request["updateCells"]; start=body["start"]; title=self.title(start["sheetId"])
                cell=self.state["cells"][title].setdefault((start["rowIndex"],start["columnIndex"]),{})
                value=body["rows"][0]["values"][0]
                for field in body["fields"].split(","):
                    target=cell; source=value; path=field.split(".")
                    for key in path[:-1]: target=target.setdefault(key,{}); source=source.get(key,{})
                    target.pop(path[-1],None)
                    if path[-1] in source: target[path[-1]]=deepcopy(source[path[-1]])
            elif "updateDimensionProperties" in request:
                b=request["updateDimensionProperties"]; area=b["range"]; title=self.title(area["sheetId"])
                key="rowMetadata" if area["dimension"]=="ROWS" else "columnMetadata"
                for i in range(area["startIndex"],area["endIndex"]): self.state["dimensions"][title][key][i]=b["properties"]["pixelSize"]
            elif "updateSheetProperties" in request:
                p=request["updateSheetProperties"]["properties"]
                self.state["sheets"][self.title(p["sheetId"])]["properties"]["gridProperties"].update(p["gridProperties"])
            elif "unmergeCells" in request:
                area=request["unmergeCells"]["range"]; self.state["sheets"][self.title(area["sheetId"])]["merges"].remove(area)
            elif "mergeCells" in request:
                area=request["mergeCells"]["range"]; self.state["sheets"][self.title(area["sheetId"])]["merges"].append(deepcopy(area))
            elif "createDeveloperMetadata" in request:
                m=deepcopy(request["createDeveloperMetadata"]["developerMetadata"]); m["metadataId"]=10+self.calls+len(self.state["sheets"][WORKFLOW].get("developerMetadata",[]))
                self.state["sheets"][self.title(m["location"]["sheetId"])].setdefault("developerMetadata",[]).append(m)
            elif "updateDeveloperMetadata" in request:
                b=request["updateDeveloperMetadata"]; mid=b["dataFilters"][0]["developerMetadataLookup"]["metadataId"]
                for s in self.state["sheets"].values():
                    for m in s.get("developerMetadata",[]):
                        if m["metadataId"]==mid: m.update(b["developerMetadata"])
            elif "deleteSheet" in request:
                title=self.title(request["deleteSheet"]["sheetId"]); self.state["sheets"].pop(title); self.state["cells"].pop(title,None)
        self.recalculate()
        if self.corrupt=="ledger": self.state["cells"][LEDGER][1,4]["userEnteredValue"]=entered(999)
        if self.corrupt=="input": self.state["cells"][WORKFLOW][7,5]["userEnteredValue"]=entered(False)
        if self.corrupt=="order": self.state["cells"][WORKFLOW][7,7],self.state["cells"][WORKFLOW][8,7]=self.state["cells"][WORKFLOW][8,7],self.state["cells"][WORKFLOW][7,7]
        if self.corrupt=="protection": self.state["sheets"][LEDGER]["protectedRanges"]=[]
        if self.corrupt=="value": self.state["cells"][WORKFLOW][7,0]["effectiveValue"]=entered("incorrect")
        if self.corrupt=="error": self.state["cells"][WORKFLOW][7,0]["effectiveValue"]={"errorValue":{"type":"ERROR"}}
        return {}

    def recalculate(self):
        for cells in self.state["cells"].values():
            for cell in cells.values():
                cell.pop("hyperlink",None)
                if "formulaValue" not in cell.get("userEnteredValue",{}): cell["effectiveValue"]=deepcopy(cell.get("userEnteredValue",{}))
        sources=source_rows(self.rows(self.state,WORKFLOW,27,formulas=True)); counts=Counter(i for _,i in sources)
        records={r[0]:r[:13] for r in self.rows(self.state,LEDGER,14)[1:] if r[0]}
        for row,identity in sources:
            cell=self.state["cells"][WORKFLOW][row-1,0]
            if cell["userEnteredValue"]=={"formulaValue":representative_summary_formula()}:
                cell["effectiveValue"]=entered(representative_summary(records[identity]))
            else: cell["hyperlink"]="https://old.example/"; cell["effectiveValue"]=entered("旧表示")
        for r,row in enumerate(self.rows(self.state,LEDGER,14,formulas=True)[1:],1):
            if not row[13]: continue
            cell=self.state["cells"][LEDGER][r,13]
            cell["effectiveValue"]=entered("↩ カテゴリ操作に戻る" if counts[row[0]] else "")
            if counts[row[0]]:
                target=next(n for n,i in sources if i==row[0]) if counts[row[0]]==1 else 1
                cell["hyperlink"]=sheet_url(self.sid,20)+f"&range=A{target}"


def test_canary_only_three_inline_and_inputs_order_instruction_preserved_then_replay_zero():
    rig=Rig(); before=rig.snapshot()
    assert rig.apply("preview")["target_rows"]==3 and rig.calls==0
    result=rig.apply("canary")
    assert result["id_matches"]==3 and result["formula_errors"]==0
    assert result["phase"]=="verified-inline-v3" and result["frozen_rows"]==4
    assert rig.state["cells"][WORKFLOW][3,1]["userEnteredValue"]==before["cells"][WORKFLOW][3,0]["userEnteredValue"]
    assert rig.state["cells"][LEDGER]==before["cells"][LEDGER]
    for key,cell in before["cells"][WORKFLOW].items():
        if key[1]>0: assert rig.state["cells"][WORKFLOW][key]==cell
    assert rig.state["cells"][WORKFLOW][10,0]==before["cells"][WORKFLOW][10,0]
    calls=rig.calls; assert rig.apply("canary")["write_requests"]==0 and rig.calls==calls


def test_full_rollout_phone_gate_all_ids_then_retire_and_zero_replay():
    rig=Rig()
    with pytest.raises(ValueError,match="iphone_verified_canary_required"): rig.apply("expand",iphone_sheets_verified=True)
    rig.apply("canary")
    with pytest.raises(ValueError,match="iphone_verified_canary_required"): rig.apply("expand")
    with pytest.raises(ValueError,match="full_inline_required"): rig.apply("retire")
    assert rig.apply("expand",iphone_sheets_verified=True)["id_matches"]==4
    assert rig.apply("retire")["legacy_sheet_deleted"]
    assert LEGACY_VIEW not in rig.state["sheets"]
    assert rig.apply("retire")["write_requests"]==0


def test_sort_and_regeneration_bind_h_identity_without_expanding_canary():
    rig=Rig(); rig.apply("canary")
    for c in range(27):
        a=rig.state["cells"][WORKFLOW].pop((7,c),None); b=rig.state["cells"][WORKFLOW].pop((9,c),None)
        if b: rig.state["cells"][WORKFLOW][7,c]=b
        if a: rig.state["cells"][WORKFLOW][9,c]=a
    rig.recalculate(); assert rig.apply("refresh")["id_matches"]==3
    rig.state["cells"][WORKFLOW][8,0]["userEnteredValue"]=entered("regenerated label")
    assert rig.apply("refresh")["target_rows"]==3
    assert rig.state["cells"][WORKFLOW][10,0]["effectiveValue"]["stringValue"]=="旧表示"


@pytest.mark.parametrize("corrupt,error",[("ledger","unrelated_readback_changed"),("input","unrelated_readback_changed"),
    ("order","unrelated_readback_changed"),("protection","protection_changed"),("value","summary_value_mismatch"),("error","formula_error")])
def test_stop_on_readback_corruption_without_verified_phase(corrupt,error):
    rig=Rig(corrupt)
    with pytest.raises(ValueError,match=error): rig.apply("canary")
    assert rig.phase(rig.snapshot())["metadataValue"]=="canary-inline-v3"


@pytest.mark.parametrize("ids",[("missing",),("EXP-1","EXP-1")])
def test_missing_duplicate_expense_identity_stops_before_write(ids):
    rig=Rig(); rig.state["cells"][LEDGER][1,0]["userEnteredValue"]=entered(ids[0])
    if len(ids)>1: rig.state["cells"][LEDGER][2,0]["userEnteredValue"]=entered(ids[1])
    with pytest.raises(ValueError,match="representative_id_mismatch"): rig.apply("canary")
    assert rig.calls==0


def test_retire_rejects_remaining_formula_chart_named_range_and_gid_links():
    rig=Rig(); rig.apply("canary"); rig.apply("expand",iphone_sheets_verified=True)
    for response in ({"namedRanges":[{"range":{"sheetId":99}}]},
        {"sheets":[{"data":[{"rowData":[{"values":[{"userEnteredValue":{"formulaValue":"='代表取引確認'!A3"}}]}]}]}]},
        {"sheets":[{"data":[{"rowData":[{"values":[{"hyperlink":"https://example.test/#gid=99&range=A3"}]}]}]}]}):
        rig.ref_response=response; calls=rig.calls
        with pytest.raises(ValueError,match="legacy_references_remain"): rig.apply("retire")
        assert LEGACY_VIEW in rig.state["sheets"] and rig.calls==calls


def test_workflow_has_same_lock_main_gate_and_only_sheet_credentials():
    data=yaml.safe_load(Path('.github/workflows/category-navigation.yml').read_text())
    assert data['concurrency']['group']=='kakeibo-production'
    assert 'schedule' not in data.get('on',data.get(True))
    job=data['jobs']['navigation']
    assert "github.sha == vars.KAKEIBO_VALIDATED_MAIN_SHA" in job['if']
    assert set(job['env'])=={'GITHUB_SHA','KAKEIBO_PRODUCTION_ENABLED','KAKEIBO_VALIDATED_MAIN_SHA','SPREADSHEET_ID','GOOGLE_SERVICE_ACCOUNT_JSON'}
    assert 'python -m app.category_navigation_maintenance --mode "$NAVIGATION_MODE"' in job['steps'][-1]['run']


def test_canary_repairs_backlink_after_ledger_a_to_m_sort_without_changing_ledger_data():
    rig=Rig(); rig.apply("canary")
    # Sorting A:M alone leaves N at its previous physical location.
    for c in range(13):
        a=rig.state["cells"][LEDGER].pop((1,c),None); b=rig.state["cells"][LEDGER].pop((4,c),None)
        if b: rig.state["cells"][LEDGER][1,c]=b
        if a: rig.state["cells"][LEDGER][4,c]=a
    rig.recalculate(); before=rig.snapshot()
    result=rig.apply("refresh")
    assert result["id_matches"]==3
    assert rig.state["cells"][LEDGER][4,13]["hyperlink"].endswith("&range=A9")
    assert all(rig.state["cells"][LEDGER][k]==v for k,v in before["cells"][LEDGER].items() if k[1]<13)
    assert rig.apply("refresh")["write_requests"]==0


def test_remaining_rich_text_reference_blocks_expansion_before_any_write():
    rig=Rig(); rig.apply("canary"); calls=rig.calls
    rig.ref_response={"sheets":[{"data":[{"rowData":[{"values":[{
        "textFormatRuns":[{"format":{"link":{"uri":"https://example.test/#gid=99"}}}]}]}]}]}]}
    with pytest.raises(ValueError,match="legacy_references_remain"):
        rig.apply("expand",iphone_sheets_verified=True)
    assert rig.calls==calls and rig.phase(rig.snapshot())["metadataValue"]=="verified-inline-v3"
