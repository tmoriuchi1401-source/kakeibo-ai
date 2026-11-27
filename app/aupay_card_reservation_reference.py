"""Small signed reference to a fixed record restored from existing Google state.

No new store or authority. Full predecessor bytes stay in their exact archives;
the existing 1MiB capsule limit is not raised to inline nested state evidence.
"""
import base64
from copy import deepcopy
import hashlib
from uuid import UUID

from .aupay_card_reservation_recovery import digest,summaries,PROTOCOL,validate_pending_archives
from .aupay_card_window_preview import require

KIND="fixed-card-unused-reservation-reference-v1"
NEW_RECORD_SHA256="470ebe41d42e57eef06be0448e2f5511f1741042b349f1f6afd3cd6a8302ce3c"
REQUEST_KEYS={"schema","kind","bindings","failed_run_uuid","failed_record_sha256","native_before_sha256",
    "native_before_metadata_digest","parent_before_sha256","parent_before_metadata_digest","old_failure","record_sha256"}


def derive_record(request,previous):
    require(set(request)==REQUEST_KEYS and request["schema"]==1 and request["kind"]==KIND
            and request["bindings"]==previous["bindings"] and request["failed_record_sha256"]==digest(previous),
            "recovery_reference_changed")
    record=deepcopy(previous)
    record.pop("business_record",None)
    for key in ("native_before_sha256","native_before_metadata_digest","parent_before_sha256","parent_before_metadata_digest","old_failure"):
        record[key]=deepcopy(request[key])
    record["recovery"]=dict(previous_record=deepcopy(previous),failed_run_uuid=request["failed_run_uuid"])
    require(digest(record)==request["record_sha256"],"recovery_reference_digest_changed")
    return record


def restore_failed_record(native,failed_run_uuid):
    runs=summaries(native)
    reservation=runs.get("RESERVATION:"+failed_run_uuid,{})
    require(reservation.get("protocol")==PROTOCOL and reservation.get("status")=="REPAIR_RESERVED"
            and reservation.get("replay_allowed") is False,"recovery_reference_missing")
    archives=[v for v in runs.values() if v.get("status")=="UNKNOWN_NO_REPLAY" and v.get("repair_run_id")==failed_run_uuid]
    require(len(archives)==1,"recovery_reference_missing")
    archive=archives[0]
    if archive.get("recovery_request"):
        previous=archive["recovery"]["previous_record"]
        restored=derive_record(archive["recovery_request"],previous)
        if archive["recovery"].get("bank_transitions"):
            from .aupay_card_bank_premises import effective_record
            restored=effective_record(restored,archive["recovery"]["bank_transitions"])
    else:
        from .aupay_card_bank_premises import effective_record
        proof=archive["recovery"]["bank_transitions"]
        restored=effective_record(proof["business_record"],proof)
    require(digest(restored)==reservation["record_sha256"]==archive["evidence_sha256"]
            and restored["native_before_sha256"]==hashlib.sha256(base64.b64decode(archive["native_before_raw_b64"],validate=True)).hexdigest()
            and restored["parent_before_sha256"]==hashlib.sha256(base64.b64decode(archive["parent_before_raw_b64"],validate=True)).hexdigest(),
            "recovery_reference_digest_changed")
    return restored


def resolve_reference(request,native,observed):
    require(request.get("kind")==KIND and native["phase"]=="pending"
            and native["attempt"]==UUID(request["failed_run_uuid"]).hex
            and observed==dict(sha256=request["native_before_sha256"],metadata_digest=request["native_before_metadata_digest"]),
            "recovery_reference_state_changed")
    validate_pending_archives(native)
    previous=restore_failed_record(native,request["failed_run_uuid"])
    record=derive_record(request,previous)
    require(digest(record)==NEW_RECORD_SHA256,"recovery_reference_digest_changed")
    return record
