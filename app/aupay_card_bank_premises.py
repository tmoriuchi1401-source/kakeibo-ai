"""Signed execution premises for a fixed card contract; GET-only evidence.

Only complete, zero-write bank runs may extend the legacy parent barrier.
The business record, its predecessor, and all native card bytes stay immutable.
No writer, capability, state transport, credential or new persistence is created.
"""
import base64
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path

from .aupay_card_window_preview import require
from .aupay_card_reservation_recovery import digest

BUSINESS_RECORD_SHA256 = "1558548ced53ca1689e5f076424b79c4fa88c95877bf35492eafb93a411ea7c5"
# PR211's bank control flow predates PR212's inert diagnostics callbacks.
# Both contracts are reviewed; bank content changes require separate review.
HISTORICAL_BANK_CODE_SHA256 = "3546b0022cc33d5cfec5721ab41540bd69d57c505e52c8df7be05889bfbb7995"
REVIEWED_BANK_CODE_SHA256 = frozenset({HISTORICAL_BANK_CODE_SHA256,
    # PR221: optional begin diagnostics and a card-only operator receipt.
    # Default bank begin/commit and its persisted payload/control flow are unchanged.
    '697ee56c44321eb0576a9c6fc4444525ba14cde95934e616af153aea35e692df',
    '4b4c1dc5777aaf0b497e0b809d5ba73c4b38e18dbbc0c854e46c2f1e9fa76d0e',
    '6bda01b7ad40955f281dec77ae848fe59ea17fc7cf209e5d7b7952729b93dcf1',
    'ae04d1e2d2c5fcd4aec1440e47e275ab88c92100a62623b0cfa5c58f23e3d7aa',
    "d93faaff02964e0235588d3eb0970984b5e29cedb29a61c3573b9d366234075d"})
BANK_WORKFLOW = ".github/workflows/kakeibo-production.yml"
CODE_PATHS = (BANK_WORKFLOW, "app/production_flow.py", "app/production_run.py",
              "app/production_ledger.py", "app/drive_run_state.py",
              "app/bank_pdf_recurring.py", "app/bank_review_refresh.py")
ZERO_COUNTS = ("written", "write_requests", "write_attempted", "income_created",
               "deposit_imports_created", "deposit_reviews_saved", "files_processed",
               "planned_expense_writes", "planned_import_updates", "planned_income_writes",
               "planned_deposit_imports", "planned_deposit_reviews", "failure", "catch_up_pending")
RUN_KEYS = ("id", "head_sha", "head_branch", "run_attempt", "event", "path",
            "created_at", "updated_at", "status", "conclusion")
JOB_KEYS = ("id", "name", "status", "conclusion", "started_at", "completed_at")
ROOT = Path(__file__).resolve().parents[1]


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def stamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None, "bank_premises_invalid")
    return result


def raw_node(node):
    raw = base64.b64decode(node["raw_b64"], validate=True)
    require(sha(raw) == node["ref"]["sha256"], "bank_premises_revision_changed")
    return raw


def code_contract():
    return {p: sha((ROOT / p).read_text(encoding="utf-8").encode()) for p in CODE_PATHS}


def reviewed_business(record):
    from .aupay_card_reservation_reference import NEW_RECORD_SHA256
    from .auto_expense_reconciliation import RECORD_SHA256 as AUTO_EXPENSE_RECORD_SHA256, POSTED_RECORD_SHA256
    return digest(record) in {BUSINESS_RECORD_SHA256, NEW_RECORD_SHA256, AUTO_EXPENSE_RECORD_SHA256, POSTED_RECORD_SHA256}


def validate_bank_chain(record, proof, parent_raw):
    """Replay archived exact proof without network or mutable present-day clocks."""
    from .drive_run_state import StateBinding, validate
    from .production_ledger import validate_ledger
    from .production_run import COUNT_KEYS, DEPENDENCIES
    from .aupay_card_incremental_recovery import _rows
    manifest = proof["manifest"]
    saved_archive = proof.get('saved_auto_expense_archive')
    posted = proof.get('posted_card_expenses')
    if posted is not None:
        from .auto_expense_reconciliation import validate_posted_summary
        validate_posted_summary(record,posted)
    if saved_archive is not None:
        from .auto_expense_reconciliation import validate_saved_archive
        validate_saved_archive(record, saved_archive)
        require(sha(base64.b64decode(saved_archive['reconciled_parent_exact_b64'],validate=True))
                ==record['parent_before_sha256'], 'bank_premises_parent_changed')
    require(reviewed_business(record) and proof["business_record"] == record
            and set(manifest) == {"schema", "business_record_sha256", "parent_snapshot", "bank_snapshot", "transitions"}
            and manifest["schema"] == 1 and manifest["business_record_sha256"] == digest(record)
            and 1 <= len(manifest["transitions"]) <= 16
            and len(proof["steps"]) == len(manifest["transitions"]), "bank_premises_invalid")
    b = record["bindings"]
    bank = next(x for x in record["other_native"] if x["source"] == "bank_pdf_drive")
    pb = StateBinding("production_run", b["spreadsheet_id"], b["folder_id"], b["parent_id"])
    bb = StateBinding("bank_pdf_drive", b["spreadsheet_id"], b["folder_id"], bank["file_id"])
    previous_parent_hash, previous_bank_hash = record["parent_before_sha256"], bank["sha256"]
    baseline_meta, final_meta = proof["baseline_parent_metadata"], proof["parent_metadata"]
    require(digest(baseline_meta) == record["parent_before_metadata_digest"]
            and digest(final_meta) == manifest["parent_snapshot"]["metadata_digest"]
            and set(baseline_meta) == set(final_meta)
            and all(final_meta[k] == v for k, v in baseline_meta.items() if k not in {"version", "modifiedTime", "size", "md5Checksum"})
            and int(final_meta["version"]) - int(baseline_meta["version"]) == 4 * len(proof["steps"]),
            "bank_premises_revision_changed")
    # Existing nonresumable state updates have two Drive version increments
    # per saved generation. Extra metadata/content churn is never excused.
    require(manifest["transitions"][0]["parent"][0]["modifiedTime"] == baseline_meta["modifiedTime"]
            and manifest["transitions"][-1]["parent"][-1]["modifiedTime"] == final_meta["modifiedTime"],
            "bank_premises_revision_changed")
    previous_end = stamp(record["old_failure"]["created_at"])
    used = set()
    for descriptor, step in zip(manifest["transitions"], proof["steps"]):
        require(set(descriptor) == {"parent", "bank", "run", "job_id", "log_sha256", "code_sha256"}
                and len(descriptor["parent"]) == len(descriptor["bank"]) == 3
                and [n["ref"] for n in step["parent"]] == descriptor["parent"]
                and [n["ref"] for n in step["bank"]] == descriptor["bank"], "bank_premises_incomplete")
        parent_bytes = [raw_node(n) for n in step["parent"]]
        bank_bytes = [raw_node(n) for n in step["bank"]]
        require(sha(parent_bytes[0]) == previous_parent_hash and sha(bank_bytes[0]) == previous_bank_hash,
                "bank_premises_chain_broken")
        parents = [validate_ledger(raw, pb) for raw in parent_bytes]
        banks = [validate(raw, bb) for raw in bank_bytes]
        first, pending, done = parents
        require([v["generation"] for v in parents] == list(range(first["generation"], first["generation"] + 3)),
                "bank_premises_revision_changed")
        expected = deepcopy(first); expected["generation"] += 1
        expected["sources"]["bank"].update(phase="pending", attempt=pending["sources"]["bank"]["attempt"], error="")
        require(first["sources"]["bank"]["phase"] == "ready" and not first["sources"]["bank"]["attempt"]
                and not first["sources"]["bank"]["error"] and pending == expected
                and len(pending["sources"]["bank"]["attempt"]) == 32,
                "bank_premises_parent_changed")
        require(all(first["sources"][k] == pending["sources"][k] == done["sources"][k]
                    for k in first["sources"] if k != "bank")
                and all(done[k] == first[k] for k in ("schema", "binding")), "bank_premises_parent_changed")
        n0, n1, n2 = banks
        expected_native = deepcopy(n0)
        expected_native.update(generation=n0["generation"] + 1, phase="pending", attempt=n1["attempt"])
        require(n0["phase"] == "ready" and not n0["attempt"] and n1 == expected_native and len(n1["attempt"]) == 32
                and n2["generation"] == n0["generation"] + 2 and n2["phase"] == "ready" and not n2["attempt"]
                and all(n2[k] == n0[k] for k in ("schema", "source", "binding"))
                and set(n2["files"]) == set(n0["files"]), "bank_premises_native_changed")
        require(all(item == n2["files"][name] for name, item in n0["files"].items() if name != "bank-recurring.sqlite3"),
                "bank_premises_native_changed")
        before = _rows(n0, "bank-recurring.sqlite3", "recurring_runs")
        after = _rows(n2, "bank-recurring.sqlite3", "recurring_runs")
        added = [row for row in after if row not in before]
        require(all(row in after for row in before) and len(added) == 1, "bank_premises_history_changed")
        row = added[0]; result = json.loads(row["summary_json"])
        require(result["status"] == row["status"] == "noop" and result["run_id"] == row["run_id"]
                and result["safe_noop"] is True and result["source"] == "bank_pdf_drive"
                and all(type(result[k]) is int and result[k] == 0 for k in ZERO_COUNTS), "bank_premises_not_noop")
        old_cp = _rows(n0, "bank-recurring.sqlite3", "recurring_checkpoint")
        new_cp = _rows(n2, "bank-recurring.sqlite3", "recurring_checkpoint")
        require(new_cp == old_cp or new_cp == [dict(singleton=1, successful_window_end=result["source_window_end"], run_id=result["run_id"])],
                "bank_premises_history_changed")
        counts = {k: v for k, v in result.items() if k in COUNT_KEYS and type(v) is int and v >= 0}
        counts["preview_cursor_epoch"] = max(first["sources"]["bank"]["counts"].get("preview_cursor_epoch", 0), counts.get("preview_cursor_epoch", 0))
        terminal = done["sources"]["bank"]
        require(terminal["phase"] == "ready" and not terminal["attempt"] and not terminal["error"] and terminal["counts"] == counts,
                "bank_premises_incomplete")
        run, job = step["run"], step["job"]
        require(set(run) == set(RUN_KEYS) and run == descriptor["run"] and run["id"] not in used
                and run["head_branch"] == "main" and run["event"] == "schedule" and run["path"] == BANK_WORKFLOW
                and run["run_attempt"] == 1 and run["status"] == "completed" and run["conclusion"] == "success"
                and set(job) == set(JOB_KEYS) and job["id"] == descriptor["job_id"] and job["name"] == "production"
                and job["status"] == "completed" and job["conclusion"] == "success", "bank_premises_run_unproven")
        used.add(run["id"])
        start, end = stamp(job["started_at"]), stamp(run["updated_at"])
        times = [stamp(descriptor["parent"][1]["modifiedTime"]), stamp(descriptor["bank"][1]["modifiedTime"]),
                 stamp(row["recorded_at"]), stamp(descriptor["bank"][2]["modifiedTime"]),
                 stamp(terminal["last_success"]), stamp(descriptor["parent"][2]["modifiedTime"])]
        require(previous_end < start and stamp(run["created_at"]) <= start, "bank_premises_run_unproven")
        require(all(start < t < end for t in times) and all(a < b for a, b in zip(times, times[1:])), "bank_premises_run_unproven")
        log = step["log"]
        require(sha(log.encode()) == descriptor["log_sha256"] and "EVENT_SCHEDULE: 47 21 * * *" in log,
                "bank_premises_run_unproven")
        completion = [line for line in log.splitlines() if '"scope": "drive_bank"' in line and '"mode": "apply"' in line]
        require(len(completion) == 1 and '"bank_mode": "apply"' in completion[0] and '"success": true' in completion[0]
                and terminal["last_success"] in completion[0]
                and not any('"' + source + '":' in completion[0] for source in DEPENDENCIES if source != "bank")
                and all('"' + k + '": 0' in completion[0] for k in ("written", "write_requests", "bank_home_write_requests", "bank_ui_updates", "bank_usage_metadata_writes", "bank_rule_applications_new", "bank_rule_counts_updated")),
                "bank_premises_run_unproven")
        require(set(step["code"]) == set(CODE_PATHS) and {p: sha(v.encode()) for p, v in step["code"].items()} == descriptor["code_sha256"],
                "bank_premises_code_changed")
        require(digest(descriptor["code_sha256"]) in REVIEWED_BANK_CODE_SHA256, "bank_premises_code_changed")
        workflow = step["code"][BANK_WORKFLOW]
        require("group: kakeibo-production" in workflow and "cancel-in-progress: false" in workflow
                and "'47 21 * * *') scope=drive_bank" in workflow, "bank_premises_code_changed")
        for label in ("parent", "bank"):
            suffix = proof["lineages"][label]
            ids = [n["id"] for n in descriptor[label]]
            positions = [next((i for i, v in enumerate(suffix) if v["id"] == rid), -1) for rid in ids]
            require(positions[0] >= 0 and positions == list(range(positions[0], positions[0] + 3))
                    and all(suffix[pos]["modifiedTime"] == ref["modifiedTime"] for pos, ref in zip(positions, descriptor[label])),
                    "bank_premises_revision_changed")
        previous_parent_hash, previous_bank_hash = sha(parent_bytes[2]), sha(bank_bytes[2])
        previous_end = end
    require(final_meta["md5Checksum"] == hashlib.md5(parent_raw).hexdigest()
            and int(final_meta["size"]) == len(parent_raw), "bank_premises_snapshot_changed")
    require(sha(parent_raw) == previous_parent_hash == manifest["parent_snapshot"]["sha256"]
            and previous_bank_hash == manifest["bank_snapshot"]["sha256"], "bank_premises_snapshot_changed")
    for label in ("parent", "bank"):
        lineage=proof["lineages"][label]
        require(len(lineage)==len({v["id"] for v in lineage})==2*len(proof["steps"])+1
                and lineage[0]["id"]==manifest["transitions"][0][label][0]["id"]
                and lineage[-1]["id"] == manifest["transitions"][-1][label][-1]["id"], "bank_premises_revision_changed")
    require(proof["history_complete"] is True and isinstance(proof["history"], list), "bank_premises_incomplete")
    for prior in proof["history"]:
        if prior["id"] in used:
            require(prior == next(s["run"] for s in proof["steps"] if s["run"]["id"] == prior["id"]), "bank_premises_run_unproven")
        elif prior["id"] == record["old_failure"]["id"]:
            require(all(prior.get(k) == v for k, v in record["old_failure"].items()), "bank_premises_run_unproven")
        elif saved_archive is not None and prior['id']==saved_archive['operator_run']['id']:
            require(all(prior.get(k)==v for k,v in saved_archive['operator_run'].items())
                and prior['status']=='completed' and prior['conclusion']=='failure',
                'bank_premises_other_writer')
        elif posted is not None and prior['id']==posted['operator']['id']:
            require(all(prior.get(k)==v for k,v in posted['operator'].items())
                and prior['status']=='completed' and prior['conclusion'] in {'failure','success'},
                'bank_premises_other_writer')
        elif prior["path"] == ".github/workflows/synthetic-tests.yml":
            require(prior["run_attempt"] == 1, "bank_premises_other_writer")
        else:
            jobs = proof["skipped_jobs"].get(str(prior["id"]), [])
            require(prior["status"] == "completed" and prior["conclusion"] == "skipped" and prior["run_attempt"] == 1
                    and jobs and all(j["status"] == "completed" and j["conclusion"] == "skipped" and j.get("steps") == [] for j in jobs),
                    "bank_premises_other_writer")
    require(used <= {p["id"] for p in proof["history"]}, "bank_premises_incomplete")
    return digest(proof)


def effective_record(business, proof):
    """Only this deterministic projection may change execution snapshots."""
    require(reviewed_business(business), "bank_premises_business_changed")
    parent_raw = raw_node(proof["steps"][-1]["parent"][-1])
    validate_bank_chain(business, proof, parent_raw)
    result = deepcopy(business)
    result["parent_before_sha256"] = proof["manifest"]["parent_snapshot"]["sha256"]
    result["parent_before_metadata_digest"] = proof["manifest"]["parent_snapshot"]["metadata_digest"]
    bank = next(x for x in result["other_native"] if x["source"] == "bank_pdf_drive")
    bank["sha256"] = proof["manifest"]["bank_snapshot"]["sha256"]
    result["business_record"] = deepcopy(business)
    result["recovery"]["bank_transitions"] = deepcopy(proof)
    return result


def validate_effective_record(record):
    business = record["business_record"]
    require(record == effective_record(business, record["recovery"]["bank_transitions"]), "bank_premises_business_changed")
    return business


def _job_log(github, job_id):
    """Existing GH_TOKEN, exact Actions GET; never forward it to log storage."""
    from urllib.request import Request, HTTPRedirectHandler, build_opener
    from urllib.parse import urlsplit
    from .aupay_card_window_preview import REPO
    require(type(job_id) is int and job_id > 0, "bank_premises_run_unproven")
    class Redirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            require(urlsplit(newurl).scheme == "https", "bank_premises_run_unproven")
            result = super().redirect_request(req, fp, code, msg, headers, newurl)
            result.remove_header("Authorization")
            return result
    request = Request(f"https://api.github.com/repos/{REPO}/actions/jobs/{job_id}/logs",
                      headers={"Authorization": "Bearer " + github.token})
    with build_opener(Redirect()).open(request, timeout=30) as response:
        raw = response.read(512 * 1024 + 1)
    require(len(raw) <= 512 * 1024, "bank_premises_incomplete")
    return raw.decode("utf-8")


def collect_bank_premises(record, manifest, baseline_metadata, drive, github, now, *, head,
                          current_run_id=None, log_read=None, saved_archive=None, posted_summary=None):
    """Independently materialize signed references. All Google/GitHub calls GET.

    Used by both readonly preparation and the locked hosted runtime. References
    may change after another proven bank no-op, never the pinned business bytes.
    """
    from .drive_run_state import StateBinding
    from .aupay_card_window_preview import ReadOnlyState
    from .aupay_card_window_evidence import GetOnly
    import time
    require(reviewed_business(record) and manifest["business_record_sha256"] == digest(record)
            and 1 <= len(manifest["transitions"]) <= 16, "bank_premises_business_changed")
    require(digest(baseline_metadata) == record["parent_before_metadata_digest"], "bank_premises_revision_changed")
    service = drive.api if isinstance(drive, GetOnly) else drive
    def get(request):
        require(request.method == "GET", "bank_premises_invalid")
        return request.execute(num_retries=0)
    b = record["bindings"]
    bank = next(x for x in record["other_native"] if x["source"] == "bank_pdf_drive")
    file_ids = dict(parent=b["parent_id"], bank=bank["file_id"])
    lineages = {}
    for label, fid in file_ids.items():
        refs, token = [], None
        for _ in range(32):
            value = get(service.revisions().list(fileId=fid, pageSize=1000, pageToken=token,
                fields="nextPageToken,revisions(id,modifiedTime)"))
            refs.extend(value.get("revisions", [])); token = value.get("nextPageToken")
            if not token: break
        require(not token and len({v["id"] for v in refs}) == len(refs), "bank_premises_incomplete")
        first = manifest["transitions"][0][label][0]["id"]
        position = next((i for i, v in enumerate(refs) if v["id"] == first), -1)
        require(position >= 0, "bank_premises_incomplete")
        lineages[label] = refs[position:]
    proof = dict(manifest=deepcopy(manifest), steps=[], lineages=lineages, business_record=deepcopy(record),
                 baseline_parent_metadata=deepcopy(baseline_metadata), history=[], skipped_jobs={}, history_complete=False)
    if saved_archive is not None:
        from .auto_expense_reconciliation import validate_saved_archive
        validate_saved_archive(record,saved_archive)
        proof['saved_auto_expense_archive']=deepcopy(saved_archive)
    if posted_summary is not None:
        from .auto_expense_reconciliation import validate_posted_summary
        validate_posted_summary(record,posted_summary)
        proof['posted_card_expenses']=deepcopy(posted_summary)
    for descriptor in manifest["transitions"]:
        step = {}
        for label, fid in file_ids.items():
            step[label] = []
            require(len(descriptor[label]) == 3, "bank_premises_incomplete")
            for ref in descriptor[label]:
                raw = get(service.revisions().get_media(fileId=fid, revisionId=ref["id"]))
                raw = raw.encode() if isinstance(raw, str) else raw
                step[label].append(dict(ref=deepcopy(ref), raw_b64=base64.b64encode(raw).decode()))
        run = github.get(f"actions/runs/{descriptor['run']['id']}")
        step["run"] = {k: run[k] for k in RUN_KEYS}
        comparison = github.get(f"compare/{run['head_sha']}...{head}")
        require(comparison["status"] in {"identical", "ahead"} and comparison["behind_by"] == 0,
                "bank_premises_code_changed")
        jobs = github.get(f"actions/runs/{run['id']}/jobs?per_page=100")
        require(jobs["total_count"] == len(jobs["jobs"]) == 1, "bank_premises_run_unproven")
        step["job"] = {k: jobs["jobs"][0][k] for k in JOB_KEYS}
        step["log"] = (log_read or (lambda jid: _job_log(github, jid)))(descriptor["job_id"]).replace("\r\n", "\n")
        step["code"] = {}
        for path in CODE_PATHS:
            value = github.get(f"contents/{path}?ref={run['head_sha']}")
            step["code"][path] = base64.b64decode(value["content"], validate=False).decode("utf-8").replace("\r\n", "\n")
        observed_contract = {p: sha(v.encode()) for p, v in step["code"].items()}
        require(digest(observed_contract) in REVIEWED_BANK_CODE_SHA256,
                "bank_premises_code_changed")
        proof["steps"].append(step)
    ro = GetOnly(service, time.monotonic() + 120)
    for label, source in (("parent", "production_run"), ("bank", "bank_pdf_drive")):
        binding = StateBinding(source, b["spreadsheet_id"], b["folder_id"], file_ids[label])
        raw, observed = ReadOnlyState(ro, binding).observe()
        require(observed == manifest[label + "_snapshot"] and raw == raw_node(proof["steps"][-1][label][-1]),
                "bank_premises_snapshot_changed")
        if label == "parent":
            proof["parent_metadata"] = get(service.files().get(fileId=file_ids[label], supportsAllDrives=True,
                fields="id,parents,trashed,mimeType,version,modifiedTime,size,md5Checksum"))
    # Full bounded inventory. No date-time coercion, first-page shortcuts, or
    # exemption for an unexplained successful financial workflow.
    beginning = stamp(record["old_failure"]["created_at"])
    all_runs, total = [], None
    for page in range(1, 35):
        value = github.get("actions/runs?per_page=100&created=" + beginning.date().isoformat() + ".." + now.date().isoformat() + "&page=" + str(page))
        if total is None: total = value["total_count"]
        require(total == value["total_count"] and total < 1000, "bank_premises_incomplete")
        all_runs.extend(value["workflow_runs"])
        if len(all_runs) >= total: break
    require(len(all_runs) == len({r["id"] for r in all_runs}) == total, "bank_premises_incomplete")
    used = {s["run"]["id"] for s in proof["steps"]}
    for run in all_runs:
        if stamp(run["created_at"]) > now or stamp(run["updated_at"]) < beginning or run["id"] == current_run_id:
            continue
        proof["history"].append({k: run[k] for k in RUN_KEYS})
        if run["id"] not in used | {record["old_failure"]["id"]} and run["path"] != ".github/workflows/synthetic-tests.yml":
            jobs = github.get(f"actions/runs/{run['id']}/jobs?per_page=100")
            require(jobs["total_count"] == len(jobs["jobs"]), "bank_premises_incomplete")
            proof["skipped_jobs"][str(run["id"])] = jobs["jobs"]
    proof["history_complete"] = True
    effective_record(record, proof)
    return proof
