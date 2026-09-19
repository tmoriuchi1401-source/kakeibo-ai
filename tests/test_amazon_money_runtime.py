import base64
from dataclasses import replace

import pytest

from app.amazon_money import MoneyError
from app.amazon_money_runtime import money_enabled,money_writer,money_review_items,run_money_records
from test_amazon_money import setup,record,mail


def opt_in(monkeypatch):
    store,ledger,writer=setup()
    store.data["money"]["cutover_day"]="2026-09-01"
    monkeypatch.setenv("KAKEIBO_AMAZON_MONEY_MODE","confirmed-v1")
    monkeypatch.setattr("app.amazon_money_runtime.money_writer",lambda *a,**kw:writer)
    return store,ledger,writer


def test_mode_requires_validated_projection_and_migration_binding(monkeypatch):
    assert not money_enabled({})
    with pytest.raises(MoneyError,match="mode_invalid"):money_enabled({"KAKEIBO_AMAZON_MONEY_MODE":"true"})
    monkeypatch.setattr("app.projection_store.store_from_environment",lambda *a:None)
    with pytest.raises(MoneyError,match="projection_binding_required"):
        money_writer(type("DB",(),{"sid":"s"})(),{"KAKEIBO_AMAZON_MONEY_MODE":"confirmed-v1"})


def test_existing_amazon_runner_does_not_touch_order_or_event_sheets(monkeypatch,tmp_path):
    from test_amazon_production import authority_components,Gmail,order_mail,NOW,raw_mail
    from app.amazon_production import run_amazon_recurring
    store,ledger,writer=opt_in(monkeypatch)
    state,provider,db=authority_components(tmp_path)
    original_get=db.get
    db.get=lambda rng:(_ for _ in ()).throw(AssertionError("legacy_read_forbidden"))
    kwargs=dict(db=db,state=state,authority_provider=provider,now=NOW)
    # Order-only input advances the bounded checkpoint, without any money/event.
    result=run_amazon_recurring(gmail_service=Gmail(order_mail()),**kwargs)
    assert result["event_rows_written"]==result["header_rows_written"]==0
    assert db.write_calls==ledger.calls==[] and store.data["money"]["records"]=={}
    confirmed=raw_mail("お支払いが確定","請求金額: 1500円\n支払い方法: ギフトカード\n決済ID: payment-one")
    assert run_amazon_recurring(gmail_service=Gmail(confirmed),**kwargs)["money_posted"]==1
    assert run_amazon_recurring(gmail_service=Gmail(confirmed),**kwargs)["expense_rows_written"]==0
    assert db.write_calls==[] and len(ledger.rows["支出明細"])==1


def test_actual_card_runner_posts_unmatched_amazon_without_legacy_matching(monkeypatch,tmp_path):
    from test_aupay_card_recurring import components,run,Gmail
    store,ledger,writer=opt_in(monkeypatch)
    parts=components(tmp_path)
    body="本会員さま ご利用分\nNo.1 --------\n▼ご利用日\n2026年9月11日\n▼ご利用先\nAMAZON.CO.JP\n▼ご利用金額\n1000円"
    raw=base64.urlsafe_b64encode(mail("【ご利用詳細】au PAY カード",body,sender="info@kddi-fs.com")).decode()
    result=run(parts,Gmail(raw))
    assert result["status"]=="complete" and result["money_posted"]==1
    assert result["written"]==0 and parts[5].write_calls==0
    replay=run(parts,Gmail(raw))
    assert replay["expense_rows_written"]==0 and len(ledger.rows["支出明細"])==1


def test_card_and_amazon_notifications_of_same_payment_post_once(monkeypatch,tmp_path):
    from test_aupay_card_recurring import components,run,Gmail
    from app.amazon_money_runtime import run_amazon_money_messages
    from app.amazon_gmail_storage import GmailRawMessage
    store,ledger,writer=opt_in(monkeypatch)
    notice=mail("お支払いが確定","請求金額: 1000円\n支払い方法: Visa\n決済ID: a1")
    a=run_amazon_money_messages(writer,[GmailRawMessage("a1","",notice)],dry_run=False,limit=10)
    assert a["money_supplement"]==1 and ledger.calls==[]
    parts=components(tmp_path)
    body="本会員さま ご利用分\nNo.1 --------\n▼ご利用日\n2026年9月11日\n▼ご利用先\nAMAZON.CO.JP\n▼ご利用金額\n1000円"
    raw=base64.urlsafe_b64encode(mail("【ご利用詳細】au PAY カード",body,sender="info@kddi-fs.com")).decode()
    assert run(parts,Gmail(raw))["money_posted"]==1
    assert len(ledger.rows["支出明細"])==1


def test_money_preview_and_incomplete_card_collection_never_write_or_advance(monkeypatch,tmp_path):
    from test_aupay_card_recurring import components,run,Gmail
    store,ledger,writer=opt_in(monkeypatch)
    parts=components(tmp_path,max_batch=1)
    body="本会員さま ご利用分\nNo.1 --------\n▼ご利用日\n2026年9月11日\n▼ご利用先\nAMAZON.CO.JP\n▼ご利用金額\n1000円"
    raw=base64.urlsafe_b64encode(mail("【ご利用詳細】au PAY カード",body,sender="info@kddi-fs.com")).decode()
    assert run(parts,Gmail(raw),dry_run=True)["money_eligible"]==1
    failed=run(parts,Gmail(raw,truncated=True))
    assert failed["failure"]==1 and ledger.calls==[]
    assert parts[2].successful_window_end() is None


def test_legacy_source_identity_and_receipt_overlap_are_reviews_not_auto_merges():
    store,ledger,writer=setup();value=record()
    ledger.rows["取込データ"][value.source_id]=[value.source_id]+[""]*11
    assert writer.apply([value],limit=1)["money_review"]==1 and ledger.calls==[]
    other=record(source_id="other",reference="transaction:other")
    writer.possible_duplicates=lambda r:True
    assert writer.apply([other],limit=1)["money_review"]==1 and ledger.calls==[]
    reviews=money_review_items(store)
    assert len(reviews)==2 and all(r.status=="要確認" for r in reviews)


def test_pending_source_failure_does_not_advance_checkpoint(monkeypatch,tmp_path):
    from test_aupay_card_recurring import components,run,Gmail
    store,ledger,writer=opt_in(monkeypatch);ledger.fail_after="支出明細"
    parts=components(tmp_path)
    body="本会員さま ご利用分\nNo.1 --------\n▼ご利用日\n2026年9月11日\n▼ご利用先\nAMAZON.CO.JP\n▼ご利用金額\n1000円"
    raw=base64.urlsafe_b64encode(mail("【ご利用詳細】au PAY カード",body,sender="info@kddi-fs.com")).decode()
    result=run(parts,Gmail(raw))
    assert result["failure"]==1 and parts[2].successful_window_end() is None
    assert money_review_items(store)[0].status=="反映待ち"
    assert run(parts,Gmail(raw))["money_posted"]==1
    assert ledger.calls==[("取込データ",1),("支出明細",1)]


def test_real_overlap_adapter_refreshes_dirty_month_and_holds_receipt_candidate(monkeypatch):
    from types import SimpleNamespace
    from test_projection_refresh import initialized,row,PAIRS
    from test_amazon_money import Ledger,book
    from app.projection_store import ProjectionJournal
    purchase=row("receipt-import","2026-09-20",1000);purchase[2]="Amazon"
    store,reader,refresh=initialized([purchase]);store.data["money"]=book()
    monkeypatch.setattr("app.projection_store.store_from_environment",lambda *a:store)
    monkeypatch.setattr("app.monthly_projection_sheets.SheetsLedgerReader",lambda db:reader)
    ledger=Ledger();monkeypatch.setattr("app.amazon_money_runtime.MoneyLedger",lambda db:ledger)
    writer=money_writer(SimpleNamespace(sid="source",categories=lambda:PAIRS),{"KAKEIBO_AMAZON_MONEY_MODE":"confirmed-v1"})
    ProjectionJournal(store).mark([(2,2)])
    with pytest.raises(MoneyError,match="projection_refresh_required"):
        run_money_records(writer,[record()],dry_run=True,limit=1)
    assert ledger.calls==[]
    result=run_money_records(writer,[record()],dry_run=False,limit=1)
    assert result["money_review"]==1 and ledger.calls==[]
    assert store.data["money"]["records"][record().money_id]["reason"]=="existing_expense_identity_required"
    assert not ProjectionJournal(store).read()["ranges"]
