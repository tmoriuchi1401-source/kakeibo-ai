"""Invented mail and in-memory remote APIs; network is denied by conftest."""
import base64
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import aupay_card_first_window_apply as apply
from app import aupay_card_first_window_guard as guard
from app.amazon_money import decide, MoneyError
from app.amazon_money_writer import MoneyWriter
from app.aupay_card_apply_plan import build_canonical_apply_plan
from app.aupay_mail_pipeline import AuPayCardMailPipeline
from app.drive_run_state import DurableState, StateBinding, envelope, snapshot, validate
from app.production_ledger import ProductionLedger, initial_ledger
from app.projection_store import ReadOnlyProjectionStore, empty_journal
from app.transaction_plan import reconcile_transactions
from app.sheets import HEADERS
from test_amazon_money import book, mail, record, Ledger
from card_first_window_fakes import Gmail, DB as AuditDB, encoded
from test_aupay_card_window_preview import parts, NOW
from test_projection_refresh import Store

ENV = {"KAKEIBO_AMAZON_MONEY_MODE": "confirmed-v1", "CATEGORY_RULE_AUTO_APPLY_ENABLED": "true"}


class Transport:
    def __init__(self, payload): self.payload, self.writes = payload, []
    def read(self): return self.payload
    def write(self, payload): self.payload = payload; self.writes.append(payload)


class DB(AuditDB):
    def __init__(self):
        super().__init__()
        self.write_calls, self.mode = 0, "normal"
    def get(self, rng=None, **kwargs):
        if rng == "取込データ!A1:L1": return [HEADERS["取込データ"]]
        return super().get(rng, **kwargs)
    def values(self): return self
    def append(self, **kwargs):
        assert kwargs["range"] == "取込データ!A:L"
        assert kwargs["valueInputOption"] == "RAW"
        rows = deepcopy(kwargs["body"]["values"])
        db = self
        class Append:
            def execute(self):
                db.write_calls += 1
                if db.mode != "drop": db.rows.extend(rows)
                if db.mode == "conflict": db.rows[-1][6] += 1
                if db.mode == "expense": db.expenses.append(["forbidden-expense"])
                if db.mode == "unknown": raise RuntimeError("private lost response")
                return {}
        return Append()


def inventory():
    # 18 ordinary messages: 39 canonical rows plus 39 resends. Four held mails.
    amounts = [4000] * 32 + [21218] + [18000] * 5 + [20493]
    groups = [list(range(i, min(i+5, 39))) for i in range(0, 35, 5)] + [[35, 36], [37, 38]]
    raws = []
    for group, indices in enumerate(groups):
        body = "本会員さま ご利用分\n" + "\n".join(
            f"No.{i+1} --------\n▼ご利用日\n2026年10月20日\n▼ご利用先\n合成店{i}\n▼ご利用金額\n{amounts[i]}円"
            for i in indices)
        for resend in range(2):
            raw = mail("【ご利用詳細】au PAY カード", body, sender="info@kddi-fs.com",
                message_id=f"<first-window-{group}-{resend}@example.invalid>")
            raws.append(base64.urlsafe_b64encode(raw).decode())
    return raws + [encoded(100+i, n) for i, n in enumerate((1000, 1100, 1200, 1034))]


def fixture(tmp_path, saved=0):
    p = parts(tmp_path)
    repo, old_dir, old_state, key, authority, _ = p
    db, raws = DB(), inventory()
    transactions, collection = AuPayCardMailPipeline._collect(Gmail(raws), "synthetic", 500,
        money_records=[], money_notices=[], sleeper=lambda _: None, request_interval=0)
    plan = build_canonical_apply_plan(collection, reconcile_transactions(transactions, []))
    duplicates = [c for c in plan.candidates if c.merchant in {f"合成店{i}" for i in range(33, 39)}]
    assert len(duplicates) == 6
    db.rows = [c.to_import_row(imported_at=NOW, status="auto_expense") for c in duplicates]
    store = Store()
    store.data["journal"] = empty_journal()
    store.data["money"] = book({"legacy-synthetic": dict(state="open", amount=9999, expense_ids=["legacy-id"])})
    writer = MoneyWriter(store, apply.ReviewLedger(db))
    # Use the production configuration, including duplicate checks, for prepare.
    writer = apply._configured_writer(db, store, ENV, ledger=apply.ReviewLedger(db))
    gmail = Gmail(raws)
    native = tmp_path / "native"; native.mkdir()
    import shutil
    shutil.copy2(old_state.path, native / "recurring.sqlite3")
    binding = StateBinding(apply.SOURCE, db.sid, "folder", "card-file")
    card_transport = Transport(envelope(binding, snapshot(native, binding)))
    durable = DurableState(card_transport, binding)
    local = tmp_path / "restored"
    durable.restore(local)
    state = apply.SqliteRecurringRunState(local / "recurring.sqlite3", repo_root=repo)
    parent_binding = StateBinding("production_run", db.sid, "folder", "parent-file")
    parent_transport = Transport(initial_ledger(parent_binding))
    ledger = ProductionLedger(parent_transport, parent_binding)
    readback = apply._configured_writer(db, ReadOnlyProjectionStore(store), ENV, ledger=apply.ReviewLedger(db))
    args = dict(gmail=gmail, db=db, writer=writer, policy=authority.load(), state=state,
        now=NOW, key=key.load(), sleeper=lambda _: None)
    prepared = apply.prepare(**args)
    for d in prepared["decisions"][:saved]:
        r = d.record
        store.data["money"]["records"][r.money_id] = {**asdict(r), "state": "review", "reason": d.reason,
            "fingerprint": r.fingerprint, "expense_ids": [], "related_id": d.related_id}
    prepared["money_book"] = deepcopy(store.data["money"])
    events = []
    def financial():
        return {(t, render): [["header"]] + deepcopy(rows) for t, rows in
            (("取込データ", db.rows), ("支出明細", db.expenses), ("収入明細", []))
            for render in ("UNFORMATTED_VALUE", "FORMULA")}
    def final_read(*, card_only):
        assert apply.checkpoint(validate(card_transport.read(), binding), apply.END) == apply.END
        assert parent_transport.read() == ledger.payload
        if not card_only: assert ledger.value["sources"]["aupay_card"]["phase"] == "ready"
        events.append("card-read" if card_only else "parent-read")
    execution = dict(prepared=prepared, db=db, writer=writer, readback_writer=readback,
        policy=authority.load(), key_provider=key, authority_provider=authority, state=state,
        state_dir=local, repo_root=repo, durable=durable, ledger=ledger, guard=lambda: None,
        financial_read=financial, documents_check=lambda _: None, final_read=final_read, clock=lambda: NOW)
    return SimpleNamespace(args=args, execution=execution, prepared=prepared, writer=writer,
        store=store, db=db, state=state, durable=durable, ledger=ledger, events=events, gmail=gmail)


@pytest.mark.parametrize("saved", [0, 2, 4])
def test_complete_real_batch_writer_review_protection_and_durable_commit(tmp_path, saved):
    p = fixture(tmp_path, saved=saved)
    before = deepcopy(p.store.data["money"]["records"])
    result = apply.execute_fixed(**p.execution)
    assert result["written"] == 33 and result["amount_yen"] == 149218
    assert p.db.write_calls == 1 and len(p.db.rows) == 39 and p.db.expenses == []
    assert p.state.successful_window_end().isoformat() == apply.END
    assert p.durable.inspect()["phase"] == "ready" and p.events == ["card-read", "parent-read"]
    assert all(p.store.data["money"]["records"][k] == v for k, v in before.items())
    assert len(p.store.data["money"]["records"]) == 4
    assert len(p.gmail.gets) == 22  # no second collection or independent Preview


@pytest.mark.parametrize("mode", ["unknown", "drop", "conflict", "expense"])
def test_unresolved_or_wrong_financial_delta_never_advances_checkpoint(tmp_path, mode):
    p = fixture(tmp_path); p.db.mode = mode
    with pytest.raises(RuntimeError): apply.execute_fixed(**p.execution)
    assert p.db.write_calls == 1
    assert p.state.successful_window_end().isoformat() == apply.CHECKPOINT
    assert p.durable.inspect()["phase"] == "pending"
    assert p.ledger.value["sources"]["aupay_card"]["phase"] == "pending"
    assert not p.events


@pytest.mark.parametrize("field", ["original_url", "reason", "fingerprint"])
def test_saved_review_mismatch_stops_before_any_write(tmp_path, field):
    p = fixture(tmp_path, saved=4)
    first = next(iter(p.store.data["money"]["records"].values()))
    first[field] += "changed"
    with pytest.raises(RuntimeError): apply.execute_fixed(**p.execution)
    assert p.db.write_calls == 0 and not p.durable.transport.writes and not p.ledger.transport.writes


def test_same_aggregate_different_identity_stops_on_fresh_binding(tmp_path):
    p = fixture(tmp_path)
    # Replacing one transaction while retaining its amount is not approval.
    tx = p.prepared["transactions"][0]
    p.prepared["transactions"][0] = dict(tx, merchant="異なる同額店舗")
    with pytest.raises(RuntimeError): apply.execute_fixed(**p.execution)
    assert p.db.write_calls == 0 and not p.durable.transport.writes


def test_existing_row_change_stops_before_review_saves(tmp_path):
    p = fixture(tmp_path); p.db.rows.append(list(p.db.rows[0]))
    with pytest.raises(RuntimeError, match="existing_rows_changed"): apply.execute_fixed(**p.execution)
    assert not p.store.data["money"]["records"] and not p.durable.transport.writes


@pytest.mark.parametrize("action", ["post", "resume", "transfer", "supplement"])
def test_review_only_money_writer_rejects_entire_batch_before_saving(action):
    store, ledger = Store(), Ledger()
    store.data["money"] = book()
    writer = MoneyWriter(store, ledger)
    records = [record(confirmed=False), record(source_id="second", reference="transaction:second")]
    original = writer._decision
    writer._decision = lambda r, b: replace(original(r, b), action=action) if r == records[1] else original(r, b)
    with pytest.raises(MoneyError, match="review_only"): writer.apply(records, limit=2, review_only=True)
    assert store.data["money"]["records"] == {} and ledger.calls == []


def test_review_save_unknown_leaves_pending_without_import(tmp_path):
    p = fixture(tmp_path)
    original = p.store.write
    def unknown(key, value): original(key, value); raise RuntimeError("private unknown")
    p.store.write = unknown
    with pytest.raises(RuntimeError): apply.execute_fixed(**p.execution)
    assert p.db.write_calls == 0 and p.durable.inspect()["phase"] == "pending"
    assert p.state.successful_window_end().isoformat() == apply.CHECKPOINT


@pytest.mark.parametrize("change", ["raw", "end", "total", "classification"])
def test_collector_or_aggregate_mismatch_is_read_only(tmp_path, change):
    p = fixture(tmp_path)
    if change == "raw": p.gmail.missing_raw = True
    if change == "end": p.gmail.stamp = str(int(__import__('datetime').datetime.fromisoformat(apply.END).timestamp()) * 1000)
    if change == "total": p.args["gmail"] = Gmail(inventory()[:-1])
    if change == "classification": p.store.data["money"]["legacy"] = {}
    with pytest.raises(RuntimeError): apply.prepare(**p.args)
    assert p.db.write_calls == 0 and not p.durable.transport.writes


def environment():
    return dict(apply.FLAGS, GITHUB_ACTIONS="true", GITHUB_REPOSITORY=apply.REPO,
        GITHUB_REF="refs/heads/main", GITHUB_EVENT_NAME="workflow_dispatch", GITHUB_SHA="a"*40,
        WORKFLOW_SHA="a"*40, EXPECTED_COMMIT="a"*40, KAKEIBO_VALIDATED_MAIN_SHA="a"*40,
        WORKFLOW_REF=f"{apply.REPO}/.github/workflows/{apply.WORKFLOW}@refs/heads/main",
        GITHUB_ACTOR=apply.REPO.split('/')[0], GITHUB_TRIGGERING_ACTOR=apply.REPO.split('/')[0],
        GITHUB_RUN_ATTEMPT="1", GITHUB_RUN_ID="17", CONFIRM=apply.CONFIRM,
        PREVIEW_SOURCE=apply.SOURCE, PREVIEW_START=apply.START, PREVIEW_END=apply.END,
        EXPECTED_CHECKPOINT=apply.CHECKPOINT)


class GitHub:
    def __init__(self): self.previous, self.competing = False, False
    def get(self, path):
        if path == f"actions/runs/{guard.FAILED_PREFLIGHT_RUN}":
            return dict(id=guard.FAILED_PREFLIGHT_RUN, head_sha=guard.FAILED_PREFLIGHT_SHA, run_attempt=1,
                status="completed", conclusion="failure", event="workflow_dispatch", path=".github/workflows/"+apply.WORKFLOW)
        if path == f"actions/runs/{guard.FAILED_PREFLIGHT_RUN}/jobs?per_page=100":
            return dict(total_count=1, jobs=[dict(id=guard.FAILED_PREFLIGHT_JOB, name="apply", status="completed",
                conclusion="failure", steps=[dict(number=n, name=name, status="completed", conclusion=conclusion)
                    for n, name, conclusion in (
                        (5, "Live main and run guard with job-start validated and flags", "failure"),
                        (6, "Install reviewed hash-pinned wheels before Google credentials", "skipped"),
                        (7, "One fixed Apply with protected reviews and exact readback", "skipped"))])])
        if path == "git/ref/heads/main": return {"object": {"sha": "a"*40}}
        if path == "actions/runs/17": return dict(head_sha="a"*40, run_attempt=1,
            event="workflow_dispatch", path=".github/workflows/" + apply.WORKFLOW)
        if "actions/workflows/" in path:
            assert "head_sha" not in path
            return dict(total_count=3 if self.previous else 2,
                workflow_runs=[{"id": 17}, {"id": guard.FAILED_PREFLIGHT_RUN}])
        return dict(total_count=int(self.competing), workflow_runs=[{"id": 99}] if self.competing else [])


@pytest.mark.parametrize("field,value", [("GITHUB_RUN_ATTEMPT", "2"), ("CONFIRM", "PREVIEW_ONLY"),
    ("PREVIEW_END", "2026-10-28T08:55:56+09:00"), ("GITHUB_ACTOR", "someone"),
    ("KAKEIBO_VALIDATED_MAIN_SHA", "b"*40), ("RUNNER_DEBUG", "1")])
def test_boundary_rejects_unapproved_scope_or_revision(field, value):
    env = environment(); env[field] = value
    with pytest.raises(RuntimeError): apply.controls(env, "a"*40, GitHub())


def test_cross_revision_previous_dispatch_and_competing_run_are_blocked():
    github = GitHub(); apply.controls(environment(), "a"*40, github)
    github.previous = True
    with pytest.raises(RuntimeError): apply.controls(environment(), "a"*40, github)
    github.previous, github.competing = False, True
    with pytest.raises(RuntimeError): apply.controls(environment(), "a"*40, github)


def test_public_failure_suppresses_private_exception(monkeypatch, capsys):
    monkeypatch.setattr(apply.subprocess, "check_output", lambda *a, **k: "a"*40)
    monkeypatch.setattr(apply, "GitHub", lambda _: None)
    monkeypatch.setattr(apply.sys, "argv", ["entry"])
    def fail(*_): raise RuntimeError("private original and transaction information")
    monkeypatch.setattr(apply, "run", fail)
    assert apply.main() == 1
    out = capsys.readouterr()
    assert "private" not in out.out and not out.err
    assert json.loads(out.out)["automatic_retry"] is False


@pytest.mark.parametrize("state", ["queued", "pending", "unknown"])
def test_money_review_request_blocks_before_pending_marker(tmp_path, state):
    p = fixture(tmp_path)
    p.store.data["money-reviews"] = {"requests": {"REQ-synthetic": {"state": state}}}
    with pytest.raises(RuntimeError, match="review_request_pending"): apply.execute_fixed(**p.execution)
    assert not p.durable.transport.writes and p.db.write_calls == 0


@pytest.mark.parametrize("column", [2, 4, 5, 6, 7, 10, 11])
def test_existing_duplicate_content_mismatch_is_rejected_before_writes(tmp_path, column):
    p = fixture(tmp_path)
    p.db.rows[0][column] = 999 if column == 6 else "changed"
    with pytest.raises(RuntimeError, match="existing_identity_content_mismatch"): apply.prepare(**p.args)
    assert p.db.write_calls == 0 and not p.durable.transport.writes


def test_unrelated_money_change_is_rejected_before_pending_marker(tmp_path):
    p = fixture(tmp_path)
    p.store.data["money"]["legacy"]["legacy-synthetic"]["amount"] += 1
    with pytest.raises(RuntimeError, match="money_book_changed_before_write"): apply.execute_fixed(**p.execution)
    assert p.db.write_calls == 0 and not p.durable.transport.writes


def test_workflow_is_manual_locked_pinned_and_has_no_artifact_or_other_source_credentials():
    import yaml
    raw = (Path(__file__).resolve().parents[1]/".github/workflows"/apply.WORKFLOW).read_text("utf-8")
    workflow = yaml.load(raw, Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == dict(contents="read", actions="read")
    assert workflow["concurrency"] == dict(group="kakeibo-production", **{"cancel-in-progress": "false", "queue": "max"})
    job = workflow["jobs"]["apply"]
    assert "github.run_attempt == 1" in job["if"]
    assert "github.sha == vars.KAKEIBO_VALIDATED_MAIN_SHA" in job["if"]
    steps = job["steps"]
    assert all("secrets." not in json.dumps(step) for step in steps[:-1])
    for step in steps:
        if "uses" in step: assert len(step["uses"].split("@")[1]) == 40
        assert "continue-on-error" not in step
    assert not {"BANK_STATE_FILE_ID", "AMAZON_STATE_FILE_ID", "GEMINI_API_KEY"} & set(steps[-1]["env"])
    assert all("upload-artifact" not in s.get("uses", "") and "actions/cache" not in s.get("uses", "") for s in steps)
    guard_step = next(s for s in steps if s.get("name") == "Live main and run guard with job-start validated and flags")
    assert guard_step["run"] == "python -B -m app.aupay_card_first_window_guard"
    assert steps.index(guard_step) < next(i for i, s in enumerate(steps) if "pip install" in s.get("run", ""))


@pytest.mark.parametrize("incremental,recovery_result", [(False, None), (True, None), (True, True), (True, False)])
def test_whole_entry_uses_one_collection_existing_durable_stores_and_real_clock(monkeypatch, tmp_path, incremental, recovery_result):
    import app.google_clients as google
    import app.private_state_bindings as bindings
    import app.projection_store as projections
    import app.drive_run_state as native
    import app.sheets as sheets
    import app.amazon_gmail_preview as gmail_module
    import app.aupay_card_window_preview as preview
    if incremental:
        from test_aupay_card_incremental_apply import followup, env as incremental_env, GitHub as IncrementalGitHub
        p = followup(tmp_path)
        p.gmail = p.args["gmail"]
    else:
        p = fixture(tmp_path)
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
    monkeypatch.setattr(gmail_module, "gmail_readonly_service", lambda token: p.gmail)
    # Existing batch SQLite context managers release transactions at exit;
    # Windows also needs their unreachable connections collected before unlink.
    original_execute = apply.execute_fixed
    def collected_execute(**kwargs):
        result = original_execute(**kwargs)
        import gc
        gc.collect()
        return result
    monkeypatch.setattr(apply, "execute_fixed", collected_execute)
    class CurrentClock(__import__('datetime').datetime):
        @classmethod
        def now(cls, tz): return NOW.astimezone(tz)
    monkeypatch.setattr(apply, "datetime", CurrentClock)
    values = dict((incremental_env() if incremental else environment()) | ENV, RUNNER_TEMP=str(tmp_path), SPREADSHEET_ID=p.db.sid,
        GOOGLE_SERVICE_ACCOUNT_JSON=json.dumps({"private_key": "synthetic"}), GOOGLE_GMAIL_TOKEN_JSON="synthetic",
        KAKEIBO_STATE_FOLDER_ID="folder", AUPAY_CARD_STATE_FILE_ID="card-file", KAKEIBO_RUN_LEDGER_FILE_ID="parent-file",
        AUPAY_CARD_AUDIT_KEY_JSON=p.execution["key_provider"]._path.read_text("utf-8"),
        AUPAY_CARD_RECURRING_AUTHORITY_JSON=p.execution["authority_provider"].path.read_text("utf-8"))
    verified = []
    if recovery_result is not None:
        import app.aupay_card_incremental_guard as incremental_guard
        import app.aupay_card_incremental_recovery as recovery
        monkeypatch.setattr(incremental_guard, "controls", lambda *args: True)
        def proof(native, parent, expected_checkpoint):
            assert native["phase"] == parent["sources"]["aupay_card"]["phase"] == "ready"
            assert expected_checkpoint == apply.END
            verified.append(True)
            if not recovery_result: raise RuntimeError("synthetic_recovery_proof_rejected")
        monkeypatch.setattr(recovery, "verify_reconciled_state", proof)
    p.gmail.gets.clear()
    if recovery_result is False:
        with pytest.raises(RuntimeError, match="synthetic_recovery_proof_rejected"):
            apply.run(values, "a"*40, IncrementalGitHub(), incremental=True)
        assert verified == [True] and not p.gmail.gets and p.db.write_calls == 0
        assert p.durable.inspect()["phase"] == "ready"
        return
    result = (apply.run(values, "a"*40, IncrementalGitHub(), incremental=True) if incremental
              else apply.run(values, "a"*40, GitHub()))
    assert result["written"] == (2 if incremental else 33) and result["expense_rows_written"] == 0
    assert len(p.gmail.gets) == (25 if incremental else 22) and p.db.write_calls == 1
    assert verified == ([True] if recovery_result else [])
    assert not list(tmp_path.glob("card-first-apply-*"))
