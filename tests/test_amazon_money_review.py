from copy import deepcopy
import json

import pytest

from app.amazon_money import MoneyError,MoneyItem,decide
from app.amazon_money_review import MoneyReviews
from app.amazon_money_runtime import money_review_items
from test_amazon_money import setup,record

REQ="REQ-"+"1"*32


def exception(**kwargs):
    store,ledger,writer=setup({"old":{"state":"open","amount":1000,"expense_ids":["legacy"]}})
    value=record(**kwargs)
    writer.apply([value],limit=1)
    return store,ledger,writer,value,MoneyReviews(writer)


def test_explicit_separate_payment_posts_once_without_treating_same_amount_as_identity():
    store,ledger,writer,value,inbox=exception(items=(MoneyItem("商品",1000),))
    # Simulate actual Drive JSON serialization; tuples cannot sneak through a
    # memory-only test and fail the production compare/readback.
    store.data=json.loads(json.dumps(store.data))
    inbox.prepare(REQ,value.money_id,"separate")
    assert inbox.apply(REQ)["state"]=="applied"
    assert inbox.read()["requests"][REQ]["state"]=="applied"
    assert len(ledger.rows["支出明細"])==1
    assert next(iter(ledger.rows["支出明細"].values()))[3]=="商品"
    assert inbox.apply(REQ)["state"]=="applied" and len(ledger.calls)==2
    assert writer.apply([value],limit=1)["expense_rows_written"]==0


def test_hold_survives_rescan_and_can_be_replaced_by_a_new_request():
    store,ledger,writer,value,inbox=exception()
    inbox.prepare(REQ,value.money_id,"hold");inbox.apply(REQ)
    writer.apply([value],limit=1)
    assert money_review_items(store)[0].status=="保留" and ledger.calls==[]
    second="REQ-"+"2"*32
    inbox.prepare(second,value.money_id,"separate");inbox.apply(second)
    assert money_review_items(store)==[]


def test_split_charges_link_existing_group_without_changing_date_category_or_amount():
    store,ledger,writer,first,inbox=exception(amount=400)
    original=["legacy","2026-08-01","Amazon","本人分類",1000,"食費","外食","old","Amazon","","old-import","old-note","active"]
    ledger.rows["支出明細"]["legacy"]=deepcopy(original)
    inbox.prepare(REQ,first.money_id,"link",expense_ids=("legacy",));inbox.apply(REQ)
    second=record(source_id="second",reference="transaction:second",amount=600)
    writer.apply([second],limit=1)
    inbox.prepare("REQ-"+"2"*32,second.money_id,"link",expense_ids=("legacy",));inbox.apply("REQ-"+"2"*32)
    assert ledger.rows["支出明細"]=={"legacy":original} and ledger.calls==[]
    assert store.data["money"]["legacy"]["old"]["state"]=="settled"
    third=record(source_id="third",reference="transaction:third",amount=1,order_id="old")
    writer.apply([third],limit=1)
    with pytest.raises(MoneyError,match="link_capacity"):
        inbox.prepare("REQ-"+"3"*32,third.money_id,"link",expense_ids=("legacy",))


def test_latest_existing_expense_change_prevents_stale_link_approval():
    store,ledger,writer,value,inbox=exception()
    ledger.rows["支出明細"]["legacy"]=["legacy","2026-08-01","Amazon","商品",1000,"その他","未分類","old","Amazon","","old","","active"]
    inbox.prepare(REQ,value.money_id,"link",expense_ids=("legacy",))
    ledger.rows["支出明細"]["legacy"][5]="本人変更"
    assert inbox.apply(REQ)["state"]=="failed" and ledger.calls==[]
    assert store.data["money"]["aliases"]=={}


def test_refund_selection_uses_canonical_origin_and_cumulative_limit():
    store,ledger,writer=setup();purchase=record()
    writer.apply([purchase],limit=1)
    refund=record(source_id="refund",reference="transaction:refund",kind="refund",amount=-300,day="2026-10-01")
    writer.apply([refund],limit=1)
    inbox=MoneyReviews(writer);inbox.prepare(REQ,refund.money_id,"refund_link",related_id=purchase.money_id)
    original=deepcopy(ledger.rows["支出明細"])
    assert inbox.apply(REQ)["state"]=="applied"
    assert all(ledger.rows["支出明細"][k]==v for k,v in original.items())
    assert sum(row[4] for row in ledger.rows["支出明細"].values())==700
    too_much=record(source_id="r2",reference="transaction:r2",kind="refund",amount=-800)
    writer.apply([too_much],limit=1)
    with pytest.raises(MoneyError,match="still_unresolved"):
        inbox.prepare("REQ-"+"2"*32,too_much.money_id,"refund_link",related_id=purchase.money_id)


def test_daily_amount_edit_of_origin_is_not_overruled_by_old_money_book():
    store,ledger,writer=setup();purchase=record();writer.apply([purchase],limit=1)
    next(iter(ledger.rows["支出明細"].values()))[4]=900
    refund=record(source_id="r",reference="transaction:r",kind="refund",amount=-100,related_id=purchase.money_id)
    assert writer.apply([refund],limit=1)["money_review"]==1
    assert store.data["money"]["records"][refund.money_id]["reason"]=="refund_origin_ledger_changed"


def test_refunds_share_canonical_limit_across_legacy_and_split_charge_ids():
    store,ledger,writer,first,inbox=exception(amount=400)
    ledger.rows["支出明細"]["legacy"]=["legacy","2026-08-01","Amazon","商品",1000,"その他","未分類","old","Amazon","","old","","active"]
    inbox.prepare(REQ,first.money_id,"link",expense_ids=("legacy",));inbox.apply(REQ)
    refund=record(source_id="r1",reference="transaction:r1",kind="refund",amount=-800,related_id="legacy:old")
    assert writer.apply([refund],limit=1)["money_posted"]==1
    second=record(source_id="r2",reference="transaction:r2",kind="refund",amount=-300,related_id=first.money_id)
    assert writer.apply([second],limit=1)["money_review"]==1
    assert store.data["money"]["records"][second.money_id]["reason"]=="refund_exceeds_purchase"
    assert sum(row[4] for row in ledger.rows["支出明細"].values())==200


@pytest.mark.parametrize("fail_title",["取込データ","支出明細"])
def test_unknown_post_result_resumes_immutable_intent_without_duplicate_rows(fail_title):
    store,ledger,writer,value,inbox=exception()
    inbox.prepare(REQ,value.money_id,"separate")
    ledger.fail_after=fail_title
    with pytest.raises(RuntimeError):inbox.apply(REQ)
    assert inbox.read()["requests"][REQ]["state"]=="pending"
    assert MoneyReviews(writer).apply(REQ)["state"]=="applied"
    assert len(ledger.rows["支出明細"])==len(ledger.rows["取込データ"])==1
    assert ledger.calls==[("取込データ",1),("支出明細",1)]


@pytest.mark.parametrize("key",["money","money-reviews"])
@pytest.mark.parametrize("after_save",[False,True])
def test_review_document_failure_recovers_before_or_after_durable_save(key,after_save):
    store,ledger,writer,value,inbox=exception()
    inbox.prepare(REQ,value.money_id,"separate")
    store.fail_key=key;store.after_save=after_save
    with pytest.raises(RuntimeError):inbox.apply(REQ)
    store.data=json.loads(json.dumps(store.data))
    assert MoneyReviews(writer).apply(REQ)["state"]=="applied"
    assert ledger.calls==[("取込データ",1),("支出明細",1)]
    assert inbox.apply(REQ)["state"]=="applied" and len(ledger.calls)==2


@pytest.mark.parametrize("kwargs",[{"confirmed":False},{"source":"amazon","payment":"mixed"},
    {"source":"amazon","payment":"unknown"}])
def test_confirmation_and_payment_authority_cannot_be_overridden(kwargs):
    store,ledger,writer,value,inbox=exception(**kwargs)
    with pytest.raises(MoneyError):inbox.prepare(REQ,value.money_id,"separate")
    assert ledger.calls==[]


def test_confirmed_own_balance_funding_does_not_create_expense():
    store,ledger,writer,value,inbox=exception()
    inbox.prepare(REQ,value.money_id,"transfer");inbox.apply(REQ)
    assert store.data["money"]["records"][value.money_id]["state"]=="transfer"
    assert ledger.calls==[]
