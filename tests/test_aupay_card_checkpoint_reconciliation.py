"""Detached postimage proofs; never an executor for a production repair."""
import base64
from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from app import aupay_card_first_window_apply as apply
from app import aupay_card_incremental_guard as guard
from app.aupay_card_incremental_recovery import _hash, _rows, preserved_unknown_proofs
from app.drive_run_state import validate
from test_aupay_card_first_window_apply import fixture
from test_aupay_card_incremental_apply import env, GitHub, prior


def replace_summary(native, run_id, summary, *, insert=False):
    item = native["files"]["recurring.sqlite3"]
    with sqlite3.connect(":memory:") as con:
        con.deserialize(base64.b64decode(item["data"]))
        # Only the synthetic, disposable postimage is assembled here. Actual
        # recurring_runs remain append-only; no application migration uses SQL.
        if insert:
            con.execute("INSERT INTO recurring_runs VALUES (?,?,?,?,?,?)", (run_id,
                "2026-10-26T00:00:00+09:00", summary["status"], summary["source_window_start"],
                summary["source_window_end"], json.dumps(summary)))
        else:
            con.execute("DROP TRIGGER IF EXISTS recurring_run_no_update")
            con.execute("UPDATE recurring_runs SET summary_json=? WHERE run_id=?", (json.dumps(summary), run_id))
        con.commit()
        data = con.serialize()
    item.update(data=base64.b64encode(data).decode(), sha256=hashlib.sha256(data).hexdigest())


def postimage(tmp_path):
    p = fixture(tmp_path)
    old = p.durable.inspect()
    old.update(phase="pending", attempt="c" * 32)
    old_card = deepcopy(p.ledger.value["sources"]["aupay_card"])
    old_card.update(phase="pending", attempt="d" * 32, error="source_execution_failed")
    apply.execute_fixed(**p.execution)
    native = p.durable.inspect()
    parent = deepcopy(p.ledger.value)
    cp = p.state.successful_window_end().isoformat()
    rid = _rows(native, "recurring.sqlite3", "recurring_checkpoint")[0]["run_id"]
    repair = json.loads(next(r["summary_json"] for r in _rows(native, "recurring.sqlite3", "recurring_runs")
                            if r["run_id"] == rid))
    failure = prior(conclusion="failure", head_sha="b" * 40, head_branch="main", event="workflow_dispatch",
        path=".github/workflows/" + guard.WORKFLOW)
    archive = dict(run_id="synthetic-unknown-no-replay", status="UNKNOWN_NO_REPLAY", replay_allowed=False,
        source_window_start=apply.START, source_window_end=cp, native_before=old,
        native_sha256=_hash(old), parent_card_before=old_card, parent_card_sha256=_hash(old_card),
        evidence_sha256="e" * 64, repair_run_id=rid, github_failure=failure,
        checkpoint_before=apply.CHECKPOINT, checkpoint_after=cp)
    manifest = next(r for r in _rows(native, "manifests.sqlite3", "batch_manifests") if r["run_id"] == rid)
    cap = next(r for r in _rows(native, "capabilities.sqlite3", "batch_capabilities") if r["run_id"] == rid)
    events = sorted((r for r in _rows(native, "journal.sqlite3", "batch_events") if r["run_id"] == rid), key=lambda r:r["seq"])
    repair["checkpoint_reconciliation"] = dict(archive_id=archive["run_id"], evidence_sha256="e" * 64,
        aliases_sha256=_hash(list(p.state.alias_registry())), manifest_sha256=manifest["payload_hash"],
        capability_sha256=_hash(cap), journal_sha256=_hash(events))
    replace_summary(native, archive["run_id"], archive, insert=True)
    replace_summary(native, rid, repair)
    return p, native, parent, archive, repair


def test_new_completion_preserves_old_unknown_and_guards_require_all_proof(tmp_path):
    p, native, parent, archive, repair = postimage(tmp_path)
    cp = repair["source_window_end"]
    before = deepcopy((native, parent))
    assert preserved_unknown_proofs(native, parent, cp) == {16: archive["github_failure"]}
    assert (native, parent) == before
    class History(GitHub):
        def get(self, path):
            if path == "actions/runs/16": return archive["github_failure"] | {"unrelated_api_field": True}
            return super().get(path)
    history = History([archive["github_failure"]])
    with pytest.raises(RuntimeError, match="previous_dispatch_no_retry"):
        guard.controls(env(), "a"*40, history)
    guard.controls(env(), "a"*40, history, reconciliation_state=(native, parent))
    # A subsequent unrelated failure has no certificate, even with an older cp.
    history.prior.append(prior(id=19, conclusion="failure"))
    with pytest.raises(RuntimeError, match="previous_dispatch_no_retry"):
        guard.controls(env(), "a"*40, history, reconciliation_state=(native, parent))
    assert archive["status"] == "UNKNOWN_NO_REPLAY" and archive["replay_allowed"] is False


@pytest.mark.parametrize("mode", ["pending", "parent_pending", "old_success", "old_digest", "old_history",
    "no_alias", "no_ack", "manifest", "checkpoint", "new_failure", "old_rerun", "certificate"])
def test_incomplete_or_tampered_checkpoint_proof_stops_without_effects(tmp_path, mode):
    p, native, parent, archive, repair = postimage(tmp_path)
    cp = repair["source_window_end"]
    if mode == "pending": native.update(phase="pending", attempt="f"*32)
    if mode == "parent_pending": parent["sources"]["aupay_card"].update(phase="pending", attempt="f"*32)
    if mode == "old_success": archive["status"] = "complete"
    if mode == "old_digest": archive["native_sha256"] = "0"*64
    if mode == "old_history":
        old = archive["native_before"]
        # A preserved old checkpoint is not permission to erase its history.
        raw = old["files"]["recurring.sqlite3"]
        raw["sha256"] = "0"*64
        archive["native_sha256"] = _hash(old)
    if mode == "no_alias": repair["card_aliases"] = []
    if mode == "no_ack": repair["checkpoint_reconciliation"]["journal_sha256"] = "0"*64
    if mode == "manifest": repair["checkpoint_reconciliation"]["manifest_sha256"] = "0"*64
    if mode == "checkpoint": archive["checkpoint_after"] = apply.CHECKPOINT
    if mode == "new_failure": repair["status"] = "failed"
    if mode == "old_rerun": archive["github_failure"]["run_attempt"] = 2
    if mode == "certificate": repair.pop("checkpoint_reconciliation")
    # Keep archive's SQL status UNKNOWN: changing just the JSON must be rejected.
    replace_summary(native, archive["run_id"], archive)
    # replace_summary dropped its disposable trigger already.
    item = native["files"]["recurring.sqlite3"]
    with sqlite3.connect(":memory:") as con:
        con.deserialize(base64.b64decode(item["data"]))
        con.execute("UPDATE recurring_runs SET summary_json=? WHERE run_id=?", (json.dumps(repair), repair["run_id"]))
        con.commit(); data = con.serialize()
    item.update(data=base64.b64encode(data).decode(), sha256=hashlib.sha256(data).hexdigest())
    with pytest.raises(RuntimeError): preserved_unknown_proofs(native, parent, cp)
    assert p.db.write_calls == 1  # The fixture's one synthetic append only.
