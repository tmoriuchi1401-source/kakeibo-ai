"""Read-only proof of the single independently reconciled card failure.

Hashes bind existing private evidence without publishing transaction identities.
This module cannot save a ledger, advance a checkpoint, or invoke a writer.
"""
import base64
from contextlib import closing
from datetime import datetime
import hashlib
import json
import sqlite3

from .aupay_card_window_preview import require

FAILED_RUN = 41560668947
FAILED_JOB = 125670234114
FAILED_SHA = "6284b63206813e3f731ea77bd6228b235336dd84"
RECOVERED_CHECKPOINT = "2026-11-03T10:35:38+09:00"
RECONCILED_AT = "2026-11-03T04:14:00.641320+00:00"
EVIDENCE_SHA256 = "1fcbcbfa043abb9bf7c2a14ea30851bd46dd1f40604baf64b199a763de60226a"
NATIVE_SHA256 = "10f59540ada795baa478783991ebdf7c24ebc83121a41000029568d5c32889b9"
RESULT_SHA256 = "0371d17ce24f49df27e432f5efbce9f4a63e29f46cc0b104a98ec828cf539788"
MANIFEST_SHA256 = "54b830424aa3032d7874fb3c3507de0cf66a463fe61f4438a3322436bc338f0a"
JOURNAL_SHA256 = "2a3c48e0d17500b16f75b5451204785db49b61c64b64043b32f2ae306c36e5cf"
CAPABILITY_SHA256 = "e2198cb76eb328323baa358458248d2c839deee0e8f5e634c8310c45505bfbcd"
PARENT_CARD_SHA256 = "a3e7eb487d494b62176511b537ef416b517ab4255e9322243c89ea8b6929989e"


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                    separators=(",", ":")).encode()).hexdigest()


def verify_failed_run(github):
    """This is a reconciled financial completion, never a no-write exemption."""
    run = github.get(f"actions/runs/{FAILED_RUN}")
    require(run["id"] == FAILED_RUN and run["head_sha"] == FAILED_SHA
            and run["run_attempt"] == 1 and run["status"] == "completed"
            and run["conclusion"] == "failure" and run["event"] == "workflow_dispatch"
            and run["path"] == ".github/workflows/aupay-card-incremental-apply.yml"
            and run["created_at"] == "2026-11-03T01:35:03Z", "reconciled_run_identity_unproven")
    jobs = github.get(f"actions/runs/{FAILED_RUN}/jobs?per_page=100")
    require(jobs["total_count"] == len(jobs["jobs"]) == 1, "reconciled_jobs_unproven")
    job = jobs["jobs"][0]
    require(job["id"] == FAILED_JOB and job["name"] == "apply"
            and job["status"] == "completed" and job["conclusion"] == "failure",
            "reconciled_job_identity_unproven")
    expected = {
        5: ("Live main and run guard with job-start validated and flags", "success"),
        6: ("Install reviewed hash-pinned wheels before Google credentials", "success"),
        7: ("One checkpoint Apply with protected reviews and exact readback", "failure"),
    }
    for number, (name, conclusion) in expected.items():
        steps = [s for s in job["steps"] if s["number"] == number]
        require(len(steps) == 1 and steps[0]["name"] == name
                and steps[0]["status"] == "completed" and steps[0]["conclusion"] == conclusion,
                "reconciled_step_identity_unproven")


def _rows(native, file, table):
    # Existing envelope validation runs first. Only SELECT on disposable memory.
    raw = base64.b64decode(native["files"][file]["data"], validate=True)
    with closing(sqlite3.connect(":memory:")) as con:
        con.deserialize(raw)
        con.execute("PRAGMA trusted_schema=OFF")
        con.execute("PRAGMA query_only=ON")
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute("SELECT * FROM " + table)]


def successful_checkpoint(native):
    """Resolve coverage from the protected native object, memory/GET only."""
    from .aupay_card_window_preview import checkpoint

    rows = _rows(native, "recurring.sqlite3", "recurring_checkpoint")
    require(len(rows) == 1, "checkpoint_invalid")
    return checkpoint(native, rows[0]["successful_window_end"])


def verify_reconciled_state(native, parent, expected_checkpoint):
    """Before prepare/pending/write, require the exact completed native proof."""
    from .production_run import COUNT_KEYS

    require(datetime.fromisoformat(expected_checkpoint) >= datetime.fromisoformat(RECOVERED_CHECKPOINT),
            "reconciled_checkpoint_regression")
    require(native["phase"] == "ready" and not native["attempt"], "reconciled_native_pending")
    card = parent["sources"]["aupay_card"]
    require(card["phase"] == "ready" and not card["attempt"] and not card["error"],
            "reconciled_parent_pending")
    runs = _rows(native, "recurring.sqlite3", "recurring_runs")
    baseline = [json.loads(r["summary_json"]) for r in runs
                if _hash(json.loads(r["summary_json"])) == RESULT_SHA256]
    require(len(baseline) == 1, "reconciled_result_unproven")
    run_id = baseline[0]["run_id"]
    require(baseline[0]["status"] == "complete"
            and baseline[0]["source_window_end"] == RECOVERED_CHECKPOINT
            and (baseline[0]["written"], baseline[0]["duplicate"], baseline[0]["money_review"],
                 baseline[0]["write_requests"], baseline[0]["failure"]) == (22, 3, 4, 1, 0),
            "reconciled_result_unproven")
    manifests = [r for r in _rows(native, "manifests.sqlite3", "batch_manifests") if r["run_id"] == run_id]
    require(len(manifests) == 1 and manifests[0]["payload_hash"] == MANIFEST_SHA256
            and hashlib.sha256(manifests[0]["payload"].encode()).hexdigest() == MANIFEST_SHA256,
            "reconciled_manifest_unproven")
    caps = [r for r in _rows(native, "capabilities.sqlite3", "batch_capabilities") if r["run_id"] == run_id]
    require(len(caps) == 1 and caps[0]["state"] == "sealed" and _hash(caps[0]) == CAPABILITY_SHA256,
            "reconciled_capability_unproven")
    events = sorted((r for r in _rows(native, "journal.sqlite3", "batch_events")
                     if r["run_id"] == run_id), key=lambda r: r["seq"])
    require(_hash(events) == JOURNAL_SHA256, "reconciled_journal_unproven")
    cp = _rows(native, "recurring.sqlite3", "recurring_checkpoint")
    require(len(cp) == 1 and cp[0]["successful_window_end"] == expected_checkpoint,
            "reconciled_checkpoint_changed")
    current = [json.loads(r["summary_json"]) for r in runs if r["run_id"] == cp[0]["run_id"]]
    require(len(current) == 1 and current[0]["status"] in {"complete", "noop"}
            and current[0]["source_window_end"] == expected_checkpoint
            and type(current[0].get("failure")) is int and current[0]["failure"] == 0,
            "reconciled_latest_result_unproven")
    counts = {k: v for k, v in current[0].items() if k in COUNT_KEYS and type(v) is int and v >= 0}
    require(card["counts"] == counts and datetime.fromisoformat(card["last_success"])
            >= datetime.fromisoformat(RECONCILED_AT), "reconciled_parent_result_changed")
    if expected_checkpoint == RECOVERED_CHECKPOINT:
        require(_hash(native) == NATIVE_SHA256 and _hash(card) == PARENT_CARD_SHA256,
                "reconciled_first_boundary_changed")
