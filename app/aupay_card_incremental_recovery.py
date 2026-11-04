"""Read-only proof of the two independently reconciled, fixed card failures.

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

# The later run stopped before persistence; it is not the older financial
# completion above. Exact hashes bind the independent before/after GET audit.
NO_WRITE_RUN = 41729101706
NO_WRITE_JOB = 126241487787
NO_WRITE_SHA = "22b0a374dcd7c6b344e092b414c096c067130431"
NO_WRITE_CREATED = "2026-11-03T22:14:53Z"
NO_WRITE_CHECKPOINT = "2026-11-03T14:45:43+09:00"
NO_WRITE_NATIVE_SHA256 = "2ee8d898168d5db2ad59ed006944b2a8306316990c1c3df7ab5a2a62d5a44892"
NO_WRITE_PARENT_SHA256 = "cdee51239e3fadf3a34dec4c4a76e4db64c65dd827d1127bfe3557f1c09c9c14"
NO_WRITE_RESULT_SHA256 = "258c1adcfe85f2e799f4b63eca53534fc5787fec7ae67c91d455011db5894c4c"
NO_WRITE_FINANCIAL_SHA256 = "4388e4c1abb0de1c14ef14adb7900d9c121c368db95676a249f893ca88c36d7a"
NO_WRITE_MONEY_SHA256 = "58d87b362afc69e041de2c31e34e753c0959fb91146dc4518c1e58969c8224b2"
NO_WRITE_MONEY_KEYS = ("money", "money-reviews", "money-migration", "money-notices", "journal")


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


def verify_no_write_run(github):
    """Only this exact attempt/job/step history has the no-write audit."""
    run = github.get(f"actions/runs/{NO_WRITE_RUN}")
    require(run["id"] == NO_WRITE_RUN and run["head_sha"] == NO_WRITE_SHA
            and run["head_branch"] == "main" and run["run_attempt"] == 1
            and run["status"] == "completed" and run["conclusion"] == "failure"
            and run["event"] == "workflow_dispatch" and run["created_at"] == NO_WRITE_CREATED
            and run["path"] == ".github/workflows/aupay-card-incremental-apply.yml",
            "no_write_run_unproven")
    jobs = github.get(f"actions/runs/{NO_WRITE_RUN}/jobs?per_page=100")
    require(jobs["total_count"] == len(jobs["jobs"]) == 1, "no_write_jobs_unproven")
    job = jobs["jobs"][0]
    require(job["id"] == NO_WRITE_JOB and job["run_id"] == NO_WRITE_RUN
            and job["name"] == "apply" and job["status"] == "completed"
            and job["conclusion"] == "failure", "no_write_job_unproven")
    for number, name, conclusion in (
        (5, "Live main and run guard with job-start validated and flags", "success"),
        (6, "Install reviewed hash-pinned wheels before Google credentials", "success"),
        (7, "One checkpoint Apply with protected reviews and exact readback", "failure"),
    ):
        steps = [s for s in job["steps"] if s["number"] == number]
        require(len(steps) == 1 and steps[0]["name"] == name and steps[0]["status"] == "completed"
                and steps[0]["conclusion"] == conclusion, "no_write_step_unproven")


def financial_evidence(tables):
    """Existing reader's complete financial columns, independent of UI/sort."""
    result = {}
    for title, width in (("支出明細", 13), ("取込データ", 12), ("収入明細", 10), ("レシート", 9)):
        rows = tables[(title, "FORMULA")]
        require(bool(rows), "no_write_financial_missing")
        indexed = {}
        for row in rows[1:]:
            row = (list(row[:width]) + [""] * width)[:width]
            if not any(v not in ("", None) for v in row):
                continue
            require(isinstance(row[0], str) and row[0] and row[0] not in indexed,
                    "no_write_financial_identity")
            indexed[row[0]] = row
        result[title] = [rows[0][:width], indexed]
    return _hash(result)


def verify_no_write_state(native, parent, expected_checkpoint, *, financial, money):
    """No state saves: first resume pins all audited data; daily reuse retains proof."""
    from .aupay_card_window_preview import checkpoint

    require(datetime.fromisoformat(expected_checkpoint) >= datetime.fromisoformat(NO_WRITE_CHECKPOINT),
            "no_write_checkpoint_regression")
    checkpoint(native, expected_checkpoint)  # Also rejects leases/unsealed/UNKNOWN journals.
    verify_reconciled_state(native, parent, expected_checkpoint)  # Older proof is never replaced.
    runs = _rows(native, "recurring.sqlite3", "recurring_runs")
    baseline = [json.loads(r["summary_json"]) for r in runs
                if _hash(json.loads(r["summary_json"])) == NO_WRITE_RESULT_SHA256]
    require(len(baseline) == 1 and baseline[0]["source_window_end"] == NO_WRITE_CHECKPOINT
            and baseline[0]["status"] == "noop" and baseline[0]["written"] == 0
            and baseline[0]["write_requests"] == 0 and baseline[0]["failure"] == 0,
            "no_write_baseline_unproven")
    if expected_checkpoint == NO_WRITE_CHECKPOINT:
        require(_hash(native) == NO_WRITE_NATIVE_SHA256
                and _hash(parent["sources"]["aupay_card"]) == NO_WRITE_PARENT_SHA256,
                "no_write_state_changed")
        require(financial_evidence(financial()) == NO_WRITE_FINANCIAL_SHA256, "no_write_financial_changed")
        require(_hash({k: money.read(k) for k in NO_WRITE_MONEY_KEYS}) == NO_WRITE_MONEY_SHA256,
                "no_write_money_changed")
    else:
        # A later daily run must have actually advanced beyond the failed job,
        # not just supplied some other older checkpoint to skip the first proof.
        require(datetime.fromisoformat(expected_checkpoint) > datetime.fromisoformat(NO_WRITE_CREATED.replace("Z", "+00:00")),
                "no_write_followup_unproven")


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
