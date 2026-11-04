"""Synthetic guard tests for an opt-in isolated intake lane.

No Google credentials, card reconciliation, or financial writer are exercised.
"""
from copy import deepcopy
from pathlib import Path

import pytest

from app import production_flow as flow
from app.drive_run_state import StateError
from app.production_run import DEPENDENCIES, execute_serial


def history():
    result = {
        name: dict(phase="ready", attempt="", error="", counts={})
        for name in DEPENDENCIES
    }
    result["aupay_card"].update(phase="pending", attempt="a" * 32)
    return result


def natives():
    return {
        "amazon": dict(phase="ready", attempt=""),
        "aupay_card": dict(phase="pending", attempt="b" * 32),
        "bank": dict(phase="ready", attempt=""),
    }


def env():
    return {
        "KAKEIBO_CARD_PENDING_ISOLATION_ENABLED": "true",
        "KAKEIBO_CARD_SCHEDULE_ENABLED": "false",
    }


def fake_natives(monkeypatch, values):
    monkeypatch.setattr(flow, "read_native_state",
                        lambda _env, source: deepcopy(values[source]))


@pytest.mark.parametrize("scope", ["core", "drive_paypay"])
def test_card_pending_requires_explicit_opt_in_and_preserves_default_no_go(
        monkeypatch, scope):
    parent, native = history(), natives()
    fake_natives(monkeypatch, native)
    assert not flow.card_pending_isolation({}, parent, scope)
    with pytest.raises(StateError, match="source_reconciliation_required"):
        flow.verify_source_readiness({}, parent)


@pytest.mark.parametrize("scope", ["core", "drive_paypay"])
def test_isolation_requires_all_other_sources_and_natives_ready(monkeypatch, scope):
    parent, native, controls = history(), natives(), env()
    fake_natives(monkeypatch, native)
    assert flow.card_pending_isolation(controls, parent, scope)
    assert flow.verify_source_readiness(controls, parent, isolate_card=True) == native["aupay_card"]
    assert parent["aupay_card"]["phase"] == native["aupay_card"]["phase"] == "pending"


@pytest.mark.parametrize("modified", [
    "other_parent_pending", "other_parent_error", "card_parent_no_attempt",
    "card_parent_error", "other_native_pending", "card_native_ready",
    "card_native_no_attempt",
])
def test_isolation_fail_closed_on_any_unrelated_or_card_mismatch(monkeypatch, modified):
    parent, native, controls = history(), natives(), env()
    if modified == "other_parent_pending":
        parent["paypay"].update(phase="pending", attempt="c" * 32)
    elif modified == "other_parent_error":
        parent["bank"]["error"] = "source_execution_failed"
    elif modified == "card_parent_no_attempt":
        parent["aupay_card"]["attempt"] = ""
    elif modified == "card_parent_error":
        parent["aupay_card"]["error"] = "source_execution_failed"
    elif modified == "other_native_pending":
        native["bank"].update(phase="pending", attempt="d" * 32)
    elif modified == "card_native_ready":
        native["aupay_card"].update(phase="ready", attempt="")
    else:
        native["aupay_card"]["attempt"] = ""
    fake_natives(monkeypatch, native)
    with pytest.raises(StateError, match="source_reconciliation_required"):
        isolate = flow.card_pending_isolation(controls, parent, "core")
        flow.verify_source_readiness(controls, parent, isolate_card=isolate)


def test_card_schedule_must_stay_disabled_in_isolated_lane():
    controls, parent = env(), history()
    controls["KAKEIBO_CARD_SCHEDULE_ENABLED"] = "true"
    with pytest.raises(StateError, match="source_reconciliation_required"):
        flow.card_pending_isolation(controls, parent, "core")


def test_only_core_and_paypay_can_request_isolation():
    for scope in ("drive_bank", "drive_receipts", "all", "amazon_catchup"):
        assert flow.card_pending_isolation(env(), history(), scope) is False


def test_card_ready_keeps_full_existing_core_pipeline(monkeypatch):
    parent, native = history(), natives()
    parent["aupay_card"].update(phase="ready", attempt="")
    native["aupay_card"].update(phase="ready", attempt="")
    fake_natives(monkeypatch, native)
    assert not flow.card_pending_isolation(env(), parent, "core")
    assert flow.verify_source_readiness(env(), parent) is None
    selected = flow.selected_scheduled_sources("core", isolate_card=False)
    assert "review_apply" in selected and "reconcile" in selected
    assert selected == flow.DRIVE_SCHEDULE_SCOPES["core"]


@pytest.mark.parametrize("scope,expected", [
    ("core", frozenset({"amazon", "aupay_balance"})),
    ("drive_paypay", frozenset({"paypay"})),
])
def test_only_independent_intake_runs_while_card_pending(scope, expected):
    selected = flow.selected_scheduled_sources(scope, isolate_card=True)
    assert selected == expected
    called = []
    def entry(source):
        def perform():
            called.append(source)
            return {"status": "complete", "failure": 0}
        return perform
    tasks = {name: entry(name) for name in DEPENDENCIES}
    result = execute_serial(
        tasks, history=history(), selected_sources=selected,
        require_omitted_ready=False, stop_on_failure=(scope == "core"))
    assert result["success"] is True
    assert set(result["sources"]) == expected
    assert set(called) == expected
    assert not {"aupay_card", "review_apply", "reconcile", "auto_expense",
                "review_refresh", "expenses_refresh"} & set(called)


def test_new_boundary_is_disabled_by_default_in_existing_workflow():
    workflow = (Path(__file__).resolve().parents[1] /
                ".github/workflows/kakeibo-production.yml").read_text(encoding="utf-8")
    assert "KAKEIBO_CARD_PENDING_ISOLATION_ENABLED: ${{ vars.KAKEIBO_CARD_PENDING_ISOLATION_ENABLED || 'false' }}" in workflow
    assert "KAKEIBO_CARD_SCHEDULE_ENABLED: ${{ vars.KAKEIBO_CARD_SCHEDULE_ENABLED || 'false' }}" in workflow
    assert '"23 20 * * *": ("core", "KAKEIBO_CORE_SCHEDULE_ENABLED")' in (
        Path(__file__).resolve().parents[1] / "app/production_flow.py"
    ).read_text(encoding="utf-8")
