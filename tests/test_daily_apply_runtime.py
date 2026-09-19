from copy import deepcopy
from unittest.mock import Mock

import pytest

from app.daily_runtime import run_daily_requests
from app.daily_corrections import DailyCorrections
from app.monthly_projection import ProjectionError
from test_daily_sheets import daily_setup
from test_daily_edit_cutover import metadata, WRITER
from test_daily_corrections import REQUEST
from test_projection_refresh import PAIRS
from test_production_flow import valid_env


def configured(monkeypatch):
    daily,grid,ledger,reader=daily_setup()
    from app.projection_refresh import ProjectionRefresh
    refresh=ProjectionRefresh(daily.store,reader)
    ledger.sid="source";ledger.svc=Mock()
    ledger.svc.spreadsheets().get().execute.return_value=metadata()
    ledger._execute_sheet_read=lambda factory:factory().execute()
    ledger.categories=lambda:PAIRS
    monkeypatch.setattr("app.settings.service_account_source",lambda:("",{"client_email":WRITER}))
    monkeypatch.setattr("app.projection_store.store_from_environment",lambda *args:daily.store)
    monkeypatch.setattr("app.sheets.SheetsDB",lambda *args,**kwargs:ledger)
    monkeypatch.setattr("app.google_clients.sheets_service",lambda:object())
    monkeypatch.setattr("app.google_clients.read_only_sheets_service",lambda:object())
    monkeypatch.setattr("app.daily_runtime.daily_from_environment",lambda *args,**kwargs:daily)
    monkeypatch.setattr("app.monthly_projection_sheets.SheetsLedgerReader",lambda db:reader)
    monkeypatch.setattr("app.projection_refresh.ProjectionRefresh",lambda *args:refresh)
    monkeypatch.setattr("app.daily_runtime.refresh_daily",lambda *args:{"daily_changed_blocks":0})
    env=dict(valid_env(),SPREADSHEET_ID="source",KAKEIBO_DAILY_CORRECTIONS_MODE="fixed-id-v1")
    return env,daily,grid,ledger,reader,refresh


def test_disabled_and_preview_never_write_or_submit(monkeypatch):
    assert run_daily_requests({},apply=True)=={}
    env,daily,grid,ledger,reader,refresh=configured(monkeypatch)
    before=deepcopy(daily.store.data)
    assert run_daily_requests(env)=={"corrections_pending":0,"correction_form_ready":1}
    assert daily.store.data==before and ledger.calls==[] and grid.writes==[]


def test_actual_form_to_canonical_to_changed_months_and_replay(monkeypatch):
    env,daily,grid,ledger,reader,refresh=configured(monkeypatch)
    grid.put(62,2,"2026-01-01")
    result=run_daily_requests(env,apply=True)
    assert result["corrections_applied"]==1
    assert refresh.read_month("2025-12").amount==0
    assert refresh.read_month("2026-01").amount==80
    assert len(ledger.calls)==1 and len(reader.rows)==2
    again=run_daily_requests(env,apply=True)
    assert again["corrections_applied"]==again["corrections_submitted"]==0
    assert len(ledger.calls)==1


def test_unknown_canonical_write_resumes_pending_even_if_form_is_unchecked(monkeypatch):
    env,daily,grid,ledger,reader,refresh=configured(monkeypatch)
    ledger.fail_after=True
    with pytest.raises(RuntimeError):run_daily_requests(env,apply=True)
    assert daily.store.data["corrections"]["requests"][REQUEST]["state"]=="pending"
    grid.put(68,2,False)
    result=run_daily_requests(env,apply=True)
    assert result["corrections_applied"]==1 and len(ledger.calls)==1
    assert refresh.read_month("2025-12").amount==80
    assert daily.form()[1]!=REQUEST
    from app.daily_view import SHEETS
    assert grid.data[(SHEETS["確認"][0],68,1)]=="反映済み"


def test_display_failure_replays_no_ledger_write_and_does_not_require_native_checkpoint_release(monkeypatch):
    env,daily,grid,ledger,reader,refresh=configured(monkeypatch)
    def fail(*args):raise RuntimeError("synthetic display failure")
    monkeypatch.setattr("app.daily_runtime.refresh_daily",fail)
    with pytest.raises(RuntimeError):run_daily_requests(env,apply=True)
    assert daily.store.data["corrections"]["requests"][REQUEST]["state"]=="applied"
    monkeypatch.setattr("app.daily_runtime.refresh_daily",lambda *args:{"daily_changed_blocks":1})
    assert run_daily_requests(env,apply=True)["corrections_applied"]==0
    assert len(ledger.calls)==1


def test_pending_status_write_failure_keeps_durable_request_for_next_run(monkeypatch):
    env,daily,grid,ledger,reader,refresh=configured(monkeypatch)
    grid.fail_after=True
    with pytest.raises(RuntimeError):run_daily_requests(env,apply=True)
    assert daily.store.data["corrections"]["requests"][REQUEST]["state"]=="queued"
    assert ledger.calls==[]
    result=run_daily_requests(env,apply=True)
    assert result["corrections_applied"]==1 and len(ledger.calls)==1


def test_cutover_binding_failure_precedes_every_inbox_or_sheet_write(monkeypatch):
    env,daily,grid,ledger,reader,refresh=configured(monkeypatch)
    ledger.svc.spreadsheets().get().execute.return_value={"sheets":[]}
    before=deepcopy(daily.store.data)
    with pytest.raises(ProjectionError,match="categories_required"):run_daily_requests(env,apply=True)
    assert before==daily.store.data and ledger.calls==[] and grid.writes==[]
