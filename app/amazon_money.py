"""Amazon money decisions, without an order/delivery/return state machine.

Adapters establish confirmation and source identity. Only one authority owns
each payment leg. Date/amount similarities are review evidence, never aliases.
The existing ledger remains canonical; this book stores posting links/intents.
"""
from dataclasses import dataclass, field
from datetime import date
import hashlib
import json
import re


class MoneyError(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class MoneyItem:
    name:str
    amount:int
    major:str="その他"
    minor:str="未分類"


@dataclass(frozen=True)
class MoneyRecord:
    source:str
    source_id:str=field(repr=False)
    account:str=field(repr=False)
    reference:str=field(repr=False)
    day:str
    amount:int=field(repr=False)
    kind:str
    payment:str
    confirmed:bool
    order_id:str=field(default="",repr=False)
    related_id:str=field(default="",repr=False)
    original_url:str=field(default="",repr=False)
    items:tuple[MoneyItem,...]=field(default=(),repr=False)

    @property
    def money_id(self):
        # Order IDs are relationships only. Split charges/refunds retain IDs.
        return "AM-"+digest([self.source,self.account,self.reference])[:32]

    @property
    def fingerprint(self):
        return digest([self.source,self.account,self.reference,self.day,self.amount,self.kind,self.payment])

    def validate(self):
        if type(self.confirmed) is not bool:raise MoneyError("money_confirmation_invalid")
        if not self.source_id or not self.reference or not self.account:
            raise MoneyError("money_identity_missing")
        try:
            if date.fromisoformat(self.day).isoformat()!=self.day:raise ValueError()
        except (ValueError,TypeError):raise MoneyError("money_date_invalid") from None
        if type(self.amount) is not int or self.amount==0:raise MoneyError("money_amount_invalid")
        if self.kind not in {"purchase","refund","transfer"}:raise MoneyError("money_kind_invalid")
        if (self.kind=="refund")!=(self.amount<0):raise MoneyError("money_sign_invalid")
        if self.source not in {"au_pay_card","amazon"}:raise MoneyError("money_source_invalid")
        if self.payment not in {"card","gift_balance","cash","bank_payment","mixed","unknown"}:
            raise MoneyError("money_payment_invalid")
        if any(type(i.amount) is not int for i in self.items):raise MoneyError("money_item_amount_invalid")


def empty_book(*,cutover_day,legacy):
    date.fromisoformat(cutover_day)
    return {"schema":1,"cutover_day":cutover_day,"legacy":legacy,"records":{},"aliases":{}}


def validate_book(book):
    if not isinstance(book,dict) or set(book)!={"schema","cutover_day","legacy","records","aliases"} or book["schema"]!=1:
        raise MoneyError("money_migration_required")
    date.fromisoformat(book["cutover_day"])
    if any(not isinstance(book[k],dict) for k in ("legacy","records","aliases")):
        raise MoneyError("money_book_invalid")
    for order,entry in book["legacy"].items():
        if (not order or entry.get("state") not in {"open","settled"}
                or not entry.get("expense_ids") or type(entry.get("amount")) is not int or entry["amount"]<=0):
            raise MoneyError("money_legacy_invalid")


@dataclass(frozen=True)
class MoneyDecision:
    record:MoneyRecord
    action:str
    reason:str
    expense_ids:tuple[str,...]=()
    related_id:str=""


def source_alias(record):
    return record.source+":"+record.source_id


def decide(record,book):
    record.validate();validate_book(book)
    def result(action,reason,ids=(),related=""):
        return MoneyDecision(record,action,reason,tuple(ids),related)
    old=book["records"].get(record.money_id)
    resolution={}
    if old:
        if old["fingerprint"]!=record.fingerprint:raise MoneyError("money_identity_conflict")
        if old["state"] in {"posted","linked","transfer","supplement"}:
            return result("duplicate","already_recorded",old.get("expense_ids",()),old.get("related_id",""))
        if old["state"]=="pending":return result("resume","posting_pending",old["expense_ids"],old.get("related_id",""))
        if old["state"]!="review":raise MoneyError("money_state_invalid")
        resolution=old.get("resolution",{})
        if resolution and (resolution.get("fingerprint")!=record.fingerprint
                or not re.fullmatch(r"REQ-[a-f0-9]{32}",resolution.get("request_id",""))):
            raise MoneyError("money_resolution_binding_invalid")
        if resolution.get("action")=="hold":return result("review",old["reason"])
    alias=book["aliases"].get(source_alias(record))
    if alias:
        if alias["fingerprint"]!=record.fingerprint:raise MoneyError("money_alias_conflict")
        return result("linked","verified_existing_identity",alias["expense_ids"],alias.get("related_id",""))
    if not record.confirmed:return result("review","confirmation_missing")
    if resolution.get("action")=="transfer":return result("transfer","confirmed_own_balance_funding")
    if record.kind=="transfer":return result("transfer","own_balance_funding")
    if record.source=="amazon" and record.payment=="card":
        return result("supplement","card_issuer_is_authority")
    if record.payment in {"mixed","unknown"}:return result("review","payment_leg_unresolved")
    if record.source=="au_pay_card" and record.payment!="card":
        return result("review","authority_payment_mismatch")
    separate=resolution.get("action")=="separate"
    if record.day<book["cutover_day"] and not separate:
        return result("review","before_cutover_identity_required")
    if record.kind=="purchase":
        if not separate and record.source=="au_pay_card" and any(v.get("kind")=="transfer" and v.get("amount")==record.amount
            and abs((date.fromisoformat(v["day"])-date.fromisoformat(record.day)).days)<=7 for v in book["records"].values()):
            return result("review","balance_funding_identity_required")
        legacy=book["legacy"].get(record.order_id)
        if legacy and not separate:
            # Same order/amount is not sufficient for linking a split charge.
            return result("review","legacy_payment_identity_required")
        if not separate and not record.order_id and any(x["state"]=="open" for x in book["legacy"].values()):
            return result("review","legacy_unsettled_purchase_possible")
    related=resolution.get("related_id",record.related_id)
    if record.kind=="refund":
        if not related and record.order_id:
            matches=[k for k,v in book["records"].items() if v.get("order_id")==record.order_id
                     and v.get("kind")=="purchase" and v.get("state") in {"posted","linked"}]
            if len(matches)==1:related=matches[0]
            elif record.order_id in book["legacy"]:related="legacy:"+record.order_id
        if not related:return result("review","refund_purchase_link_required")
        if related.startswith("legacy:"):
            origin=book["legacy"].get(related[7:])
        else:
            origin=book["records"].get(related)
            if origin and (origin.get("kind")!="purchase" or origin.get("state") not in {"posted","linked"}):origin=None
        if not origin:return result("review","refund_purchase_link_invalid")
        refunded=-sum(v["amount"] for k,v in book["records"].items() if k!=record.money_id
            and v.get("related_id")==related and v.get("kind")=="refund" and v.get("state") in {"pending","posted"})
        if refunded-record.amount>origin["amount"]:return result("review","refund_exceeds_purchase")
    # Repeated source messages without a shared issuer reference remain visible;
    # never collapse two real same-day same-amount payments automatically.
    possible=[v for k,v in book["records"].items() if k!=record.money_id
        and v.get("source")==record.source and v.get("account")==record.account
        and v.get("day")==record.day and v.get("amount")==record.amount
        and v.get("kind")==record.kind and v.get("state") in {"pending","posted","linked"}
        and v.get("reference")!=record.reference
        and not (record.source=="au_pay_card" and record.reference.rpartition(":")[2].isdigit()
                 and str(v.get("reference","")).rpartition(":")[2].isdigit()
                 and str(v.get("reference","")).rpartition(":")[0]==record.reference.rpartition(":")[0])]
    if possible and record.reference.startswith("message:") and not separate:
        return result("review","possible_resend_requires_identity",related=related)
    details=record.items
    if not details or sum(i.amount for i in details)!=record.amount:
        details=(MoneyItem("Amazon／未分類",record.amount),)
    ids=tuple(record.money_id+f"-{i+1:03d}" for i in range(len(details)))
    return result("post","confirmed_money",ids,related)


def expense_rows(decision):
    record=decision.record
    if decision.action not in {"post","resume"}:return []
    details=record.items
    if not details or sum(i.amount for i in details)!=record.amount:
        details=(MoneyItem("Amazon／未分類",record.amount),)
    if len(details)!=len(decision.expense_ids):raise MoneyError("money_detail_plan_changed")
    note="返金確定" if record.kind=="refund" else "請求確定"
    if decision.related_id:note+="; 関連="+decision.related_id
    if record.order_id:note+="; 注文="+record.order_id
    if record.original_url:note+="; 原本="+record.original_url
    return [[identity,record.day,"Amazon",item.name,item.amount,item.major,item.minor,
             record.account,"au PAYカード" if record.source=="au_pay_card" else "Amazon金銭",
             "",record.money_id,note,"active"] for identity,item in zip(decision.expense_ids,details)]
