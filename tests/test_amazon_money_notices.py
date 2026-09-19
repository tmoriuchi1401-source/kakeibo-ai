import base64
from copy import deepcopy

import pytest

from app.amazon_gmail_storage import GmailRawMessage
from app.amazon_money import MoneyError
from app.amazon_money_mail import amazon_money_outcome,card_money_outcome
from app.amazon_money_notices import save_notices,read_notices
from app.amazon_money_review import MoneyReviews
from app.amazon_money_runtime import run_amazon_money_messages,money_review_items
from test_amazon_money import setup,mail
from test_amazon_money_review import REQ


def raw_notice():return mail("返金が完了","返金予定額: 1000円\n原文は保存しない")


def value():return amazon_money_outcome(raw_notice(),gmail_id="g1")[1]


def test_amazon_unparsed_final_is_durable_before_normal_checkpoint_without_made_up_amount():
    store,ledger,writer=setup()
    store.data["money"]["cutover_day"]="2026-09-01"
    messages=[GmailRawMessage("g1","",raw_notice()),GmailRawMessage("g2","",mail("お支払いが確定",
        "請求金額: 1500円\n支払い方法: ギフトカード\n決済ID: actual",message_id="<actual@example.invalid>"))]
    before=deepcopy(store.data)
    preview=run_amazon_money_messages(writer,messages,dry_run=True,limit=10)
    assert preview["money_notice_review"]==1 and store.data==before and ledger.calls==[]
    result=run_amazon_money_messages(writer,messages,dry_run=False,limit=10)
    assert result["money_posted"]==result["money_notice_review"]==1
    assert len(ledger.rows["支出明細"])==1
    saved=read_notices(store)["notices"]
    assert len(saved)==1 and "原文は保存しない" not in str(saved) and "amount" not in next(iter(saved.values()))
    writes=list(store.writes)
    assert run_amazon_money_messages(writer,messages,dry_run=False,limit=10)["expense_rows_written"]==0
    assert writes==store.writes
    assert money_review_items(store)[0].fixed_id.startswith("MN-")


@pytest.mark.parametrize("subject",["ご注文の確認","発送しました","配達完了","返品リクエストを受け付けました"])
def test_nonfinal_mail_has_no_notice_accumulation(subject):
    record,notice=amazon_money_outcome(mail(subject,"金額: 1000円"),gmail_id="g1")
    assert record is notice is None


@pytest.mark.parametrize("subject",["返金が完了していません","返金処理が完了するまでお待ちください",
    "お支払いが確定する予定です","請求が確定しておりません"])
def test_negative_or_future_confirmation_does_not_create_money_or_notice(subject):
    record,notice=amazon_money_outcome(mail(subject,"返金額: 1000円\n請求金額: 1000円\n支払い方法: ギフトカード"),gmail_id="g1")
    assert record is notice is None


def test_canary_does_not_persist_unrelated_notices():
    store,ledger,writer=setup()
    store.data["money"]["cutover_day"]="2026-09-01"
    raw=mail("お支払いが確定","請求金額: 1500円\n支払い方法: ギフトカード\n決済ID: actual")
    record,_=amazon_money_outcome(raw,gmail_id="g2")
    result=run_amazon_money_messages(writer,[GmailRawMessage("g1","",raw_notice()),GmailRawMessage("g2","",raw)],
        dry_run=False,limit=1,canary_target=record.money_id)
    assert result["money_canary_verified"]==1 and "money-notices" not in store.data


def card_raw():
    body="本会員さま ご利用分\nNo.1 --------\n▼ご利用日\n2026年9月11日\n▼ご利用先\nAMAZON.CO.JP\n▼ご利用金額\n1000円\nNo.2 --------\n▼ご利用日\n2026年9月11日\n▼ご利用先\nAMAZON.CO.JP\n▼ご利用金額\n不明"
    return mail("【ご利用詳細】au PAY カード",body,sender="info@kddi-fs.com")


def test_card_partial_keeps_valid_charge_and_queues_unparsed_reference():
    records,notice=card_money_outcome(card_raw(),gmail_id="g1")
    assert len(records)==1 and records[0].amount==1000
    assert notice["reason"]=="money_card_partial"
    assert card_money_outcome(mail("【ご利用速報】au PAY カード","AMAZON.CO.JP",sender="info@kddi-fs.com"),gmail_id="g1")==([],None)


def test_card_incomplete_collection_does_not_persist_notices_or_money(monkeypatch,tmp_path):
    from test_amazon_money_runtime import opt_in
    from test_aupay_card_recurring import components,run,Gmail
    store,ledger,writer=opt_in(monkeypatch);parts=components(tmp_path,max_batch=1)
    result=run(parts,Gmail(base64.urlsafe_b64encode(card_raw()).decode(),truncated=True))
    assert result["status"]=="failed" and parts[2].successful_window_end() is None
    assert "money-notices" not in store.data and ledger.calls==[]


def test_card_whole_message_parse_rejection_is_visible_after_checkpoint(monkeypatch,tmp_path):
    from test_amazon_money_runtime import opt_in
    from test_aupay_card_recurring import components,run,Gmail
    store,ledger,writer=opt_in(monkeypatch);parts=components(tmp_path)
    raw=mail("【ご利用詳細】au PAY カード","AMAZON.CO.JP\n形式が未対応",sender="info@kddi-fs.com")
    result=run(parts,Gmail(base64.urlsafe_b64encode(raw).decode()))
    assert result["money_notice_review"]==1 and parts[2].successful_window_end() is not None
    assert money_review_items(store) and ledger.calls==[]


@pytest.mark.parametrize("after_save",[False,True])
def test_actual_card_source_keeps_checkpoint_when_notice_save_is_unknown(monkeypatch,tmp_path,after_save):
    from test_amazon_money_runtime import opt_in
    from test_aupay_card_recurring import components,run,Gmail
    store,ledger,writer=opt_in(monkeypatch);parts=components(tmp_path)
    raw=base64.urlsafe_b64encode(card_raw()).decode()
    store.fail_key="money-notices";store.after_save=after_save
    assert run(parts,Gmail(raw))["status"]=="failed"
    assert parts[2].successful_window_end() is None and ledger.calls==[]
    result=run(parts,Gmail(raw))
    assert result["money_notice_review"]==result["money_posted"]==1
    assert parts[2].successful_window_end() is not None
    assert run(parts,Gmail(raw))["expense_rows_written"]==0
    assert len(read_notices(store)["notices"])==1 and len(ledger.rows["支出明細"])==1


@pytest.mark.parametrize("after_save",[False,True])
def test_actual_amazon_source_cannot_advance_past_unpersisted_notice(monkeypatch,tmp_path,after_save):
    from test_amazon_money_runtime import opt_in
    from test_amazon_production import authority_components,Gmail,NOW,raw_mail
    from app.amazon_production import run_amazon_recurring
    store,ledger,writer=opt_in(monkeypatch);state,provider,db=authority_components(tmp_path)
    args=dict(db=db,state=state,authority_provider=provider,now=NOW)
    message=raw_mail("返金が完了","返金予定額: 1000円")
    store.fail_key="money-notices";store.after_save=after_save
    with pytest.raises(RuntimeError):run_amazon_recurring(gmail_service=Gmail(message),**args)
    assert state.successful_window_end() is None
    assert run_amazon_recurring(gmail_service=Gmail(message),**args)["money_notice_review"]==1
    assert state.successful_window_end() is not None and ledger.calls==[]


@pytest.mark.parametrize("action",["separate","link","transfer","refund_link"])
def test_notice_cannot_be_forced_into_money(action):
    store,ledger,writer=setup();notice=value();save_notices(store,[notice],dry_run=False)
    with pytest.raises(MoneyError,match="no_posting"):MoneyReviews(writer).prepare(REQ,notice["notice_id"],action)
    assert ledger.calls==[] and store.data["money"]["records"]=={}


def test_hold_and_ack_are_replayable_and_rescan_preserves_human_decision():
    store,ledger,writer=setup();notice=value();save_notices(store,[notice],dry_run=False);inbox=MoneyReviews(writer)
    inbox.prepare(REQ,notice["notice_id"],"hold");assert inbox.apply(REQ)["state"]=="applied"
    assert money_review_items(store)[0].status=="保留"
    save_notices(store,[notice],dry_run=False);assert money_review_items(store)[0].status=="保留"
    second="REQ-"+"2"*32
    inbox.prepare(second,notice["notice_id"],"acknowledge");inbox.apply(second)
    save_notices(store,[notice],dry_run=False)
    assert money_review_items(store)==[] and ledger.calls==[] and store.data["money"]["records"]=={}
    old=deepcopy(store.data);inbox.apply(second);assert store.data==old


@pytest.mark.parametrize("after_save",[False,True])
def test_notice_decision_unknown_write_recovers_pending_without_accounting(after_save):
    store,ledger,writer=setup();notice=value();save_notices(store,[notice],dry_run=False);inbox=MoneyReviews(writer)
    inbox.prepare(REQ,notice["notice_id"],"acknowledge")
    store.fail_key="money-notices";store.after_save=after_save
    with pytest.raises(RuntimeError):inbox.apply(REQ)
    assert inbox.read()["requests"][REQ]["state"]=="pending"
    assert inbox.apply(REQ)["state"]=="applied" and money_review_items(store)==[] and ledger.calls==[]


def test_changed_notice_reopens_and_stale_human_request_is_rejected():
    store,ledger,writer=setup();notice=value();save_notices(store,[notice],dry_run=False);inbox=MoneyReviews(writer)
    inbox.prepare(REQ,notice["notice_id"],"acknowledge")
    changed={**notice,"fingerprint":"f"*64};save_notices(store,[changed],dry_run=False)
    assert inbox.apply(REQ)["state"]=="failed" and money_review_items(store)
    assert ledger.calls==[]


def test_existing_daily_form_accepts_notice_and_preserves_input():
    from test_daily_money_review import configured
    store,ledger,writer,grid,daily,form=configured();notice=value();save_notices(store,[notice],dry_run=False)
    grid.put(81,2,notice["notice_id"]);grid.put(82,2,"通知を確認済みにする（記帳なし）")
    original=form.form()[0][:4]
    assert form.submit()["money_reviews_submitted"]==1
    assert form.form()[0][:4]==original and ledger.calls==[]
    assert read_notices(store)["notices"][notice["notice_id"]]["state"]=="acknowledged"
