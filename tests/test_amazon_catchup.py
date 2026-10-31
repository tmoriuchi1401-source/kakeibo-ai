"""Offline catch-up through the existing money writer and durable state."""
import base64
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import re
from types import SimpleNamespace

import pytest

from app.amazon_money import MoneyError
from app.amazon_production import build_incremental_amazon_window, run_amazon_recurring
from app.drive_run_state import DurableState, StateBinding, StateError, envelope, snapshot, validate
from app.aupay_card_recurring import SqliteRecurringRunState
from app.production_run import DEPENDENCIES, execute_serial, run_durable_source
from app import production_flow as flow
from test_amazon_production import NOW, authority_components, raw_mail
from test_amazon_money_runtime import opt_in


def confirmed(identity, amount=1000):
    return raw_mail("お支払いが確定", f"請求金額: {amount}円\n支払い方法: ギフトカード\n決済ID: {identity}",
                    gmail_id=identity, message_id=f"<{identity}@example.invalid>")


class Request:
    def __init__(self, value): self.value = value
    def execute(self): return self.value


class Gmail:
    def __init__(self, received=(), malformed=None):
        self.received, self.malformed = received, malformed
        self.windows, self.gets = [], []
    def users(self): return self
    def messages(self): return self
    def list(self, **kw):
        start, end = (int(re.search(name + r":(\d+)", kw["q"])[1]) for name in ("after", "before"))
        self.windows.append((start, end))
        found = [{"id": m.gmail_message_id} for stamp, m in self.received if start <= stamp.timestamp() < end]
        response = {"messages": found[:kw["maxResults"]]}
        if len(found) > kw["maxResults"]: response["nextPageToken"] = "more"
        return Request(self.malformed if self.malformed is not None else response)
    def get(self, **kw):
        self.gets.append(kw["id"])
        message = next(m for _, m in self.received if m.gmail_message_id == kw["id"])
        return Request({"id": kw["id"], "raw": base64.urlsafe_b64encode(message.raw_mime).decode()})


def components(tmp_path, monkeypatch):
    state, provider, db = authority_components(tmp_path, max_messages=100)
    state.record({"run_id": "seed", "status": "noop", "source_window_start": (NOW-timedelta(days=1)).isoformat(),
                  "source_window_end": NOW.isoformat()}, advance_checkpoint=True)
    store, ledger, writer = opt_in(monkeypatch)
    return state, provider, db, store, ledger, writer


def run(parts, gmail, now, *, preview=False):
    state, provider, db, *_ = parts
    return run_amazon_recurring(gmail_service=gmail, db=db, state=state, authority_provider=provider,
                                now=now, dry_run=preview, catch_up=True)


def durable_payload(state, binding, tmp_path):
    # Only native SQLite belongs in the durable envelope, never authority files.
    native = tmp_path/"native-only"; native.mkdir()
    (native/"recurring.sqlite3").write_bytes(state.path.read_bytes())
    return envelope(binding, snapshot(native, binding))


def test_short_window_unchanged_and_long_window_only_clipped_on_explicit_catchup(tmp_path):
    state, provider, _ = authority_components(tmp_path)
    policy = provider.load()
    short = build_incremental_amazon_window(policy, state, NOW)
    assert build_incremental_amazon_window(policy, state, NOW, catch_up=True) == short
    later = NOW + timedelta(days=16)
    with pytest.raises(RuntimeError, match="window_exceeds_authority"):
        build_incremental_amazon_window(policy, state, later)
    window = build_incremental_amazon_window(policy, state, later, catch_up=True)
    assert window.start == policy.initial_start-timedelta(seconds=policy.overlap_seconds)
    assert (window.end-window.start).total_seconds() == policy.max_window_seconds == 72*3600


def test_six_windows_advance_contiguously_and_overlap_dedupes_real_money_writer(tmp_path, monkeypatch):
    parts = components(tmp_path, monkeypatch)
    state, provider, _, store, ledger, _ = parts
    clock = NOW + timedelta(days=16, hours=7)
    gmail = Gmail([(NOW+timedelta(hours=69), confirmed("first")),
                   (NOW+timedelta(hours=71), confirmed("second", 2000))])
    reports = []; previous = NOW
    while previous < clock:
        report = run(parts, gmail, clock); reports.append(report)
        current = state.successful_window_end()
        assert previous < current <= clock
        assert gmail.windows[-1][0] == int((previous-timedelta(hours=2)).timestamp())
        assert gmail.windows[-1][1] == int(current.timestamp())
        assert current-(previous-timedelta(hours=2)) <= timedelta(hours=72)
        previous = current
        assert len(reports) <= 6
    assert len(reports) == 6
    assert [r["catch_up_pending"] for r in reports] == [1, 1, 1, 1, 1, 0]
    assert sum(r["money_posted"] for r in reports) == 2
    assert sum(r["money_duplicate"] for r in reports) == 1
    assert len(ledger.rows["取込データ"]) == len(ledger.rows["支出明細"]) == 2
    assert sum(row[4] for row in ledger.rows["支出明細"].values()) == 3000
    assert len(ledger.calls) == 4
    assert all(v["state"] == "posted" for v in store.data["money"]["records"].values())
    assert provider.load().max_window_seconds == 72*3600


def test_preview_uses_disposable_native_state_and_zero_persistent_writes(tmp_path, monkeypatch):
    parts = components(tmp_path, monkeypatch)
    state, provider, db, store, ledger, _ = parts
    binding = StateBinding("amazon_gmail", db.sid, "synthetic-folder", "synthetic-file")
    class Drive:
        payload = durable_payload(state, binding, tmp_path)
        def read(self): return self.payload
        def write(self, data): raise AssertionError("preview_write_forbidden")
    drive = Drive(); before = drive.payload; money_before = deepcopy(store.data)
    def invoke(local):
        local_state = SqliteRecurringRunState(local/"recurring.sqlite3", repo_root=tmp_path/"repo")
        return run((local_state, *parts[1:]), Gmail([(NOW+timedelta(hours=1), confirmed("one"))]),
                   NOW+timedelta(days=16), preview=True)
    result = run_durable_source(DurableState(drive, binding), tmp_path/"preview-copy", invoke, apply=False)
    assert result["catch_up_pending"] == 1 and not result["checkpoint_advanced"]
    assert result["money_eligible"] == 1
    assert drive.payload == before and store.data == money_before
    assert ledger.calls == db.write_calls == []
    assert state.successful_window_end() == NOW


@pytest.mark.parametrize("malformed", [
    {"messages": [{"id": "one"}], "nextPageToken": "more"},
    {"messages": [{}]}, {"messages": [{"id": "same"}, {"id": "same"}]},
    {"messages": [{"id": str(n)} for n in range(101)]},
])
def test_incomplete_or_invalid_collection_stops_before_fetch_or_write(tmp_path, monkeypatch, malformed):
    parts = components(tmp_path, monkeypatch); gmail = Gmail(malformed=malformed)
    before = deepcopy(parts[3].data)
    with pytest.raises(RuntimeError, match="gmail_collection_incomplete"):
        run(parts, gmail, NOW+timedelta(days=16))
    assert gmail.gets == [] and gmail.windows and parts[4].calls == []
    assert parts[3].data == before and parts[0].successful_window_end() == NOW


@pytest.mark.parametrize("preview", [False, True])
def test_four_records_stop_before_notice_projection_or_financial_writes(tmp_path, monkeypatch, preview):
    parts = components(tmp_path, monkeypatch); before = deepcopy(parts[3].data)
    parts[5].prepare = lambda: (_ for _ in ()).throw(AssertionError("prepare_before_limit_check"))
    messages = [(NOW+timedelta(hours=1), confirmed(str(n))) for n in range(4)]
    with pytest.raises(MoneyError, match="batch_limit_exceeded"):
        run(parts, Gmail(messages), NOW+timedelta(days=16), preview=preview)
    assert parts[3].data == before and parts[4].calls == []
    assert parts[0].successful_window_end() == NOW


def test_three_records_per_window_and_repeated_notification_share_one_money_id(tmp_path, monkeypatch):
    parts = components(tmp_path, monkeypatch)
    messages = [(NOW+timedelta(hours=1), confirmed(str(n))) for n in range(3)]
    repeated = replace(messages[0][1], gmail_message_id="different-gmail-delivery")
    messages.append((NOW+timedelta(hours=1), repeated))
    result = run(parts, Gmail(messages), NOW+timedelta(days=16))
    assert result["money_posted"] == 3 and result["money_duplicate"] == 1
    assert len(parts[4].rows["支出明細"]) == 3


def test_exactly_100_messages_fit_collection_without_relaxing_purchase_cap(tmp_path, monkeypatch):
    parts=components(tmp_path,monkeypatch)
    message=confirmed("one")
    gmail=Gmail([(NOW,replace(message,gmail_message_id=f"notification-{n}")) for n in range(100)])
    result=run(parts,gmail,NOW+timedelta(days=16))
    assert result["found"] == 100 and result["money_posted"] == 1 and result["money_duplicate"] == 99
    assert len(gmail.gets) == 100 and len(parts[4].rows["取込データ"]) == 1


@pytest.mark.parametrize("raw",[{"id":"wrong","raw":"eA=="},{"id":"one","raw":""}])
def test_bad_raw_message_binding_stops_before_money_work(tmp_path,monkeypatch,raw):
    parts=components(tmp_path,monkeypatch);gmail=Gmail([(NOW,confirmed("one"))])
    monkeypatch.setattr(gmail,"get",lambda **kw:Request(raw))
    with pytest.raises(RuntimeError,match="gmail_collection_incomplete"):
        run(parts,gmail,NOW+timedelta(days=16))
    assert parts[4].calls == [] and parts[0].successful_window_end() == NOW


def test_unrelated_unknown_money_intent_blocks_even_empty_window(tmp_path,monkeypatch):
    parts=components(tmp_path,monkeypatch)
    parts[3].data["money"]["records"]["unrelated"]={"state":"UNKNOWN"}
    with pytest.raises(MoneyError,match="reconciliation_required"):
        run(parts,Gmail(),NOW+timedelta(days=16))
    assert parts[4].calls == [] and parts[0].successful_window_end() == NOW


def test_conflicting_amount_for_same_transaction_stops_entire_batch_before_write(tmp_path, monkeypatch):
    parts = components(tmp_path, monkeypatch); before = deepcopy(parts[3].data)
    one, other = confirmed("same", 1000), replace(confirmed("same", 2000), gmail_message_id="other")
    with pytest.raises(MoneyError, match="identity_conflict"):
        run(parts, Gmail([(NOW, one), (NOW, other)]), NOW+timedelta(days=16))
    assert parts[3].data == before and parts[4].calls == []


@pytest.mark.parametrize("title", ["取込データ", "支出明細"])
def test_lost_append_response_blocks_new_attempt_and_never_advances_checkpoint(tmp_path, monkeypatch, title):
    parts = components(tmp_path, monkeypatch); parts[4].fail_after = title
    gmail = Gmail([(NOW, confirmed("one"))])
    with pytest.raises(RuntimeError, match="lost_response"):
        run(parts, gmail, NOW+timedelta(days=16))
    before = deepcopy(parts[4].calls)
    assert parts[0].successful_window_end() == NOW
    assert next(iter(parts[3].data["money"]["records"].values()))["state"] == "pending"
    with pytest.raises(MoneyError, match="reconciliation_required"):
        run(parts, gmail, NOW+timedelta(days=16))
    assert parts[4].calls == before and parts[0].successful_window_end() == NOW


def test_remote_state_unknown_never_retries_or_invokes_source_again(tmp_path, monkeypatch):
    parts = components(tmp_path, monkeypatch); state, _, db, *_ = parts
    binding = StateBinding("amazon_gmail", db.sid, "synthetic-folder", "synthetic-file")
    class Drive:
        payload = durable_payload(state, binding, tmp_path); writes = 0
        def read(self): return self.payload
        def write(self, payload):
            self.writes += 1
            if self.writes == 2: raise StateError("state_drive_write_unknown")
            self.payload = payload
    drive = Drive(); calls = []
    def invoke(local):
        calls.append("source")
        local_state = SqliteRecurringRunState(local/"recurring.sqlite3", repo_root=tmp_path/"repo")
        return run((local_state, *parts[1:]), Gmail([(NOW, confirmed("one"))]), NOW+timedelta(days=16))
    with pytest.raises(StateError, match="write_unknown"):
        run_durable_source(DurableState(drive,binding),tmp_path/"attempt",invoke,apply=True)
    assert validate(drive.payload,binding)["phase"] == "pending"
    writes = deepcopy(parts[4].calls)
    with pytest.raises(StateError, match="reconciliation_required"):
        run_durable_source(DurableState(drive,binding),tmp_path/"blocked",invoke,apply=True)
    assert drive.writes == 2 and calls == ["source"] and parts[4].calls == writes
    assert state.successful_window_end() == NOW


def test_checkpoint_save_without_progress_is_failure_and_no_next_window(tmp_path, monkeypatch):
    parts = components(tmp_path, monkeypatch)
    monkeypatch.setattr(parts[0],"record",lambda *a,**kw:None)
    gmail = Gmail()
    with pytest.raises(RuntimeError, match="checkpoint_did_not_advance"):
        run(parts,gmail,NOW+timedelta(days=16))
    assert parts[0].successful_window_end() == NOW and len(gmail.windows) == 1
    # A future/equal checkpoint also fails before Gmail access.
    with pytest.raises(RuntimeError, match="checkpoint_did_not_advance"):
        run(parts,gmail,NOW)
    assert len(gmail.windows) == 1


def test_overlap_posted_identity_requires_exact_ledger_readback(tmp_path, monkeypatch):
    parts = components(tmp_path,monkeypatch); gmail = Gmail([(NOW+timedelta(hours=69),confirmed("one"))])
    clock = NOW+timedelta(days=16)
    run(parts,gmail,clock)
    row = next(iter(parts[4].rows["支出明細"].values())); row[4] += 1
    checkpoint = parts[0].successful_window_end(); calls = deepcopy(parts[4].calls)
    with pytest.raises(MoneyError, match="expense_changed"):
        run(parts,gmail,clock)
    assert parts[0].successful_window_end() == checkpoint and parts[4].calls == calls


def test_scope_is_only_amazon_and_partial_is_success_only_there():
    assert flow.MANUAL_SOURCE_SCOPES["amazon_catchup"] == {"amazon"}
    assert "amazon_catchup" not in flow.DRIVE_SCHEDULE_SCOPES
    assert not flow.needs_ledger_order("amazon_catchup",{},bank_apply=False)
    assert "--catch-up" in flow.command("amazon",apply=True,amazon_catchup=True)
    assert "--catch-up" not in flow.command("amazon",apply=True)
    calls = []
    runners = {name:lambda name=name:calls.append(name) or {"written":0,"catch_up_pending":1} for name in DEPENDENCIES}
    result = execute_serial(runners,selected_sources=frozenset({"amazon"}),allow_amazon_partial=True)
    assert result["success"] and result["sources"]["amazon"]["status"] == "partial"
    assert calls == ["amazon"] and result["sources"]["amazon"]["error"] == ""
    with pytest.raises(StateError,match="invalid_source_scope"):
        execute_serial(runners,selected_sources=frozenset({"amazon","bank"}),allow_amazon_partial=True)
    result = execute_serial(runners,selected_sources=frozenset({"amazon"}))
    assert not result["success"]


@pytest.mark.parametrize("flag,value",[("bank_apply",True),("amazon_target","target"),("receipt_store","store"),
    ("receipt_manifest","a"*64),("projection_bootstrap",True)])
def test_scope_rejects_other_inputs(flag,value):
    args=dict(scope="amazon_catchup",mode="apply",bank_apply=False,amazon_target="",receipt_store="",receipt_manifest="",projection_bootstrap=False)
    args[flag]=value
    with pytest.raises(StateError,match="scope_other_source_forbidden"):
        flow.validate_scope(SimpleNamespace(**args))


def test_cli_catchup_rejects_forged_clock_before_credentials(monkeypatch,capsys):
    from app import cli
    monkeypatch.setattr("sys.argv",["cli","amazon-gmail-recurring","--catch-up","--dry-run","--now",NOW.isoformat()])
    with pytest.raises(SystemExit):cli.main()
    assert "amazon_catchup_clock_override_forbidden" in capsys.readouterr().out


@pytest.mark.parametrize("limit_name,oversized",[("max_purchases",4),("max_window_seconds",73*3600)])
def test_catchup_never_widens_authority(tmp_path,monkeypatch,limit_name,oversized):
    parts=components(tmp_path,monkeypatch)
    policy=replace(parts[1].load(),**{limit_name:oversized})
    monkeypatch.setattr(parts[1],"load",lambda:policy)
    gmail=Gmail()
    with pytest.raises(RuntimeError,match="catchup_authority_invalid"):
        run(parts,gmail,NOW+timedelta(days=16))
    assert gmail.windows == [] and parts[4].calls == []


def test_failure_in_second_window_keeps_first_checkpoint_and_stops(tmp_path,monkeypatch):
    parts=components(tmp_path,monkeypatch)
    gmail=Gmail([(NOW+timedelta(hours=1),confirmed("first")),
                 (NOW+timedelta(hours=71),confirmed("second"))])
    clock=NOW+timedelta(days=16)
    assert run(parts,gmail,clock)["catch_up_pending"] == 1
    checkpoint=parts[0].successful_window_end()
    parts[4].fail_after="取込データ"
    with pytest.raises(RuntimeError,match="lost_response"):
        run(parts,gmail,clock)
    assert parts[0].successful_window_end() == checkpoint
    calls=deepcopy(parts[4].calls)
    with pytest.raises(MoneyError,match="reconciliation_required"):
        run(parts,gmail,clock)
    assert parts[4].calls == calls and parts[0].successful_window_end() == checkpoint


@pytest.mark.parametrize("mode",["preview","apply"])
def test_parent_manual_scope_runs_only_amazon_without_common_sort_or_bank_refresh(monkeypatch,capsys,tmp_path,mode):
    monkeypatch.setattr(flow.tempfile,"tempdir",str(tmp_path))
    env={"GITHUB_ACTIONS":"true","GITHUB_REF":"refs/heads/main","GITHUB_REPOSITORY":"tmoriuchi1401-source/kakeibo-ai",
         "KAKEIBO_PRODUCTION_ENABLED":"true","GITHUB_SHA":"a"*40,"KAKEIBO_VALIDATED_MAIN_SHA":"a"*40,
         "GITHUB_EVENT_NAME":"workflow_dispatch","SPREADSHEET_ID":"synthetic-sheet","KAKEIBO_STATE_FOLDER_ID":"synthetic-folder",
         "KAKEIBO_RUN_LEDGER_FILE_ID":"synthetic-ledger"}
    for key,value in env.items():monkeypatch.setenv(key,value)
    monkeypatch.setattr("sys.argv",["production_flow","--mode",mode,"--scope","amazon_catchup"])
    monkeypatch.setattr(flow.subprocess,"check_output",lambda *a,**kw:"a"*40)
    monkeypatch.setattr("app.private_state_bindings.decode_environment",lambda value,**kw:(value,""))
    monkeypatch.setattr("app.google_clients.drive_service",lambda:object())
    monkeypatch.setattr("app.google_clients.read_only_drive_service",lambda:object())
    history={name:{"phase":"ready","last_success":None,"counts":{}} for name in DEPENDENCIES}
    monkeypatch.setattr(flow,"ProductionLedger",lambda *a,**kw:SimpleNamespace(value={"sources":history},transport=object()))
    calls=[]
    def assemble(*a,**kw):
        assert kw["amazon_catchup"] and not kw["bank_apply"] and not kw["money_canary"]
        def invoke(name):
            assert name=="amazon";calls.append(name)
            return {"written":0,"catch_up_pending":1}
        return {name:lambda name=name:invoke(name) for name in DEPENDENCIES}
    monkeypatch.setattr(flow,"assemble",assemble)
    monkeypatch.setattr(flow,"preview_authority_summary",lambda *a:{})
    monkeypatch.setattr(flow,"finish_ledger_updates",lambda *a,**kw:pytest.fail("common_sort_forbidden"))
    monkeypatch.setattr("app.bank_review_refresh.refresh_bank_review_after_sources",lambda env,**kw:{} if kw["scope"]=="amazon_catchup" else pytest.fail("wrong_scope"))
    flow.main()
    import json
    report=json.loads(capsys.readouterr().out)
    assert report["success"] and report["bank_mode"]=="not_run" and calls==["amazon"]
    assert set(report["sources"])=={"amazon"}
    assert report["sources"]["amazon"]["status"] == ("success" if mode=="preview" else "partial")


def test_scope_cannot_be_invoked_by_schedule(monkeypatch,capsys):
    monkeypatch.setattr("sys.argv",["production_flow","--scope","amazon_catchup"])
    monkeypatch.setenv("GITHUB_EVENT_NAME","schedule")
    monkeypatch.setattr(flow,"verify_execution_boundary",lambda *a:None)
    monkeypatch.setattr(flow.subprocess,"check_output",lambda *a,**kw:b"a"*40)
    monkeypatch.setattr("app.private_state_bindings.decode_environment",lambda *a,**kw:pytest.fail("credentials_before_manual_guard"))
    with pytest.raises(SystemExit):flow.main()
    assert '"success": false' in capsys.readouterr().out
