from copy import deepcopy
from dataclasses import replace
import re

import pytest

from app.compact_categories import TITLE, category_rows, migration_plan
from app.compact_category_sync import sync_choices, backfill_month_choices
from app.monthly_projection import Category, CategoryCatalog, ProjectionError
from app.projection_refresh import catalog_document, load_catalog
from test_compact_categories import snapshot, catalog, apply_requests
from test_projection_refresh import initialized, PAIRS, row


class Call:
    def __init__(self,fn):self.fn=fn
    def execute(self,**kw):return self.fn()


class DB:
    sid="synthetic"
    def __init__(self):
        self.state=apply_requests(snapshot(),migration_plan(snapshot(),catalog())["requests"])
        self.svc=self;self.writes=[];self.reads=[];self.fail=False;self.corrupt=False
        self.state["metadata"]["sheets"][2]["properties"]["gridProperties"]["rowCount"]=2501
        rule=self.state["validation_sources"][0][-1]
        self.state["validation_sources"].append(["カテゴリ操作",2100,2,deepcopy(rule)])
    def spreadsheets(self):return self
    def _execute_sheet_read(self,fn):return fn().execute()
    def _invalidate_sheet_metadata(self):pass
    def get(self,**kw):
        self.reads.append(kw)
        if "ranges" not in kw:return Call(lambda:deepcopy(self.state["metadata"]))
        assert kw["fields"]=="sheets(data(startRow,rowData(values(dataValidation))))"
        title,col,first,last=re.fullmatch(r"'([^']+)'!([A-Z]+)(\d+):[A-Z]+(\d+)",kw["ranges"][0]).groups()
        first,last=int(first)-1,int(last);index=ord(col)-65
        data=[{"startRow":r,"rowData":[{"values":[{"dataValidation":{"condition":deepcopy(condition),"strict":True,"showCustomUi":True}}]}]}
              for t,r,c,condition in self.state["validation_sources"] if t==title and c==index and first<=r<last]
        return Call(lambda:{"sheets":[{"data":data}]})
    def get_raw(self,rng):
        assert rng.startswith(f"'{TITLE}'!")
        first,last=map(int,re.search(r"A(\d+):D(\d+)",rng).groups())
        return deepcopy(self.state["helper_values"][first-1:last])
    def batchUpdate(self,**kw):
        def apply():
            requests=kw["body"]["requests"];self.writes.append(deepcopy(requests))
            for request in requests:
                if "appendDimension" in request:
                    b=request["appendDimension"]
                    sheet=next(s for s in self.state["metadata"]["sheets"] if s["properties"]["sheetId"]==b["sheetId"])
                    sheet["properties"]["gridProperties"]["rowCount"]+=b["length"]
            self.state=apply_requests(self.state,requests)
            if self.corrupt:self.state["helper_values"][1][0]="changed"
            if self.fail:self.fail=False;raise RuntimeError("response_unknown")
            return {}
        return Call(apply)


def test_sync_growth_updates_all_consumers_preserves_inputs_and_replay_writes_zero():
    db=DB();original=deepcopy(db.state["workflow_values"])
    new=CategoryCatalog([*catalog().categories,Category("CAT-new","日用品","消耗品")])
    assert sync_choices(db,new)["category_choice_writes"]==1
    assert db.state["helper_values"]==category_rows(new)
    assert db.state["workflow_values"]==original
    assert all(ref[-1]["values"][0]["userEnteredValue"].endswith("$A$4") for ref in db.state["validation_sources"])
    assert any("2100" not in q.get("ranges",[""])[0] and "2002" in q.get("ranges",[""])[0] for q in db.reads)
    writes=len(db.writes);assert sync_choices(db,new)=={"category_choice_writes":0} and len(db.writes)==writes
    for request in db.writes[0]:
        if "updateCells" in request:assert request["updateCells"]["range"]["sheetId"]==db.state["metadata"]["sheets"][0]["properties"]["sheetId"]


def test_rename_keeps_ids_old_labels_remain_resolvable_and_retirement_clears_tail():
    db=DB();renamed=catalog().rename("CAT-food","食費","食品")
    sync_choices(db,renamed)
    assert db.state["helper_values"][1][1]=="CAT-food"
    assert renamed.resolve("食費","食料品")==renamed.resolve("食費","食品")
    retired=CategoryCatalog(replace(c,active=False) if c.category_id=="CAT-meal" else c for c in renamed.categories)
    sync_choices(db,retired)
    assert db.state["helper_values"][-1]==[""]*4
    assert db.state["metadata"]["sheets"][0]["properties"]["gridProperties"]["rowCount"]==3


def test_unknown_sync_write_is_not_retried_and_next_read_observes_completed_batch():
    db=DB();db.fail=True;renamed=catalog().rename("CAT-food","食費","食品")
    with pytest.raises(RuntimeError):sync_choices(db,renamed)
    assert len(db.writes)==1
    assert sync_choices(db,renamed)=={"category_choice_writes":0}
    db.corrupt=True
    with pytest.raises(ProjectionError,match="readback"):sync_choices(db,catalog())
    assert len(db.writes)==2


def test_master_only_change_updates_catalog_without_reading_ledger_index_or_totals():
    store,reader,refresh=initialized([row("historical","2010-01-01")])
    old=load_catalog(store.data["catalog"]);identity=old.resolve(*PAIRS[0])
    original=deepcopy(store.data["month-2010-01"])
    # An explicit rename preserves identity; a new pair is an addition, never
    # an inferred rename or a rewrite of historical category strings.
    store.data["catalog"]=catalog_document(old.rename(identity,"食費","食品"))
    pairs=[("食費","食品"),("日用品","消耗品")]
    refresh.refresh(pairs)
    current=load_catalog(store.data["catalog"])
    assert current.resolve(*PAIRS[0])==current.resolve("食費","食品")==identity
    assert current.resolve("日用品","消耗品")!=identity
    assert reader.reads==[] and set(store.reads)<={"category-requests","journal","catalog"}
    assert store.data["month-2010-01"]==original and store.writes==["catalog"]
    store.writes.clear();refresh.refresh(pairs);assert store.writes==[]


def test_backfill_literal_choices_include_old_months_and_every_current_selection():
    from types import SimpleNamespace
    reads=[];store=SimpleNamespace(read=lambda key:reads.append(key) or {"months":{"2001-01":{},"2026-09":{}}})
    validations=[{"setDataValidation":{"range":{"sheetId":20,"startColumnIndex":c},"rule":{"condition":{"type":"ONE_OF_RANGE","values":[]}}}} for c in (2,3)]
    legacy={"updateCells":{"range":{"startColumnIndex":25},"rows":[{"values":[{"userEnteredValue":{"formulaValue":"=legacy"}}]}]}}
    rows=[["","","1999-12","2000-01"] for _ in range(1200)]+[["","","2030-01","過去すべて"]]
    result=backfill_month_choices([legacy,*validations],sheet_id=20,title="カテゴリ操作",rows=rows,extent=2000,store=store)
    assert reads==["summary"] and "formulaValue" not in str(result) and "5001" not in str(result)
    writes=[r["updateCells"] for r in result if "updateCells" in r]
    assert [r["values"][0]["userEnteredValue"]["stringValue"] for r in writes[0]["rows"]][1:]==["1999-12","2000-01","2001-01","2026-09","2030-01"]
    assert all(w["range"]["endRowIndex"]==2000 for w in writes)
    assert validations[0]["setDataValidation"]["rule"]["condition"]["values"][0]["userEnteredValue"].endswith("$Z$6")


def test_backfill_choices_grow_beyond_old_thousand_month_limit():
    from types import SimpleNamespace
    from app.monthly_projection import shift_month
    months={shift_month("1900-01",i):{} for i in range(1201)}
    result=backfill_month_choices([],sheet_id=20,title="カテゴリ操作",rows=[],extent=1000,
        store=SimpleNamespace(read=lambda key:{"months":months}))
    assert result[0]["appendDimension"]["length"]==203
    assert len(result[-1]["updateCells"]["rows"])==1203
