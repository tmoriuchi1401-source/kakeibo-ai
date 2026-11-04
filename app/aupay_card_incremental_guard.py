"""Standard-library-only manual/daily boundary, before Google credentials."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import io
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys

from .aupay_card_window_preview import END, FLAGS, REPO, ROOT, SOURCE, GitHub, require
from .aupay_card_incremental_recovery import FAILED_RUN, FAILED_SHA, RECOVERED_CHECKPOINT, verify_failed_run
from .aupay_card_incremental_recovery import (
    NO_WRITE_RUN, NO_WRITE_SHA, NO_WRITE_CHECKPOINT, verify_no_write_run,
)

WORKFLOW = "aupay-card-incremental-apply.yml"
CONFIRM = "APPLY_CHECKPOINT_FRESH_PLAN_ONCE"
DAILY_CRON = "23 0 * * *"


def verify_skipped_run(github, prior):
    """Only a fully skipped apply job proves no Apply body ever started."""
    run = github.get(f"actions/runs/{prior['id']}")
    require(run["id"] == prior["id"] and run["head_sha"] == prior["head_sha"]
            and run["head_branch"] == "main" and run["run_attempt"] == 1
            and run["status"] == "completed" and run["conclusion"] == "skipped"
            and run["event"] in {"schedule", "workflow_dispatch"}
            and run["path"] == ".github/workflows/" + WORKFLOW, "skipped_run_unproven")
    jobs = github.get(f"actions/runs/{prior['id']}/jobs?per_page=100")
    require(jobs["total_count"] == len(jobs["jobs"]) == 1, "skipped_jobs_unproven")
    job = jobs["jobs"][0]
    require(job["name"] == "apply" and job["status"] == "completed"
            and job["conclusion"] == "skipped" and job.get("steps") == [],
            "skipped_apply_body_unproven")


def controls(env, head, github, *, expected_checkpoint=None):
    scheduled = env.get("GITHUB_EVENT_NAME") == "schedule"
    require(len(head) == 40 and all(c in "0123456789abcdef" for c in head), "commit_invalid")
    require(env.get("GITHUB_ACTIONS") == "true" and env.get("GITHUB_REPOSITORY") == REPO
            and env.get("GITHUB_REF") == "refs/heads/main"
            and env.get("GITHUB_EVENT_NAME") in {"workflow_dispatch", "schedule"}, "main_event_required")
    require(head == env.get("GITHUB_SHA") == env.get("WORKFLOW_SHA")
            == env.get("EXPECTED_COMMIT") == env.get("KAKEIBO_VALIDATED_MAIN_SHA"), "commit_mismatch")
    require(env.get("WORKFLOW_REF") == f"{REPO}/.github/workflows/{WORKFLOW}@refs/heads/main",
            "workflow_mismatch")
    require(env.get("GITHUB_RUN_ATTEMPT") == "1", "attempt_one_required")
    if scheduled:
        require(env.get("KAKEIBO_CARD_SCHEDULE_ENABLED") == "true", "card_schedule_disabled")
        event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        require(env.get("SCHEDULE_CRON") == event.get("schedule") == DAILY_CRON,
                "card_schedule_cron_mismatch")
        # Native is unavailable before credential injection. Never read or
        # export a manual input/GitHub Variable as the scheduled checkpoint.
        checkpoint_value = expected_checkpoint
    else:
        require(env.get("GITHUB_ACTOR") == env.get("GITHUB_TRIGGERING_ACTOR") == REPO.split("/")[0]
                and env.get("CONFIRM") == CONFIRM, "owner_once_approval_required")
        require(expected_checkpoint is None or expected_checkpoint == env["EXPECTED_CHECKPOINT"],
                "manual_checkpoint_override_forbidden")
        checkpoint_value = env["EXPECTED_CHECKPOINT"]
    require(env.get("PREVIEW_SOURCE") == SOURCE, "source_forbidden")
    cp = datetime.fromisoformat(checkpoint_value) if checkpoint_value is not None else None
    if cp is not None:
        require(cp.tzinfo is not None and cp.utcoffset() is not None and not cp.microsecond
                and cp.isoformat() == checkpoint_value and cp >= datetime.fromisoformat(END),
                "checkpoint_invalid")
    require(env.get("RUNNER_DEBUG", "0") != "1" and not env.get("ACTIONS_STEP_DEBUG")
            and not env.get("ACTIONS_RUNNER_DEBUG"), "debug_forbidden")
    require(all(env.get(k) == v for k, v in FLAGS.items()), "production_flags_mismatch")
    require(github.get("git/ref/heads/main")["object"]["sha"] == head, "main_changed")
    current = int(env["GITHUB_RUN_ID"])
    run = github.get(f"actions/runs/{current}")
    require(run["id"] == current and run["head_sha"] == head and run["run_attempt"] == 1
            and run["event"] == env["GITHUB_EVENT_NAME"]
            and run["path"] == ".github/workflows/" + WORKFLOW
            and (cp is None or datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")) > cp),
            "run_identity_mismatch")
    for status in ("in_progress", "queued", "waiting", "pending", "requested"):
        runs = github.get(f"actions/runs?status={status}&per_page=100")
        require(runs["total_count"] <= 100 and len(runs["workflow_runs"]) == runs["total_count"],
                "run_inventory_incomplete")
        require(all(r["id"] == current for r in runs["workflow_runs"]), "competing_run")
    # All revisions and all pages. A dispatch after the approved checkpoint
    # consumes that checkpoint even if it failed before credential injection.
    # Completed successes older than a newer checkpoint allow daily reuse. The
    # one pinned reconciled failure additionally mandates protected state proof.
    seen, page, total = set(), 1, None
    recovered_failure = False
    no_write_failure = False
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
            if prior["id"] == FAILED_RUN:
                require(cp is None or cp >= datetime.fromisoformat(RECOVERED_CHECKPOINT),
                        "reconciled_checkpoint_regression")
                require(prior["head_sha"] == FAILED_SHA and prior["run_attempt"] == 1
                        and prior["status"] == "completed" and prior["conclusion"] == "failure",
                        "reconciled_run_identity_unproven")
                verify_failed_run(github)
                recovered_failure = True
                continue
            if prior["id"] == NO_WRITE_RUN:
                require(cp is None or cp >= datetime.fromisoformat(NO_WRITE_CHECKPOINT),
                        "no_write_checkpoint_regression")
                require(prior["head_sha"] == NO_WRITE_SHA and prior["run_attempt"] == 1
                        and prior["status"] == "completed" and prior["conclusion"] == "failure",
                        "no_write_run_unproven")
                verify_no_write_run(github)
                no_write_failure = True
                continue
            if prior["status"] == "completed" and prior["conclusion"] == "skipped":
                require(prior["run_attempt"] == 1, "skipped_attempt_unproven")
                verify_skipped_run(github, prior)
                continue
            require(prior["status"] == "completed" and prior["conclusion"] == "success"
                    and prior["run_attempt"] == 1
                    and (cp is None or datetime.fromisoformat(prior["created_at"].replace("Z", "+00:00")) < cp),
                    "previous_dispatch_no_retry")
        if len(seen) == total:
            break
        page += 1
    require(current in seen, "run_inventory_incomplete")
    if scheduled or (cp is not None and cp >= datetime.fromisoformat(RECOVERED_CHECKPOINT)):
        require(recovered_failure, "reconciled_history_missing")
    if cp is not None and cp >= datetime.fromisoformat(NO_WRITE_CHECKPOINT):
        require(no_write_failure, "no_write_history_missing")
    # True mandates protected native/parent proof before any prepare or writer.
    return recovered_failure


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
