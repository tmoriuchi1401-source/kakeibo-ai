"""Explicit category identity changes without rewriting historical expenses.

Rename keeps its ID and old names as aliases. Split/merge create new IDs and
record parent IDs; original classifications and approved rules remain usable.
The old master names are retained as compatibility aliases, not new choices.
"""
from copy import deepcopy
import re

from .amazon_money import digest
from .monthly_projection import Category, CategoryCatalog, ProjectionError
from .projection_refresh import catalog_document, load_catalog
from .projection_store import replace_document
from .utils import now_jst_string

ACTIONS={"改名（IDを保持）":"rename", "分割（新しいID）":"split", "統合（新しいID）":"merge", "新規追加":"add"}
ERRORS={"category_action_invalid":"操作を選んでください。",
    "category_target_invalid":"元カテゴリの固定IDを指定してください。複数の場合は改行かカンマで区切ります。",
    "category_name_invalid":"新しい分類を「大カテゴリ｜小カテゴリ」で指定してください。分割は1行に1分類です。",
    "category_name_exists":"その分類名は使用済みです。改名は同じIDの別名だけ再利用できます。",
    "category_management_changed":"カテゴリが変更されました。最新の一覧で確認して再送信してください。"}


class CategoryMaster:
    def __init__(self,db):self.db=db

    def read(self):
        meta=self.db._execute_sheet_read(lambda:self.db.svc.spreadsheets().get(
            spreadsheetId=self.db.sid,fields="sheets(properties)"))
        sheets=[s["properties"] for s in meta["sheets"] if s["properties"]["title"]=="カテゴリ"]
        if len(sheets)!=1:raise ProjectionError("category_master_missing")
        props=sheets[0];rows=[]
        for start in range(1,props["gridProperties"]["rowCount"]+1,2000):
            end=min(start+1999,props["gridProperties"]["rowCount"])
            block=self.db.get_raw(f"'カテゴリ'!A{start}:B{end}")
            if len(block)>end-start+1:raise ProjectionError("category_master_read_invalid")
            rows.extend([(list(r)+[""]*2)[:2] for r in block]+[["",""]]*(end-start+1-len(block)))
        while rows and not any(rows[-1]):rows.pop()
        return rows

    def write(self,before,after):
        if self.read()!=before:raise ProjectionError("category_management_changed")
        # The only native mutation is adding absent names after existing A:B.
        # No old master row, approved rule, product category, or expense changes.
        if after[:len(before)]!=before:raise ProjectionError("category_master_append_only")
        extra=after[len(before):]
        if not extra:return
        meta=self.db._execute_sheet_read(lambda:self.db.svc.spreadsheets().get(
            spreadsheetId=self.db.sid,fields="sheets(properties)"))
        props=next(s["properties"] for s in meta["sheets"] if s["properties"]["title"]=="カテゴリ")
        requests=[]
        if len(after)>props["gridProperties"]["rowCount"]:
            requests.append({"updateSheetProperties":{"properties":{"sheetId":props["sheetId"],
                "gridProperties":{"rowCount":len(after)}},"fields":"gridProperties.rowCount"}})
        from .compact_categories import _cells
        requests.append(_cells(props["sheetId"],len(before)+1,0,extra,2))
        self.db.svc.spreadsheets().batchUpdate(spreadsheetId=self.db.sid,body={"requests":requests}).execute(num_retries=0)
        if self.read()!=after:raise ProjectionError("category_master_readback_failed")


def changed_catalog(catalog,token,action,targets,names):
    if action not in ACTIONS.values():raise ProjectionError("category_action_invalid")
    ids=re.findall(r"CAT-[a-f0-9]{32}",targets)
    if targets.strip() and not ids:raise ProjectionError("category_target_invalid")
    active={c.category_id:c for c in catalog.categories if c.active}
    expected={"add":(0,0),"rename":(1,1),"split":(1,1),"merge":(2,20)}[action]
    if not expected[0]<=len(ids)<=expected[1] or len(ids)!=len(set(ids)) or any(x not in active for x in ids):
        raise ProjectionError("category_target_invalid")
    lines=[x.strip() for x in names.splitlines() if x.strip()]
    if not (2<=len(lines)<=20 if action=="split" else len(lines)==1):raise ProjectionError("category_name_invalid")
    pairs=[]
    for line in lines:
        parts=line.split("｜")
        if len(parts)!=2 or any(not p.strip() or len(p.strip())>80 or any(ord(c)<32 for c in p) for p in parts):
            raise ProjectionError("category_name_invalid")
        pairs.append(tuple(p.strip() for p in parts))
    if len(pairs)!=len(set(pairs)):raise ProjectionError("category_name_invalid")
    if any(pair in catalog.by_name and not (action=="rename" and catalog.by_name[pair]==ids[0]) for pair in pairs):
        raise ProjectionError("category_name_exists")
    if action=="rename":
        result=catalog.rename(ids[0],*pairs[0]);created=ids
    else:
        added=[Category("CAT-"+digest([token,n])[:32],*pair) for n,pair in enumerate(pairs)]
        result=CategoryCatalog((*catalog.categories,*added));created=[c.category_id for c in added]
    return result,ids,created,pairs


class CategoryManagement:
    def __init__(self,store,master,sync=lambda catalog:None):self.store,self.master,self.sync=store,master,sync

    def read(self):
        value=self.store.read("category-requests")
        if value is None:return {"requests":{}}
        if not isinstance(value,dict) or set(value)!={"requests"} or not isinstance(value["requests"],dict):
            raise ProjectionError("category_requests_invalid")
        return value

    def prepare(self,token,values,*,form_digest):
        if not re.fullmatch(r"REQ-[a-f0-9]{32}",token):raise ProjectionError("category_token_invalid")
        before=self.store.read("category-requests");doc=self.read()
        if token in doc["requests"]:return deepcopy(doc["requests"][token])
        item={"state":"queued","error":"","form_digest":form_digest,"updated_at":now_jst_string(),
            "action":ACTIONS.get(str(values[0]),""),"input":list(values)}
        try:
            original=self.store.read("catalog")
            catalog=load_catalog(original)
            after,parents,created,pairs=changed_catalog(catalog,token,item["action"],str(values[1]),str(values[2]))
            master=self.master.read();updated=deepcopy(master)
            for pair in pairs:
                if list(pair) not in updated:updated.append(list(pair))
            item.update(parent_ids=parents,result_ids=created,catalog_before=original,catalog_after=catalog_document(after),
                master_before=master,master_after=updated)
        except ProjectionError as error:
            if str(error) not in ERRORS:raise
            item.update(state="failed",error=str(error))
        doc["requests"][token]=item
        replace_document(self.store,"category-requests",before,doc)
        return deepcopy(item)

    def apply(self,token):
        before=self.read();item=deepcopy(before["requests"][token])
        if item["state"] in {"applied","failed"}:return item
        current=self.master.read();catalog=self.store.read("catalog")
        if item["state"]=="queued":
            if current!=item["master_before"] or catalog!=item["catalog_before"]:
                item.update(state="failed",error="category_management_changed")
            else:item["state"]="pending"
            after=deepcopy(before);after["requests"][token]=deepcopy(item)
            replace_document(self.store,"category-requests",before,after);before=after
        if item["state"]=="pending":
            if current not in (item["master_before"],item["master_after"]) or catalog not in (item["catalog_before"],item["catalog_after"]):
                raise ProjectionError("category_management_pending_conflict")
            if current!=item["master_after"]:self.master.write(current,item["master_after"])
            if self.master.read()!=item["master_after"]:raise ProjectionError("category_master_readback_failed")
            replace_document(self.store,"catalog",catalog,item["catalog_after"])
            self.sync(load_catalog(item["catalog_after"]))
            item.update(state="applied",updated_at=now_jst_string())
        after=deepcopy(before);after["requests"][token]=item
        replace_document(self.store,"category-requests",before,after)
        return deepcopy(item)


def require_settled(store):
    doc=store.read("category-requests")
    if doc and any(v.get("state")=="pending" for v in doc["requests"].values()):
        raise ProjectionError("category_management_pending")
