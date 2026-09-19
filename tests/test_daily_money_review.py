from copy import deepcopy
from hashlib import sha256

import pytest

from app.amazon_money_review import MoneyReviews
from app.daily_money_review import DailyMoneyForm, MARKER, run_money_reviews, install_requests
from app.daily_sheets import DailySheets
from app.daily_view import SHEETS, render_requests
from test_daily_sheets import Grid
from test_amazon_money_review import exception, REQ


def configured():
    store,ledger,writer,value,inbox=exception()
    grid=Grid();daily=DailySheets(grid,"source",store)
    daily.verify=lambda:{"developerMetadata":[{"metadataKey":MARKER,"metadataValue":sha256(b"source").hexdigest()}]}
    form=DailyMoneyForm(daily,inbox)
    for row,text in [(81,value.money_id),(82,"別の確定取引として計上"),(85,True)]:grid.put(row,2,text)
    grid.put(81,10,REQ)
    return store,ledger,writer,grid,daily,form


def test_money_form_posts_once_preserves_inputs_and_replays_no_changes():
    store,ledger,writer,grid,daily,form=configured()
    original=form.form()[0][:4]
    assert form.submit()["money_reviews_submitted"]==1
    assert form.form()[0][:4]==original and form.form()[0][4] is False
    assert form.submit()=={"money_reviews_submitted":0}
    assert ledger.calls==[("取込データ",1),("支出明細",1)]
    assert grid.data[(SHEETS["確認"][0],85,1)]=="反映済み"


def test_money_form_unknown_ack_does_not_duplicate_expense():
    store,ledger,writer,grid,daily,form=configured()
    grid.on_write=lambda:setattr(grid,"fail_after",len(grid.writes)==2)
    with pytest.raises(RuntimeError):form.submit()
    assert form.submit()=={"money_reviews_submitted":0}
    assert len(ledger.rows["支出明細"])==1


def test_money_form_user_edit_during_post_is_not_silently_resubmitted():
    store,ledger,writer,grid,daily,form=configured()
    append=ledger.append
    def edit(title,rows):
        append(title,rows)
        if title=="支出明細":grid.put(82,2,"保留")
    ledger.append=edit
    assert form.submit()["money_review_input_changed"]==1
    assert form.form()[0][1]=="保留" and form.form()[0][4] is True
    form.submit()
    assert form.form()[0][1]=="保留" and form.form()[0][4] is False
    assert "未送信" in grid.data[(SHEETS["確認"][0],85,1)]
    assert len(ledger.rows["支出明細"])==1


def test_invalid_money_form_has_a_visible_failure_and_keeps_user_text():
    store,ledger,writer,grid,daily,form=configured();grid.put(81,2,"missing")
    form.submit()
    assert form.form()[0][0]=="missing" and form.form()[0][4] is False
    assert "失敗" in grid.data[(SHEETS["確認"][0],85,1)]
    assert ledger.calls==[]


def test_connected_money_review_preview_and_pending_recovery(monkeypatch):
    store,ledger,writer,grid,daily,form=configured()
    env={"KAKEIBO_AMAZON_MONEY_MODE":"confirmed-v1"}
    monkeypatch.setattr("app.amazon_money_runtime.money_writer",lambda *args:writer)
    before=deepcopy(store.data)
    assert run_money_reviews(daily,None,env,apply=False)=={"money_reviews_pending":0,"money_review_form_ready":1}
    assert before==store.data and grid.writes==[] and ledger.calls==[]
    ledger.fail_after="取込データ"
    with pytest.raises(RuntimeError):run_money_reviews(daily,None,env,apply=True)
    assert run_money_reviews(daily,None,env,apply=True)["money_reviews_applied"]==1
    assert run_money_reviews(daily,None,env,apply=True)["money_reviews_applied"]==0
    assert ledger.calls==[("取込データ",1),("支出明細",1)]
    assert run_money_reviews(None,None,{},apply=True)=={}


def test_install_only_authors_the_reserved_small_form_and_marker():
    requests=install_requests("source")
    for req in requests:
        if "updateCells" not in req:continue
        target=req["updateCells"]["range"]
        assert target["sheetId"]==SHEETS["確認"][0] and target["startRowIndex"]>=79 and target["endRowIndex"]<=88
    assert requests[-1]["createDeveloperMetadata"]["developerMetadata"]["metadataKey"]==MARKER


def test_form_upgrade_requires_blank_range_and_valid_binding():
    from app.amazon_money import MoneyError
    from app.daily_money_review import upgrade_requests
    store,ledger,writer,grid,daily,form=configured()
    assert upgrade_requests(daily)==[]
    daily.verify=lambda:{"developerMetadata":[]}
    with pytest.raises(MoneyError,match="occupied"):upgrade_requests(daily)
    daily.read_ranges=lambda *args,**kwargs:[[],[]]
    assert upgrade_requests(daily)[-1]["createDeveloperMetadata"]["developerMetadata"]["metadataKey"]==MARKER
    daily.verify=lambda:{"developerMetadata":[{"metadataKey":MARKER,"metadataValue":"wrong"}]}
    with pytest.raises(MoneyError,match="migration_required"):upgrade_requests(daily)
