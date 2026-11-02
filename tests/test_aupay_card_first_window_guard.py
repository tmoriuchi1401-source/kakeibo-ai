"""Exercise the exact pre-install boundary without any site-packages."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from app import aupay_card_first_window_guard as guard
from test_aupay_card_first_window_apply import GitHub, environment


def test_guard_succeeds_without_site_packages_google_clients_or_network():
    github = GitHub()
    paths = ["git/ref/heads/main", "actions/runs/17",
        f"actions/workflows/{guard.WORKFLOW}/runs?event=workflow_dispatch&per_page=100",
        f"actions/runs/{guard.FAILED_PREFLIGHT_RUN}",
        f"actions/runs/{guard.FAILED_PREFLIGHT_RUN}/jobs?per_page=100"]
    paths += [f"actions/runs?status={s}&per_page=100" for s in
        ("in_progress", "queued", "waiting", "pending", "requested")]
    payload = dict(env=environment(), responses={p: github.get(p) for p in paths})
    code = '''
import json, socket, sys
def denied(*a, **k): raise AssertionError("network forbidden")
socket.socket.connect = denied
socket.socket.connect_ex = denied
from app import aupay_card_first_window_guard as guard
data = json.load(sys.stdin)
guard.os.environ = data["env"]
guard.sys.argv = ["guard"]
guard.subprocess.check_output = lambda *a, **k: "a"*40
class GitHub:
    def get(self, path): return data["responses"][path]
guard.GitHub = lambda env: GitHub()
assert not any(n.startswith(("google", "pydantic", "cryptography")) for n in sys.modules)
raise SystemExit(guard.main())
'''
    result = subprocess.run([sys.executable, "-S", "-c", code], input=json.dumps(payload),
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == dict(success=True, guard=True)
    assert not result.stderr


def test_module_cli_rejects_missing_approval_without_dependency_traceback():
    result = subprocess.run([sys.executable, "-S", "-m", "app.aupay_card_first_window_guard", "forbidden"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert json.loads(result.stdout) == dict(success=False, error="first_window_guard_stopped_no_retry")
    assert not result.stderr


@pytest.mark.parametrize("change", ["sha", "attempt", "conclusion", "job_id", "job_count",
    "guard_success", "install_executed", "apply_executed", "apply_unknown", "missing_step"])
def test_only_exact_proven_precredential_failure_can_be_exempted(change):
    original = GitHub()
    run = original.get(f"actions/runs/{guard.FAILED_PREFLIGHT_RUN}")
    jobs = original.get(f"actions/runs/{guard.FAILED_PREFLIGHT_RUN}/jobs?per_page=100")
    job = jobs["jobs"][0]
    if change == "sha": run["head_sha"] = "b"*40
    if change == "attempt": run["run_attempt"] = 2
    if change == "conclusion": run["conclusion"] = "success"
    if change == "job_id": job["id"] += 1
    if change == "job_count": jobs["total_count"] = 2
    if change == "guard_success": job["steps"][0]["conclusion"] = "success"
    if change == "install_executed": job["steps"][1]["conclusion"] = "success"
    if change == "apply_executed": job["steps"][2]["conclusion"] = "failure"
    if change == "apply_unknown": job["steps"][2]["status"] = "in_progress"
    if change == "missing_step": job["steps"].pop()
    class Changed:
        def get(self, path): return deepcopy(jobs if "/jobs?" in path else run)
    with pytest.raises(RuntimeError): guard.verify_failed_preflight(Changed())


def test_previous_confirmation_cannot_authorize_replacement():
    env = environment()
    env["CONFIRM"] = "APPLY_FIRST_WINDOW_33_149218_ONCE"
    with pytest.raises(RuntimeError): guard.controls(env, "a"*40, GitHub())
