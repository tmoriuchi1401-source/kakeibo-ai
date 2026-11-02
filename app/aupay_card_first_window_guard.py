"""Standard-library-only guard, safe before pinned dependency installation."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import logging
import os
import socket
import subprocess
import sys

from .aupay_card_window_preview import (
    CHECKPOINT, END, FLAGS, REPO, ROOT, SOURCE, START, GitHub, require,
)

WORKFLOW = "aupay-card-first-window-apply.yml"
CONFIRM = "APPLY_FIRST_WINDOW_33_149218_REPLACEMENT_ONCE"
FAILED_PREFLIGHT_RUN = 41543799949
FAILED_PREFLIGHT_JOB = 125614231722
FAILED_PREFLIGHT_SHA = "df36c40f6ef00a44392a6173c82b749a20067145"


def verify_failed_preflight(github):
    """Permit only the independently proven, fixed no-credential failure.

    No UNKNOWN/financial attempt may be exempted, retried, deleted or released.
    A separate replacement confirmation and new reviewed SHA remain required.
    """
    run = github.get(f"actions/runs/{FAILED_PREFLIGHT_RUN}")
    require(run["id"] == FAILED_PREFLIGHT_RUN and run["head_sha"] == FAILED_PREFLIGHT_SHA
            and run["run_attempt"] == 1 and run["status"] == "completed" and run["conclusion"] == "failure"
            and run["event"] == "workflow_dispatch" and run["path"] == ".github/workflows/" + WORKFLOW,
            "failed_preflight_identity_unproven")
    result = github.get(f"actions/runs/{FAILED_PREFLIGHT_RUN}/jobs?per_page=100")
    require(result["total_count"] == len(result["jobs"]) == 1, "failed_preflight_jobs_unproven")
    job = result["jobs"][0]
    require(job["id"] == FAILED_PREFLIGHT_JOB and job["name"] == "apply"
            and job["status"] == "completed" and job["conclusion"] == "failure",
            "failed_preflight_job_unproven")
    expected = {
        5: ("Live main and run guard with job-start validated and flags", "failure"),
        6: ("Install reviewed hash-pinned wheels before Google credentials", "skipped"),
        7: ("One fixed Apply with protected reviews and exact readback", "skipped"),
    }
    for number, (name, conclusion) in expected.items():
        steps = [s for s in job["steps"] if s["number"] == number]
        require(len(steps) == 1 and steps[0]["name"] == name and steps[0]["status"] == "completed"
                and steps[0]["conclusion"] == conclusion, "failed_preflight_no_write_unproven")


def controls(env, head, github):
    """Real main and one replacement dispatch, preserving all prior history."""
    require(len(head) == 40 and all(c in "0123456789abcdef" for c in head), "commit_invalid")
    require(env.get("GITHUB_ACTIONS") == "true" and env.get("GITHUB_REPOSITORY") == REPO
            and env.get("GITHUB_REF") == "refs/heads/main"
            and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "manual_main_required")
    require(head == env.get("GITHUB_SHA") == env.get("WORKFLOW_SHA")
            == env.get("EXPECTED_COMMIT") == env.get("KAKEIBO_VALIDATED_MAIN_SHA"), "commit_mismatch")
    require(env.get("WORKFLOW_REF") == f"{REPO}/.github/workflows/{WORKFLOW}@refs/heads/main",
            "workflow_mismatch")
    require(env.get("GITHUB_ACTOR") == env.get("GITHUB_TRIGGERING_ACTOR") == REPO.split("/")[0]
            and env.get("GITHUB_RUN_ATTEMPT") == "1" and env.get("CONFIRM") == CONFIRM,
            "owner_once_approval_required")
    require(env.get("PREVIEW_SOURCE") == SOURCE and env.get("PREVIEW_START") == START
            and env.get("PREVIEW_END") == END and env.get("EXPECTED_CHECKPOINT") == CHECKPOINT,
            "fixed_window_mismatch")
    require(env.get("RUNNER_DEBUG", "0") != "1" and not env.get("ACTIONS_STEP_DEBUG")
            and not env.get("ACTIONS_RUNNER_DEBUG"), "debug_forbidden")
    require(all(env.get(k) == v for k, v in FLAGS.items()), "production_flags_mismatch")
    require(github.get("git/ref/heads/main")["object"]["sha"] == head, "main_changed")
    current = int(env["GITHUB_RUN_ID"])
    run = github.get(f"actions/runs/{current}")
    require(run["head_sha"] == head and run["run_attempt"] == 1
            and run["event"] == "workflow_dispatch" and run["path"] == ".github/workflows/" + WORKFLOW,
            "run_identity_mismatch")
    for status in ("in_progress", "queued", "waiting", "pending", "requested"):
        runs = github.get(f"actions/runs?status={status}&per_page=100")
        require(runs["total_count"] <= 100 and len(runs["workflow_runs"]) == runs["total_count"],
                "run_inventory_incomplete")
        require(all(r["id"] == current for r in runs["workflow_runs"]), "competing_run")
    prior = github.get(f"actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch&per_page=100")
    require(current != FAILED_PREFLIGHT_RUN
            and prior["total_count"] == len(prior["workflow_runs"]) == 2
            and {r["id"] for r in prior["workflow_runs"]} == {current, FAILED_PREFLIGHT_RUN},
            "previous_dispatch_no_retry")
    verify_failed_preflight(github)


def main():
    logging.disable(logging.CRITICAL)
    socket.setdefaulttimeout(30)
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            require(not sys.argv[1:], "arguments_forbidden")
            env = dict(os.environ)
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
            controls(env, head, GitHub(env))
        result = dict(success=True, guard=True)
    except BaseException:
        result = dict(success=False, error="first_window_guard_stopped_no_retry")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
