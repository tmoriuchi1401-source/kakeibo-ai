from pathlib import Path

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
    assert parent["on"]["schedule"] == [{"cron": "17 9,21 * * *"}]
    inputs = parent["on"]["workflow_dispatch"]["inputs"]
    assert inputs["mode"]["default"] == "preview"
    assert inputs["bank_apply"]["default"] == "false"
    assert inputs["scope"]["default"] == "all"
    assert inputs["scope"]["options"] == ["all", "amazon_canary", "receipt_reimport", "receipt_confirmation", "receipts", "projection"]
    assert inputs["projection_bootstrap"]["default"] == "false"
    assert inputs["receipt_operation"]["options"] == ["reanalyze", "replay"]
    assert inputs["receipt_limit"]["default"] == "1"
    assert inputs["receipt_limit"]["options"] == ["1", "2", "3"]
    job = parent["jobs"]["production"]
    assert "vars.KAKEIBO_PRODUCTION_ENABLED == 'true'" in job["if"]
    assert "vars.KAKEIBO_LEGACY_DISABLED == 'true'" in job["if"]
    assert "vars.KAKEIBO_SCHEDULE_ENABLED == 'true'" in job["if"]
    assert "github.sha == vars.KAKEIBO_VALIDATED_MAIN_SHA" in job["if"]
    uses = [step.get("uses", "") for step in job["steps"]]
    assert not any("cache/save" in value or "upload-artifact" in value for value in uses)
    assert all("continue-on-error" not in step for step in job["steps"])
    checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "main"
    guard = next(step["run"] for step in job["steps"] if step.get("name", "").startswith("Bind verified"))
    assert 'test "$(git rev-parse HEAD)" = "$KAKEIBO_VALIDATED_MAIN_SHA"' in guard
    ocr=next(step for step in job["steps"] if step.get("name")=="Install existing OCR runtime")
    assert "inputs.scope != 'projection'" in ocr["if"]


def test_legacy_daily_entries_stop_before_new_entry_can_start():
    all_workflows = workflows()
    for name in ("amazon-daily-import.yml", "aupay-card-recurring-production.yml", "bank-pdf-recurring.yml", "process-receipts.yml"):
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
