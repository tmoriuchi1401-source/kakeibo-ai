"""Standard-library-only boundary for one checkpoint-bound manual Apply."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import io
import json
import logging
import os
import socket
import subprocess
import sys

from .aupay_card_window_preview import END, FLAGS, REPO, ROOT, SOURCE, GitHub, require

WORKFLOW = "aupay-card-incremental-apply.yml"
CONFIRM = "APPLY_CHECKPOINT_FRESH_PLAN_ONCE"


def controls(env, head, github):
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
    require(env.get("PREVIEW_SOURCE") == SOURCE, "source_forbidden")
    cp = datetime.fromisoformat(env["EXPECTED_CHECKPOINT"])
    require(cp.tzinfo is not None and cp.utcoffset() is not None and not cp.microsecond
            and cp.isoformat() == env["EXPECTED_CHECKPOINT"] and cp >= datetime.fromisoformat(END),
            "checkpoint_invalid")
    require(env.get("RUNNER_DEBUG", "0") != "1" and not env.get("ACTIONS_STEP_DEBUG")
            and not env.get("ACTIONS_RUNNER_DEBUG"), "debug_forbidden")
    require(all(env.get(k) == v for k, v in FLAGS.items()), "production_flags_mismatch")
    require(github.get("git/ref/heads/main")["object"]["sha"] == head, "main_changed")
    current = int(env["GITHUB_RUN_ID"])
    run = github.get(f"actions/runs/{current}")
    require(run["id"] == current and run["head_sha"] == head and run["run_attempt"] == 1
            and run["event"] == "workflow_dispatch" and run["path"] == ".github/workflows/" + WORKFLOW
            and datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")) > cp,
            "run_identity_mismatch")
    for status in ("in_progress", "queued", "waiting", "pending", "requested"):
        runs = github.get(f"actions/runs?status={status}&per_page=100")
        require(runs["total_count"] <= 100 and len(runs["workflow_runs"]) == runs["total_count"],
                "run_inventory_incomplete")
        require(all(r["id"] == current for r in runs["workflow_runs"]), "competing_run")
    # All revisions and all pages. A dispatch after the approved checkpoint
    # consumes that checkpoint even if it failed before credential injection.
    # Only completed successes older than a newer checkpoint allow daily reuse.
    seen, page, total = set(), 1, None
    while True:
        result = github.get(f"actions/workflows/{WORKFLOW}/runs?per_page=100&page={page}")
        if total is None:
            total = result["total_count"]
        require(total == result["total_count"] and 1 <= total <= 10000, "run_inventory_incomplete")
        runs = result["workflow_runs"]
        require(len(runs) == min(100, total - len(seen)), "run_inventory_incomplete")
        for prior in runs:
            require(prior["id"] not in seen, "run_inventory_incomplete")
            seen.add(prior["id"])
            if prior["id"] == current:
                require(prior["head_sha"] == head and prior["run_attempt"] == 1, "run_identity_mismatch")
                continue
            require(prior["status"] == "completed" and prior["conclusion"] == "success"
                    and prior["run_attempt"] == 1
                    and datetime.fromisoformat(prior["created_at"].replace("Z", "+00:00")) < cp,
                    "previous_dispatch_no_retry")
        if len(seen) == total:
            break
        page += 1
    require(current in seen, "run_inventory_incomplete")


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
        result = dict(success=False, error="incremental_guard_stopped_no_retry")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
