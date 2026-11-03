"""Synthetic evidence only; no real identities, credentials, or network."""
import base64
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from app import aupay_card_incremental_guard as guard
from app import aupay_card_incremental_recovery as recovery
from app.production_run import COUNT_KEYS
from test_aupay_card_incremental_apply import env, GitHub


def packed(statements):
    with closing(sqlite3.connect(":memory:")) as con:
        for sql, args in statements:
            con.execute(sql, args)
        con.commit()
        return dict(data=base64.b64encode(con.serialize()).decode())


def change(native, file, sql, args=()):
    with closing(sqlite3.connect(":memory:")) as con:
        con.deserialize(base64.b64decode(native["files"][file]["data"]))
        con.execute(sql, args)
        con.commit()
        native["files"][file]["data"] = base64.b64encode(con.serialize()).decode()


def proof(monkeypatch):
    cp = recovery.RECOVERED_CHECKPOINT
    summary = dict(run_id="synthetic-completed-run", status="complete", found=16, fetched=16,
        written=22, new_eligible=22, duplicate=3, money_review=4, write_requests=1, failure=0,
        source_window_start="2026-10-25T08:55:56+09:00", source_window_end=cp)
    payload = json.dumps(dict(batch_size=22, source_window=dict(end=cp)), sort_keys=True)
    manifest_hash = hashlib.sha256(payload.encode()).hexdigest()
    cap = dict(run_id=summary["run_id"], state="sealed", capability_id="synthetic-capability")
    events = [dict(run_id=summary["run_id"], seq=i, stage=stage, state=state)
        for i, (stage, state) in enumerate((("pre_read", "verified"), ("write_attempted", "attempted"),
            ("write_result", "acknowledged"), ("post_read", "confirmed"), ("final", "confirmed")), 1)]
    native = dict(phase="ready", attempt="", generation=118, files={})
    native["files"]["recurring.sqlite3"] = packed([
        ("CREATE TABLE recurring_runs (run_id TEXT,summary_json TEXT)", ()),
        ("INSERT INTO recurring_runs VALUES (?,?)", (summary["run_id"], json.dumps(summary))),
        ("CREATE TABLE recurring_checkpoint (successful_window_end TEXT,run_id TEXT)", ()),
        ("INSERT INTO recurring_checkpoint VALUES (?,?)", (cp, summary["run_id"]))])
    native["files"]["manifests.sqlite3"] = packed([
        ("CREATE TABLE batch_manifests (run_id TEXT,payload TEXT,payload_hash TEXT)", ()),
        ("INSERT INTO batch_manifests VALUES (?,?,?)", (summary["run_id"], payload, manifest_hash))])
    native["files"]["capabilities.sqlite3"] = packed([
        ("CREATE TABLE batch_capabilities (run_id TEXT,state TEXT,capability_id TEXT)", ()),
        ("INSERT INTO batch_capabilities VALUES (?,?,?)", tuple(cap.values()))])
    native["files"]["journal.sqlite3"] = packed([
        ("CREATE TABLE batch_events (run_id TEXT,seq INTEGER,stage TEXT,state TEXT)", ()),
        *[("INSERT INTO batch_events VALUES (?,?,?,?)", tuple(e.values())) for e in events]])
    counts = {k: v for k, v in summary.items() if k in COUNT_KEYS and type(v) is int}
    parent = dict(sources=dict(aupay_card=dict(phase="ready", attempt="", error="", counts=counts,
        duration_seconds=0, last_success=recovery.RECONCILED_AT), bank=dict(phase="ready", counts={"written": 0})))
    for name, value in dict(RESULT_SHA256=recovery._hash(summary), MANIFEST_SHA256=manifest_hash,
        CAPABILITY_SHA256=recovery._hash(cap), JOURNAL_SHA256=recovery._hash(events),
        NATIVE_SHA256=recovery._hash(native), PARENT_CARD_SHA256=recovery._hash(parent["sources"]["aupay_card"])).items():
        monkeypatch.setattr(recovery, name, value)
    return native, parent


def test_exact_reconciled_first_boundary_and_later_bank_updates(monkeypatch):
    native, parent = proof(monkeypatch)
    before = deepcopy((native, parent))
    recovery.verify_reconciled_state(native, parent, recovery.RECOVERED_CHECKPOINT)
    assert (native, parent) == before
    parent["sources"]["bank"]["counts"] = {"written": 2}
    recovery.verify_reconciled_state(native, parent, recovery.RECOVERED_CHECKPOINT)


@pytest.mark.parametrize("tamper", ["pending_native", "pending_parent", "counts", "old_time", "new_time",
    "generation", "result", "manifest", "capability", "journal", "checkpoint"])
def test_changed_or_unresolved_evidence_never_satisfies_exception(monkeypatch, tamper):
    native, parent = proof(monkeypatch)
    if tamper == "pending_native": native["phase"] = "pending"
    if tamper == "pending_parent": parent["sources"]["aupay_card"].update(phase="pending", attempt="unresolved")
    if tamper == "counts": parent["sources"]["aupay_card"]["counts"]["written"] = 33
    if tamper == "old_time": parent["sources"]["aupay_card"]["last_success"] = "2026-11-02T23:41:07+00:00"
    if tamper == "new_time": parent["sources"]["aupay_card"]["last_success"] = "2026-11-03T04:15:00+00:00"
    if tamper == "generation": native["generation"] += 1
    if tamper == "result": change(native, "recurring.sqlite3", "UPDATE recurring_runs SET summary_json='{}'")
    if tamper == "manifest": change(native, "manifests.sqlite3", "UPDATE batch_manifests SET payload='{}'")
    if tamper == "capability": change(native, "capabilities.sqlite3", "UPDATE batch_capabilities SET state='authorized'")
    if tamper == "journal": change(native, "journal.sqlite3", "UPDATE batch_events SET state='unknown' WHERE stage='write_result'")
    if tamper == "checkpoint": change(native, "recurring.sqlite3", "UPDATE recurring_checkpoint SET successful_window_end='2026-10-27T08:55:56+09:00'")
    with pytest.raises(RuntimeError):
        recovery.verify_reconciled_state(native, parent, recovery.RECOVERED_CHECKPOINT)


def test_original_checkpoint_and_missing_history_are_not_recovery(monkeypatch):
    native, parent = proof(monkeypatch)
    with pytest.raises(RuntimeError):
        recovery.verify_reconciled_state(native, parent, "2026-10-27T08:55:56+09:00")
    change(native, "recurring.sqlite3", "DELETE FROM recurring_runs")
    with pytest.raises(RuntimeError):
        recovery.verify_reconciled_state(native, parent, recovery.RECOVERED_CHECKPOINT)


def test_daily_reuse_requires_preserved_baseline_and_current_parent_result(monkeypatch):
    native, parent = proof(monkeypatch)
    end = (datetime.fromisoformat(recovery.RECOVERED_CHECKPOINT) + timedelta(days=1)).isoformat()
    summary = dict(run_id="synthetic-next-success", status="noop", source_window_end=end,
        written=0, new_eligible=0, found=0, fetched=0, duplicate=0, money_review=0, write_requests=0, failure=0)
    change(native, "recurring.sqlite3", "INSERT INTO recurring_runs VALUES (?,?)", (summary["run_id"], json.dumps(summary)))
    change(native, "recurring.sqlite3", "UPDATE recurring_checkpoint SET successful_window_end=?,run_id=?", (end, summary["run_id"]))
    native["generation"] += 2
    parent["sources"]["aupay_card"].update(counts={k: v for k, v in summary.items() if k in COUNT_KEYS and type(v) is int}, last_success=end)
    recovery.verify_reconciled_state(native, parent, end)
    parent["sources"]["aupay_card"]["counts"]["written"] = 22
    with pytest.raises(RuntimeError): recovery.verify_reconciled_state(native, parent, end)
    parent["sources"]["aupay_card"]["counts"]["written"] = 0
    changed = dict(summary, failure=1)
    change(native, "recurring.sqlite3", "UPDATE recurring_runs SET summary_json=? WHERE run_id=?",
           (json.dumps(changed), summary["run_id"]))
    parent["sources"]["aupay_card"]["counts"]["failure"] = 1
    with pytest.raises(RuntimeError): recovery.verify_reconciled_state(native, parent, end)
    change(native, "recurring.sqlite3", "UPDATE recurring_runs SET summary_json=? WHERE run_id=?",
           (json.dumps(summary), summary["run_id"]))
    parent["sources"]["aupay_card"]["counts"]["failure"] = 0
    change(native, "journal.sqlite3", "DELETE FROM batch_events WHERE stage='final'")
    with pytest.raises(RuntimeError): recovery.verify_reconciled_state(native, parent, end)


class ReconciledGitHub(GitHub):
    def __init__(self):
        super().__init__([dict(id=recovery.FAILED_RUN, head_sha=recovery.FAILED_SHA,
            run_attempt=1, status="completed", conclusion="failure")])
        self.bad = None
    def get(self, path):
        if path == f"actions/runs/{recovery.FAILED_RUN}":
            run = dict(id=recovery.FAILED_RUN, head_sha=recovery.FAILED_SHA, run_attempt=1,
                status="completed", conclusion="failure", event="workflow_dispatch",
                path=".github/workflows/" + guard.WORKFLOW, created_at="2026-11-03T01:35:03Z")
            if self.bad == "sha": run["head_sha"] = "b"*40
            if self.bad == "attempt": run["run_attempt"] = 2
            return run
        if path == f"actions/runs/{recovery.FAILED_RUN}/jobs?per_page=100":
            return dict(total_count=1, jobs=[dict(id=recovery.FAILED_JOB, name="apply", status="completed",
                conclusion="failure", steps=[dict(number=n, name=name, status="completed", conclusion=c)
                    for n, name, c in (
                        (5, "Live main and run guard with job-start validated and flags", "success"),
                        (6, "Install reviewed hash-pinned wheels before Google credentials", "success"),
                        (7, "One checkpoint Apply with protected reviews and exact readback", "success" if self.bad == "step" else "failure"))])])
        result = super().get(path)
        if path == "actions/runs/17": result["created_at"] = "2026-11-04T06:00:00Z"
        return result


def test_only_exact_failed_run_can_require_protected_state_proof():
    values = env() | dict(EXPECTED_CHECKPOINT=recovery.RECOVERED_CHECKPOINT)
    github = ReconciledGitHub()
    assert guard.controls(values, "a"*40, github) is True
    for bad in ("sha", "attempt", "step"):
        github.bad = bad
        with pytest.raises(RuntimeError): guard.controls(values, "a"*40, github)


def test_old_checkpoint_deleted_history_and_other_failure_are_blocked():
    values = env() | dict(EXPECTED_CHECKPOINT=recovery.RECOVERED_CHECKPOINT)
    with pytest.raises(RuntimeError): guard.controls(env(), "a"*40, ReconciledGitHub())
    github = ReconciledGitHub(); github.prior = []
    with pytest.raises(RuntimeError): guard.controls(values, "a"*40, github)
    github.prior = [dict(id=99, run_attempt=1, status="completed", conclusion="failure")]
    with pytest.raises(RuntimeError): guard.controls(values, "a"*40, github)
    github = ReconciledGitHub()
    github.prior.append(dict(id=98, run_attempt=1, status="completed", conclusion="success", created_at="2026-11-04T00:00:00Z"))
    with pytest.raises(RuntimeError): guard.controls(values, "a"*40, github)
