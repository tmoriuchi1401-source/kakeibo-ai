from copy import deepcopy
from types import SimpleNamespace

import pytest

from app import bank_review_refresh as refresh
from app.category_sheet_requests import CapturedCategoryDB, execute_request
from app.sheets import CATEGORY_REQUEST_SHEET
from test_category_sheet_requests import RequestDB, Store, blocks, ENV, REQUEST
from test_bank_category_request_integration import CombinedUI, snapshot
from test_bank_review_requests import prepared


def test_regenerated_category_links_wait_for_committed_layout():
    db = RequestDB()
    links = []
    db.link_category_rule_representatives = lambda rows, records: links.append((deepcopy(rows), records))
    adapter = CapturedCategoryDB(db, blocks(db))
    new_rows = [["new candidate", "", "", "", False, False, "new-key", "expense-id", "", "", "", ""]]
    records = {"expense-id": (2, ["expense-id", "2026-09-01"])}
    adapter.replace_category_rule_ui_rows(new_rows, adapter.blocks["rule"][0])
    adapter.link_category_rule_representatives(new_rows, records)
    assert links == [] and db.ui == []
    db._write_category_workflow_blocks(adapter.blocks)
    adapter.write_representative_links(adapter.blocks)
    assert links[0][0] == db.ui == new_rows


def test_unanswered_submit_refreshes_groups_without_approval_or_financial_replay():
    _, rows, _ = prepared()
    rows[0][2:6] = ["未選択", "", "未選択", False]
    db = CombinedUI(rows)
    store = Store(snapshot(db))
    refreshed = []
    result = execute_request(db, REQUEST, env={**ENV, "BANK_REVIEW_ENABLED": "true"}, store=store,
        bank_processor=SimpleNamespace(process=lambda *a: pytest.fail("meaning not answered")),
        bank_replay=lambda *_: pytest.fail("no financial authority"),
        bank_refresh=lambda: refreshed.append(True) or {"bank_confirmation_groups": 1},
        refresh_projection=lambda: {})
    assert refreshed == [True] and result["bank_confirmation_groups"] == 1
    assert not db.rule_rows and not db.category_updates
    assert store.states == ["running", "complete"]
    from app.bank_review_ui import checked
    assert db.bank_rows[0][2:5] == ["未選択", "", "未選択"]
    assert not checked(db.bank_rows[0][5])


@pytest.mark.parametrize("scope,success,enabled", [
    ("drive_receipts", True, True), ("drive_paypay", True, True),
    ("amazon_canary", True, True), ("core", True, True), ("drive_bank", False, True),
    ("drive_bank", True, False),
])
def test_nonbank_failed_or_disabled_run_does_not_touch_bank_ui(monkeypatch, scope, success, enabled):
    monkeypatch.setattr("app.google_clients.sheets_service", lambda: pytest.fail("credentials"))
    assert refresh.refresh_bank_review_after_sources(
        {"BANK_REVIEW_ENABLED": "true" if enabled else "false"},
        scope=scope, source_success=success, apply=True) == {}


@pytest.mark.parametrize("scope", ["all", "drive_bank"])
@pytest.mark.parametrize("apply", [False, True])
def test_bank_source_completion_refreshes_only_ui_and_keeps_mode(monkeypatch, scope, apply):
    writable, readonly = object(), object()
    monkeypatch.setattr("app.production_flow.verify_execution_boundary", lambda *args: None)
    monkeypatch.setattr("app.google_clients.sheets_service", lambda: writable)
    monkeypatch.setattr("app.google_clients.read_only_sheets_service", lambda: readonly)
    db = SimpleNamespace(sheet_titles=lambda: [CATEGORY_REQUEST_SHEET])
    services = []
    def build(*args, **kwargs):
        services.append(kwargs["service"])
        return db
    monkeypatch.setattr("app.sheets.SheetsDB", build)
    calls = []
    monkeypatch.setattr(refresh, "run_bank_review_refresh",
        lambda actual_db, env, **kwargs: calls.append((actual_db, kwargs)) or {"bank_ui_groups": 44})
    result = refresh.refresh_bank_review_after_sources({"BANK_REVIEW_ENABLED": "true"},
        scope=scope, source_success=True, apply=apply)
    assert services == [writable if apply else readonly]
    assert calls == [(db, {"apply": apply})] and result == {"bank_ui_groups": 44}


def test_bank_ui_cannot_be_installed_without_shared_submit_surface(monkeypatch):
    monkeypatch.setattr("app.production_flow.verify_execution_boundary", lambda *args: None)
    monkeypatch.setattr("app.google_clients.sheets_service", lambda: object())
    monkeypatch.setattr("app.sheets.SheetsDB", lambda *a, **kw: SimpleNamespace(sheet_titles=lambda: []))
    monkeypatch.setattr(refresh, "run_bank_review_refresh", lambda *a, **kw: pytest.fail("no backend"))
    with pytest.raises(Exception, match="bank_review_submit_surface_required"):
        refresh.refresh_bank_review_after_sources({"BANK_REVIEW_ENABLED": "true"},
            scope="drive_bank", source_success=True, apply=True)
