from copy import deepcopy
from hashlib import sha256
import json

import pytest

from app.daily_coverage import Coverage,CoverageForm,MARKER,run_coverage,upgrade_requests,render_requests
from app.daily_sheets import DailySheets
from app.daily_view import coverage_status
from app.monthly_projection import compare_years,ProjectionError
from app.projection_store import ProjectionJournal
from test_projection_refresh import initialized,row
from test_daily_sheets import Grid

TOKEN="REQ-"+"a"*32


def setup(rows=None):
    store,reader,refresh=initialized([row("one")] if rows is None else rows)
    return store,reader,refresh,Coverage(store)


def submit(inbox,*,start="2026-08",end="2026-08",route="カード",account="メイン",status="完了",token=TOKEN):
    inbox.prepare(token,[start,end,route,account,status],current_month="2026-09",form_digest="test")
    return inbox.apply(token)


def test_completed_empty_months_are_zero_but_unknown_months_are_absent():
    store,reader,refresh,inbox=setup()
    original=deepcopy(reader.rows);reads=list(reader.reads)
    assert submit(inbox,start="2025-08",end="2026-08")["state"]=="applied"
    summary=store.data["summary"]
    assert summary["months"]["2026-08"]["amount"]==0
    assert "2025-07" not in summary["months"]
    assert summary["months"]["2026-09"]["amount"]==100
    assert reader.rows==original and reader.reads==reads
    coverage={(m,k):v for m,states in summary["coverage"].items() for k,v in states.items()}
    compared=compare_years(2026,"2026-09",{m:v["amount"] for m,v in summary["months"].items()},coverage,summary["required_routes"])
    assert compared.months==(8,) and compared.current_average==0
    before=deepcopy(store.data);writes=list(store.writes);index_reads=store.reads.count("index")
    inbox.apply(TOKEN);inbox.sync()
    assert store.data==before and store.writes==writes and store.reads.count("index")==index_reads


def test_existing_completed_input_survives_bootstrap_and_new_real_purchase_survives_reopening():
    store,reader,refresh,inbox=setup()
    submit(inbox)
    reader.rows.append(row("late","2026-08-01",200))
    ProjectionJournal(store).mark(append=True);refresh.refresh([("食費","食料品")])
    submit(inbox,status="未確認",token="REQ-"+"b"*32)
    assert store.data["summary"]["months"]["2026-08"]["amount"]==200
    original=deepcopy(store.data["coverage"])
    refresh.bootstrap([("食費","食料品")]);inbox.sync()
    assert store.data["coverage"]==original


def test_lost_summary_rebuild_restores_user_completion_and_empty_month():
    store,reader,refresh,inbox=setup()
    submit(inbox)
    original=deepcopy(store.data["coverage"])
    del store.data["summary"]
    refresh.bootstrap([("食費","食料品")])
    assert store.data["coverage"]==original
    assert coverage_status(store.data["summary"],"2026-08")=="完了"
    assert store.data["summary"]["months"]["2026-08"]["amount"]==0


def test_daily_runtime_runs_coverage_without_expense_mutation(monkeypatch):
    from test_daily_apply_runtime import configured
    from app.daily_runtime import run_daily_requests
    env,daily,grid,ledger,reader,refresh=configured(monkeypatch)
    daily.verify=lambda:{"developerMetadata":[{"metadataKey":MARKER,"metadataValue":sha256(b"source").hexdigest()}]}
    grid.put(68,2,False)
    for r,v in enumerate(["2026-08","2026-08","カード","メイン","完了",True],11):grid.put(r,2,v,title="設定")
    grid.put(11,6,TOKEN,title="設定")
    assert run_daily_requests(env,apply=True)["coverage_applied"]==1
    assert run_daily_requests(env,apply=True)["coverage_applied"]==0 and ledger.calls==[]


def test_new_route_and_reopened_month_remove_inferred_zeros_without_erasing_real_totals():
    store,reader,refresh,inbox=setup()
    submit(inbox)
    submit(inbox,route="現金",status="未確認",token="REQ-"+"b"*32)
    summary=store.data["summary"]
    assert "2026-08" not in summary["months"] and coverage_status(summary,"2026-08")!="完了"
    submit(inbox,route="現金",status="対象外",token="REQ-"+"c"*32)
    assert coverage_status(store.data["summary"],"2026-08")=="完了"
    submit(inbox,status="取込中",token="REQ-"+"d"*32)
    assert "2026-08" not in store.data["summary"]["months"]
    assert store.data["summary"]["months"]["2026-09"]["amount"]==100


def test_oldest_120_month_range_year_boundary_and_current_month_excluded_from_comparison():
    store,reader,refresh,inbox=setup([])
    assert submit(inbox,start="2016-10",end="2026-09")["state"]=="applied"
    assert len(inbox.read()["months"])==120
    summary=store.data["summary"]
    states={(m,k):v for m,s in summary["coverage"].items() for k,v in s.items()}
    compared=compare_years(2026,"2026-09",{m:v["amount"] for m,v in summary["months"].items()},states,summary["required_routes"])
    assert compared.months==tuple(range(1,9))


@pytest.mark.parametrize("changes",[{"start":"bad"},{"start":"2026-09","end":"2026-08"},
    {"end":"2026-10"},{"start":"2016-08"},{"route":""},{"account":""},{"status":"done"}])
def test_invalid_input_is_visible_failure_without_changing_coverage_or_summary(changes):
    store,reader,refresh,inbox=setup();summary=deepcopy(store.data["summary"])
    assert submit(inbox,**changes)["state"]=="failed"
    assert inbox.read()["routes"]=={} and inbox.read()["months"]=={} and store.data["summary"]==summary


@pytest.mark.parametrize("after_save",[False,True])
@pytest.mark.parametrize("key",["coverage","summary"])
def test_durable_input_or_summary_save_failure_is_recoverable(key,after_save):
    store,reader,refresh,inbox=setup()
    inbox.prepare(TOKEN,["2026-08","2026-08","カード","メイン","完了"],current_month="2026-09",form_digest="test")
    store.fail_key=key;store.after_save=after_save
    with pytest.raises(RuntimeError):inbox.apply(TOKEN)
    store.data=json.loads(json.dumps(store.data))
    assert Coverage(store).apply(TOKEN)["state"]=="applied"
    assert store.data["summary"]["months"]["2026-08"]["amount"]==0


def test_stale_month_request_is_rejected_and_dirty_ledger_cannot_prove_empty_month():
    store,reader,refresh,inbox=setup()
    inbox.prepare(TOKEN,["2026-08","2026-08","カード","メイン","完了"],current_month="2026-09",form_digest="test")
    submit(inbox,status="取込中",token="REQ-"+"b"*32)
    assert inbox.apply(TOKEN)["error"]=="coverage_changed"
    ProjectionJournal(store).mark(append=True)
    with pytest.raises(ProjectionError,match="projection_pending"):
        submit(inbox,token="REQ-"+"c"*32)
    refresh.refresh([("食費","食料品")])
    assert inbox.apply("REQ-"+"c"*32)["state"]=="applied"


def form_setup():
    store,reader,refresh,inbox=setup();grid=Grid();daily=DailySheets(grid,"source",store)
    daily.verify=lambda:{"developerMetadata":[{"metadataKey":MARKER,"metadataValue":sha256(b"source").hexdigest()}]}
    for r,v in enumerate(["2026-08","2026-08","カード","メイン","完了",True],11):grid.put(r,2,v,title="設定")
    grid.put(11,6,TOKEN,title="設定")
    return store,reader,grid,daily,CoverageForm(daily)


def test_real_form_preview_submit_replay_preserve_inputs_and_no_accounting():
    store,reader,grid,daily,form=form_setup();before=deepcopy(store.data);values=form.form()[0][:5]
    assert run_coverage(daily,apply=False,current_month="2026-09")=={"coverage_pending":0,"coverage_form_ready":1}
    assert store.data==before and grid.writes==[]
    assert run_coverage(daily,apply=True,current_month="2026-09")["coverage_applied"]==1
    assert form.form()[0][:5]==values and form.form()[0][5] is False
    assert run_coverage(daily,apply=True,current_month="2026-09")["coverage_submitted"]==0
    assert len(reader.rows)==1


def test_ack_unknown_and_midflight_user_edits_do_not_lose_or_auto_submit_new_input():
    store,reader,grid,daily,form=form_setup()
    original=form.inbox.apply
    def edit(token):
        result=original(token);grid.put(15,2,"取込中",title="設定");return result
    form.inbox.apply=edit
    assert form.submit("2026-09")["coverage_input_changed"]==1
    form.inbox.apply=original
    grid.on_write=lambda:setattr(grid,"fail_after",True)
    with pytest.raises(RuntimeError):form.submit("2026-09")
    grid.on_write=None
    assert form.submit("2026-09")=={"coverage_submitted":0}
    assert form.form()[0][4]=="取込中" and form.form()[0][5] is False
    assert coverage_status(store.data["summary"],"2026-08")=="完了"


def test_renderer_pages_every_route_and_never_writes_inputs():
    routes={str(i):{"route":"カード","account":str(i)} for i in range(105)}
    summary={"coverage_routes":routes,"coverage":{"2026-08":{"0":"complete"}}}
    requests=render_requests(summary,"2026-09",{"coverage_month":"2026-08","coverage_page":3})
    authored=[r["updateCells"] for r in requests if "updateCells" in r]
    assert all(r["range"]["startRowIndex"]>=23 for r in authored)
    body=next(r for r in authored if r["range"]["startRowIndex"]==25)
    assert sum(bool(r["values"][0]) for r in body["rows"])==5


def test_upgrade_does_not_overwrite_existing_input_or_wrong_binding():
    store,reader,grid,daily,form=form_setup()
    assert upgrade_requests(daily,current_month="2026-09")==[]
    daily.verify=lambda:{"developerMetadata":[]}
    with pytest.raises(ProjectionError,match="occupied"):upgrade_requests(daily,current_month="2026-09")
    grid.data={}
    assert upgrade_requests(daily,current_month="2026-09")[-1]["createDeveloperMetadata"]["developerMetadata"]["metadataKey"]==MARKER


def test_existing_coverage_input_requires_migration_instead_of_silent_replacement():
    store,reader,refresh,inbox=setup()
    store.data["summary"]["required_routes"]=["existing-card"]
    store.data["summary"]["coverage"]={"2020-01":{"existing-card":"complete"}}
    before=deepcopy(store.data)
    with pytest.raises(ProjectionError,match="existing_input_migration_required"):submit(inbox)
    assert before==store.data
