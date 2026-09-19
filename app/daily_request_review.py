"""All-period request status and explicit acknowledgement of past failures.

The acknowledgement is another fixed request in the existing inbox. Original
failed inputs/results stay intact. It cannot retry or alter accounting, coverage
or native source checkpoints; a changed failure becomes visible again.
"""
from copy import deepcopy
import re

from .amazon_money import MoneyError,digest
from .projection_store import replace_document
from .utils import now_jst_string

KINDS={"corrections":"支出修正","money-reviews":"金銭・通知の確認","coverage":"取込状況"}


def parts(reference):
    match=re.fullmatch(r"RQ-(corrections|money-reviews|coverage):(REQ-[a-f0-9]{32})",reference)
    if not match:raise MoneyError("request_review_target_invalid")
    return match.groups()


def requests(store,key):
    value=store.read(key)
    if value is None:return {}
    if not isinstance(value,dict) or not isinstance(value.get("requests"),dict):
        raise MoneyError("request_review_document_invalid")
    return value["requests"]


def review_items(store,daily_id):
    from .daily_view import ReviewItem,SHEETS,STATE_LABELS
    from .daily_corrections import INPUT_ERRORS
    from .daily_coverage import ERRORS as COVERAGE_ERRORS
    from .amazon_money_review import ERRORS as MONEY_ERRORS
    documents={key:requests(store,key) for key in KINDS}
    acknowledged={(r.get("money_id"),r.get("snapshot")) for r in documents["money-reviews"].values()
        if r.get("action")=="acknowledge_failure" and r.get("state")=="applied"}
    labels={"corrections":INPUT_ERRORS,"money-reviews":MONEY_ERRORS,"coverage":COVERAGE_ERRORS}
    result=[]
    for kind,values in documents.items():
        title,row=("設定",11) if kind=="coverage" else ("確認",61 if kind=="corrections" else 81)
        url=f"https://docs.google.com/spreadsheets/d/{daily_id}/edit#gid={SHEETS[title][0]}&range=B{row}"
        for token,item in values.items():
            state=item.get("state")
            if state not in {"queued","pending","failed"}:continue
            reference="RQ-"+kind+":"+token
            parts(reference)
            if state=="failed" and (reference,digest(item)) in acknowledged:continue
            reason=labels[kind].get(item.get("error"),"元のフォームで内容を確認してください。") if state=="failed" else "既存の反映処理を待っています。"
            target=item.get("expense_id") or item.get("money_id") or ""
            detail=f"{item.get('updated_at','')}\n{reason}\n{str(target)[:80]}"
            result.append(ReviewItem(reference,KINDS[kind],detail,STATE_LABELS[state],url))
    return result


def prepare(inbox,request_id,payload,form_digest):
    kind,token=parts(payload["money_id"])
    item=requests(inbox.store,kind).get(token)
    if not item or item.get("state")!="failed":raise MoneyError("request_review_target_invalid")
    if payload["action"]!="acknowledge_failure" or payload["related_id"] or payload["expense_ids"]:
        raise MoneyError("request_review_no_retry")
    before=inbox.store.read("money-reviews");after=inbox.read();now=now_jst_string()
    intent={**payload,"snapshot":digest(item),"form_digest":form_digest,
        "state":"queued","error":"","created_at":now,"updated_at":now}
    after["requests"][request_id]=intent
    replace_document(inbox.store,"money-reviews",before,after)
    return deepcopy(intent)


def apply(inbox,request_id,before,item):
    kind,token=parts(item["money_id"])
    latest=requests(inbox.store,kind).get(token)
    if not latest or latest.get("state")!="failed" or digest(latest)!=item["snapshot"]:
        item.update(state="failed",error="money_review_changed")
    else:item.update(state="applied",error="")
    item["updated_at"]=now_jst_string()
    after=deepcopy(before);after["requests"][request_id]=item
    replace_document(inbox.store,"money-reviews",before,after)
    return item
