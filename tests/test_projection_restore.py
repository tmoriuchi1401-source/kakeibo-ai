"""Combined pending-state restore fixture, also used for isolated Drive QA.

All ledger values here are synthetic. The Google exercise uses owner-only,
unregistered files; it never sends these records to a production ledger.
"""
from copy import deepcopy
import json

import pytest

from app.amazon_money import empty_book
from app.amazon_money_review import MoneyReviews
from app.amazon_money_writer import MoneyWriter
from app.daily_corrections import DailyCorrections
from app.projection_cache import RollingProjectionStore, initial_files
from app.projection_refresh import ProjectionRefresh
from app.projection_store import ProjectionJournal
from test_amazon_money import Ledger as MoneyLedger, record
from test_daily_corrections import Ledger as CorrectionLedger
from test_projection_refresh import Store, Reader, row, PAIRS


SOURCE = "synthetic-isolated-projection-restore-20260920"
CORRECTION = "REQ-" + "c" * 32
REVIEW = "REQ-" + "d" * 32


def pending_record():
    return record(source_id="synthetic-restore-charge", reference="transaction:synthetic-restore-charge", amount=900)


def pending_snapshot():
    backing = Store()
    store = RollingProjectionStore(backing, current_month="2026-09")
    reader = Reader([row("synthetic-old", "2025-12-31", 100)])
    ProjectionRefresh(store, reader).bootstrap(PAIRS)
    backing.data["money"] = empty_book(cutover_day="2026-09-19", legacy={})
    ledger = MoneyLedger()
    ledger.rows["支出明細"] = {r[0]: deepcopy(r) for r in reader.rows}
    writer = MoneyWriter(store, ledger)
    # As in the real Sheets adapter, invalidation precedes an expense append.
    ProjectionJournal(store).mark(append=True)
    ledger.fail_after = "支出明細"
    with pytest.raises(RuntimeError):
        writer.apply([pending_record()], limit=1)
    assert backing.data["money"]["records"][pending_record().money_id]["state"] == "pending"
    reader.rows = deepcopy(list(ledger.rows["支出明細"].values()))
    corrections_db = CorrectionLedger(store, reader)
    corrections = DailyCorrections(store, corrections_db)
    corrections.prepare(CORRECTION, "synthetic-old", {"date": "2026-01-01", "amount": 80,
        "note": "合成・本人の修正入力"}, form_digest="synthetic-correction-form")
    corrections_db.fail_after = True
    with pytest.raises(RuntimeError):
        corrections.apply(CORRECTION)
    # A second unresolved money record has a saved pending human hold request.
    unknown = record(source_id="synthetic-unconfirmed", reference="transaction:synthetic-unconfirmed",
                     confirmed=False, amount=222)
    writer.apply([unknown], limit=1)
    reviews = MoneyReviews(writer)
    reviews.prepare(REVIEW, unknown.money_id, "hold", form_digest="synthetic-review-form")
    backing.fail_key, backing.after_save = "money", True
    with pytest.raises(RuntimeError):
        reviews.apply(REVIEW)
    assert backing.data["corrections"]["requests"][CORRECTION]["state"] == "pending"
    assert backing.data["money-reviews"]["requests"][REVIEW]["state"] == "pending"
    envelopes = initial_files(SOURCE)
    for item in envelopes:
        item["payload"]["data"] = deepcopy(backing.data.get(item["payload"]["key"]))
    return {"schema": 1, "source": SOURCE, "documents": envelopes,
        "ledger": {"expenses": deepcopy(reader.rows), "imports": deepcopy(list(ledger.rows["取込データ"].values()))}}


def resume_snapshot(snapshot):
    """Resume only in-memory synthetic ledger adapters after native readback."""
    expected = {item["payload"]["key"]: item for item in initial_files(SOURCE)}
    assert snapshot["schema"] == 1 and snapshot["source"] == SOURCE
    assert len(snapshot["documents"]) == len(expected) == 24
    backing = Store()
    seen = set()
    for item in snapshot["documents"]:
        payload = item["payload"]
        key = payload["key"]
        assert key not in seen and key in expected
        seen.add(key)
        assert item["name"] == expected[key]["name"] and item["mimeType"] == "application/json"
        assert set(payload) == {"schema", "binding", "key", "data"}
        assert payload["schema"] == 1 and payload["binding"] == expected[key]["payload"]["binding"]
        assert payload["data"] is None or isinstance(payload["data"], dict)
        if payload["data"] is not None:
            backing.data[key] = deepcopy(payload["data"])
    store = RollingProjectionStore(backing, current_month="2026-09")
    reader = Reader(deepcopy(snapshot["ledger"]["expenses"]))
    ledger = MoneyLedger()
    ledger.rows = {"支出明細": {r[0]: deepcopy(r) for r in reader.rows},
                   "取込データ": {r[0]: deepcopy(r) for r in snapshot["ledger"]["imports"]}}
    before_ledger = deepcopy(ledger.rows)
    before_rows = deepcopy(reader.rows)
    writer = MoneyWriter(store, ledger)
    result = writer.apply([pending_record()], limit=1)
    assert result["expense_rows_written"] == result["import_rows_written"] == 0
    writer.verify_posted(pending_record())
    correction_db = CorrectionLedger(store, reader)
    corrections = DailyCorrections(store, correction_db)
    assert corrections.apply(CORRECTION)["state"] == "applied"
    reviews = MoneyReviews(writer)
    assert reviews.apply(REVIEW)["state"] == "applied"
    assert backing.data["corrections"]["requests"][CORRECTION]["form_digest"] == "synthetic-correction-form"
    assert backing.data["money-reviews"]["requests"][REVIEW]["form_digest"] == "synthetic-review-form"
    refresh = ProjectionRefresh(store, reader)
    refresh.refresh(PAIRS)
    assert refresh.read_month("2025-12").amount == 0
    assert refresh.read_month("2026-01").amount == 80
    assert refresh.read_month("2026-09").amount == 900
    assert ledger.rows == before_ledger and reader.rows == before_rows
    assert ledger.calls == [] and correction_db.calls == []
    saved, writes = deepcopy(backing.data), len(backing.writes)
    assert writer.apply([pending_record()], limit=1)["money_duplicate"] == 1
    assert corrections.apply(CORRECTION)["state"] == reviews.apply(REVIEW)["state"] == "applied"
    refresh.refresh(PAIRS)
    assert backing.data == saved and len(backing.writes) == writes
    return {"pending_resumed": 3, "ledger_writes": 0, "replay_document_writes": 0,
            "documents_verified": 24, "year_crossing_verified": True}


def test_complete_serialized_snapshot_resumes_three_pending_operations_without_reposting():
    snapshot = json.loads(json.dumps(pending_snapshot(), ensure_ascii=False))
    assert resume_snapshot(snapshot) == {"pending_resumed": 3, "ledger_writes": 0,
        "replay_document_writes": 0, "documents_verified": 24, "year_crossing_verified": True}


@pytest.mark.parametrize("damage", ["missing", "duplicate", "wrong-binding", "wrong-key"])
def test_partial_or_wrong_source_restore_is_rejected_before_resume(damage):
    snapshot = pending_snapshot()
    if damage == "missing": snapshot["documents"].pop()
    elif damage == "duplicate": snapshot["documents"][-1] = deepcopy(snapshot["documents"][0])
    elif damage == "wrong-binding": snapshot["documents"][0]["payload"]["binding"] = "wrong"
    else: snapshot["documents"][0]["payload"]["key"] = "unknown"
    with pytest.raises(AssertionError): resume_snapshot(snapshot)
