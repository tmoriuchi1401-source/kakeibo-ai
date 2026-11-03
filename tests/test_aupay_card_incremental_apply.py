"""Invented overlapping windows using the real existing batch writer/stores."""
import base64
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from app import aupay_card_first_window_apply as apply
from app import aupay_card_incremental_guard as guard
from app.drive_run_state import validate
from app.projection_store import ReadOnlyProjectionStore
from card_first_window_fakes import Gmail, encoded, Request
from test_aupay_card_first_window_apply import fixture, inventory, ENV, environment
from test_aupay_card_window_preview import NOW


def followup(tmp_path, *, new=2, review=True):
    p = fixture(tmp_path)
    # Actually register the prior synthetic 33 and protect its four reviews.
    apply.execute_fixed(**p.execution)
    assert len(p.db.rows) == 39
    p.writer = apply._configured_writer(p.db, p.store, ENV, ledger=apply.ReviewLedger(p.db))
    readback = apply._configured_writer(p.db, ReadOnlyProjectionStore(p.store), ENV,
        ledger=apply.ReviewLedger(p.db))
    raws = inventory() + [encoded(200+i, 1711+i, money=False) for i in range(new)]
    if review:
        raws.append(encoded(999, 883))
    gmail = Gmail(raws)
    gmail.stamp = str(int(datetime.fromisoformat(apply.END).timestamp() - 86400) * 1000)
    p.args.update(gmail=gmail, writer=p.writer, now=NOW, incremental=True, expected_checkpoint=apply.END)
    p.prepared = apply.prepare(**p.args)
    p.db.write_calls = 0
    p.events.clear()
    def final_read(*, card_only):
        end = p.prepared["window"].end.isoformat()
        assert apply.checkpoint(validate(p.durable.transport.read(), p.durable.binding), end) == end
        if not card_only:
            assert p.ledger.value["sources"]["aupay_card"]["counts"]["written"] == new
        p.events.append(card_only)
    p.execution.update(prepared=p.prepared, writer=p.writer, readback_writer=readback, final_read=final_read)
    return p


def test_overlap_retrieves_previous_33_and_only_appends_fresh_plan(tmp_path):
    p = followup(tmp_path)
    before = deepcopy(p.store.data["money"]["records"])
    other_sources = deepcopy({k: v for k, v in p.ledger.value["sources"].items() if k != "aupay_card"})
    assert p.prepared["totals"]["ordinary"]["duplicate"]["count"] == 39
    assert len(p.prepared["decisions"]) == 5
    assert p.prepared["projection"].full_plan_binding_ref == p.prepared["full_plan_ref"]
    result = apply.execute_fixed(**p.execution)
    assert result == dict(written=2, amount_yen=3423, held_protected=5, duplicate=39,
        financial_write_requests=1, expense_rows_written=0, checkpoint=NOW.isoformat(), parent_ledger="complete")
    assert p.db.write_calls == 1 and len(p.db.rows) == 41 and not p.db.expenses
    assert len(p.args["gmail"].gets) == 25  # one complete collection, no second Preview
    assert all(p.store.data["money"]["records"][k] == v for k, v in before.items())
    assert len(p.store.data["money"]["records"]) == 5
    for d in p.prepared["decisions"]:
        assert apply.exact_review(p.store.data["money"], d)
    assert {k: v for k, v in p.ledger.value["sources"].items() if k != "aupay_card"} == other_sources
    assert p.events == [True, False]
    # The same in-memory preparation cannot be executed a second time.
    with pytest.raises(RuntimeError):
        apply.execute_fixed(**p.execution)
    assert p.db.write_calls == 1


@pytest.mark.parametrize("review", [False, True])
def test_no_new_canonical_rows_still_commits_only_after_proofs(tmp_path, review):
    p = followup(tmp_path, new=0, review=review)
    result = apply.execute_fixed(**p.execution)
    assert result["written"] == result["financial_write_requests"] == 0
    assert p.db.write_calls == 0 and len(p.db.rows) == 39
    assert p.state.successful_window_end() == NOW
    assert p.events == [True, False]


def test_empty_gmail_window_does_not_rewrite_financial_or_reviews(tmp_path):
    p = followup(tmp_path, new=0, review=False)
    p.args["gmail"].raws = []
    p.prepared = apply.prepare(**p.args)
    p.execution["prepared"] = p.prepared
    before = deepcopy(p.store.data)
    result = apply.execute_fixed(**p.execution)
    assert result["written"] == result["held_protected"] == result["financial_write_requests"] == 0
    assert p.store.data == before and p.db.write_calls == 0
    assert p.state.successful_window_end() == NOW


@pytest.mark.parametrize("mode", ["unknown", "drop", "conflict", "expense"])
def test_financial_failure_or_unknown_keeps_checkpoint_and_pending(tmp_path, mode):
    p = followup(tmp_path)
    p.db.mode = mode
    with pytest.raises(RuntimeError):
        apply.execute_fixed(**p.execution)
    assert p.state.successful_window_end().isoformat() == apply.END
    assert p.db.write_calls == 1 and p.durable.inspect()["phase"] == "pending"
    assert p.ledger.value["sources"]["aupay_card"]["phase"] == "pending" and not p.events


@pytest.mark.parametrize("change", ["raw", "missing", "wrong_id", "outside", "overflow", "partial"])
def test_incomplete_or_invalid_gmail_never_starts_write(tmp_path, change):
    p = followup(tmp_path)
    gmail = p.args["gmail"]
    if change == "raw": gmail.missing_raw = True
    if change == "outside": gmail.stamp = str(int(NOW.timestamp()) * 1000)
    if change == "overflow": gmail.raws = [encoded(i, i+100, money=False) for i in range(501)]
    if change in {"missing", "wrong_id"}:
        original = gmail.get
        def get(**kw):
            result = original(**kw).execute()
            if change == "missing": raise RuntimeError("lost synthetic mail")
            result["id"] = "wrong"
            return Request(result)
        gmail.get = get
    if change == "partial":
        gmail.list = lambda **kw: Request({"messages": [{"id": "g0"}], "nextPageToken": "same"})
    with pytest.raises(RuntimeError):
        apply.prepare(**p.args)
    assert p.db.write_calls == 0 and p.state.successful_window_end().isoformat() == apply.END


def test_14_day_boundary_not_truncated_and_checkpoint_must_match(tmp_path):
    p = followup(tmp_path)
    start = datetime.fromisoformat(apply.END) - timedelta(seconds=172800)
    p.args["now"] = start + timedelta(seconds=1209600)
    assert apply.prepare(**p.args)["window"].start == start
    p.args["now"] += timedelta(seconds=1)
    with pytest.raises(RuntimeError, match="exceeds_authority"):
        apply.prepare(**p.args)
    p.args["now"] = NOW
    p.args["expected_checkpoint"] = apply.CHECKPOINT
    with pytest.raises(RuntimeError, match="checkpoint_changed"):
        apply.prepare(**p.args)
    assert p.db.write_calls == 0


def test_500_mail_boundary_all_pages_are_fetched(tmp_path):
    p = followup(tmp_path, new=0, review=False)
    gmail = p.args["gmail"]
    original = base64.urlsafe_b64decode(gmail.raws[0])
    for i in range(500 - len(gmail.raws)):
        raw = original.replace(b"<first-window-0-0@example.invalid>", f"<resend-{i}@example.invalid>".encode())
        gmail.raws.append(base64.urlsafe_b64encode(raw).decode())
    gmail.gets.clear()
    result = apply.prepare(**p.args)
    assert result["collection"]["found"] == result["collection"]["fetched"] == len(gmail.gets) == 500
    assert result["projection"] is None and p.db.write_calls == 0


@pytest.mark.parametrize("new,review,allowed", [(96, False, True), (97, False, False), (96, True, False)])
def test_100_total_candidate_bound_including_saved_reviews(tmp_path, new, review, allowed):
    p = followup(tmp_path, new=0, review=False)
    p.args["gmail"].raws += [encoded(300+i, 2000+i, money=False) for i in range(new)]
    if review: p.args["gmail"].raws.append(encoded(999, 883))
    if allowed:
        prepared = apply.prepare(**p.args)
        assert len(prepared["statuses"]) + len(prepared["decisions"]) == 100
    else:
        with pytest.raises(RuntimeError): apply.prepare(**p.args)
    assert p.db.write_calls == 0


@pytest.mark.parametrize("action", ["post", "resume", "transfer", "supplement", "linked", "duplicate"])
def test_any_nonreview_money_stops_before_financial_write(tmp_path, action):
    p = followup(tmp_path)
    original = p.writer.preview
    p.writer.preview = lambda records: [replace(d, action=action) for d in original(records)]
    with pytest.raises(RuntimeError): apply.prepare(**p.args)
    assert p.db.write_calls == 0 and p.durable.inspect()["phase"] == "ready"


@pytest.mark.parametrize("change", ["sheet", "identity", "review", "review_unknown", "readback"])
def test_fresh_reconciliation_and_review_readback_fail_closed(tmp_path, change):
    p = followup(tmp_path)
    if change == "sheet": p.db.rows[0][6] += 1
    if change == "identity":
        p.prepared["transactions"][-1] = dict(p.prepared["transactions"][-1], merchant="same yen different identity")
    if change == "review": next(iter(p.store.data["money"]["records"].values()))["fingerprint"] += "changed"
    if change == "review_unknown":
        original = p.store.write
        def unknown(key, value): original(key, value); raise RuntimeError("unknown")
        p.store.write = unknown
    if change == "readback":
        p.execution["readback_writer"].preview = lambda records: []
    with pytest.raises(RuntimeError): apply.execute_fixed(**p.execution)
    assert p.db.write_calls == 0 and p.state.successful_window_end().isoformat() == apply.END


def env():
    return environment() | dict(WORKFLOW_REF=f"{apply.REPO}/.github/workflows/{guard.WORKFLOW}@refs/heads/main",
        CONFIRM=guard.CONFIRM, EXPECTED_CHECKPOINT=apply.END)


class GitHub:
    def __init__(self, prior=()): self.prior = list(prior)
    def get(self, path):
        current = dict(id=17, head_sha="a"*40, run_attempt=1, event="workflow_dispatch",
            path=".github/workflows/"+guard.WORKFLOW, created_at="2026-10-31T00:00:00Z")
        if path == "git/ref/heads/main": return {"object": {"sha": "a"*40}}
        if path == "actions/runs/17": return current
        if "actions/workflows/" in path:
            page = int(path.split("page=")[-1])
            runs = [current] + self.prior
            return dict(total_count=len(runs), workflow_runs=runs[(page-1)*100:page*100])
        return dict(total_count=0, workflow_runs=[])


def prior(**changes):
    return dict(id=16, status="completed", conclusion="success", run_attempt=1,
        created_at="2026-10-26T00:00:00Z") | changes


@pytest.mark.parametrize("changes", [dict(GITHUB_RUN_ATTEMPT="2"), dict(CONFIRM="PREVIEW_ONLY"),
    dict(EXPECTED_CHECKPOINT=apply.CHECKPOINT), dict(PREVIEW_SOURCE="amazon"),
    dict(KAKEIBO_VALIDATED_MAIN_SHA="b"*40), dict(GITHUB_EVENT_NAME="schedule"), dict(RUNNER_DEBUG="1")])
def test_dispatch_scope_and_rerun_rejected(changes):
    with pytest.raises(RuntimeError): guard.controls(env() | changes, "a"*40, GitHub())


@pytest.mark.parametrize("older", [prior(conclusion="failure"), prior(run_attempt=2),
    prior(created_at="2026-10-30T00:00:00Z"), prior(status="in_progress")])
def test_prior_dispatch_failure_unknown_or_same_checkpoint_rejected(older):
    with pytest.raises(RuntimeError): guard.controls(env(), "a"*40, GitHub([older]))


def test_successful_prior_window_and_all_history_pages():
    guard.controls(env(), "a"*40, GitHub())
    guard.controls(env(), "a"*40, GitHub([prior(id=i+100) for i in range(101)]))
    history = [prior(id=i+100) for i in range(101)]
    history[-1]["conclusion"] = "failure"
    with pytest.raises(RuntimeError): guard.controls(env(), "a"*40, GitHub(history))


def test_guard_imports_without_site_packages():
    result = subprocess.run([sys.executable, "-S", "-B", "-c",
        "import app.aupay_card_incremental_guard"], capture_output=True, text=True)
    assert result.returncode == 0 and not result.stdout and not result.stderr


def test_daily_workflow_preserves_manual_inputs_secrets_and_pinned_install():
    workflow = yaml.load((Path(apply.ROOT)/".github/workflows"/guard.WORKFLOW).read_text("utf-8"), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch", "schedule"}
    assert workflow["on"]["schedule"] == [{"cron": "23 0 * * *"}]
    assert "window_start" not in workflow["on"]["workflow_dispatch"]["inputs"]
    assert "window_end" not in workflow["on"]["workflow_dispatch"]["inputs"]
    assert workflow["permissions"] == dict(contents="read", actions="read")
    assert workflow["concurrency"]["group"] == "kakeibo-production"
    steps = workflow["jobs"]["apply"]["steps"]
    assert "vars.KAKEIBO_CARD_SCHEDULE_ENABLED == 'true'" in workflow["jobs"]["apply"]["if"]
    assert all("secrets." not in json.dumps(s) for s in steps[:-1])
    assert not {"BANK_STATE_FILE_ID", "AMAZON_STATE_FILE_ID", "GEMINI_API_KEY"} & set(steps[-1]["env"])
    for step in steps:
        if "uses" in step: assert len(step["uses"].split("@")[1]) == 40
    assert any(s.get("run") == "python -B -m app.aupay_card_incremental_guard" for s in steps)
    assert "--require-hashes" in json.dumps(steps)
    assert not any("upload-artifact" in s.get("uses", "") or "actions/cache" in s.get("uses", "") for s in steps)
