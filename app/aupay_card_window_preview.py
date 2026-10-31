"""One owner-dispatched first-window Preview; no persistent writer surface."""
from __future__ import annotations

import base64
from contextlib import closing, redirect_stdout, redirect_stderr
from datetime import datetime
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

REPO = "tmoriuchi1401-source/kakeibo-ai"
WORKFLOW = "aupay-card-window-preview.yml"
SOURCE = "au_pay_card_gmail"
START = "2026-10-13T08:55:56+09:00"
END = "2026-10-27T08:55:56+09:00"
CHECKPOINT = "2026-10-15T08:55:56+09:00"
ROOT = Path(__file__).resolve().parents[1]
FLAGS = {"KAKEIBO_PRODUCTION_ENABLED": "true", "KAKEIBO_LEGACY_DISABLED": "true",
         "KAKEIBO_BANK_SCHEDULE_ENABLED": "true", "KAKEIBO_SCHEDULE_ENABLED": "false",
         "CATEGORY_BACKFILL_SHEET_RUNNER_ENABLED": "false"}
COUNTS = ("found", "fetched", "new_eligible", "already_present", "duplicate", "withheld", "review",
          "money_eligible", "money_review", "money_supplement", "money_duplicate", "money_notice_review")
SOURCE_ERRORS = frozenset({"incremental_gmail_collection_incomplete", "new_eligible_exceeds_bounded_batch",
    "recurring_authority_scope_mismatch", "money_batch_limit_exceeded", "money_reconciliation_required",
    "money_identity_conflict", "money_projection_refresh_required", "preview_projection_write_forbidden", "preview_timeout"})


class Stop(RuntimeError):
    """Only fixed, locally authored codes may cross the public boundary."""


def require(ok, code):
    if not ok:
        raise Stop(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def boundary(env, head):
    require(re.fullmatch("[0-9a-f]{40}", head) is not None, "commit_invalid")
    require(env.get("GITHUB_ACTIONS") == "true" and env.get("GITHUB_REPOSITORY") == REPO
            and env.get("GITHUB_REF") == "refs/heads/main"
            and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "manual_main_required")
    require(head == env.get("GITHUB_SHA") == env.get("WORKFLOW_SHA")
            == env.get("EXPECTED_COMMIT") == env.get("KAKEIBO_VALIDATED_MAIN_SHA"), "commit_mismatch")
    require(env.get("WORKFLOW_REF") == f"{REPO}/.github/workflows/{WORKFLOW}@refs/heads/main",
            "workflow_mismatch")
    require(env.get("GITHUB_ACTOR") == env.get("GITHUB_TRIGGERING_ACTOR") == REPO.split("/")[0]
            and env.get("GITHUB_RUN_ATTEMPT") == "1", "owner_attempt_required")
    require(env.get("CONFIRM") == "PREVIEW_ONLY" and env.get("PREVIEW_SOURCE") == SOURCE
            and env.get("PREVIEW_START") == START and env.get("PREVIEW_END") == END
            and env.get("EXPECTED_CHECKPOINT") == CHECKPOINT, "fixed_window_mismatch")
    require(env.get("RUNNER_DEBUG", "0") != "1"
            and not env.get("ACTIONS_STEP_DEBUG") and not env.get("ACTIONS_RUNNER_DEBUG"), "debug_forbidden")
    require(all(env.get(k) == v for k, v in FLAGS.items()), "production_flags_mismatch")


class GitHub:
    def __init__(self, env): self.token = env["GH_TOKEN"]

    def get(self, path):
        # This client has no mutation method. API payloads/exceptions stay private.
        request = Request(f"https://api.github.com/repos/{REPO}/{path}",
                          headers={"Authorization": "Bearer " + self.token,
                                   "Accept": "application/vnd.github+json"})
        with urlopen(request, timeout=30) as response:
            return json.load(response)


def controls(env, head, github):
    boundary(env, head)
    require(github.get("git/ref/heads/main")["object"]["sha"] == head, "main_changed")
    # vars are GitHub's job-start snapshot, as in the unified production guard.
    # GITHUB_TOKEN has no repository Variables permission; do not introduce a
    # PAT or pretend contents/actions:read can continuously observe Variables.
    current = int(env["GITHUB_RUN_ID"])
    run = github.get(f"actions/runs/{current}")
    require(run["head_sha"] == head and run["run_attempt"] == 1
            and run["event"] == "workflow_dispatch"
            and run["path"] == ".github/workflows/" + WORKFLOW, "run_identity_mismatch")
    # Query active statuses separately, so a high volume of completed CI cannot
    # hide an older competing run. Never queue/retry a source after this guard.
    for status in ("in_progress", "queued", "waiting", "pending", "requested"):
        runs = github.get(f"actions/runs?status={status}&per_page=100")
        require(runs["total_count"] <= 100 and len(runs["workflow_runs"]) == runs["total_count"],
                "run_inventory_incomplete")
        require(all(r["id"] == current for r in runs["workflow_runs"]), "competing_run")
    previous = github.get(f"actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch&head_sha={head}&per_page=100")
    require(previous["total_count"] == 1 and len(previous["workflow_runs"]) == 1
            and previous["workflow_runs"][0]["id"] == current, "previous_dispatch")


def checkpoint(value):
    require(value["phase"] == "ready" and not value["attempt"], "card_pending")
    checkpoints = []
    for name, item in value["files"].items():
        if not name.endswith(".sqlite3"):
            continue
        with closing(sqlite3.connect(":memory:")) as con:
            con.deserialize(base64.b64decode(item["data"], validate=True))
            con.execute("PRAGMA trusted_schema=OFF")
            con.execute("PRAGMA query_only=ON")
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "recurring_checkpoint" in tables:
                rows = con.execute("SELECT successful_window_end,run_id FROM recurring_checkpoint WHERE singleton=1").fetchall()
                require(len(rows) == 1, "checkpoint_invalid")
                cp, rid = rows[0]
                last = con.execute("SELECT status,window_end FROM recurring_runs WHERE run_id=?", (rid,)).fetchone()
                require(last and last[0] in ("noop", "complete") and last[1] == cp, "checkpoint_invalid")
                checkpoints.append(cp)
            if "writer_leases" in tables:
                require(con.execute("SELECT COUNT(*) FROM writer_leases").fetchone()[0] == 0, "card_native_pending")
            if "batch_capabilities" in tables:
                require(con.execute("SELECT COUNT(*) FROM batch_capabilities WHERE state!='sealed'").fetchone()[0] == 0,
                        "card_native_pending")
            if "batch_events" in tables:
                cur = con.execute("SELECT run_id,attempt_id,stage,state FROM batch_events ORDER BY seq")
                latest = {(r[0], r[1]): r[2:] for r in cur}
                require(all(r[0] == "final" and r[1] in ("confirmed", "write_confirmed") for r in latest.values()),
                        "card_native_pending")
    require(checkpoints == [CHECKPOINT], "checkpoint_changed")
    return CHECKPOINT


class ReadOnlyState:
    def __init__(self, drive, binding): self.drive, self.binding = drive, binding

    def observe(self):
        # Compare content AND version/metadata, including a change-and-revert.
        args = dict(fileId=self.binding.file_id, supportsAllDrives=True,
                    fields="id,parents,trashed,mimeType,version,modifiedTime,size,md5Checksum")
        before = self.drive.files().get(**args).execute(num_retries=0)
        require(before.get("id") == self.binding.file_id and not before.get("trashed")
                and before.get("parents") == [self.binding.folder_id]
                and before.get("mimeType") == "application/json" and before.get("version"), "state_target_mismatch")
        raw = self.drive.files().get_media(fileId=self.binding.file_id, supportsAllDrives=True).execute(num_retries=0)
        after = self.drive.files().get(**args).execute(num_retries=0)
        require(before == after, "state_changed_during_read")
        return raw, {"sha256": hashlib.sha256(raw).hexdigest(), "metadata_digest": digest(after)}

    def read(self): return self.observe()[0]
    def write(self, payload): raise Stop("persistent_state_write_forbidden")


FINANCIAL_SHEETS = ("取込データ", "支出明細", "収入明細", "レシート", "Amazon注文")


def financial_snapshot(service, sid):
    sheets = service.spreadsheets()
    metadata = sheets.get(spreadsheetId=sid, fields="spreadsheetId,sheets(properties)").execute(num_retries=0)
    require(metadata.get("spreadsheetId") == sid, "financial_target_mismatch")
    props = {s["properties"]["title"]: s["properties"] for s in metadata["sheets"]}
    require(all(t in props for t in FINANCIAL_SHEETS[:3]), "financial_sheet_missing")
    titles = [t for t in FINANCIAL_SHEETS if t in props]
    # Whole worksheet ranges, not a fixed row/column cutoff; sparse tail included.
    ranges = ["'" + t.replace("'", "''") + "'" for t in titles]
    observations = {}
    for render in ("UNFORMATTED_VALUE", "FORMULA"):
        result = sheets.values().batchGet(spreadsheetId=sid, ranges=ranges,
            valueRenderOption=render, dateTimeRenderOption="SERIAL_NUMBER").execute(num_retries=0)
        require(result.get("spreadsheetId") == sid and len(result.get("valueRanges", [])) == len(titles),
                "financial_collection_incomplete")
        require(all(any(item.get("range", "").startswith(prefix) for prefix in (quoted + "!", title + "!"))
                    for item, quoted, title in zip(result["valueRanges"], ranges, titles)), "financial_range_mismatch")
        values = [v.get("values", []) for v in result["valueRanges"]]
        require(sum(len(json.dumps(v, ensure_ascii=True)) for v in values) <= 64 * 1024 * 1024,
                "financial_snapshot_too_large")
        observations[render] = values
    return {"sha256": digest([[(t, props[t]) for t in titles], observations]),
            "rows": {t: sum(any(c != "" for c in r) for r in rows[1:])
                     for t, rows in zip(titles, observations["UNFORMATTED_VALUE"])}}


def observe(card, parent, sheets, sid, money_store):
    from .drive_run_state import validate
    from .production_ledger import validate_ledger
    raw, card_proof = card.observe()
    cp = checkpoint(validate(raw, card.binding))
    raw, parent_proof = parent.observe()
    ledger = validate_ledger(raw, parent.binding)
    require(all(v["phase"] == "ready" and not v["attempt"] and not v["error"]
                for v in ledger["sources"].values()), "parent_pending")
    require(ledger["sources"]["amazon"]["counts"].get("catch_up_pending") == 0, "amazon_catchup_pending")
    money = {key: money_store.read(key) for key in ("money", "money-migration", "money-notices", "journal")} if money_store else {}
    return {"card": card_proof, "parent": parent_proof, "checkpoint": cp,
            "financial": financial_snapshot(sheets, sid), "money_digest": digest(money)}


def checked_preview(observation, execute):
    """Even a reported source failure gets a fresh, independent after-read."""
    before = observation()
    result = None
    try:
        result = execute()
    finally:
        after = observation()
        for field in ("card", "parent", "checkpoint", "financial", "money_digest"):
            require(before[field] == after[field], "after_" + field + "_changed")
    require(isinstance(result, dict) and result.get("status") in {"dry_run_ready", "dry_run_noop"}
            and result.get("failure") == 0, "card_preview_failed")
    require(result.get("source_window_start") == START and result.get("source_window_end") == END,
            "source_window_mismatch")
    require(result.get("manifest_final") == "not_created" and result.get("capability_final") == "not_issued"
            and result.get("journal_final") in {"not_started", "not_needed"}
            and result.get("lease_final") in {"not_acquired", "not_needed"}, "writer_path_started")
    for key in ("written", "write_requests", "money_posted", "expense_rows_written", "import_rows_written"):
        require(type(result.get(key, 0)) is int and result.get(key, 0) == 0, "financial_write_detected")
    counts = {key: result.get(key, 0) for key in COUNTS}
    require(all(type(v) is int and v >= 0 for v in counts.values())
            and counts["found"] == counts["fetched"] <= 500
            and counts["new_eligible"] + counts["money_eligible"] <= 100, "summary_limits_invalid")
    return {"counts": counts, "before": before, "after": after,
            "financial_write_requests": 0, "checkpoint_updates": 0,
            "persistent_state_saves": 0, "other_source_executions": 0,
            "local_preview_history": "one summary in disposable SQLite only; discarded"}


def run(env, head, github):
    controls(env, head, github)
    from .aupay_card_recurring import SqliteRecurringRunState, build_explicit_preview_window, run_recurring_ingestion
    from .aupay_card_batch import ProtectedRecurringAuthorityProvider
    from .aupay_card_production import ProtectedAuditKeyProvider
    from .amazon_gmail_preview import gmail_readonly_service
    from .drive_run_state import DurableState, StateBinding
    from .google_clients import read_only_drive_service, read_only_sheets_service
    from .private_state_bindings import unwrap
    from .production_flow import _secret_file
    from .production_run import run_durable_source
    from .projection_store import ProjectionJournal, store_from_environment
    from .sheets import SheetsDB

    info = json.loads(env["GOOGLE_SERVICE_ACCOUNT_JSON"])
    decoded = {n: unwrap(n, env[n], info["private_key"]) for n in
               ("KAKEIBO_STATE_FOLDER_ID", "AUPAY_CARD_STATE_FILE_ID", "KAKEIBO_RUN_LEDGER_FILE_ID")}
    sid, folder = env["SPREADSHEET_ID"], decoded["KAKEIBO_STATE_FOLDER_ID"]
    drive, sheets = read_only_drive_service(), read_only_sheets_service()
    card = ReadOnlyState(drive, StateBinding(SOURCE, sid, folder, decoded["AUPAY_CARD_STATE_FILE_ID"]))
    parent = ReadOnlyState(drive, StateBinding("production_run", sid, folder, decoded["KAKEIBO_RUN_LEDGER_FILE_ID"]))
    money_store = store_from_environment(sid, env, read_only=True)
    if env.get("KAKEIBO_AMAZON_MONEY_MODE") == "confirmed-v1":
        require(money_store is not None, "money_binding_required")
    with tempfile.TemporaryDirectory(prefix="aupay-window-", dir=env["RUNNER_TEMP"]) as temp:
        private = Path(temp)
        authority = ProtectedRecurringAuthorityProvider(_secret_file(private, "authority.json",
            env["AUPAY_CARD_RECURRING_AUTHORITY_JSON"]), repo_root=ROOT)
        key = ProtectedAuditKeyProvider(_secret_file(private, "key.json", env["AUPAY_CARD_AUDIT_KEY_JSON"]), repo_root=ROOT)
        policy = authority.load()
        require(policy.source == SOURCE and policy.expected_spreadsheet_id == sid
                and policy.expected_worksheet == "取込データ", "authority_target_mismatch")
        require((policy.overlap_seconds, policy.max_window_seconds, policy.max_messages, policy.max_batch_size)
                == (172800, 1209600, 500, 100)
                and policy.valid_from.isoformat() == "2026-09-11T00:00:00+09:00"
                and policy.expires_at.isoformat() == "2027-09-11T00:00:00+09:00", "authority_policy_changed")
        db = SheetsDB(sid, service=sheets,
                      projection_journal=ProjectionJournal(money_store) if money_store else None)
        def execute():
            def source(path):
                state = SqliteRecurringRunState(path / "recurring.sqlite3", repo_root=ROOT)
                require(state.successful_window_end() == datetime.fromisoformat(CHECKPOINT), "checkpoint_changed")
                # Validate before making even a Gmail authentication request.
                now = datetime.now(ZoneInfo("Asia/Tokyo"))
                window = tuple(datetime.fromisoformat(v) for v in (START, END))
                build_explicit_preview_window(policy, state, now, *window)
                result = run_recurring_ingestion(gmail_service=gmail_readonly_service(env["GOOGLE_GMAIL_TOKEN_JSON"]),
                    db=db, state=state, key_provider=key, authority_provider=authority,
                    state_dir=path, repo_root=ROOT, now=datetime.now(ZoneInfo("Asia/Tokyo")),
                    dry_run=True, preview_window=window)
                require(state.successful_window_end() == datetime.fromisoformat(CHECKPOINT), "local_checkpoint_changed")
                if result.get("failure"):
                    code = result.get("failure_reason")
                    raise Stop(code if isinstance(code, str) and code in SOURCE_ERRORS else "card_preview_failed")
                return result
            return run_durable_source(DurableState(card, card.binding), private / "state", source, apply=False)
        try:
            return checked_preview(lambda: observe(card, parent, sheets, sid, money_store), execute)
        finally:
            controls(env, head, github)


def main():
    result = {"success": False}
    # Dependency output, API exceptions, raw source summaries and tracebacks are
    # memory-only. No tee, step summary, raw artifact, cache save or Google write.
    logging.disable(logging.CRITICAL)
    socket.setdefaulttimeout(30)
    def timeout(*_): raise Stop("preview_timeout")
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, timeout)
        signal.alarm(900)
    signal.signal(signal.SIGTERM, timeout)
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            env = dict(os.environ)
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
            github = GitHub(env)
            if sys.argv[1:] == ["guard"]:
                controls(env, head, github)
                result = {"success": True, "guard": True}
            else:
                require(not sys.argv[1:], "arguments_forbidden")
                result = {"success": True, **run(env, head, github), "commit": head,
                          "source": SOURCE, "start": START, "end": END,
                          "observed_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat()}
    except Stop as error:
        result = {"success": False, "error": str(error)}
    except BaseException:
        result = {"success": False, "error": "preview_failed_private_details_suppressed"}
    finally:
        if hasattr(signal, "SIGALRM"): signal.alarm(0)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
