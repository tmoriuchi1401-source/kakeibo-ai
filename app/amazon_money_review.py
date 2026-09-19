"""Human decisions for monetary exceptions, with fixed-ID recoverable intents.

No order-event state machine. The exception's date/amount/source stay immutable.
Mixed or unconfirmed payment legs cannot be forced into an expense by this UI.
"""
from copy import deepcopy
from dataclasses import fields
import re

from .amazon_money import MoneyError, MoneyItem, MoneyRecord, digest, source_alias
from .projection_store import replace_document
from .utils import now_jst_string

ACTIONS={"保留":"hold","別の確定取引として計上":"separate","既存支出へ対応付け":"link",
         "返金元を指定":"refund_link","自分用残高のチャージ":"transfer"}
ERRORS={"money_review_changed":"対象の情報が変わりました。再確認してください。",
    "money_review_target_invalid":"確認中の金銭IDを選んでください。",
    "money_review_confirmation_required":"確定情報が不足しています。原本を確認してください。",
    "money_review_action_invalid":"対応を選んでください。",
    "money_review_link_invalid":"既存支出ID・金額・計上状態を確認してください。",
    "money_review_link_capacity":"指定した支出への対応金額が重複しています。",
    "money_review_refund_target_required":"返金元の金銭IDを指定してください。",
    "money_review_transfer_invalid":"確定した購入金額だけを振替として確認できます。",
    "money_review_still_unresolved":"支払内訳・元購入などの情報が不足しています。保留して原本を確認してください。"}


def record_from_entry(item):
    names={f.name for f in fields(MoneyRecord)}-{"items","confirmed"}
    record=MoneyRecord(**{k:v for k,v in item.items() if k in names},
        confirmed=item.get("confirmed",False),items=tuple(MoneyItem(**i) for i in item.get("items",[])))
    record.validate()
    return record


class MoneyReviews:
    def __init__(self,writer):
        self.writer,self.store=writer,writer.store

    def read(self):
        value=self.store.read("money-reviews")
        if value is None:return {"requests":{}}
        if not isinstance(value,dict) or set(value)!={"requests"} or not isinstance(value["requests"],dict):
            raise MoneyError("money_review_inbox_invalid")
        return value

    def _link(self,record,ids,book):
        # Negative legacy rows need their original-purchase binding in the
        # migration manifest; this form must not invent an unlinked refund.
        if record.kind!="purchase":raise MoneyError("money_review_link_invalid")
        if not ids or len(ids)>100 or len(set(ids))!=len(ids):raise MoneyError("money_review_link_invalid")
        found=self.writer.ledger.find("支出明細",13,set(ids))
        if set(found)!=set(ids) or any(row[12] not in {"","active"} for row in found.values()):
            raise MoneyError("money_review_link_invalid")
        from .monthly_projection import _yen,ProjectionError
        try:total=sum(_yen(row[4]) for row in found.values())
        except ProjectionError:raise MoneyError("money_review_link_invalid") from None
        if total==0 or (total<0)!=(record.amount<0):raise MoneyError("money_review_link_invalid")
        used=0
        for key,alias in book["aliases"].items():
            if key==source_alias(record):continue
            other=set(alias.get("expense_ids",[]))
            if other & set(ids):
                # Shared partial charges must use the same complete expense
                # group; overlapping subsets do not define an allocation.
                if other!=set(ids) or type(alias.get("amount")) is not int:
                    raise MoneyError("money_review_link_capacity")
                used+=abs(alias["amount"])
        if used+abs(record.amount)>abs(total):raise MoneyError("money_review_link_capacity")
        return {"fingerprint":record.fingerprint,"expense_ids":list(ids),
                "expense_amounts":{k:row[4] for k,row in found.items()},"amount":record.amount,
                "expense_fingerprints":{k:digest(row) for k,row in found.items()}}

    def prepare(self,request_id,money_id,action,*,related_id="",expense_ids=(),form_digest=""):
        if not re.fullmatch(r"REQ-[a-f0-9]{32}",request_id):raise MoneyError("money_review_request_invalid")
        if action not in ACTIONS.values():raise MoneyError("money_review_action_invalid")
        ids=tuple(sorted(expense_ids))
        payload={"money_id":money_id,"action":action,"related_id":related_id,"expense_ids":list(ids)}
        before=self.store.read("money-reviews");inbox=self.read()
        old=inbox["requests"].get(request_id)
        if old:
            if any(old[k]!=v for k,v in payload.items()):raise MoneyError("money_review_request_reused")
            return deepcopy(old)
        book=self.writer._book();entry=book["records"].get(money_id)
        if not entry or entry.get("state")!="review":raise MoneyError("money_review_target_invalid")
        record=record_from_entry(entry)
        if record.money_id!=money_id or record.fingerprint!=entry["fingerprint"]:raise MoneyError("money_review_changed")
        if action!="hold" and not record.confirmed:raise MoneyError("money_review_confirmation_required")
        if action not in {"hold","transfer"} and record.payment in {"mixed","unknown"}:
            raise MoneyError("money_review_still_unresolved")
        if action=="refund_link" and (record.kind!="refund" or not related_id):
            raise MoneyError("money_review_refund_target_required")
        if action=="transfer" and (record.kind!="purchase" or record.amount<=0):raise MoneyError("money_review_transfer_invalid")
        if (action!="link" and ids) or (action!="refund_link" and related_id):raise MoneyError("money_review_action_invalid")
        resolution={"request_id":request_id,"fingerprint":record.fingerprint,"action":action}
        if action=="refund_link":resolution["related_id"]=related_id
        candidate=deepcopy(book);candidate["records"][money_id]["resolution"]=resolution
        alias=self._link(record,ids,book) if action=="link" else None
        if alias:candidate["aliases"][source_alias(record)]=alias
        decision=self.writer._decision(record,candidate)
        if action!="hold" and decision.action not in {"post","linked","transfer"}:
            raise MoneyError("money_review_still_unresolved")
        now=now_jst_string()
        item={**payload,"snapshot":digest(entry),"resolution":resolution,"alias":alias,
            "state":"queued","error":"","created_at":now,"updated_at":now,"form_digest":form_digest}
        inbox["requests"][request_id]=item
        replace_document(self.store,"money-reviews",before,inbox)
        return deepcopy(item)

    def reject(self,request_id,money_id,error,*,form_digest):
        if error not in ERRORS or not re.fullmatch(r"REQ-[a-f0-9]{32}",request_id):raise MoneyError("money_review_rejection_invalid")
        before=self.store.read("money-reviews");inbox=self.read()
        if request_id in inbox["requests"]:raise MoneyError("money_review_request_reused")
        now=now_jst_string()
        item={"money_id":money_id[:200],"state":"failed","error":error,"form_digest":form_digest,
              "created_at":now,"updated_at":now}
        inbox["requests"][request_id]=item
        replace_document(self.store,"money-reviews",before,inbox)
        return deepcopy(item)

    def apply(self,request_id):
        before=self.read();item=deepcopy(before["requests"][request_id])
        if item["state"] in {"applied","failed"}:return item
        if item["state"] not in {"queued","pending"}:raise MoneyError("money_review_state_invalid")
        book=self.writer._book();entry=book["records"].get(item["money_id"])
        installed=entry and entry.get("resolution")==item["resolution"]
        if not installed and (not entry or digest(entry)!=item["snapshot"]):
            item.update(state="failed",error="money_review_changed")
        else:
            record=record_from_entry(entry)
            if not installed:
                alias=item["alias"]
                if alias:
                    try:fresh=self._link(record,item["expense_ids"],book)
                    except MoneyError as error:
                        if str(error) not in ERRORS:raise
                        item.update(state="failed",error=str(error));fresh=None
                    if fresh is not None and fresh!=alias:item.update(state="failed",error="money_review_changed")
                if item["state"]!="failed":
                    item.update(state="pending",updated_at=now_jst_string())
                    pending=deepcopy(before);pending["requests"][request_id]=item
                    replace_document(self.store,"money-reviews",before,pending);before=pending
                    after=deepcopy(book);after["records"][item["money_id"]]["resolution"]=item["resolution"]
                    if alias:
                        after["aliases"][source_alias(record)]=alias
                        for legacy in after["legacy"].values():
                            if set(legacy["expense_ids"])!=set(alias["expense_ids"]):continue
                            claims=[a for a in after["aliases"].values() if set(a.get("expense_ids",[]))==set(alias["expense_ids"])]
                            if (all(type(a.get("amount")) is int for a in claims)
                                    and sum(max(0,a["amount"]) for a in claims)==legacy["amount"]):
                                legacy["state"]="settled"
                    replace_document(self.store,"money",book,after)
            if item["state"]!="failed":
                decision=self.writer._decision(record,self.writer._book())
                if item["action"]!="hold" and decision.action not in {"post","resume","linked","transfer","duplicate"}:
                    item.update(state="failed",error="money_review_still_unresolved")
                else:
                    self.writer.apply([record],limit=1)
                    item.update(state="applied",error="")
        item["updated_at"]=now_jst_string()
        after=deepcopy(before);after["requests"][request_id]=item
        replace_document(self.store,"money-reviews",before,after)
        return item

    def apply_pending(self,*,limit=20):
        if type(limit) is not int or not 1<=limit<=100:raise MoneyError("money_review_limit_invalid")
        count=0
        for key,item in self.read()["requests"].items():
            if count>=limit:break
            if item["state"] in {"queued","pending"}:self.apply(key);count+=1
        return count
