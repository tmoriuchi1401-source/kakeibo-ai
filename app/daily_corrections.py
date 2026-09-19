"""One fixed-ID correction inbox, with durable intent and fresh ledger checks.

This changes existing active expense fields only. It cannot create expenses,
alter source IDs/status, execute intake, or accept Medical/Payroll documents.
Call under the existing production Workflow lock. Display refresh never submits.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re

from .monthly_projection import ProjectionError, _date, _yen, fingerprint
from .projection_refresh import load_catalog, load_index
from .projection_store import replace_document


FIELDS={"date":1,"merchant":2,"item":3,"amount":4,"note":11}


def stamp():
    return datetime.now(timezone.utc).isoformat()


class DailyCorrections:
    def __init__(self, store, db):
        self.store,self.db=store,db

    def _requests(self):
        value=self.store.read("corrections")
        if value is None:return {"requests":{}}
        if not isinstance(value,dict) or set(value)!={"requests"} or not isinstance(value["requests"],dict):
            raise ProjectionError("correction_inbox_invalid")
        return value

    def _read_target(self, expense_id):
        index=load_index(self.store.read("index"))
        number=index.identity_rows.get(expense_id)
        if number is None:raise ProjectionError("correction_expense_not_found")
        rows=self.db.get_raw(f"'支出明細'!A{number}:M{number}")
        if len(rows)!=1:raise ProjectionError("correction_expense_not_found")
        row=list(rows[0])+[""]*max(0,13-len(rows[0]))
        if str(row[0])!=expense_id:raise ProjectionError("correction_identity_changed")
        if row[12] not in ("","active"):raise ProjectionError("correction_expense_inactive")
        return number,row[:13]

    def prepare(self, request_id, expense_id, changes):
        if not re.fullmatch(r"REQ-[a-f0-9]{32}",request_id):
            raise ProjectionError("correction_request_id_invalid")
        if not expense_id or not changes or not set(changes)<=set(FIELDS)|{"category_id"}:
            raise ProjectionError("correction_fields_invalid")
        before=self.store.read("corrections")
        inbox=self._requests()
        old=inbox["requests"].get(request_id)
        if old:
            if old["expense_id"]!=expense_id or old["changes"]!=changes:
                raise ProjectionError("correction_request_reused")
            return deepcopy(old)
        _,row=self._read_target(expense_id)
        after=list(row)
        fields=[]
        category_pair=None
        for name,value in changes.items():
            if name=="category_id":
                catalog=load_catalog(self.store.read("catalog"))
                chosen=[c for c in catalog.categories if c.category_id==value and c.active]
                if len(chosen)!=1:raise ProjectionError("correction_category_invalid")
                category_pair=[chosen[0].major,chosen[0].minor]
                after[5:7]=category_pair
                fields.extend([5,6])
            else:
                if name=="date":value=_date(value)
                elif name=="amount":value=_yen(value)
                elif not isinstance(value,str) or len(value)>2000:
                    raise ProjectionError("correction_text_invalid")
                after[FIELDS[name]]=value
                fields.append(FIELDS[name])
        now=stamp()
        item={"expense_id":expense_id,"changes":deepcopy(changes),"before":fingerprint(row),
              "after":fingerprint(after),"cells":[[c,after[c]] for c in sorted(set(fields))],
              "category_pair":category_pair,"state":"queued","created_at":now,"updated_at":now,"error":""}
        inbox["requests"][request_id]=item
        replace_document(self.store,"corrections",before,inbox)
        return deepcopy(item)

    def apply(self, request_id):
        before=self._requests()
        if request_id not in before["requests"]:raise ProjectionError("correction_request_not_found")
        item=deepcopy(before["requests"][request_id])
        if item["state"] in {"applied","failed"}:return item
        if item["state"] not in {"queued","pending"}:raise ProjectionError("correction_state_invalid")
        try:
            number,row=self._read_target(item["expense_id"])
        except ProjectionError as error:
            if str(error) not in {"correction_expense_not_found","correction_identity_changed","correction_expense_inactive"}:raise
            item.update(state="failed",updated_at=stamp(),error=str(error))
            after=deepcopy(before);after["requests"][request_id]=item
            replace_document(self.store,"corrections",before,after)
            return item
        observed=fingerprint(row)
        if item["state"]=="pending" and observed==item["after"]:
            item.update(state="applied",updated_at=stamp(),error="")
        elif observed!=item["before"]:
            item.update(state="failed",updated_at=stamp(),error="latest_values_changed")
        else:
            if item["category_pair"] is not None:
                catalog=load_catalog(self.store.read("catalog"))
                chosen=[c for c in catalog.categories if c.category_id==item["changes"]["category_id"] and c.active]
                if len(chosen)!=1 or [chosen[0].major,chosen[0].minor]!=item["category_pair"]:
                    item.update(state="failed",updated_at=stamp(),error="category_changed")
            if item["state"]!="failed":
                item.update(state="pending",updated_at=stamp())
                pending=deepcopy(before);pending["requests"][request_id]=item
                replace_document(self.store,"corrections",before,pending)
                # Read again immediately before mutation, after intent save.
                latest_number,latest=self._read_target(item["expense_id"])
                if latest_number!=number or fingerprint(latest)!=item["before"]:
                    item=deepcopy(item)
                    item.update(state="failed",updated_at=stamp(),error="latest_values_changed")
                else:
                    self.db.update_expense_fields(number,item["cells"])
                    _,latest=self._read_target(item["expense_id"])
                    if fingerprint(latest)!=item["after"]:
                        raise ProjectionError("correction_write_readback_unknown")
                    item=deepcopy(item)
                    item.update(state="applied",updated_at=stamp(),error="")
                before=pending
        after=deepcopy(before);after["requests"][request_id]=item
        replace_document(self.store,"corrections",before,after)
        return item

    def apply_pending(self):
        result={"corrections_applied":0,"corrections_failed":0}
        for key,item in self._requests()["requests"].items():
            if item["state"] not in {"queued","pending"}:continue
            outcome=self.apply(key)
            result["corrections_applied" if outcome["state"]=="applied" else "corrections_failed"]+=1
        return result
