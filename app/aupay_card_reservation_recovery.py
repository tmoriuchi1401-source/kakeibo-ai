"""Proofs for unused reservations. No transport, writer, DB or authority is created."""
import base64
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from uuid import UUID

from .aupay_card_window_preview import require

PROTOCOL = "card-reservation-recovery-v2"
RECOVERY_WORKFLOW = "aupay-card-reservation-recovery.yml"
FAILURE_PATHS = frozenset({
    ".github/workflows/aupay-card-incremental-apply.yml",
    ".github/workflows/aupay-card-reconciliation-once.yml",
    ".github/workflows/" + RECOVERY_WORKFLOW,
})


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=True,separators=(",",":")).encode()).hexdigest()


def summaries(native):
    from .aupay_card_incremental_recovery import _rows
    return {r["run_id"]:json.loads(r["summary_json"]) for r in _rows(native,"recurring.sqlite3","recurring_runs")}


def unused_reservation_proof(native, parent_raw, recovery, parent_metadata_digest, now, failure=None):
    """Parent barrier for legacy runs; durable intent barrier for this protocol.

    Empty journal/issued alone is NEVER evidence of no dispatch: both can be
    stale after runner loss. Parent bytes+revision, or this protocol's required
    write-ahead intent barrier, independently dominate the financial writer.
    """
    from .aupay_card_incremental_recovery import _rows
    from .production_ledger import _encode
    prior = recovery["previous_record"]
    rid = recovery["failed_run_uuid"]
    require(native["phase"]=="pending" and UUID(rid).hex==native["attempt"],"recovery_reservation_owner_changed")
    runs=summaries(native)
    reservation=runs.get("RESERVATION:"+rid,{})
    require(reservation.get("status")=="REPAIR_RESERVED" and reservation.get("replay_allowed") is False
            and reservation.get("record_sha256")==digest(prior),"recovery_reservation_record_changed")
    if reservation.get("protocol")==PROTOCOL:
        identity=reservation.get("github_execution",{})
        require(identity and failure is not None and all(failure.get(k)==v for k,v in identity.items()),
                "recovery_failure_identity_invalid")
    origins=[a for a in runs.values() if a.get("status")=="UNKNOWN_NO_REPLAY" and a.get("repair_run_id")==rid]
    require(len(origins)==1 and origins[0].get("archive_schema") in {2,3},"recovery_origin_missing")
    origin=origins[0]
    baseline=base64.b64decode(origin["parent_before_raw_b64"],validate=True)
    require(hashlib.sha256(baseline).hexdigest()==prior["parent_before_sha256"]
            and origin["evidence_sha256"]==digest(prior)
            and origin["checkpoint_before"]==prior["checkpoint_before"]
            and origin["checkpoint_after"]==prior["checkpoint_after"],"recovery_parent_barrier_changed")
    require("INTENT:"+rid not in runs,"recovery_financial_dispatch_possible")
    if parent_raw==baseline:
        require(parent_metadata_digest==prior["parent_before_metadata_digest"],"recovery_parent_revision_changed")
        mode="unchanged_parent_revision"
    else:
        expected=json.loads(baseline)
        expected["generation"]+=1
        expected["sources"]["aupay_card"].update(phase="pending",attempt=UUID(rid).hex,error="")
        require(reservation.get("protocol")==PROTOCOL and parent_raw==_encode(expected),
                "recovery_financial_dispatch_possible")
        mode="required_intent_absent"
    caps=[r for r in _rows(native,"capabilities.sqlite3","batch_capabilities") if r["run_id"]==rid]
    manifests=[r for r in _rows(native,"manifests.sqlite3","batch_manifests") if r["run_id"]==rid]
    require(len(caps)==len(manifests)==1,"recovery_unused_batch_missing")
    cap,manifest=caps[0],manifests[0]
    require(cap["state"]=="issued" and not cap["attempt_id"] and not cap["terminal_reason"]
            and datetime.fromisoformat(cap["expires_at"])<=now
            and reservation["capability_id"]==cap["capability_id"]
            and cap["batch_ref"]==reservation["batch_ref"]
            and reservation["manifest_sha256"]==manifest["payload_hash"]
            and hashlib.sha256(manifest["payload"].encode()).hexdigest()==manifest["payload_hash"],
            "recovery_unused_capability_unproven")
    payload=json.loads(manifest["payload"])
    require(payload["run_id"]==rid and payload["batch_ref"]==cap["batch_ref"]
            and payload["target_ref"]==cap["target_ref"]==prior["historical_target_ref"]
            and payload["audit_key_id"]==prior["audit_key_id"]
            and payload["batch_size"]==prior["import_count"]
            and payload["source_window"]["end"]==prior["checkpoint_after"],"recovery_unused_manifest_changed")
    require(not any(r["run_id"]==rid for r in _rows(native,"journal.sqlite3","batch_events"))
            and not _rows(native,"leases.sqlite3","writer_leases")
            and all(r==cap or r["state"]=="sealed" for r in _rows(native,"capabilities.sqlite3","batch_capabilities")),
            "recovery_financial_dispatch_possible")
    cp=_rows(native,"recurring.sqlite3","recurring_checkpoint")
    require(len(cp)==1 and cp[0]["successful_window_end"]==prior["checkpoint_before"],"recovery_checkpoint_changed")
    return dict(mode=mode,failed_run_uuid=rid,capability_before=cap,manifest_sha256=manifest["payload_hash"],
                reservation_sha256=digest(reservation),parent_metadata_digest=parent_metadata_digest)


def validate_revocations(native, all_summaries):
    """Only exact issued->sealed revocations bound to an archived unused proof."""
    from .aupay_card_incremental_recovery import _rows
    current={r["capability_id"]:r for r in _rows(native,"capabilities.sqlite3","batch_capabilities")}
    result={}
    for item in all_summaries.values():
        if item.get("status")!="CAPABILITY_REVOKED_UNUSED":continue
        archive=all_summaries.get(item["archive_id"],{})
        require(archive.get("archive_schema")==3 and archive.get("status")=="UNKNOWN_NO_REPLAY"
                and item["run_id"]=="REVOCATION:"+item["before"]["capability_id"]
                and item["evidence_sha256"]==archive["evidence_sha256"],"recovery_revocation_archive_missing")
        raw=base64.b64decode(archive["native_before_raw_b64"],validate=True)
        parent=base64.b64decode(archive["parent_before_raw_b64"],validate=True)
        require(hashlib.sha256(raw).hexdigest()==archive["native_before_bytes_sha256"]
                and hashlib.sha256(parent).hexdigest()==archive["parent_before_bytes_sha256"]
                and json.loads(raw)==archive["native_before"],"recovery_revocation_archive_changed")
        proof=unused_reservation_proof(json.loads(raw),parent,archive["recovery"],
            archive["parent_before_metadata_digest"],datetime.fromisoformat(item["revoked_at"]),archive["github_failure"])
        before=item["before"];after=deepcopy(before)
        after.update(state="sealed",terminal_reason="revoked_unused_no_dispatch")
        cid=before["capability_id"]
        owner=[r for r in current.values() if r["run_id"]==archive["repair_run_id"]]
        require(cid not in result and item["replay_allowed"] is False
                and len(owner)==1 and datetime.fromisoformat(item["revoked_at"])<=datetime.fromisoformat(owner[0]["issued_at"])
                and proof==item["unused_proof"] and proof["capability_before"]==before
                and item["before_sha256"]==digest(before) and item["after_sha256"]==digest(after)
                and item["after"]==current.get(cid)==after,"recovery_revocation_changed")
        result[cid]=item
    return result


def archive_chain(all_summaries):
    return [a for _,a in sorted(all_summaries.items()) if a.get("status")=="UNKNOWN_NO_REPLAY"]


def validate_pending_archives(native):
    """Check immutable history BEFORE allowing another unused-reservation repair."""
    from .aupay_card_incremental_recovery import _rows
    from .drive_run_state import CARD_FILES, _validate_file
    runs=summaries(native);revocations=validate_revocations(native,runs)
    require(all(json.loads(r["summary_json"]).get("status")==r["status"]
                for r in _rows(native,"recurring.sqlite3","recurring_runs")),"recovery_archive_history_changed")
    for archive in archive_chain(runs):
        require(archive.get("archive_schema") in {2,3} and archive["replay_allowed"] is False,
                "recovery_archive_history_changed")
        old=archive["native_before"]
        if archive.get("archive_schema")==3:
            require(archive["adopted_archive_ids"]==[a["run_id"] for a in archive_chain(summaries(old))]
                    and len([v for v in revocations.values() if v["archive_id"]==archive["run_id"]])==1,
                    "recovery_archive_history_changed")
        raw=base64.b64decode(archive["native_before_raw_b64"],validate=True)
        parent=base64.b64decode(archive["parent_before_raw_b64"],validate=True)
        require(json.loads(raw)==old and digest(old)==archive["native_sha256"]
                and hashlib.sha256(raw).hexdigest()==archive["native_before_bytes_sha256"]
                and hashlib.sha256(parent).hexdigest()==archive["parent_before_bytes_sha256"]
                and json.loads(parent)["sources"]["aupay_card"]==archive["parent_card_before"]
                and digest(archive["parent_card_before"])==archive["parent_card_sha256"]
                and old["binding"]==native["binding"] and old["source"]==native["source"]
                and old["phase"]=="pending" and old["attempt"],"recovery_archive_history_changed")
        for file,item in old["files"].items():
            require(file in CARD_FILES,"recovery_archive_history_changed")
            data=base64.b64decode(item["data"],validate=True)
            require(hashlib.sha256(data).hexdigest()==item["sha256"],"recovery_archive_history_changed")
            _validate_file(old["source"],file,data)
            for table in CARD_FILES[file]-{"recurring_checkpoint"}:
                retained={digest(r) for r in _rows(native,file,table)}
                for row in _rows(old,file,table):
                    if digest(row) in retained:continue
                    revoked=revocations.get(row.get("capability_id"),{}) if table=="batch_capabilities" else {}
                    require(revoked.get("before")==row and digest(revoked["after"]) in retained,
                            "recovery_archive_history_changed")
        cp=_rows(old,"recurring.sqlite3","recurring_checkpoint")
        require(len(cp)==1 and cp[0]["successful_window_end"]==archive["checkpoint_before"],
                "recovery_archive_history_changed")
    return revocations


def completed_successor(archive, all_summaries):
    """Follow immutable predecessor links; never rewrite an old failed run."""
    seen=set();terminal=archive
    for _ in range(len(all_summaries)+1):
        rid=terminal["repair_run_id"]
        require(rid not in seen,"recovery_archive_cycle")
        seen.add(rid)
        repair=all_summaries.get(rid,{})
        if repair.get("status")=="complete":return terminal,repair
        next_archives=[a for a in all_summaries.values() if a.get("status")=="UNKNOWN_NO_REPLAY"
                       and a.get("archive_schema")==3 and a.get("recovery",{}).get("failed_run_uuid")==rid]
        require(len(next_archives)==1,"reconciliation_new_completion_missing")
        terminal=next_archives[0]
    require(False,"recovery_archive_cycle")
