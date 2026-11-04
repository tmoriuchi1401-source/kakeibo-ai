"""Daily auth and native bootstrap; real existing writers on invented data."""
from copy import deepcopy
from datetime import datetime, timedelta
import gc
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from app import aupay_card_first_window_apply as apply
from app import aupay_card_incremental_guard as guard
from app import aupay_card_incremental_recovery as recovery
from app.drive_run_state import validate
from app.production_run import COUNT_KEYS
from app.projection_store import ReadOnlyProjectionStore
from test_aupay_card_incremental_apply import followup, env, ENV, NOW
from test_aupay_card_incremental_recovery import ReconciledGitHub
from test_aupay_card_resume_recovery import NoWriteGitHub


def schedule_env(tmp_path):
    event = tmp_path / "schedule-event.json"
    event.write_text(json.dumps({"schedule": guard.DAILY_CRON}), encoding="utf-8")
    return env() | dict(GITHUB_EVENT_NAME="schedule", GITHUB_EVENT_PATH=str(event),
        SCHEDULE_CRON=guard.DAILY_CRON, KAKEIBO_CARD_SCHEDULE_ENABLED="true",
        GITHUB_ACTOR="schedule-service", GITHUB_TRIGGERING_ACTOR="schedule-service",
        CONFIRM="", EXPECTED_CHECKPOINT="untrusted-manual-input-is-ignored")


class DailyGitHub(NoWriteGitHub):
    def __init__(self):
        super().__init__()
        self.competing = False
        self.skip_bad = None
    def get(self, path):
        if path == "actions/runs/17":
            return super().get(path) | dict(event="schedule")
        if path == "actions/runs/88":
            return dict(id=88, head_sha="b"*40, head_branch="main", run_attempt=1,
                status="completed", conclusion="skipped", event="schedule",
                path=".github/workflows/" + guard.WORKFLOW)
        if path == "actions/runs/88/jobs?per_page=100":
            job = dict(name="apply", status="completed", conclusion="skipped", steps=[])
            if self.skip_bad == "body":
                job["steps"] = [dict(name="One checkpoint Apply with protected reviews and exact readback",
                    number=7, status="completed", conclusion="failure")]
            if self.skip_bad == "job": job["conclusion"] = "failure"
            return dict(total_count=1, jobs=[job])
        if self.competing and "actions/runs?status=in_progress" in path:
            return dict(total_count=1, workflow_runs=[dict(id=99)])
        return super().get(path)


def test_two_phase_schedule_auth_ignores_owner_confirmation_and_manual_checkpoint(tmp_path):
    values = schedule_env(tmp_path); github = DailyGitHub()
    assert guard.controls(values, "a"*40, github) is True
    assert guard.controls(values, "a"*40, github,
        expected_checkpoint="2026-11-03T14:45:43+09:00") is True
    assert values["EXPECTED_CHECKPOINT"] == "untrusted-manual-input-is-ignored"
    # A success after the native checkpoint cannot be silently reprocessed.
    github.prior.append(dict(id=90, status="completed", conclusion="success",
        run_attempt=1, created_at="2026-11-04T00:00:00Z"))
    assert guard.controls(values, "a"*40, github) is True
    with pytest.raises(RuntimeError):
        guard.controls(values, "a"*40, github, expected_checkpoint="2026-11-03T14:45:43+09:00")


def test_disabled_schedule_stops_before_credentials_or_google(tmp_path):
    values = schedule_env(tmp_path) | dict(KAKEIBO_CARD_SCHEDULE_ENABLED="false")
    with pytest.raises(RuntimeError, match="card_schedule_disabled"):
        apply.run(values, "a"*40, DailyGitHub(), incremental=True)


def test_manual_owner_confirmation_and_checkpoint_are_unchanged_when_flag_false():
    values = env() | dict(EXPECTED_CHECKPOINT=recovery.RECOVERED_CHECKPOINT,
        KAKEIBO_CARD_SCHEDULE_ENABLED="false")
    assert guard.controls(values, "a"*40, ReconciledGitHub()) is True
    for changed in (dict(GITHUB_ACTOR="schedule-service"), dict(CONFIRM="")):
        with pytest.raises(RuntimeError):
            guard.controls(values | changed, "a"*40, ReconciledGitHub())
    with pytest.raises(RuntimeError):
        guard.controls(values, "a"*40, ReconciledGitHub(), expected_checkpoint="2026-11-04T14:45:43+09:00")


def test_schedule_auth_and_skipped_history_execute_without_site_packages(tmp_path):
    class Recording(DailyGitHub):
        def __init__(self): super().__init__(); self.responses={}
        def get(self,path):
            value=super().get(path); self.responses[path]=value; return value
    github=Recording()
    github.prior.append(dict(id=88,head_sha="b"*40,status="completed",conclusion="skipped",run_attempt=1))
    values=schedule_env(tmp_path)
    guard.controls(values,"a"*40,github,expected_checkpoint="2026-11-03T14:45:43+09:00")
    script=("import json\nfrom app.aupay_card_incremental_guard import controls\n"
        f"env=json.loads({json.dumps(values)!r})\nresponses=json.loads({json.dumps(github.responses)!r})\n"
        "class GitHub:\n def get(self,path):return responses[path]\n"
        "assert controls(env,'a'*40,GitHub(),expected_checkpoint='2026-11-03T14:45:43+09:00') is True\n")
    result=subprocess.run([sys.executable,"-S","-B","-c",script],capture_output=True,text=True)
    assert result.returncode==0 and not result.stdout and not result.stderr


@pytest.mark.parametrize("change", ["missing_flag", "false_flag", "cron", "payload", "validated",
    "sha", "branch", "repo", "attempt", "competing", "event", "source", "flags"])
def test_schedule_requires_every_auth_condition(tmp_path, change):
    values = schedule_env(tmp_path); github = DailyGitHub()
    if change == "missing_flag": values.pop("KAKEIBO_CARD_SCHEDULE_ENABLED")
    if change == "false_flag": values["KAKEIBO_CARD_SCHEDULE_ENABLED"] = "false"
    if change == "cron": values["SCHEDULE_CRON"] = "24 0 * * *"
    if change == "payload": Path(values["GITHUB_EVENT_PATH"]).write_text('{"schedule":"24 0 * * *"}')
    if change == "validated": values["KAKEIBO_VALIDATED_MAIN_SHA"] = "b"*40
    if change == "sha": values["WORKFLOW_SHA"] = "b"*40
    if change == "branch": values["GITHUB_REF"] = "refs/heads/feature"
    if change == "repo": values["GITHUB_REPOSITORY"] = "someone/else"
    if change == "attempt": values["GITHUB_RUN_ATTEMPT"] = "2"
    if change == "competing": github.competing = True
    if change == "event": values["GITHUB_EVENT_NAME"] = "push"
    if change == "source": values["PREVIEW_SOURCE"] = "amazon"
    if change == "flags": values["KAKEIBO_SCHEDULE_ENABLED"] = "true"
    with pytest.raises((RuntimeError, KeyError)):
        guard.controls(values, "a"*40, github)


@pytest.mark.parametrize("bad", [None, "body", "job", "failure", "attempt"])
def test_only_independently_proven_whole_job_skips_are_ignored(tmp_path, bad):
    values = schedule_env(tmp_path); github = DailyGitHub()
    skipped = dict(id=88, head_sha="b"*40, status="completed", conclusion="skipped", run_attempt=1)
    if bad == "failure": skipped["conclusion"] = "failure"
    if bad == "attempt": skipped["run_attempt"] = 2
    github.skip_bad = bad; github.prior.append(skipped)
    if bad is None:
        assert guard.controls(values, "a"*40, github,
            expected_checkpoint="2026-11-03T14:45:43+09:00") is True
    else:
        with pytest.raises(RuntimeError): guard.controls(values, "a"*40, github)


def setup_entry(monkeypatch, tmp_path, *, new=2):
    import app.google_clients as google
    import app.private_state_bindings as bindings
    import app.projection_store as projections
    import app.drive_run_state as native
    import app.sheets as sheets
    import app.amazon_gmail_preview as gmail_module
    import app.aupay_card_window_preview as preview
    p = followup(tmp_path, new=new)
    p.gmail = p.args["gmail"]
    p.gmail.stamp = str(int((NOW-timedelta(hours=1)).timestamp())*1000)
    parent = json.loads(p.ledger.transport.payload)
    parent["sources"]["amazon"]["counts"]["catch_up_pending"] = 0
    p.ledger.transport.payload = json.dumps(parent).encode()
    payloads = {"card-file": p.durable.transport, "parent-file": p.ledger.transport}
    class Remote:
        def __init__(self, drive, binding): self.binding = binding
        def read(self): return payloads[self.binding.file_id].read()
        def observe(self): return self.read(), {"sha256": apply.digest(self.read().decode()), "metadata_digest": "stable"}
    monkeypatch.setattr(apply, "ReadOnlyState", Remote)
    monkeypatch.setattr(native, "DriveStateTransport", lambda svc, binding: payloads[binding.file_id])
    monkeypatch.setattr(bindings, "unwrap", lambda name, value, key: value)
    monkeypatch.setattr(google, "read_only_drive_service", lambda: object())
    monkeypatch.setattr(google, "drive_service", lambda: object())
    monkeypatch.setattr(google, "read_only_sheets_service", lambda: object())
    monkeypatch.setattr(google, "sheets_service", lambda: p.db)
    monkeypatch.setattr(sheets, "SheetsDB", lambda *a, **kw: p.db)
    monkeypatch.setattr(projections, "store_from_environment", lambda *a, **kw:
        ReadOnlyProjectionStore(p.store) if kw.get("read_only") else p.store)
    monkeypatch.setattr(preview, "financial_snapshot", lambda *a: {"sha256": apply.digest(p.db.rows), "rows": {}})
    monkeypatch.setattr(apply, "financial_data", lambda *a: p.execution["financial_read"]())
    p.gmail_starts = []
    monkeypatch.setattr(gmail_module, "gmail_readonly_service", lambda token: p.gmail_starts.append(True) or p.gmail)
    original_execute = apply.execute_fixed
    def execute(**kwargs):
        try: return original_execute(**kwargs)
        finally: gc.collect()
    monkeypatch.setattr(apply, "execute_fixed", execute)
    # Keep exception traceback cleanup from hiding a synthetic failure on Windows.
    original_temp = apply.tempfile.TemporaryDirectory
    monkeypatch.setattr(apply.tempfile, "TemporaryDirectory",
        lambda *a, **kw: original_temp(*a, **dict(kw, ignore_cleanup_errors=True)))
    p.now = NOW
    class Clock(datetime):
        @classmethod
        def now(cls, tz): return p.now.astimezone(tz)
    monkeypatch.setattr(apply, "datetime", Clock)
    p.guard_checkpoints = []; p.proofs = []
    def auth(values, head, github, *, expected_checkpoint=None):
        assert values["GITHUB_EVENT_NAME"] == "schedule"
        p.guard_checkpoints.append(expected_checkpoint)
        return True
    monkeypatch.setattr(guard, "controls", auth)
    def proof(value, ledger, cp):
        apply.require(value["phase"] == "ready" and not value["attempt"], "native_pending")
        run = recovery._rows(value, "recurring.sqlite3", "recurring_runs")
        checkpoint = recovery._rows(value, "recurring.sqlite3", "recurring_checkpoint")[0]
        summary = json.loads(next(r["summary_json"] for r in run if r["run_id"] == checkpoint["run_id"]))
        expected = {k:v for k,v in summary.items() if k in COUNT_KEYS and type(v) is int}
        apply.require(ledger["sources"]["aupay_card"]["counts"] == expected, "latest_counts_mismatch")
        p.proofs.append(cp)
    monkeypatch.setattr(recovery, "verify_reconciled_state", proof)
    p.env = dict(schedule_env(tmp_path) | ENV, RUNNER_TEMP=str(tmp_path), SPREADSHEET_ID=p.db.sid,
        GOOGLE_SERVICE_ACCOUNT_JSON=json.dumps({"private_key":"synthetic"}), GOOGLE_GMAIL_TOKEN_JSON="synthetic",
        KAKEIBO_STATE_FOLDER_ID="folder", AUPAY_CARD_STATE_FILE_ID="card-file", KAKEIBO_RUN_LEDGER_FILE_ID="parent-file",
        AUPAY_CARD_AUDIT_KEY_JSON=p.execution["key_provider"]._path.read_text("utf-8"),
        AUPAY_CARD_RECURRING_AUTHORITY_JSON=p.execution["authority_provider"].path.read_text("utf-8"))
    p.gmail.gets.clear()
    return p


def test_daily_real_entry_two_days_new_rows_then_noop_and_review_preservation(monkeypatch, tmp_path):
    p = setup_entry(monkeypatch, tmp_path)
    old_records = deepcopy(p.store.data["money"]["records"])
    first = apply.run(p.env, "a"*40, object(), incremental=True)
    assert first["written"] == 2 and first["amount_yen"] == 3423 and first["duplicate"] == 39
    assert first["held_protected"] == 5 and p.db.write_calls == 1 and not p.db.expenses
    assert all(p.store.data["money"]["records"][k] == v for k,v in old_records.items())
    book = deepcopy(p.store.data["money"])
    p.now = NOW + timedelta(days=1)
    second = apply.run(p.env, "a"*40, object(), incremental=True)
    assert second["written"] == second["financial_write_requests"] == 0
    assert second["duplicate"] == 41 and p.db.write_calls == 1 and p.store.data["money"] == book
    assert first["checkpoint"] == NOW.isoformat() and second["checkpoint"] == p.now.isoformat()
    assert p.proofs == [apply.END, NOW.isoformat()]
    assert set(p.guard_checkpoints) == {None, apply.END, NOW.isoformat()}
    assert p.env["EXPECTED_CHECKPOINT"] == "untrusted-manual-input-is-ignored"


@pytest.mark.parametrize("empty_mail", [False, True])
def test_daily_no_new_canonical_advances_only_after_review_and_readback(monkeypatch, tmp_path, empty_mail):
    p = setup_entry(monkeypatch, tmp_path, new=0)
    if empty_mail: p.gmail.raws = []
    before = deepcopy(p.store.data["money"]["records"])
    result = apply.run(p.env, "a"*40, object(), incremental=True)
    assert result["written"] == result["financial_write_requests"] == p.db.write_calls == 0
    assert result["checkpoint"] == NOW.isoformat() and not p.db.expenses
    after = p.store.data["money"]["records"]
    assert all(after[k] == v for k,v in before.items())
    assert len(after)-len(before) == (0 if empty_mail else 1)


@pytest.mark.parametrize("change", ["native_pending", "parent_pending", "counts", "recovery", "expired", "gmail", "unknown"])
def test_daily_safety_stops_preserve_checkpoint(monkeypatch, tmp_path, change):
    p = setup_entry(monkeypatch, tmp_path)
    if change == "native_pending":
        value=json.loads(p.durable.transport.payload);value.update(phase="pending",attempt="a"*32)
        p.durable.transport.payload=json.dumps(value).encode()
    if change in {"parent_pending", "counts"}:
        value=json.loads(p.ledger.transport.payload)
        if change == "parent_pending": value["sources"]["aupay_card"].update(phase="pending",attempt="a"*32)
        else: value["sources"]["aupay_card"]["counts"]["written"] += 1
        p.ledger.transport.payload=json.dumps(value).encode()
    if change == "recovery":
        monkeypatch.setattr(recovery,"verify_reconciled_state",lambda *a:apply.require(False,"recovery_proof_changed"))
    if change == "expired":
        value=json.loads(p.env["AUPAY_CARD_RECURRING_AUTHORITY_JSON"])
        value["expires_at"]=(NOW-timedelta(seconds=1)).isoformat()
        p.env["AUPAY_CARD_RECURRING_AUTHORITY_JSON"]=json.dumps(value)
    if change == "gmail": p.gmail.missing_raw=True
    if change == "unknown": p.db.mode="unknown"
    with pytest.raises(RuntimeError): apply.run(p.env,"a"*40,object(),incremental=True)
    assert p.db.write_calls == (1 if change == "unknown" else 0)
    if change not in {"gmail","unknown"}: assert not p.gmail_starts
    remote=validate(p.durable.transport.read(),p.durable.binding)
    assert recovery._rows(remote,"recurring.sqlite3","recurring_checkpoint")[0]["successful_window_end"] == apply.END
    if change == "unknown": assert remote["phase"] == "pending"


def test_daily_workflow_flag_defaults_off_and_preserves_existing_manual_conditions():
    w=yaml.load((apply.ROOT/".github/workflows"/guard.WORKFLOW).read_text("utf-8"),Loader=yaml.BaseLoader)
    assert w["on"]["schedule"] == [{"cron":"23 0 * * *"}]
    assert w["concurrency"]["group"] == "kakeibo-production"
    condition=w["jobs"]["apply"]["if"]
    assert "vars.KAKEIBO_CARD_SCHEDULE_ENABLED == 'true'" in condition
    assert "github.actor == github.repository_owner" in condition and "inputs.confirm" in condition
    assert "github.event.schedule == '23 0 * * *'" in condition
    env=w["jobs"]["apply"]["env"]
    assert "|| 'false'" in env["KAKEIBO_CARD_SCHEDULE_ENABLED"]
    assert env["EXPECTED_CHECKPOINT"] == "${{ inputs.expected_checkpoint }}"
    checkpoint = w["on"]["workflow_dispatch"]["inputs"]["expected_checkpoint"]
    assert checkpoint["required"] == "true" and "default" not in checkpoint
    assert all("GITHUB_ENV" not in s.get("run","") for s in w["jobs"]["apply"]["steps"])


@pytest.mark.parametrize('scheduled', [False, True])
@pytest.mark.parametrize('stop_proof', [False, True])
def test_resume_proofs_precede_gmail_and_writer_for_both_events(monkeypatch, tmp_path, scheduled, stop_proof):
    from test_aupay_card_resume_recovery import amazon_proof
    p = setup_entry(monkeypatch, tmp_path)
    parent, amazon, _ = amazon_proof(p.db.sid, 'folder')
    value = json.loads(p.ledger.transport.payload)
    value['sources']['amazon'] = dict(parent['sources']['amazon'], duration_seconds=0)
    p.ledger.transport.payload = json.dumps(value).encode()
    original_remote = apply.ReadOnlyState
    monkeypatch.setattr(apply, 'ReadOnlyState', lambda drive,binding:
        amazon if binding.file_id == 'synthetic-amazon' else original_remote(drive,binding))
    p.env['AMAZON_STATE_FILE_ID'] = 'synthetic-amazon'
    if not scheduled:
        p.env.update(GITHUB_EVENT_NAME='workflow_dispatch', EXPECTED_CHECKPOINT=apply.END)
    monkeypatch.setattr(guard, 'controls', lambda *a, **kw:True)
    monkeypatch.setattr(recovery, 'NO_WRITE_CHECKPOINT', apply.END)
    checked = []
    def fixed_proof(native,parent,cp,*,financial,money):
        assert native['phase'] == parent['sources']['aupay_card']['phase'] == 'ready'
        assert cp == apply.END and not p.gmail_starts and p.db.write_calls == 0
        assert financial() and money.read('money') == p.store.data['money']
        checked.append(True)
        if stop_proof: raise RuntimeError('synthetic_fixed_proof_rejected')
    monkeypatch.setattr(recovery, 'verify_no_write_state', fixed_proof)
    if stop_proof:
        with pytest.raises(RuntimeError, match='synthetic_fixed_proof_rejected'):
            apply.run(p.env, 'a'*40, object(), incremental=True)
        assert not p.gmail_starts and p.db.write_calls == 0
        assert p.durable.inspect()['phase'] == 'ready'
    else:
        result = apply.run(p.env, 'a'*40, object(), incremental=True)
        assert result['written'] == 2 and p.db.write_calls == 1
        with pytest.raises(RuntimeError):
            apply.execute_fixed(**p.execution)
        assert p.db.write_calls == 1
    assert checked == [True]
