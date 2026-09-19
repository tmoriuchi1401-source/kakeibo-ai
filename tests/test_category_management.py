from copy import deepcopy
from types import SimpleNamespace
import re

import pytest

from app.category_management import CategoryManagement, CategoryMaster, ACTIONS
from app.monthly_projection import ProjectionError
from app.projection_refresh import load_catalog
from test_projection_refresh import initialized, row, PAIRS

TOKEN="REQ-"+"a"*32


class Master:
    def __init__(self):
        self.rows=[["大カテゴリ","小カテゴリ"],list(PAIRS[0])]
        self.writes=[];self.fail=False
    def read(self):return deepcopy(self.rows)
    def write(self,before,after):
        assert self.rows==before
        self.rows=deepcopy(after);self.writes.append(deepcopy(after))
        if self.fail:self.fail=False;raise RuntimeError("response_unknown")


def setup():
    store,reader,refresh=initialized([row("old","2018-01-01")])
    master=Master();sync=[]
    inbox=CategoryManagement(store,master,lambda catalog:sync.append(catalog))
    identity=load_catalog(store.data["catalog"]).resolve(*PAIRS[0])
    return store,reader,refresh,master,sync,inbox,identity


def prepare(inbox,identity,action="rename",names="食費｜食品",token=TOKEN):
    return inbox.prepare(token,[next(k for k,v in ACTIONS.items() if v==action),identity,names],form_digest="input")


def test_rename_keeps_history_and_rules_compatible_through_refresh_and_bootstrap():
    store,reader,refresh,master,sync,inbox,identity=setup()
    original=deepcopy(store.data["month-2018-01"]);expenses=deepcopy(reader.rows)
    prepare(inbox,identity)
    assert inbox.apply(TOKEN)["state"]=="applied"
    catalog=load_catalog(store.data["catalog"])
    assert catalog.resolve("食費","食品")==catalog.resolve(*PAIRS[0])==identity
    assert master.rows==[["大カテゴリ","小カテゴリ"],list(PAIRS[0]),["食費","食品"]]
    refresh.refresh([tuple(r) for r in master.rows[1:]])
    refresh.bootstrap([tuple(r) for r in master.rows[1:]])
    assert load_catalog(store.data["catalog"]).resolve("食費","食品")==identity
    assert store.data["month-2018-01"]==original and reader.rows==expenses
    writes=list(store.writes);inbox.apply(TOKEN)
    assert store.writes==writes and len(master.writes)==1 and len(sync)==1


def test_split_and_merge_create_distinct_ids_without_retiring_or_reassigning_parents():
    store,reader,refresh,master,sync,inbox,identity=setup()
    original=deepcopy(store.data["month-2018-01"])
    prepare(inbox,identity,"split","食費｜生鮮品\n食費｜保存食")
    split=inbox.apply(TOKEN)
    assert split["parent_ids"]==[identity] and len(set(split["result_ids"]))==2
    token="REQ-"+"b"*32
    prepare(inbox,",".join(split["result_ids"]),"merge","食費｜食材",token)
    merged=inbox.apply(token)
    assert set(merged["parent_ids"])==set(split["result_ids"])
    assert merged["result_ids"][0] not in [identity,*split["result_ids"]]
    catalog=load_catalog(store.data["catalog"])
    assert len(catalog.categories)==4 and all(c.active for c in catalog.categories)
    assert store.data["month-2018-01"]==original and reader.reads==[]


@pytest.mark.parametrize("key",["category-requests","catalog"])
@pytest.mark.parametrize("after_save",[False,True])
def test_private_write_unknown_recovers_without_duplicate_master_or_ids(key,after_save):
    store,reader,refresh,master,sync,inbox,identity=setup()
    prepare(inbox,identity);store.fail_key=key;store.after_save=after_save
    with pytest.raises(RuntimeError):inbox.apply(TOKEN)
    assert inbox.apply(TOKEN)["state"]=="applied"
    assert len(master.writes)==1 and len(load_catalog(store.data["catalog"]).categories)==1


@pytest.mark.parametrize("stage",["master","helper"])
def test_native_failure_blocks_projection_until_same_intent_resumes(stage):
    store,reader,refresh,master,sync,inbox,identity=setup()
    prepare(inbox,identity)
    if stage=="master":master.fail=True
    else:inbox.sync=lambda catalog:(_ for _ in ()).throw(RuntimeError("helper_unknown"))
    with pytest.raises(RuntimeError):inbox.apply(TOKEN)
    for run in [refresh.refresh,refresh.bootstrap]:
        with pytest.raises(ProjectionError,match="category_management_pending"):run(PAIRS)
    assert reader.reads==[]
    inbox.sync=lambda catalog:None
    assert inbox.apply(TOKEN)["state"]=="applied" and len(master.writes)==1


def test_changed_master_fails_before_mutation_and_pending_conflict_stays_recoverable():
    store,reader,refresh,master,sync,inbox,identity=setup()
    prepare(inbox,identity);master.rows.append(["日用品","消耗品"])
    assert inbox.apply(TOKEN)["error"]=="category_management_changed" and master.writes==[]
    token="REQ-"+"c"*32;prepare(inbox,identity,token=token);master.fail=True
    with pytest.raises(RuntimeError):inbox.apply(token)
    master.rows.append(["交通費","電車"])
    with pytest.raises(ProjectionError,match="pending_conflict"):inbox.apply(token)
    assert inbox.read()["requests"][token]["state"]=="pending" and len(master.writes)==1


@pytest.mark.parametrize("action,target,names,error",[
    ("add","","食費｜食料品","name_exists"),
    ("rename","bad","食費｜食品","target_invalid"),
    ("split",None,"食費｜食品","name_invalid"),
    ("merge",None,"食費｜食品","target_invalid"),
    ("add","","食品","name_invalid"),
    ("add","","食費｜食品\n日用品｜消耗品","name_invalid"),
])
def test_invalid_input_is_visible_without_native_changes(action,target,names,error):
    store,reader,refresh,master,sync,inbox,identity=setup()
    before=deepcopy(store.data["catalog"])
    prepare(inbox,identity if target is None else target,action,names)
    assert error in inbox.apply(TOKEN)["error"]
    assert master.writes==[] and store.data["catalog"]==before


class NativeMaster:
    sid="synthetic"
    def __init__(self):
        self.svc=self;self.rows=[["大","小"]]+[["分類",str(i)] for i in range(2000)]
        self.capacity=2001;self.reads=[];self.writes=[]
    def spreadsheets(self):return self
    def _execute_sheet_read(self,fn):return fn().execute()
    def get(self,**kwargs):
        return SimpleNamespace(execute=lambda:{"sheets":[{"properties":{"title":"カテゴリ","sheetId":77,"gridProperties":{"rowCount":self.capacity}}}]})
    def get_raw(self,rng):
        self.reads.append(rng);first,last=map(int,re.search(r"A(\d+):B(\d+)",rng).groups())
        assert last-first<2000
        return deepcopy(self.rows[first-1:last])
    def batchUpdate(self,**kwargs):
        def execute(**options):
            assert options=={"num_retries":0}
            self.writes.append(kwargs["body"])
            for r in kwargs["body"]["requests"]:
                if "updateSheetProperties" in r:self.capacity=r["updateSheetProperties"]["properties"]["gridProperties"]["rowCount"]
                else:
                    spec=r["updateCells"];assert spec["range"]["startColumnIndex"]==0 and spec["range"]["endColumnIndex"]==2
                    assert spec["fields"]=="userEnteredValue"
                    assert spec["range"]["startRowIndex"]==len(self.rows)
                    self.rows.extend([[c["userEnteredValue"]["stringValue"] for c in row["values"]] for row in spec["rows"]])
            return {}
        return SimpleNamespace(execute=execute)


def test_native_master_uses_fresh_bounded_reads_and_appends_only_ab_in_one_batch():
    db=NativeMaster();master=CategoryMaster(db);before=master.read()
    master.write(before,before+[["新規","分類"]])
    assert len(db.rows)==2002 and db.capacity==2002 and len(db.writes)==1
    assert "'カテゴリ'!A2001:B2002" in db.reads
    with pytest.raises(ProjectionError,match="append_only"):master.write(master.read(),[["replace","all"]])
    assert len(db.writes)==1
