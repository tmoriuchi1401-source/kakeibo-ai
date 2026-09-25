from pathlib import Path
from datetime import datetime, timedelta, timezone

import yaml

from app.production_run import DEPENDENCIES


ROOT = Path(__file__).resolve().parents[1]


def workflows():
    # BaseLoader leaves 'on' as a string (YAML 1.1 treats it as a boolean).
    return {path.name: yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
            for path in (ROOT / ".github/workflows").glob("*.yml")}


def test_all_existing_production_entries_share_top_level_lock_and_main_guard():
    for name, workflow in workflows().items():
        if name == "synthetic-tests.yml":
            continue
        assert workflow["concurrency"] == {"group": "kakeibo-production", "cancel-in-progress": "false", "queue": "max"}, name
        for job in workflow["jobs"].values():
            assert "github.ref == 'refs/heads/main'" in job["if"], name
            assert "concurrency" not in job, name  # no parent/child lock deadlock
        assert set(workflow["on"]) <= {"workflow_dispatch", "schedule"}, name


def test_parent_is_disabled_by_default_and_only_runs_validated_main():
    parent = workflows()["kakeibo-production.yml"]
    assert parent["on"]["schedule"] == [
        {"cron": "17 9,21 * * *"}, {"cron": "31 */3 * * *"},
        {"cron": "47 21 * * *"}, {"cron": "11 23 * * *"},
    ]
    inputs = parent["on"]["workflow_dispatch"]["inputs"]
    assert inputs["mode"]["default"] == "preview"
    assert inputs["bank_apply"]["default"] == "false"
    assert inputs["scope"]["default"] == "all"
    assert inputs["scope"]["options"] == ["all", "core", "drive_receipts", "drive_paypay", "drive_bank", "amazon_canary", "receipt_reimport", "receipt_confirmation", "receipts", "projection", "daily", "ledger_order", "categories"]
    assert inputs["projection_bootstrap"]["default"] == "false"
    assert inputs["canary_source"]["options"]==["amazon","aupay_card"]
    assert inputs["canary_source"]["default"]=="amazon"
    assert inputs["receipt_operation"]["options"] == ["reanalyze", "replay"]
    assert inputs["receipt_limit"]["default"] == "1"
    assert inputs["receipt_limit"]["options"] == ["1", "2", "3"]
    job = parent["jobs"]["production"]
    assert "vars.KAKEIBO_PRODUCTION_ENABLED == 'true'" in job["if"]
    assert "vars.KAKEIBO_LEGACY_DISABLED == 'true'" in job["if"]
    assert "vars.KAKEIBO_SCHEDULE_ENABLED == 'true'" in job["if"]
    assert "github.sha == vars.KAKEIBO_VALIDATED_MAIN_SHA" in job["if"]
    script = next(step["run"] for step in job["steps"] if step.get("name") == "Execute sources and dependent accounting serially")
    for cron, scope in [("17 9,21 * * *", "core"), ("31 */3 * * *", "drive_receipts"),
                        ("11 23 * * *", "drive_paypay")]:
        assert f"'{cron}') scope={scope} ;;" in script
    assert "'47 21 * * *') scope=drive_bank; args+=(--bank-apply) ;;" in script
    assert 'args+=(--scope "$scope")' in script
    assert job["steps"][-1]["env"]["EVENT_SCHEDULE"] == "${{ github.event.schedule }}"
    uses = [step.get("uses", "") for step in job["steps"]]
    assert not any("cache/save" in value or "upload-artifact" in value for value in uses)
    assert all("continue-on-error" not in step for step in job["steps"])
    checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "main"
    guard = next(step["run"] for step in job["steps"] if step.get("name", "").startswith("Bind verified"))
    assert 'test "$(git rev-parse HEAD)" = "$KAKEIBO_VALIDATED_MAIN_SHA"' in guard
    ocr=next(step for step in job["steps"] if step.get("name")=="Install existing OCR runtime")
    assert "github.event.schedule == '31 */3 * * *'" in ocr["if"]
    assert "inputs.scope == 'drive_receipts'" in ocr["if"]


def test_drive_schedule_jst_times_and_receipt_period():
    jst = timezone(timedelta(hours=9))
    bank = datetime(2026, 9, 23, 21, 47, tzinfo=timezone.utc).astimezone(jst)
    paypay = datetime(2026, 9, 23, 23, 11, tzinfo=timezone.utc).astimezone(jst)
    receipt_hours = [datetime(2026, 9, 24, hour, 31, tzinfo=timezone.utc).astimezone(jst).hour
                     for hour in range(0, 24, 3)]
    assert (bank.hour, bank.minute) == (6, 47)
    assert (paypay.hour, paypay.minute) == (8, 11)
    assert receipt_hours == [9, 12, 15, 18, 21, 0, 3, 6]


def test_legacy_daily_entries_stop_before_new_entry_can_start():
    all_workflows = workflows()
    for name in ("aupay-card-recurring-production.yml", "bank-pdf-recurring.yml", "process-receipts.yml"):
        job = next(iter(all_workflows[name]["jobs"].values()))
        assert "vars.KAKEIBO_LEGACY_DISABLED != 'true'" in job["if"]


def test_serial_dependency_graph_has_no_forward_edges_or_cycles():
    seen = set()
    for source, dependencies in DEPENDENCIES.items():
        assert set(dependencies) <= seen
        seen.add(source)
    assert len(seen) == 11


def test_branch_and_sensitive_synthetic_jobs_have_no_production_secrets():
    path = ROOT / ".github/workflows/synthetic-tests.yml"
    text = path.read_text(encoding="utf-8")
    assert "secrets." not in text
    workflow = workflows()[path.name]
    assert set(workflow["on"]) == {"push", "pull_request"}
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]["test"]["strategy"]["matrix"]["include"]
    assert {job["selection"] for job in jobs} == {"payroll or medical", "not payroll and not medical"}
