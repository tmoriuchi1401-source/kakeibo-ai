from copy import deepcopy
from dataclasses import replace
from email.message import EmailMessage

import pytest

from app.amazon_money import (MoneyError,MoneyItem,MoneyRecord,decide,empty_book,expense_rows,source_alias)
from app.amazon_money_mail import amazon_money_from_mail,card_money_from_mail
from app.amazon_money_writer import MoneyWriter
from test_projection_refresh import Store


def record(**kw):
    args=dict(source="au_pay_card",source_id="source-1",account="card-1",reference="transaction:charge-1",
        day="2026-09-20",amount=1000,kind="purchase",payment="card",confirmed=True)
    return MoneyRecord(**(args|kw))


def book(legacy=None):return empty_book(cutover_day="2026-09-19",legacy=legacy or {})


class Ledger:
    def __init__(self):self.rows={"取込データ":{},"支出明細":{}};self.calls=[];self.fail_after=""
    def find(self,title,width,identities):
        return {k:deepcopy(v) for k,v in self.rows[title].items() if k in identities}
    def append(self,title,rows):
        assert not any(r[0] in self.rows[title] for r in rows)
        self.calls.append((title,len(rows)))
        self.rows[title].update({r[0]:deepcopy(r) for r in rows})
        if self.fail_after==title:self.fail_after="";raise RuntimeError("lost_response")


def setup(legacy=None):
    store=Store();store.data["money"]=book(legacy);ledger=Ledger()
    return store,ledger,MoneyWriter(store,ledger)


def mail(subject,body,*,sender="no-reply@amazon.co.jp",message_id="<one@example.invalid>"):
    msg=EmailMessage();msg["From"]=sender;msg["Subject"]=subject
    msg["Message-ID"]=message_id;msg["Date"]="Tue, 01 Sep 2026 00:30:00 +0900";msg.set_content(body)
    return msg.as_bytes()


@pytest.mark.parametrize("subject,body",[
    ("ご注文の確認","注文合計: 1000円"),("発送しました","請求金額: 1000円"),
    ("配達完了","ご請求額: 1000円"),("返品リクエストを受け付けました","返金予定額: 1000円"),
    ("返金のお知らせ","返金予定額: 1000円")])
def test_nonfinal_amazon_mail_creates_no_record(subject,body):
    assert amazon_money_from_mail(mail(subject,body),gmail_id="g1") is None


def test_amazon_refund_requires_actual_amount_not_expected_refund():
    with pytest.raises(MoneyError,match="confirmed_amount_missing"):
        amazon_money_from_mail(mail("返金が完了","返金予定額: 1000円"),gmail_id="g1")


def test_amazon_card_money_is_supplement_only_and_gift_is_separate():
    for kind,amount in [("purchase",1000),("refund",-1000)]:
        assert decide(record(source="amazon",kind=kind,amount=amount),book()).action=="supplement"
    assert decide(record(source="amazon",payment="gift_balance"),book()).action=="post"
    assert decide(record(source="amazon",payment="mixed"),book()).action=="review"


def test_strict_final_card_adapter_excludes_preliminary_and_uses_refund_confirmation_month():
    body="本会員さま ご利用分\nNo.1 --------\n▼ご利用日\n2026年8月15日\n▼ご利用先\nAMAZON.CO.JP\n▼ご利用金額\n-300円 (返品)"
    raw=mail("【ご利用詳細】au PAY カード",body,sender="info@kddi-fs.com")
    rows,reviews=card_money_from_mail(raw,gmail_id="g1")
    assert len(rows)==1 and reviews==0
    assert (rows[0].day,rows[0].amount,rows[0].kind)==("2026-09-01",-300,"refund")
    assert card_money_from_mail(mail("【ご利用速報】au PAY カード",body,sender="info@kddi-fs.com"),gmail_id="g1")==([],0)
    with pytest.raises(MoneyError,match="sender_invalid"):
        card_money_from_mail(mail("【ご利用詳細】au PAY カード",body),gmail_id="g1")


def test_final_amazon_parser_keeps_refund_and_charge_references_separate():
    charge=amazon_money_from_mail(mail("お支払いが確定", "支払い方法: ギフトカード\n請求金額: 1000円\n決済ID: c1"),gmail_id="g1")
    refund=amazon_money_from_mail(mail("返金が完了", "返金方法: ギフトカード\n返金額: 300円\n返金ID: r1"),gmail_id="g2")
    assert charge.amount==1000 and refund.amount==-300
    assert charge.money_id!=refund.money_id


def test_split_charges_and_multiple_partial_refunds_keep_original_and_separate_ids():
    store,ledger,writer=setup()
    first=record(order_id="order");second=record(source_id="source-2",reference="transaction:c2",order_id="order",amount=500)
    writer.apply([first,second],limit=10)
    original=deepcopy(ledger.rows["支出明細"])
    refunds=[record(source_id="r"+str(i),reference="transaction:r"+str(i),day="2026-10-01",
                    kind="refund",amount=-value,related_id=first.money_id) for i,value in enumerate([200,300])]
    assert writer.apply(refunds,limit=10)["money_posted"]==2
    assert len(ledger.rows["支出明細"])==4
    assert all(ledger.rows["支出明細"][k]==v for k,v in original.items())
    assert sum(r[4] for r in ledger.rows["支出明細"].values())==1000
    assert writer.apply([first,second,*refunds],limit=10)["expense_rows_written"]==0


def test_same_day_amount_never_forces_order_or_source_matching():
    store,ledger,writer=setup()
    first=record(reference="message:one");second=record(source_id="two",reference="message:two")
    writer.apply([first],limit=10)
    result=writer.apply([second],limit=10)
    assert result["money_review"]==1 and len(ledger.rows["支出明細"])==1
    assert store.data["money"]["records"][second.money_id]["reason"]=="possible_resend_requires_identity"


def test_distinct_issuer_reference_allows_identical_real_charges():
    _,ledger,writer=setup()
    assert writer.apply([record(),record(source_id="two",reference="transaction:two")],limit=10)["money_posted"]==2
    assert len(ledger.rows["支出明細"])==2


def test_distinct_lines_in_one_issuer_notice_are_separate_payments():
    _,ledger,writer=setup()
    first=record(reference="message:<issuer@example.invalid>:1")
    second=record(source_id="source-2",reference="message:<issuer@example.invalid>:2")
    assert writer.apply([first,second],limit=2)["money_posted"]==2
    assert len(ledger.rows["支出明細"])==2


def test_card_payment_that_may_fund_own_balance_is_not_extra_expense():
    _,ledger,writer=setup()
    writer.apply([record(source="amazon",kind="transfer",payment="gift_balance")],limit=1)
    assert writer.apply([record()],limit=1)["money_review"]==1
    assert ledger.calls==[]


def test_legacy_order_is_not_reposted_and_exact_alias_preserves_past_values():
    legacy={"old-order":{"state":"open","amount":1000,"expense_ids":["existing"]}}
    store,ledger,writer=setup(legacy)
    value=record(order_id="old-order")
    assert writer.apply([value],limit=10)["money_review"]==1
    assert ledger.calls==[]
    ledger.rows["支出明細"]["existing"]=["existing","2025-01-01","Amazon","本人の商品",1000,"本人分類","本人小分類","card","Amazon","","old-order","","active"]
    store.data["money"]["aliases"][source_alias(value)]={"fingerprint":value.fingerprint,"expense_ids":["existing"],"expense_amounts":{"existing":1000}}
    assert writer.apply([value],limit=10)["money_linked"]==1 and ledger.calls==[]


def test_legacy_alias_cannot_claim_missing_or_changed_canonical_expense():
    store,ledger,writer=setup();value=record()
    store.data["money"]["aliases"][source_alias(value)]={"fingerprint":value.fingerprint,"expense_ids":["missing"],"expense_amounts":{"missing":1000}}
    assert writer.apply([value],limit=1)["money_review"]==1
    assert store.data["money"]["records"][value.money_id]["reason"]=="legacy_ledger_binding_changed"
    assert ledger.calls==[]


def test_unlinked_legacy_exposure_and_pre_cutover_records_require_identity():
    legacy={"old-order":{"state":"open","amount":1000,"expense_ids":["existing"]}}
    assert decide(record(),book(legacy)).reason=="legacy_unsettled_purchase_possible"
    assert decide(record(day="2026-09-01"),book()).reason=="before_cutover_identity_required"


def test_item_unavailability_does_not_block_money_and_discounts_are_not_double_counted():
    value=record(items=(MoneyItem("商品",1200),MoneyItem("値引き",-200)))
    rows=expense_rows(decide(value,book()))
    assert sum(r[4] for r in rows)==1000 and len(rows)==2
    value=replace(value,items=(MoneyItem("取得できた一部",400),))
    rows=expense_rows(decide(value,book()))
    assert len(rows)==1 and rows[0][3:5]==["Amazon／未分類",1000]


@pytest.mark.parametrize("title",["取込データ","支出明細"])
def test_unknown_append_result_recovers_without_second_posting(title):
    store,ledger,writer=setup();ledger.fail_after=title
    with pytest.raises(RuntimeError):writer.apply([record()],limit=1)
    assert store.data["money"]["records"][record().money_id]["state"]=="pending"
    assert writer.apply([record()],limit=1)["money_posted"]==1
    assert ledger.calls==[("取込データ",1),("支出明細",1)]
    assert writer.apply([record()],limit=1)["money_duplicate"]==1


def test_conflicting_posting_identity_fails_without_overwriting_human_edits():
    store,ledger,writer=setup();ledger.fail_after="支出明細"
    with pytest.raises(RuntimeError):writer.apply([record()],limit=1)
    row=next(iter(ledger.rows["支出明細"].values()));row[6]="本人分類"
    with pytest.raises(MoneyError,match="content_conflict"):writer.apply([record()],limit=1)
    assert row[6]=="本人分類" and len(ledger.calls)==2


def test_identity_conflict_and_excess_refund_are_not_posted():
    store,ledger,writer=setup();value=record();writer.apply([value],limit=1)
    with pytest.raises(MoneyError,match="identity_conflict"):writer.apply([replace(value,amount=999)],limit=1)
    refund=record(source_id="refund",reference="transaction:refund",kind="refund",amount=-1001,related_id=value.money_id)
    assert writer.apply([refund],limit=1)["money_review"]==1
    assert len(ledger.rows["支出明細"])==1


def test_transfer_is_saved_without_expense_and_confirmation_is_required():
    _,ledger,writer=setup()
    assert writer.apply([record(kind="transfer")],limit=1)["money_transfer"]==1 and ledger.calls==[]
    assert decide(record(confirmed=False),book()).reason=="confirmation_missing"


def test_money_ledger_identity_scan_has_no_5000_row_cutoff_and_detects_duplicates():
    import re
    from types import SimpleNamespace
    from app.amazon_money_writer import MoneyLedger
    reads=[];duplicate=False
    def get_raw(rng):
        reads.append(rng)
        first,last=map(int,re.search(r"!A(\d+):[A-Z](\d+)",rng).groups())
        if ":M" in rng:return [["target"]+[""]*12]
        return [["target" if n==6501 or (duplicate and n==3) else "id-"+str(n)] for n in range(first,last+1)]
    service=SimpleNamespace(spreadsheets=lambda:SimpleNamespace(get=lambda **kw:SimpleNamespace(
        execute=lambda **kw:{"sheets":[{"properties":{"title":"支出明細","gridProperties":{"rowCount":7001}}}]})))
    db=SimpleNamespace(sid="source",svc=service,get_raw=get_raw,
        _execute_sheet_read=lambda fn:fn().execute(num_retries=0))
    ledger=MoneyLedger(db)
    assert ledger.find("支出明細",13,{"target"})=={"target":["target"]+[""]*12}
    assert len(reads)==5 and reads[-1]=="'支出明細'!A6501:M6501"
    duplicate=True
    with pytest.raises(MoneyError,match="duplicate_ledger_identity"):
        ledger.find("支出明細",13,{"target"})
