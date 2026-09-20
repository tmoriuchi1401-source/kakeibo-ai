from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.monthly_projection import ProjectionError, history_page, shift_month
from app.projection_cache import CACHE_KEYS, RollingProjectionStore, cache_key, initial_files
from app.projection_refresh import ProjectionRefresh
from app.projection_store import DriveProjectionStore
from test_projection_refresh import Store, Reader, PAIRS, row
from test_projection_store import Drive


def initialized(rows, month="2026-09"):
    backing, reader = Store(), Reader(rows)
    store = RollingProjectionStore(backing,current_month=month)
    refresh = ProjectionRefresh(store,reader)
    refresh.bootstrap(PAIRS)
    backing.reads.clear();backing.writes.clear();reader.reads.clear()
    return backing,store,reader,refresh


def provision(drive):
    for i, item in enumerate(initial_files("source"),1):
        drive.rows[str(i)] = {k:v for k,v in item.items() if k != "payload"} | {"id":str(i)}
        drive.payloads[str(i)] = json.dumps(item["payload"]).encode()


def test_fixed_files_work_without_service_account_create_or_private_app_metadata():
    drive = Drive();provision(drive)
    backing = DriveProjectionStore(drive,"folder","source",precreated=True)
    store = RollingProjectionStore(backing,current_month="2026-09")
    reader = Reader([row("old","2010-01-01"),row("new")])
    refresh = ProjectionRefresh(store,reader)
    refresh.bootstrap(PAIRS)
    assert len(drive.rows) == 24 and drive.create_count == 0
    assert store.read("summary")["months"]["2010-01"]["amount"] == 100
    assert store.read("month-2010-01") is None
    next_store = RollingProjectionStore(backing,current_month="2026-10")
    ProjectionRefresh(next_store,reader).refresh(PAIRS)
    assert len(drive.rows) == 24 and drive.create_count == 0
    assert next_store.read("month-2026-10")["purchases"] == []


def test_unprovisioned_or_wrong_source_file_cannot_be_created_or_overwritten():
    drive = Drive();backing = DriveProjectionStore(drive,"folder","source",precreated=True)
    with pytest.raises(ProjectionError,match="precreated_file_required"):
        backing.write("journal",{})
    assert drive.create_count == 0
    provision(drive)
    identity = next(k for k,v in drive.rows.items() if v["name"] == "kakeibo-projection-journal.json")
    value = json.loads(drive.payloads[identity]);value["binding"] = "wrong"
    drive.payloads[identity] = json.dumps(value).encode();before = deepcopy(drive.payloads)
    with pytest.raises(ProjectionError,match="file_invalid"):
        backing.write("journal",{})
    assert drive.payloads == before


def test_cache_retains_full_summary_and_only_thirteen_months_of_detail():
    rows = [row(f"old-{i}",shift_month("2026-09",-i)+"-01",i+1) for i in range(240)]
    backing,store,reader,refresh = initialized(rows)
    assert len(backing.data["summary"]["months"]) == 240
    assert {k for k in backing.data if k.startswith("cache-")} == set(CACHE_KEYS)
    assert not any(k.startswith("month-") for k in backing.data)
    result = history_page(refresh.read_month,current_month="2026-09")
    assert result.total == 13 and reader.reads == []
    assert set(backing.reads) == set(CACHE_KEYS)
    assert sum(v["amount"] for v in backing.data["summary"]["months"].values()) == sum(range(1,241))


def test_old_correction_updates_totals_without_evicting_current_detail():
    backing,store,reader,refresh = initialized([row("old","2010-01-01"),row("new")])
    cached = {k:deepcopy(v) for k,v in backing.data.items() if k in CACHE_KEYS}
    refresh.journal.mark([(2,2)]);reader.rows[0][4] = 200
    assert refresh.refresh(PAIRS)["projection_months"] == 1
    assert backing.data["summary"]["months"]["2010-01"]["amount"] == 200
    assert all(backing.data[k] == v for k,v in cached.items())
    assert set(reader.reads) == {(2,2)}
    assert not any(k in CACHE_KEYS for k in backing.writes)


@pytest.mark.parametrize("before,after",[("2010-01-01","2026-09-01"),("2026-09-01","2010-01-01")])
@pytest.mark.parametrize("fail_key",[cache_key("2026-09"),"index","summary"])
@pytest.mark.parametrize("after_save",[False,True])
def test_date_move_across_cache_boundary_recovers_after_each_failed_save(before,after,fail_key,after_save):
    backing,store,reader,refresh = initialized([row("moving",before)])
    refresh.journal.mark([(2,2)]);reader.rows[0][1] = after
    backing.fail_key,backing.after_save = fail_key,after_save
    with pytest.raises(RuntimeError):refresh.refresh(PAIRS)
    restarted = ProjectionRefresh(RollingProjectionStore(backing,current_month="2026-09"),reader)
    restarted.refresh(PAIRS)
    summary = backing.data["summary"]["months"]
    assert summary[before[:7]]["amount"] == 0 and summary[after[:7]]["amount"] == 100
    assert restarted.read_month("2026-09").amount == (100 if after[:7] == "2026-09" else 0)
    backing.writes.clear();reader.reads.clear()
    assert restarted.refresh(PAIRS)["projection_months"] == 0
    assert backing.writes == [] and reader.reads == [] and len(reader.rows) == 1


@pytest.mark.parametrize("advance",[1,13,48])
def test_rollover_needs_no_ledger_write_and_preserves_all_old_totals(advance):
    backing,store,reader,refresh = initialized([row("old","2025-09-01"),row("new")])
    old_totals = deepcopy(backing.data["summary"]["months"])
    month = shift_month("2026-09",advance)
    next_store = RollingProjectionStore(backing,current_month=month)
    next_refresh = ProjectionRefresh(next_store,reader)
    result = next_refresh.refresh(PAIRS)
    assert result["projection_months"] == min(advance,13)
    assert reader.reads == [] and backing.data["summary"]["months"] == old_totals
    assert backing.data["summary"]["cache_month"] == month
    assert all(next_store.read("month-"+m) is not None for m in next_store.cache_months)
    assert len([k for k in backing.data if k in CACHE_KEYS]) == 13


@pytest.mark.parametrize("after_save",[False,True])
def test_month_rollover_unknown_response_reuses_same_slot(after_save):
    backing,store,reader,refresh = initialized([row("old","2025-09-01"),row("future","2026-10-01")])
    backing.fail_key,backing.after_save = cache_key("2026-10"),after_save
    next_store = RollingProjectionStore(backing,current_month="2026-10")
    with pytest.raises(RuntimeError):ProjectionRefresh(next_store,reader).refresh(PAIRS)
    ProjectionRefresh(RollingProjectionStore(backing,current_month="2026-10"),reader).refresh(PAIRS)
    assert next_store.read("month-2026-10")["amount"] == 100
    assert backing.data["summary"]["months"]["2025-09"]["amount"] == 100
    assert len(reader.rows) == 2


def test_empty_slots_are_not_completed_zero_months_and_coverage_survives_rebuild():
    from app.daily_coverage import Coverage
    backing,store,reader,refresh = initialized([])
    assert backing.data["summary"]["months"] == {}
    assert all(store.read("month-"+m)["amount"] == 0 for m in store.cache_months)
    backing.data["coverage"] = {"routes":{"r":{"route":"card","account":"one"}},
        "months":{"2020-01":{"r":"complete"}},"requests":{}}
    Coverage(store).sync()
    refresh.bootstrap(PAIRS)
    assert backing.data["summary"]["months"]["2020-01"]["amount"] == 0
    assert backing.data["summary"]["coverage"]["2020-01"]["r"] == "complete"


def test_historical_money_overlap_uses_only_requested_month_not_disappearing_cache(monkeypatch):
    from app.amazon_money_runtime import money_writer,run_money_records
    from test_amazon_money import Ledger,book,record
    purchase = row("receipt","2010-01-20",1000);purchase[2] = "Amazon"
    backing,store,reader,refresh = initialized([purchase,row("irrelevant")])
    backing.data["money"] = book();backing.data["money"]["cutover_day"] = "2000-01-01"
    monkeypatch.setattr("app.projection_store.store_from_environment",lambda *args:store)
    monkeypatch.setattr("app.monthly_projection_sheets.SheetsLedgerReader",lambda db:reader)
    ledger = Ledger();monkeypatch.setattr("app.amazon_money_runtime.MoneyLedger",lambda db:ledger)
    writer = money_writer(SimpleNamespace(sid="source",categories=lambda:PAIRS),{"KAKEIBO_AMAZON_MONEY_MODE":"confirmed-v1"})
    result = run_money_records(writer,[record(day="2010-01-20")],dry_run=False,limit=1)
    assert result["money_review"] == 1 and ledger.calls == []
    assert set(reader.reads) == {(2,2)}


def test_slot_month_and_source_validation_never_return_another_month():
    backing,store,reader,refresh = initialized([row("one")])
    slot = cache_key("2026-09")
    backing.data[slot]["month"] = "2025-08"
    assert cache_key("2025-08") == slot
    with pytest.raises(ProjectionError,match="cache_invalid"):store.read("month-2026-09")
    with pytest.raises(ProjectionError,match="outside_window"):store.write("month-2010-01",{})


@pytest.mark.parametrize("problem", ["stale", "missing"])
def test_daily_never_publishes_missing_cache_as_empty_history(problem):
    from app.daily_sheets import DailySheets
    from test_daily_sheets import Grid
    backing,store,reader,refresh = initialized([row("one")])
    if problem == "stale":backing.data["summary"]["cache_month"] = "2026-08"
    else:backing.data.pop(cache_key("2026-09"))
    grid = Grid();daily = DailySheets(grid,"source",store);daily.verify = lambda:None
    grid.put(65,2,"本人の入力")
    before = deepcopy(grid.data)
    with pytest.raises(ProjectionError,match="cache_refresh_required"):
        daily.refresh(current_month="2026-09",updated_at="synthetic",reviews=[])
    assert grid.data == before and grid.writes == [] and reader.reads == []
