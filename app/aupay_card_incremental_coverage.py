"""Proof of ordinary historical coverage, separate from execution wall time.

No recovery entry, state mutation, dispatch or financial authority lives here.
"""
from datetime import datetime, timedelta
import hashlib
import json

from .aupay_card_window_preview import require, digest

PROTOCOL = "card-incremental-coverage-v1"
WORKFLOW = ".github/workflows/aupay-card-incremental-apply.yml"


def plan_contract_sha256(prepared):
    """Fixed maintenance purchase/review contract, independent of runtime IDs."""
    from .transaction_plan import normalize_card_transaction
    sources=[(v.identity,v.source_hash,v.source,v.transaction_date,v.merchant,v.amount_yen,v.payment_method,v.memo,v.business_fingerprint)
             for v in map(normalize_card_transaction,prepared["transactions"])]
    return digest(dict(checkpoint=prepared["checkpoint"],start=prepared["window"].start.isoformat(),
        end=prepared["window"].end.isoformat(),sources=sorted(sources),statuses=sorted(prepared["statuses"].items()),
        dispositions=sorted((v.canonical_identity,v.status,sorted(v.source_identities)) for v in prepared["plan"].item_decisions),
        money=sorted((d.record.money_id,d.record.fingerprint,d.record.source_id,d.record.original_url,
                      d.action,d.reason,d.related_id,list(d.expense_ids)) for d in prepared["decisions"])))


def completion_certificate(*, snapshot_files, result):
    from .aupay_card_incremental_recovery import _rows
    native = dict(files=snapshot_files)
    rid = result["run_id"]
    require(type(result["written"]) is int and result["written"]>=0
            and result["new_eligible"]==result["written"] and result["failure"]==0
            and result["status"]==("complete" if result["written"] else "noop"),"coverage_batch_unproven")
    manifests = [v for v in _rows(native, "manifests.sqlite3", "batch_manifests") if v["run_id"] == rid]
    caps = [v for v in _rows(native, "capabilities.sqlite3", "batch_capabilities") if v["run_id"] == rid]
    events = sorted((v for v in _rows(native, "journal.sqlite3", "batch_events") if v["run_id"] == rid), key=lambda v:v["seq"])
    if result["written"]:
        require(len(manifests) == len(caps) == 1 and caps[0]["state"] == "sealed"
                and manifests[0]["payload_hash"] == hashlib.sha256(manifests[0]["payload"].encode()).hexdigest()
                and any((v["stage"], v["state"]) == ("write_result", "acknowledged") for v in events)
                and events[-1]["stage"] == "final" and events[-1]["state"] == "confirmed", "coverage_batch_unproven")
        manifest = json.loads(manifests[0]["payload"])
        require(manifest["run_id"] == rid and manifest["batch_ref"] == caps[0]["batch_ref"]
                and manifest["batch_size"] == result["written"] == result["new_eligible"]
                and manifest["source_window"]["start"] == result["source_window_start"]
                and manifest["source_window"]["end"] == result["source_window_end"]
                and result["write_requests"] == 1, "coverage_batch_unproven")
    else:
        require(not manifests and not caps and not events and result["write_requests"] == 0
                and result["status"] == "noop", "coverage_batch_unproven")
    require(not _rows(native, "leases.sqlite3", "writer_leases"), "coverage_lease_pending")
    return dict(manifests_sha256=digest(manifests), capabilities_sha256=digest(caps),
                journal_sha256=digest(events), aliases_sha256=digest(result["card_aliases"]))


def successful_coverage_proofs(native, parent, expected_checkpoint):
    from .aupay_card_incremental_recovery import _rows
    from .aupay_card_window_preview import checkpoint
    checkpoint(native, expected_checkpoint)
    card = parent["sources"]["aupay_card"]
    require(card["phase"] == "ready" and not card["attempt"] and not card["error"], "coverage_parent_pending")
    result = {}
    for row in _rows(native, "recurring.sqlite3", "recurring_runs"):
        summary = json.loads(row["summary_json"])
        if summary.get("coverage_protocol") != PROTOCOL:
            continue
        identity = summary["github_execution"]
        require(set(identity) == {"id", "head_sha", "head_branch", "run_attempt", "event", "path", "created_at"}
                and type(identity["id"]) is int and identity["id"] not in result
                and identity["head_branch"] == "main" and identity["run_attempt"] == 1
                and identity["path"] == WORKFLOW and identity["event"] in {"workflow_dispatch", "schedule"}
                and len(identity["head_sha"]) == 40 and row["status"] == summary["status"] in {"complete", "noop"}
                and summary["failure"] == 0, "coverage_execution_unproven")
        before, start, end = (datetime.fromisoformat(summary[k]) for k in
                             ("checkpoint_before", "source_window_start", "source_window_end"))
        require(start == before - timedelta(days=2) and before < end <= datetime.fromisoformat(expected_checkpoint)
                and 0 < (end-start).total_seconds() <= 14*86400
                and row["window_start"] == summary["source_window_start"] and row["window_end"] == summary["source_window_end"]
                and datetime.fromisoformat(identity["created_at"].replace("Z", "+00:00"))
                    <= datetime.fromisoformat(summary["execution_started_at"])
                    <= datetime.fromisoformat(summary["execution_completed_at"]), "coverage_window_unproven")
        require(summary["coverage_certificate"] == completion_certificate(snapshot_files=native["files"], result=summary),
                "coverage_certificate_changed")
        result[identity["id"]] = dict(identity, status="completed", conclusion="success")
    return result
