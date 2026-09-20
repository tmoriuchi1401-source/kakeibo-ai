import base64
from copy import deepcopy
from dataclasses import replace

import pytest

from app.amazon_money import MoneyError
from app.amazon_money_runtime import run_money_canary
from test_amazon_money import setup,record,mail


def test_exact_target_posts_once_and_replay_reads_canonical_rows():
    store,ledger,writer=setup();first=record();second=record(reference="transaction:second",source_id="second")
    preview=run_money_canary(writer,[first,second],dry_run=True,target=second.money_id)
    assert preview["money_eligible"]==1 and ledger.calls==[]
    result=run_money_canary(writer,[first,second],dry_run=False,target=second.money_id)
    assert result["money_canary_verified"]==result["money_posted"]==1
    assert first.money_id not in store.data["money"]["records"]
    original=deepcopy(ledger.rows);writes=list(store.writes)
    replay=run_money_canary(writer,[first,second],dry_run=False,target=second.money_id)
    assert replay["money_canary_verified"]==replay["money_canary_replay"]==1
    assert replay["money_posted"]==replay["expense_rows_written"]==replay["import_rows_written"]==0
    assert ledger.rows==original and store.writes==writes
    next(iter(ledger.rows["支出明細"].values()))[4]=999
    with pytest.raises(MoneyError,match="expense_changed"):
        run_money_canary(writer,[second],dry_run=False,target=second.money_id)


@pytest.mark.parametrize("target",["","amazon-order:"+"a"*16,"AM-"+"0"*32])
def test_missing_invalid_or_absent_target_never_posts(target):
    store,ledger,writer=setup()
    with pytest.raises(MoneyError):run_money_canary(writer,[record()],dry_run=False,target=target)
    assert ledger.calls==[] and store.data["money"]["records"]=={}


@pytest.mark.parametrize("changes",[{"confirmed":False},{"source":"amazon","payment":"card"},{"kind":"transfer"},{"payment":"mixed"}])
def test_review_supplement_or_transfer_is_not_a_posting_canary(changes):
    store,ledger,writer=setup();value=record(**changes)
    with pytest.raises(MoneyError,match="not_postable"):
        run_money_canary(writer,[value],dry_run=False,target=value.money_id)
    assert ledger.calls==[]


def test_conflicting_economic_data_for_one_id_stops_before_any_post():
    store,ledger,writer=setup();value=record()
    with pytest.raises(MoneyError,match="target_conflict"):
        run_money_canary(writer,[value,replace(value,amount=2000)],dry_run=False,target=value.money_id)
    assert ledger.calls==[]


def test_explicit_writer_recovery_after_unknown_result_uses_saved_intent():
    store,ledger,writer=setup();value=record();ledger.fail_after="支出明細"
    with pytest.raises(RuntimeError):run_money_canary(writer,[value],dry_run=False,target=value.money_id)
    result=run_money_canary(writer,[value],dry_run=False,target=value.money_id)
    assert result["money_canary_verified"]==1 and result["expense_rows_written"]==0
    assert ledger.calls==[("取込データ",1),("支出明細",1)]


def test_actual_amazon_runner_preserves_checkpoint_then_regular_run_handles_rest(monkeypatch,tmp_path):
    from test_amazon_money_runtime import opt_in
    from test_amazon_production import authority_components,Gmail,raw_mail,NOW
    from app.amazon_production import run_amazon_recurring
    from app.amazon_money_mail import amazon_money_from_mail
    store,ledger,writer=opt_in(monkeypatch)
    state,authority,db=authority_components(tmp_path)
    message=raw_mail("お支払いが確定","請求金額: 1500円\n支払い方法: ギフトカード\n決済ID: selected")
    value=amazon_money_from_mail(message.raw_mime,gmail_id=message.gmail_message_id)
    kwargs=dict(db=db,state=state,authority_provider=authority,now=NOW)
    assert run_amazon_recurring(gmail_service=Gmail(message),approved_reference=value.money_id,**kwargs)["money_canary_verified"]==1
    assert state.successful_window_end() is None
    replay=run_amazon_recurring(gmail_service=Gmail(message),approved_reference=value.money_id,**kwargs)
    assert replay["expense_rows_written"]==0 and state.successful_window_end() is None
    other=raw_mail("お支払いが確定","請求金額: 500円\n支払い方法: ギフトカード\n決済ID: other",message_id="<other@example.invalid>")
    assert run_amazon_recurring(gmail_service=Gmail(other),**kwargs)["money_posted"]==1
    assert state.successful_window_end() is not None and len(ledger.rows["支出明細"])==2


def test_actual_card_canary_ignores_other_money_and_ordinary_candidates_without_checkpoint(monkeypatch,tmp_path):
    from test_amazon_money_runtime import opt_in
    from test_aupay_card_recurring import components,Gmail,NOW
    from app.aupay_card_recurring import run_recurring_ingestion
    from app.amazon_money_mail import card_money_from_mail
    store,ledger,writer=opt_in(monkeypatch)
    parts=components(tmp_path)
    body="本会員さま ご利用分\n"+"\n".join(f"No.{i} --------\n▼ご利用日\n2026年9月11日\n▼ご利用先\n{merchant}\n▼ご利用金額\n{amount}円" for i,merchant,amount in [(1,"AMAZON.CO.JP",1000),(2,"AMAZON.CO.JP",500),(3,"合成店舗",100)])
    raw=mail("【ご利用詳細】au PAY カード",body,sender="info@kddi-fs.com")
    records,_=card_money_from_mail(raw,gmail_id="gmail-1");assert len(records)==2
    encoded=base64.urlsafe_b64encode(raw).decode()
    def run(**extra):
        repo,directory,state,key,authority,db=parts
        return run_recurring_ingestion(gmail_service=Gmail(encoded),db=db,state=state,key_provider=key,
            authority_provider=authority,state_dir=directory,repo_root=repo,now=NOW,sleeper=lambda _:None,**extra)
    preview=run(money_canary=True,dry_run=True)
    assert preview["money_eligible"]==2 and parts[2].successful_window_end() is None and ledger.calls==[]
    result=run(money_canary=True,money_target=records[0].money_id)
    assert result["money_canary_verified"]==1 and parts[5].write_calls==0
    assert parts[2].successful_window_end() is None and records[1].money_id not in store.data["money"]["records"]
    assert run(money_canary=True,money_target=records[0].money_id)["expense_rows_written"]==0
    normal=run()
    assert normal["money_posted"]==1 and normal["written"]==1 and parts[2].successful_window_end() is not None


def test_card_scope_runs_only_card_and_cannot_be_combined_with_normal_mode():
    from app.production_run import execute_serial,DEPENDENCIES
    from app.production_flow import command,validate_scope
    from app.drive_run_state import StateError
    from types import SimpleNamespace
    seen=[]
    report=execute_serial({s:lambda s=s:seen.append(s) or {"money_canary_verified":1} for s in DEPENDENCIES},amazon_canary=True,canary_source="aupay_card")
    assert report["success"] and seen==["aupay_card"]
    with pytest.raises(StateError):execute_serial({},canary_source="aupay_card")
    with pytest.raises(StateError):validate_scope(SimpleNamespace(scope="all",canary_source="aupay_card"))
    with pytest.raises(StateError):command("amazon",apply=True,money_canary=True)
    assert command("aupay_card",apply=False,money_canary=True)[-1]=="--money-canary"


@pytest.mark.parametrize("event,mode",[("schedule","confirmed-v1"),("workflow_dispatch","")])
def test_parent_canary_requires_manual_dispatch_and_migration_before_intake(monkeypatch,capsys,event,mode):
    from app import production_flow as flow
    from test_production_flow import valid_env
    monkeypatch.setattr(flow.sys,"argv",["production_flow","--scope","amazon_canary","--mode","preview"])
    for key,value in dict(valid_env(),GITHUB_EVENT_NAME=event,KAKEIBO_AMAZON_MONEY_MODE=mode).items():monkeypatch.setenv(key,value)
    monkeypatch.setattr(flow.subprocess,"check_output",lambda *args,**kw:"a"*40)
    monkeypatch.setattr("app.private_state_bindings.decode_environment",lambda env,**kw:(env,""))
    monkeypatch.setattr("app.google_clients.read_only_drive_service",lambda:pytest.fail("guard must run before private state reads"))
    monkeypatch.setattr(flow,"assemble",lambda *args,**kw:pytest.fail("guard must run before intake"))
    with pytest.raises(SystemExit) as error:flow.main()
    assert error.value.code==1 and '"success": false' in capsys.readouterr().out
