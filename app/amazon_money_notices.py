"""Minimal unresolved final-notice references; never synthetic money amounts.

No raw mail, order lifecycle, or parser evidence is stored here. A stable Gmail
reference and short reason let the existing daily form hold/acknowledge the
exception without inventing a posting. Normal source checkpoints advance only
after this private document has been saved and read back.
"""
from copy import deepcopy
from hashlib import sha256

from .amazon_money import MoneyError, digest
from .projection_store import replace_document
from .utils import now_jst_string

REASONS={"money_legacy_posting_unverified":"旧カード照合の正式支出への対応を確認",
    "money_confirmed_amount_missing":"確定通知の金額を確認",
    "money_date_missing":"確定日を確認", "money_date_invalid":"確定日を確認",
    "money_message_id_missing":"通知の識別情報を確認",
    "money_card_parse_failed":"確定カード通知の明細を確認",
    "money_card_account_missing":"カードの支払元を確認",
    "money_card_partial":"Amazonを含む確定カード通知に未解析明細があります"}


def notice(source,gmail_id,raw,reason):
    if not gmail_id or reason not in REASONS:raise MoneyError("money_notice_invalid")
    return {"notice_id":"MN-"+digest([source,gmail_id])[:32],"source":source,
        "fingerprint":sha256(raw).hexdigest(),"reason":reason,
        "original_url":"https://mail.google.com/mail/u/0/#all/"+gmail_id}


def read_notices(store):
    value=store.read("money-notices")
    if value is None:return {"notices":{}}
    if not isinstance(value,dict) or set(value)!={"notices"} or not isinstance(value["notices"],dict):
        raise MoneyError("money_notices_invalid")
    return value


def save_notices(store,notices,*,dry_run):
    unique={}
    for value in notices:
        identity=value["notice_id"]
        if identity in unique and unique[identity]!=value:raise MoneyError("money_notice_identity_conflict")
        unique[identity]=value
    if dry_run or not unique:return {"money_notice_review":len(unique)}
    before=store.read("money-notices");after=read_notices(store)
    for identity,value in unique.items():
        old=after["notices"].get(identity,{})
        if all(old.get(k)==v for k,v in value.items()):continue
        after["notices"][identity]={**value,"state":"open","updated_at":now_jst_string()}
    replace_document(store,"money-notices",before,after)
    return {"money_notice_review":len(unique)}


def review_items(store):
    from .daily_view import ReviewItem
    return [ReviewItem(identity,"旧Amazon取込の確認" if item.get("source")=="legacy_import" else "Amazon通知の確認",
        REASONS.get(item["reason"],"原本を確認")+("\n取込ID: "+item["source_id"] if item.get("source")=="legacy_import" else ""),
        "保留" if item["state"]=="held" else "要確認",item["original_url"])
        for identity,item in read_notices(store)["notices"].items() if item["state"] in {"open","held"}]


def prepare_review(inbox,request_id,payload,form_digest):
    value=read_notices(inbox.store)["notices"].get(payload["money_id"])
    if not value or value["state"] not in {"open","held"}:raise MoneyError("money_review_target_invalid")
    if payload["action"] not in {"hold","acknowledge"} or payload["related_id"] or payload["expense_ids"]:
        raise MoneyError("money_notice_no_posting")
    before=inbox.store.read("money-reviews");after=inbox.read();now=now_jst_string()
    item={**payload,"snapshot":digest(value),"resolution":{"request_id":request_id,"action":payload["action"]},
        "state":"queued","error":"","created_at":now,"updated_at":now,"form_digest":form_digest}
    after["requests"][request_id]=item
    replace_document(inbox.store,"money-reviews",before,after)
    return deepcopy(item)


def apply_review(inbox,request_id,before,item):
    document=read_notices(inbox.store);value=document["notices"].get(item["money_id"])
    installed=value and value.get("resolution")==item["resolution"]
    if not installed and (not value or digest(value)!=item["snapshot"]):
        item.update(state="failed",error="money_review_changed")
    else:
        if not installed:
            item.update(state="pending",updated_at=now_jst_string())
            pending=deepcopy(before);pending["requests"][request_id]=deepcopy(item)
            replace_document(inbox.store,"money-reviews",before,pending);before=pending
            after=deepcopy(document)
            after["notices"][item["money_id"]].update(state="held" if item["action"]=="hold" else "acknowledged",
                resolution=item["resolution"],updated_at=now_jst_string())
            replace_document(inbox.store,"money-notices",document,after)
        item.update(state="applied",error="")
    item["updated_at"]=now_jst_string()
    after=deepcopy(before);after["requests"][request_id]=item
    replace_document(inbox.store,"money-reviews",before,after)
    return item
