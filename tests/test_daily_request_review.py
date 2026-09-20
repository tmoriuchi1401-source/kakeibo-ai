from copy import deepcopy

import pytest

from app.amazon_money import MoneyError
from app.amazon_money_review import MoneyReviews
from app.daily_request_review import review_items,KINDS
from app.daily_view import review_page,render_requests,SHEETS
from app.projection_refresh import load_catalog
from test_amazon_money import setup
from test_amazon_money_review import REQ

OLD="REQ-"+"2"*32


def failure():return {"state":"failed","error":"latest_values_changed","expense_id":"EXP-old",
    "updated_at":"2018-01-01T00:00:00Z","changes":{"amount":100},"form_digest":"saved-input"}


def configured(kind="corrections"):
    store,ledger,writer=setup()
    store.data[kind]={"requests":{OLD:failure()}}
    if kind=="coverage":store.data[kind].update(routes={},months={})
    return store,ledger,MoneyReviews(writer),f"RQ-{kind}:{OLD}"


def test_all_period_requests_are_paged_with_stable_ids_and_safe_error_labels():
    store,ledger,writer=setup()
    for kind in KINDS:
        store.data[kind]={"requests":{f"REQ-{i:032x}":failure() for i in range(53)}}
    items=review_items(store,"daily")
    assert len(items)==212 and len({x.fixed_id for x in items})==212
    assert review_page(items,5)[1:]==(212,5) and len(review_page(items,5)[0])==12
    assert "2018-01-01" in items[0].detail
    assert "元の値が変わりました" in items[0].detail
    assert all("/d/daily/" in item.url for item in items)
    assert ledger.calls==[] and store.writes==[]


@pytest.mark.parametrize("kind",list(KINDS))
def test_acknowledgement_preserves_failed_request_and_does_not_retry(kind):
    store,ledger,inbox,reference=configured(kind)
    original=deepcopy(store.data[kind]["requests"][OLD])
    inbox.prepare(REQ,reference,"acknowledge_failure")
    assert inbox.apply(REQ)["state"]=="applied"
    assert store.data[kind]["requests"][OLD]==original
    assert review_items(store,"daily")==[] and ledger.calls==[]
    before=deepcopy(store.data);inbox.apply(REQ);assert store.data==before
    # A later result change must not be hidden by an old acknowledgement.
    store.data[kind]["requests"][OLD]["updated_at"]="2026-09-20"
    assert review_items(store,"daily")[0].fixed_id==reference


@pytest.mark.parametrize("state",["queued","pending","applied"])
def test_only_failures_can_be_acknowledged(state):
    store,ledger,inbox,reference=configured()
    store.data["corrections"]["requests"][OLD]["state"]=state
    with pytest.raises(MoneyError,match="target_invalid"):inbox.prepare(REQ,reference,"acknowledge_failure")
    assert ledger.calls==[]


@pytest.mark.parametrize("action",["hold","separate","link","refund_link","transfer"])
def test_failure_id_cannot_be_used_for_money_or_automatic_retry(action):
    store,ledger,inbox,reference=configured()
    with pytest.raises(MoneyError,match="no_retry"):inbox.prepare(REQ,reference,action)
    assert ledger.calls==[]


def test_changed_failure_between_selection_and_apply_stays_visible():
    store,ledger,inbox,reference=configured()
    inbox.prepare(REQ,reference,"acknowledge_failure")
    store.data["corrections"]["requests"][OLD]["error"]="category_changed"
    assert inbox.apply(REQ)["state"]=="failed"
    assert reference in {r.fixed_id for r in review_items(store,"daily")}
    assert ledger.calls==[]


@pytest.mark.parametrize("after_save",[False,True])
def test_unknown_ack_save_can_resume_without_changing_original_or_posting(after_save):
    store,ledger,inbox,reference=configured("money-reviews")
    inbox.prepare(REQ,reference,"acknowledge_failure")
    original=deepcopy(store.data["money-reviews"]["requests"][OLD])
    store.fail_key="money-reviews";store.after_save=after_save
    with pytest.raises(RuntimeError):inbox.apply(REQ)
    assert inbox.apply(REQ)["state"]=="applied"
    assert store.data["money-reviews"]["requests"][OLD]==original
    assert review_items(store,"daily")==[] and ledger.calls==[]


def test_daily_form_ack_keeps_user_inputs_and_never_calls_ledger():
    from test_daily_money_review import configured as form_setup
    store,ledger,writer,grid,daily,form=form_setup()
    store.data["corrections"]={"requests":{OLD:failure()}}
    reference="RQ-corrections:"+OLD
    grid.put(81,2,reference);grid.put(82,2,"失敗を確認済みにする（再実行なし）")
    original=form.form()[0][:4]
    assert form.submit()["money_reviews_submitted"]==1
    assert form.form()[0][:4]==original and ledger.calls==[]
    assert grid.data[(SHEETS["確認"][0],85,1)]=="確認済み（再実行なし）"
    assert review_items(store,"daily")==[]


def test_renderer_exposes_failed_request_choice_without_overwriting_inputs():
    from test_projection_refresh import initialized,row
    store,reader,refresh=initialized([row("a")])
    store.data["corrections"]={"requests":{OLD:failure()}}
    items=review_items(store,"daily")
    requests=render_requests(read_month=refresh.read_month,summary=store.data["summary"],
        catalog=load_catalog(store.data["catalog"]),current_month="2026-09",source_id="source",
        controls={},reviews=items,updated_at="now")
    choice=next(r["setDataValidation"] for r in requests if r.get("setDataValidation",{}).get("range",{}).get("startRowIndex")==80)
    assert choice["rule"]["condition"]["values"]==[{"userEnteredValue":items[0].fixed_id}]
    for request in requests:
        body=request.get("updateCells",{});target=body.get("range",{})
        if target.get("sheetId")==SHEETS["確認"][0]:assert target["endRowIndex"]<=56


def test_existing_daily_refresh_includes_request_status_without_submitting(monkeypatch):
    from unittest.mock import Mock
    from app.daily_runtime import refresh_daily
    store,ledger,inbox,reference=configured()
    daily=Mock();daily.db.sid="daily";daily.refresh.return_value={"daily_changed_blocks":1}
    monkeypatch.setattr("app.daily_runtime.daily_from_environment",lambda *args:daily)
    monkeypatch.setattr("app.daily_sheets.read_existing_reviews",lambda db:[])
    monkeypatch.setattr("app.amazon_money_runtime.money_review_items",lambda store:[])
    source=Mock();source._sheet_metadata.return_value={"sheets":[{"properties":{"title":"支出明細","sheetId":987}}]}
    assert refresh_daily(source,store,{})=={"daily_changed_blocks":1}
    assert daily.refresh.call_args.kwargs["reviews"][0].fixed_id==reference
    daily.submit.assert_not_called()
    assert store.writes==[] and ledger.calls==[]
