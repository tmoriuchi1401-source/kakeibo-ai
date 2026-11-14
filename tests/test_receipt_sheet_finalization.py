"""Limited synthetic faults; real reader, journal, adapter and validator."""
from copy import deepcopy
import json
import socket

import pytest

from app.drive_run_state import StateError
from app.page_receipt_model import digest
from app.receipt_private_adapter import ReceiptPrivateAdapter
from app.receipt_sheet_finalization import finalize_in_progress_request
from app.receipt_sheet_validation import (
    FINAL, PURPOSE, UNKNOWN, ValidationJournal, empty_journal, execute_accepted_request,
)
from test_receipt_sheet_connection import connected
from test_receipt_private_adapter import encode
from test_receipt_sheet_validation import RID


@pytest.fixture(autouse=True)
def no_network_or_financial_writer(monkeypatch):
    calls = []
    def deny(*a, **kw):
        calls.append(True)
        raise AssertionError("external/financial IO forbidden")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    from app.sheets import SheetsDB
    monkeypatch.setattr(SheetsDB, "__init__", deny)
    yield
    assert calls == []


def seed(*, calculation="balanced", state="検証中", generation=3):
    c, w, p, io = connected()
    if calculation == "difference":
        p.rows[0][20] = 869
        p.rows[0][4:6] = [1369, -1]
    if calculation == "allocation":
        p.rows[0][20:23] = [730, "日用品", "洗剤"]
        p.rows[0][27:36] = ["外税", 1, 138, "食費", "食品", "外税", "指定なし", "採用", True]
    fresh = c.reader(w.case_id)
    snapshot = fresh["request_snapshot"]
    journal = empty_journal()
    journal.update(generation=generation, requests={RID: dict(request_id=RID, purpose=PURPOSE,
        snapshot=deepcopy(snapshot), state=state, result=None)})
    envelope = json.loads(io.raw)
    envelope["journal"] = journal
    io.raw, io.generation = encode(envelope), generation
    return c, w, p, io


def options(c, tmp_path):
    before, tag = c.journal.load(writable=True)
    path = tmp_path / "consumed-finalization.json"
    def consume():
        # Durable exclusive slot, shared even by a recreated adapter/caller.
        with path.open("x", encoding="utf-8") as f:
            json.dump(dict(uuid=RID, etag=tag, journal_digest=digest(before),
                           snapshot_digest=before["requests"][RID]["snapshot"]["digest"]), f)
    return dict(reader=c.reader, journal=c.journal, projection=c.projection,
                expected_etag=tag, expected_journal_digest=digest(before),
                expected_snapshot_digest=before["requests"][RID]["snapshot"]["digest"],
                verify_stopped=lambda: True, consume_once=consume)


def no_claim(c, monkeypatch):
    def deny(*a, **kw): raise AssertionError("claim/worker forbidden")
    monkeypatch.setattr(c, "run", deny)
    monkeypatch.setattr(c, "prepare", deny)
    monkeypatch.setattr(c.journal, "transition", deny)


@pytest.mark.parametrize("calculation,expected", [
    ("balanced", "検証済み・未記帳"),
    ("difference", "修正が必要"),
    ("allocation", "修正が必要"),
])
def test_one_final_cas_real_plan_then_cache(calculation, expected, tmp_path, monkeypatch):
    c, w, p, io = seed(calculation=calculation)
    no_claim(c, monkeypatch)
    opts = options(c, tmp_path)
    rows, source, catalog = deepcopy(p.rows), w.raw, json.loads(io.raw)["catalog"]
    append = p.append_result
    observed = []
    def after_confirmed(record):
        # Both existing adapter and journal read-backs have completed.
        assert c.private._ack is None and c.private._stopped is False
        assert json.loads(io.raw)["journal"]["requests"][RID]["state"] in FINAL
        observed.append(True)
        return append(record)
    monkeypatch.setattr(p, "append_result", after_confirmed)
    out = finalize_in_progress_request(RID, **opts)
    assert out["finalization_confirmed"] and not out["display_pending"]
    assert out["record"]["state"] == expected
    assert out["record"]["result"]["posting_authority"] is False
    assert out["record"]["result"]["financial_write_count"] == 0
    assert bool(out["record"]["result"]["issues"]) == (expected == "修正が必要")
    assert io.attempts == io.writes == p.append_calls == len(observed) == 1
    assert json.loads(io.raw)["journal"]["generation"] == 4
    assert json.loads(io.raw)["catalog"] == catalog and p.rows == rows and w.raw == source


@pytest.mark.parametrize("origin", ["claim_response_unknown", "final_transition_before_put"])
def test_both_existing_unknown_routes_can_leave_generation_three(origin, monkeypatch):
    c, w, p, io = seed(state="受付済み", generation=2)
    replace = io.replace_versioned
    seen = []
    def fault(before, tag, after):
        seen.append(True)
        io.fault = "after" if origin == "claim_response_unknown" else ("before" if len(seen) == 2 else None)
        return replace(before, tag, after)
    monkeypatch.setattr(io, "replace_versioned", fault)
    out = execute_accepted_request(RID, reader=c.reader, journal=c.journal)
    durable = json.loads(io.raw)["journal"]
    assert out["state"] == UNKNOWN
    assert durable["generation"] == 3 and durable["requests"][RID]["state"] == "検証中"
    assert durable["requests"][RID]["result"] is None
    assert io.writes == 1 and p.append_calls == 0
    assert io.attempts == (1 if origin == "claim_response_unknown" else 2)


@pytest.mark.parametrize("damage", ["source_bytes", "source_version", "sheet_input"])
def test_changed_independent_evidence_has_no_cas(damage, tmp_path, monkeypatch):
    c, w, p, io = seed()
    no_claim(c, monkeypatch)
    opts = options(c, tmp_path)
    if damage == "source_bytes": w.raw = b"changed synthetic source"
    if damage == "source_version": w.metadata[w.source["source_id"]]["version"] += "-changed"
    if damage == "sheet_input":
        p.rows[0][20], p.rows[0][4:6] = 869, [1369, -1]
    with pytest.raises(StateError, match="finalization_not_confirmed_no_retry"):
        finalize_in_progress_request(RID, **opts)
    assert io.attempts == io.writes == p.append_calls == 0


@pytest.mark.parametrize("pin", ["expected_etag", "expected_journal_digest", "expected_snapshot_digest"])
def test_mismatched_fixed_pin_has_no_cas(pin, tmp_path):
    c, w, p, io = seed()
    opts = options(c, tmp_path)
    opts[pin] = '"wrong"' if pin == "expected_etag" else "a" * 64
    with pytest.raises(StateError, match="no_retry"):
        finalize_in_progress_request(RID, **opts)
    assert io.attempts == p.append_calls == 0


@pytest.mark.parametrize("state", ["受付済み", UNKNOWN])
def test_only_orphaned_in_progress_state_is_eligible(state, tmp_path, monkeypatch):
    c, w, p, io = seed(state=state)
    no_claim(c, monkeypatch)
    opts = options(c, tmp_path)
    with pytest.raises(StateError, match="no_retry"):
        finalize_in_progress_request(RID, **opts)
    assert io.attempts == io.writes == p.append_calls == 0


@pytest.mark.parametrize("when", [1, 2])
def test_run_not_ended_or_competition_appears_has_no_cas(when, tmp_path):
    c, w, p, io = seed()
    opts = options(c, tmp_path)
    seen = []
    def verify():
        seen.append(True)
        return len(seen) != when
    opts["verify_stopped"] = verify
    with pytest.raises(StateError, match="no_retry"):
        finalize_in_progress_request(RID, **opts)
    assert io.attempts == p.append_calls == 0


def test_etag_changes_during_review_has_no_cas(tmp_path):
    c, w, p, io = seed()
    opts = options(c, tmp_path)
    read = c.reader
    seen = []
    def reader(case):
        value = read(case)
        seen.append(True)
        if len(seen) == 2: io.generation += 1
        return value
    opts["reader"] = reader
    with pytest.raises(StateError, match="no_retry"):
        finalize_in_progress_request(RID, **opts)
    assert io.attempts == p.append_calls == 0


def test_server_cas_conflict_is_not_retried(tmp_path, monkeypatch):
    c, w, p, io = seed()
    opts = options(c, tmp_path)
    replace = io.replace_versioned
    def concurrent(before, tag, after):
        io.generation += 1
        return replace(before, tag, after)
    monkeypatch.setattr(io, "replace_versioned", concurrent)
    with pytest.raises(StateError, match="no_retry"):
        finalize_in_progress_request(RID, **opts)
    assert io.attempts == 1 and io.writes == p.append_calls == 0


@pytest.mark.parametrize("fault,writes", [("before", 0), ("after", 1), ("readback", 1), ("core_readback", 1)])
def test_unknown_never_touches_cache_and_durable_slot_blocks_recreation(fault, writes, tmp_path, monkeypatch):
    c, w, p, io = seed()
    opts = options(c, tmp_path)
    if fault == "core_readback":
        read, changed = c.private._read, []
        def read_then_change():
            value = read()
            if io.writes and not changed:
                io.generation += 1
                changed.append(True)
            return value
        monkeypatch.setattr(c.private, "_read", read_then_change)
    else:
        io.fault = fault
    with pytest.raises(StateError, match="no_retry"):
        finalize_in_progress_request(RID, **opts)
    assert io.attempts == 1 and io.writes == writes and p.append_calls == 0
    # Successful reconciliation GET or fresh adapter does not grant a new CAS.
    io.fault = None
    a = ReceiptPrivateAdapter(io, file_id=c.private.fid, folder_id=c.private.folder,
        owner_email=c.private.owner, service_email=c.private.service, catalog_digest=c.private.catalog_digest)
    a.read_versioned()
    opts["journal"] = ValidationJournal(a, preflight=lambda: None)
    with pytest.raises(StateError, match="no_retry"):
        finalize_in_progress_request(RID, **opts)
    assert io.attempts == 1 and io.writes == writes and p.append_calls == 0


@pytest.mark.parametrize("fault", ["before_append", "lost_append_response"])
def test_cache_fault_after_final_does_not_repeat_cas_or_append(fault, tmp_path, monkeypatch):
    c, w, p, io = seed()
    opts = options(c, tmp_path)
    if fault == "lost_append_response":
        p.lose_response = True
    else:
        def fail(entry):
            p.append_calls += 1
            raise TimeoutError("synthetic cache fault")
        monkeypatch.setattr(p, "append_result", fail)
    out = finalize_in_progress_request(RID, **opts)
    assert out["finalization_confirmed"] and out["display_pending"]
    assert out["record"]["state"] == "検証済み・未記帳"
    assert json.loads(io.raw)["journal"]["generation"] == 4
    assert io.attempts == io.writes == p.append_calls == 1
    with pytest.raises(StateError, match="no_retry"):
        finalize_in_progress_request(RID, **opts)
    assert io.attempts == io.writes == p.append_calls == 1
