"""Explicit finalization of an orphaned validation claim; no live entry point.

The host must separately authorize this operation and supply the unchanged
IndependentReceiptReader, ValidationJournal/ReceiptPrivateAdapter and cache
projection. No claim, dispatch, credentials, writer or workflow is created.
"""
from copy import deepcopy

from .drive_run_state import StateError
from .page_receipt_model import digest
from .receipt_item_correction_review import registration_plan
from .receipt_sheet_validation import (
    FINAL, cached_result, request_uuid, result_cache_entry, strong_etag,
)


def finalize_in_progress_request(request_id, *, reader, journal, projection,
                                 expected_etag, expected_journal_digest,
                                 expected_snapshot_digest, verify_stopped,
                                 consume_once):
    """Compute a real result, then attempt at most ONE existing CAS to FINAL.

    expected_* comes from an independent, just-in-time journal GET, never UI.
    verify_stopped MUST independently verify the pinned old run is completed,
    no competing worker exists, and the execution flag remains disabled; it
    returns exactly True or raises. It runs again just before the final GET.
    consume_once MUST atomically/durably consume this separately authorized,
    UUID/ETag/digest-bound attempt before any IO here, and reject EVERY reuse,
    including after process/adapter recreation. A transport fault is never a
    reason to create a new attempt. Neither callback may mutate the journal.

    Any exception before confirmed save raises a fixed no-retry error. The
    journal's existing save and adapter retain their independent read-backs,
    strong If-Match and GET-only latch. Cache handling starts only after save
    returns successfully; its failure cannot trigger another finalization.
    """
    try:
        consume_once()
        request_uuid(request_id)
        if not strong_etag(expected_etag) or verify_stopped() is not True:
            raise ValueError()
        before, tag = journal.load(writable=True)
        record = before["requests"][request_id]
        if (tag != expected_etag or digest(before) != expected_journal_digest
                or record["state"] != "検証中" or record["result"] is not None
                or record["snapshot"]["digest"] != expected_snapshot_digest):
            raise ValueError()
        snapshot = deepcopy(record["snapshot"])
        fresh = reader(snapshot["case_id"])
        if fresh["request_snapshot"] != snapshot:
            raise ValueError()
        plan = registration_plan(fresh["snapshot"], fresh)
        again = reader(snapshot["case_id"])
        if (again["request_snapshot"] != snapshot or again["snapshot"] != fresh["snapshot"]
                or verify_stopped() is not True):
            raise ValueError()
        current, current_tag = journal.load(writable=True)
        if current_tag != tag or current != before:
            raise ValueError()
        result = {k: deepcopy(plan[k]) for k in
                  ("state", "issues", "snapshot_digest", "posting_authority", "financial_write_count")}
        result["snapshot_digest"] = snapshot["digest"]
        if (result["state"] not in FINAL or result["posting_authority"] is not False
                or type(result["financial_write_count"]) is not int or result["financial_write_count"] != 0):
            raise ValueError()
        proposed = deepcopy(current)
        proposed["requests"][request_id].update(state=result["state"], result=result)
        proposed["generation"] += 1
        # Existing adapter validates exactly one allowed core transition.
        # No transition-to-検証中 and no reload/retry with a newer ETag.
        journal.save(proposed, current_tag)
        confirmed = deepcopy(proposed["requests"][request_id])
    except Exception:
        raise StateError("receipt_finalization_not_confirmed_no_retry") from None

    display_pending = True
    try:
        projection.append_result(result_cache_entry(confirmed))
        cached_result(projection.read_results(), confirmed)
        display_pending = False
    except Exception:
        pass  # Journal is confirmed FINAL; never retry its CAS or the append.
    return dict(record=confirmed, finalization_confirmed=True, display_pending=display_pending)
