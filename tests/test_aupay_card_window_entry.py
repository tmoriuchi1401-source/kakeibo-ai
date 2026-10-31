"""New entry contracts only; synthetic clients, no production credentials."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app import aupay_card_window_preview as entry
from app.drive_run_state import envelope, snapshot, StateBinding
from test_aupay_card_window_preview import parts, Gmail, NOW, START, END

class Request:
    def __init__(self, value): self.value = value
    def execute(self, **kwargs): return self.value

SHA = "a" * 40


def env():
    return dict(entry.FLAGS, GITHUB_ACTIONS="true", GITHUB_REPOSITORY=entry.REPO,
        GITHUB_REF="refs/heads/main", GITHUB_EVENT_NAME="workflow_dispatch",
        GITHUB_SHA=SHA, WORKFLOW_SHA=SHA, EXPECTED_COMMIT=SHA, KAKEIBO_VALIDATED_MAIN_SHA=SHA,
        WORKFLOW_REF=f"{entry.REPO}/.github/workflows/{entry.WORKFLOW}@refs/heads/main",
        GITHUB_ACTOR=entry.REPO.split("/")[0], GITHUB_TRIGGERING_ACTOR=entry.REPO.split("/")[0],
        GITHUB_RUN_ATTEMPT="1", GITHUB_RUN_ID="42", CONFIRM="PREVIEW_ONLY",
        PREVIEW_SOURCE=entry.SOURCE, PREVIEW_START=entry.START, PREVIEW_END=entry.END,
        EXPECTED_CHECKPOINT=entry.CHECKPOINT)


class GitHub:
    def __init__(self): self.other = False; self.history = 1; self.head = SHA
    def get(self, path):
        if path == "git/ref/heads/main": return {"object": {"sha": self.head}}
        if path == "actions/runs/42": return dict(head_sha=SHA, run_attempt=1,
            event="workflow_dispatch", path=".github/workflows/"+entry.WORKFLOW)
        if path.startswith("actions/runs?"):
            return dict(total_count=1, workflow_runs=[{"id": 43 if self.other else 42}])
        assert path.startswith("actions/workflows/"+entry.WORKFLOW+"/runs?")
        return dict(total_count=self.history, workflow_runs=[{"id": 42}])


@pytest.mark.parametrize("key,value,code", [
    ("GITHUB_REF", "refs/heads/feature", "manual_main"),
    ("GITHUB_EVENT_NAME", "schedule", "manual_main"),
    ("GITHUB_SHA", "b"*40, "commit_mismatch"),
    ("WORKFLOW_SHA", "b"*40, "commit_mismatch"),
    ("KAKEIBO_VALIDATED_MAIN_SHA", "b"*40, "commit_mismatch"),
    ("GITHUB_RUN_ATTEMPT", "2", "owner_attempt"),
    ("GITHUB_TRIGGERING_ACTOR", "other", "owner_attempt"),
    ("PREVIEW_SOURCE", "amazon_gmail", "fixed_window"),
    ("PREVIEW_START", "2026-10-13T08:55:57+09:00", "fixed_window"),
    ("PREVIEW_END", "2026-10-27T08:55:57+09:00", "fixed_window"),
    ("EXPECTED_CHECKPOINT", "2026-10-16T08:55:56+09:00", "fixed_window"),
    ("CONFIRM", "APPLY", "fixed_window"),
    ("RUNNER_DEBUG", "1", "debug_forbidden"),
    ("KAKEIBO_BANK_SCHEDULE_ENABLED", "false", "flags_mismatch"),
])
def test_reject_wrong_identity_before_google(key, value, code):
    values = env(); values[key] = value
    with pytest.raises(entry.Stop, match=code): entry.controls(values, SHA, GitHub())


def test_live_main_competition_and_previous_dispatch():
    github = GitHub(); entry.controls(env(), SHA, github)
    for field, value, code in [("head", "b"*40, "main_changed"),
                              ("other", True, "competing_run"), ("history", 2, "previous_dispatch")]:
        github = GitHub(); setattr(github, field, value)
        with pytest.raises(entry.Stop, match=code): entry.controls(env(), SHA, github)


def test_native_checkpoint_and_pending_are_read_only(tmp_path):
    p = parts(tmp_path)
    binding = StateBinding(entry.SOURCE, "sheet-production", "folder", "card")
    value = json.loads(envelope(binding, native_files(p, tmp_path / "native", binding)))
    before = deepcopy(value)
    assert entry.checkpoint(value) == entry.CHECKPOINT
    assert value == before
    value["phase"] = "pending"
    with pytest.raises(entry.Stop, match="card_pending"): entry.checkpoint(value)
    value = deepcopy(before)
    # A real checkpoint move is rejected even if its run also says complete.
    import sqlite3, base64
    con = sqlite3.connect(":memory:"); con.deserialize(base64.b64decode(value["files"]["recurring.sqlite3"]["data"]))
    con.execute("UPDATE recurring_checkpoint SET successful_window_end='2026-10-16T08:55:56+09:00'")
    con.commit(); value["files"]["recurring.sqlite3"]["data"] = base64.b64encode(con.serialize()).decode(); con.close()
    with pytest.raises(entry.Stop, match="checkpoint_invalid|checkpoint_changed"): entry.checkpoint(value)


class Financial:
    def __init__(self):
        self.value = 12; self.formula = "=6*2"; self.calls = []; self.omit = False
    def spreadsheets(self): return self
    def values(self): return self
    def get(self, **kw):
        return Request(dict(spreadsheetId="sheet", sheets=[{"properties": {
            "title": title, "sheetId": n, "gridProperties": {"rowCount": 30001, "columnCount": 40}}}
            for n, title in enumerate(entry.FINANCIAL_SHEETS)]))
    def batchGet(self, **kw):
        self.calls.append(kw)
        rows = [["header"], [self.formula if kw["valueRenderOption"] == "FORMULA" else self.value]]
        # A sparse row beyond typical bounded readers is included.
        rows += [[]]*20000 + [["tail"]]
        return Request(dict(spreadsheetId="sheet", valueRanges=[{"values": rows, "range": r+"!A1:AN30001"}
            for r in kw["ranges"]][1 if self.omit else 0:]))


def test_financial_snapshot_values_formulas_tail_and_completeness():
    api = Financial(); first = entry.financial_snapshot(api, "sheet")
    assert set(api.calls[0]["ranges"]) == {"'"+t+"'" for t in entry.FINANCIAL_SHEETS}
    assert all(n == 2 for n in first["rows"].values())
    api.formula = "=3*4"
    assert entry.financial_snapshot(api, "sheet")["sha256"] != first["sha256"]
    api.formula = "=6*2"; api.value = 13
    assert entry.financial_snapshot(api, "sheet")["sha256"] != first["sha256"]
    api.omit = True
    with pytest.raises(entry.Stop, match="collection_incomplete"): entry.financial_snapshot(api, "sheet")


def summary():
    return dict(status="dry_run_ready", failure=0, source_window_start=entry.START,
        source_window_end=entry.END, manifest_final="not_created", capability_final="not_issued",
        journal_final="not_started", lease_final="not_acquired", found=500, fetched=500, new_eligible=100)


def proof():
    return dict.fromkeys(("card", "parent", "checkpoint", "financial", "money_digest"), "original")


@pytest.mark.parametrize("field", ["card", "parent", "checkpoint", "financial", "money_digest"])
def test_after_change_stops_including_source_failure(field):
    before = {k: "original" for k in ("card", "parent", "checkpoint", "financial", "money_digest")}
    after = dict(before); after[field] = "changed"
    for source in (lambda: summary(), lambda: (_ for _ in ()).throw(RuntimeError("private"))):
        reads = iter([before, after])
        with pytest.raises(entry.Stop, match="after_"+field+"_changed"):
            entry.checked_preview(lambda: next(reads), source)


@pytest.mark.parametrize("key,value,code", [
    ("found", 501, "limits_invalid"), ("new_eligible", 101, "limits_invalid"),
    ("money_eligible", 1, "limits_invalid"), ("written", 1, "write_detected"),
    ("write_requests", 1, "write_detected"), ("money_posted", 1, "write_detected"),
    ("manifest_final", "persisted", "writer_path"), ("capability_final", "issued", "writer_path"),
    ("source_window_end", "unexpected", "window_mismatch"),
])
def test_preview_summary_limits_and_zero_writes(key, value, code):
    result = summary(); result[key] = value
    with pytest.raises(entry.Stop, match=code): entry.checked_preview(proof, lambda: result)


def test_source_exception_still_gets_after_read():
    reads = []
    def observation(): reads.append(1); return dict.fromkeys(("card", "parent", "checkpoint", "financial", "money_digest"))
    with pytest.raises(RuntimeError, match="private"):
        entry.checked_preview(observation, lambda: (_ for _ in ()).throw(RuntimeError("private")))
    assert len(reads) == 2


def test_real_card_dry_run_disposable_copy_and_persistent_equality(tmp_path):
    from app.drive_run_state import DurableState
    from app.production_run import run_durable_source
    from app import aupay_card_recurring as card
    p = parts(tmp_path)
    binding = StateBinding(entry.SOURCE, "sheet-production", "folder", "card")
    raw = envelope(binding, native_files(p, tmp_path / "native", binding))
    class Transport:
        def read(self): return raw
        def write(self, value): raise AssertionError("persistent_write")
    disposable = tmp_path / "outside" / "copy"
    gmail = Gmail()
    def source(path):
        return card.run_recurring_ingestion(gmail_service=gmail, db=p[5],
            state=card.SqliteRecurringRunState(path/"recurring.sqlite3", repo_root=p[0]),
            key_provider=p[3], authority_provider=p[4], state_dir=path, repo_root=p[0],
            now=NOW, dry_run=True, preview_window=(START, END), sleeper=lambda _: None)
    result = run_durable_source(DurableState(Transport(), binding), disposable, source, apply=False)
    proof = {"card": hashlib_sha(raw), "parent": "parent", "checkpoint": entry.CHECKPOINT,
             "financial": {"sha256": "financial", "rows": {}}, "money_digest": "money"}
    report = entry.checked_preview(lambda: deepcopy(proof), lambda: result)
    assert report["persistent_state_saves"] == report["checkpoint_updates"] == p[5].write_calls == 0
    assert result["found"] == result["fetched"] == 0
    assert raw == envelope(binding, snapshot(tmp_path / "native", binding))
    import sqlite3
    with sqlite3.connect(disposable/"recurring.sqlite3") as con:
        assert con.execute("SELECT COUNT(*) FROM recurring_runs").fetchone()[0] == 2


def hashlib_sha(raw):
    import hashlib
    return hashlib.sha256(raw).hexdigest()


def native_files(p, directory, binding):
    # The older fixture co-locates its synthetic keys/policy with SQLite; the
    # real durable envelope deliberately forbids those configuration files.
    import shutil
    directory.mkdir()
    shutil.copyfile(p[1]/"recurring.sqlite3", directory/"recurring.sqlite3")
    return snapshot(directory, binding)


def test_projection_read_only_uses_no_writable_client(monkeypatch):
    from app import projection_store as store
    from app.monthly_projection import ProjectionError
    import app.google_clients as google
    import app.private_state_bindings as bindings
    import app.settings as settings
    values = dict(env(), KAKEIBO_PROJECTION_FOLDER_ID="cipher")
    sentinel = object()
    monkeypatch.setattr(settings, "service_account_source", lambda: (None, {"private_key": "test"}))
    monkeypatch.setattr(bindings, "unwrap", lambda *a: "synthetic-folder")
    monkeypatch.setattr(google, "drive_service", lambda: pytest.fail("writable_client_created"))
    monkeypatch.setattr(google, "read_only_drive_service", lambda: sentinel)
    readonly = store.store_from_environment("sheet", values, read_only=True)
    assert readonly.backing.backing.service is sentinel
    with pytest.raises(ProjectionError, match="write_forbidden"): readonly.write("money", {})


def test_state_metadata_change_and_save_are_rejected():
    binding = StateBinding(entry.SOURCE, "sheet", "folder", "card")
    class Drive:
        def __init__(self): self.calls = 0; self.changed = False
        def files(self): return self
        def get(self, **kwargs):
            self.calls += 1
            return Request(dict(id="card", parents=["folder"], trashed=False, mimeType="application/json",
                                version=str(self.calls) if self.changed else "1"))
        def get_media(self, **kwargs): return Request(b"synthetic")
    drive = Drive(); state = entry.ReadOnlyState(drive, binding)
    assert state.observe()[0] == b"synthetic"
    drive.changed = True
    with pytest.raises(entry.Stop, match="state_changed_during_read"): state.observe()
    with pytest.raises(entry.Stop, match="state_write_forbidden"): state.write(b"anything")


def test_money_preview_factory_requests_read_only_store(monkeypatch):
    from app.amazon_money_runtime import money_writer
    calls = []
    def factory(*args, **kwargs): calls.append(kwargs); return None
    monkeypatch.setattr("app.projection_store.store_from_environment", factory)
    with pytest.raises(RuntimeError, match="money_projection_binding_required"):
        money_writer(SimpleNamespace(sid="sheet"), {"KAKEIBO_AMAZON_MONEY_MODE": "confirmed-v1"}, preview_only=True)
    assert calls == [{"read_only": True}]


def test_public_failure_suppresses_arbitrary_secret_and_dependency_output(monkeypatch, capsys):
    monkeypatch.setattr(entry.socket, "setdefaulttimeout", lambda *a: None)
    monkeypatch.setattr(entry.logging, "disable", lambda *a: None)
    monkeypatch.setattr(entry.signal, "signal", lambda *a: None)
    monkeypatch.setattr(entry.subprocess, "check_output", lambda *a, **kw: SHA)
    monkeypatch.setattr(entry, "GitHub", lambda values: object())
    monkeypatch.setattr(entry.sys, "argv", ["preview"])
    def private(*args):
        print("PRIVATE_BODY_STDOUT")
        print("PRIVATE_BODY_STDERR", file=entry.sys.stderr)
        raise ValueError("PRIVATE_TOKEN_API_RESPONSE")
    monkeypatch.setattr(entry, "run", private)
    assert entry.main() == 1
    captured = capsys.readouterr()
    assert captured.err == "" and "PRIVATE" not in captured.out
    assert json.loads(captured.out) == dict(success=False, error="preview_failed_private_details_suppressed")


def test_workflow_manual_only_pinned_and_secrets_last():
    root = Path(__file__).resolve().parents[1]
    raw = (root/".github/workflows"/entry.WORKFLOW).read_text("utf-8")
    workflow = yaml.load(raw, Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == dict(contents="read", actions="read")
    assert workflow["concurrency"] == dict(group="kakeibo-production", **{"cancel-in-progress":"false", "queue":"max"})
    job = workflow["jobs"]["preview"]
    assert "github.sha == vars.KAKEIBO_VALIDATED_MAIN_SHA" in job["if"]
    assert not any(k.startswith("GITHUB_") for k in job["env"])
    steps = job["steps"]
    assert all("secrets." not in json.dumps(s) for s in steps[:-1])
    for step in steps:
        if "uses" in step:
            assert len(step["uses"].split("@")[1]) == 40
        assert "continue-on-error" not in step
    assert "--require-hashes" in steps[-2]["run"] and "--no-index" in steps[-2]["run"]
    assert "python -B -m app.aupay_card_window_preview" in steps[-1]["run"]
    assert not any(s.get("uses", "").startswith(("actions/upload-artifact", "actions/cache")) for s in steps)
    assert not {"BANK_STATE_FILE_ID", "AMAZON_STATE_FILE_ID", "GEMINI_API_KEY"} & set(steps[-1]["env"])


def test_complete_entry_uses_real_clock_and_one_disposable_card_source(monkeypatch, tmp_path):
    from app.production_ledger import initial_ledger
    import app.google_clients as google
    import app.private_state_bindings as bindings
    import app.aupay_card_recurring as card
    import app.aupay_card_window_preview as preview
    p = parts(tmp_path)
    values = dict(env(), RUNNER_TEMP=str(tmp_path), SPREADSHEET_ID="sheet-production",
        GOOGLE_SERVICE_ACCOUNT_JSON=json.dumps({"private_key": "synthetic"}),
        AUPAY_CARD_AUDIT_KEY_JSON=p[3]._path.read_text("utf-8"),
        KAKEIBO_STATE_FOLDER_ID="folder", AUPAY_CARD_STATE_FILE_ID="card",
        KAKEIBO_RUN_LEDGER_FILE_ID="parent", GOOGLE_GMAIL_TOKEN_JSON="synthetic")
    policy = json.loads(p[4].path.read_text("utf-8"))
    policy.update(valid_from="2026-09-11T00:00:00+09:00", expires_at="2027-09-11T00:00:00+09:00")
    values["AUPAY_CARD_RECURRING_AUTHORITY_JSON"] = json.dumps(policy)
    cb = StateBinding(entry.SOURCE, "sheet-production", "folder", "card")
    pb = StateBinding("production_run", "sheet-production", "folder", "parent")
    payloads = {"card": envelope(cb, native_files(p, tmp_path/"native", cb))}
    parent = json.loads(initial_ledger(pb)); parent["sources"]["amazon"]["counts"]["catch_up_pending"] = 0
    payloads["parent"] = json.dumps(parent).encode()
    reads = []
    class Remote:
        def __init__(self, drive, binding): self.binding = binding
        def observe(self):
            reads.append(self.binding.source)
            raw = payloads[self.binding.file_id]
            return raw, {"sha256": hashlib_sha(raw), "metadata_digest": "stable"}
        def read(self): return self.observe()[0]
        def write(self, *args): pytest.fail("persistent_write")
    monkeypatch.setattr(preview, "ReadOnlyState", Remote)
    monkeypatch.setattr(bindings, "unwrap", lambda name, value, key: value)
    monkeypatch.setattr(google, "read_only_drive_service", lambda: object())
    monkeypatch.setattr(google, "read_only_sheets_service", lambda: object())
    monkeypatch.setattr("app.projection_store.store_from_environment", lambda *a, **kw: None)
    monkeypatch.setattr("app.sheets.SheetsDB", lambda *a, **kw: p[5])
    monkeypatch.setattr(preview, "financial_snapshot", lambda *a: {"sha256": "stable", "rows": {}})
    gmail_calls = []
    def gmail(token): gmail_calls.append(token); return Gmail()
    monkeypatch.setattr("app.amazon_gmail_preview.gmail_readonly_service", gmail)
    monkeypatch.setenv("KAKEIBO_AMAZON_MONEY_MODE", "")
    class CurrentClock(datetime):
        @classmethod
        def now(cls, tz): return NOW.astimezone(tz)
    monkeypatch.setattr(preview, "datetime", CurrentClock)
    clocks = []
    original = card.build_explicit_preview_window
    def clock_check(policy, state, now, *bounds):
        clocks.append(now)
        return original(policy, state, now, *bounds)
    monkeypatch.setattr(card, "build_explicit_preview_window", clock_check)
    result = preview.run(values, SHA, GitHub())
    assert result["before"] == result["after"]
    assert gmail_calls == ["synthetic"]
    assert clocks and all(stamp == NOW and stamp > END for stamp in clocks)  # current-clock provider, not historical end
    assert set(reads) == {entry.SOURCE, "production_run"}
    assert result["financial_write_requests"] == result["checkpoint_updates"] == p[5].write_calls == 0
    assert not list(tmp_path.glob("aupay-window-*"))  # disposable summaries/keys removed
