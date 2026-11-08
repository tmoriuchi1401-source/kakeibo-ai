"""Synthetic bytes + atomic mock transport only; no real UUIDs or Google IO."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import socket
from threading import Barrier, RLock

import pytest

from app.drive_run_state import StateError
from app.page_receipt_model import digest
from app.receipt_private_adapter import ReceiptPrivateAdapter, SCHEMA, MAX_BYTES
from app.receipt_sheet_cells import operations_from_cells
from app.receipt_sheet_decoder import decode_receipt_row
from app.receipt_sheet_validation import (JOURNAL_SCHEMA, PURPOSE, PREPARED, UNKNOWN,
    ValidationJournal, empty_journal, execute_accepted_request)
from test_receipt_sheet_decoder import corrected
from test_receipt_sheet_validation import RID, RID2


@pytest.fixture(autouse=True)
def no_live_or_financial_io(monkeypatch):
    calls = []
    def denied(*args, **kwargs):
        calls.append(True)
        raise AssertionError("external IO forbidden")
    monkeypatch.setattr(socket, "create_connection", denied)
    from app.sheets import SheetsDB
    monkeypatch.setattr(SheetsDB, "__init__", denied)
    yield
    assert calls == []


def encode(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


class BytesIO:
    """Same bytes port as DriveRegistryIO; atomic server If-Match stand-in."""
    def __init__(self, value):
        self.config = dict(file_id="synthetic-private", folder_id="synthetic-folder")
        self.raw, self.generation = encode(value), 0
        self.lock, self.attempts, self.writes, self.reads = RLock(), 0, 0, 0
        self.fault, self.barrier, self.after_read, self.meta_change = None, None, None, None
        self.trace = []
        self.meta = {fid: dict(id=fid, mimeType=mime, version="1", etag='"0"',
            parents=[{"id": "synthetic-folder"}], labels={"trashed": False},
            owners=[dict(emailAddress="owner@synthetic.invalid")],
            permissions=[dict(type="user", role="owner", emailAddress="owner@synthetic.invalid"),
                         dict(type="user", role="writer", emailAddress="service@synthetic.invalid")])
            for fid, mime in [("synthetic-folder", "application/vnd.google-apps.folder"),
                              ("synthetic-private", "application/json")]}

    def metadata(self, fid, fields):
        with self.lock:
            self.trace.append("metadata")
            if self.fault == "metadata":
                raise TimeoutError("must not leak private API detail")
            value = deepcopy(self.meta[fid])
            if fid == "synthetic-private":
                value["etag"], value["version"] = f'"{self.generation}"', str(self.generation + 1)
            if self.meta_change:
                self.meta_change(fid, value)
            return value

    def read_versioned(self):
        with self.lock:
            self.reads += 1
            self.trace.append("GET")
            if self.fault == "read" or self.writes and self.fault == "readback":
                raise TimeoutError("must not leak private bytes")
            raw, tag = self.raw, f'"{self.generation}"'
            if self.after_read:
                self.after_read(self)
            return raw, tag

    def replace_versioned(self, before, tag, after):
        if self.barrier:
            self.barrier.wait(timeout=5)
        with self.lock:
            self.attempts += 1
            self.trace.append("PUT If-Match")
            if self.fault == "before":
                raise TimeoutError("unknown before save")
            if before != self.raw or tag != f'"{self.generation}"':
                raise StateError("HTTP_412")
            self.raw, self.generation = after, self.generation + 1
            self.writes += 1
            if self.fault == "after":
                raise TimeoutError("unknown after save")
            if self.fault == "wrong_readback":
                value = json.loads(self.raw)
                value["journal"]["generation"] += 1
                self.raw = encode(value)
            if self.fault == "unchanged_etag":
                self.generation -= 1


def fixture():
    world, row, expected = corrected()
    row[1] = "一般（完全合成）"
    entry = dict(anchor=deepcopy(world.anchor), import_row=deepcopy(world.tx.row),
        candidate_rows=[deepcopy(r) for _, r in world.sheet["candidates"]],
        categories=deepcopy(world.sheet["categories"]), rules=[],
        decoder=dict(machine=expected["machine"], kind_label=row[1]))
    catalog = json.loads(encode({"cases": {world.case_id: entry}}))
    io = BytesIO(dict(schema=SCHEMA, version=1, catalog=catalog, journal=empty_journal()))
    return world, row, catalog, io


def adapter(io, catalog):
    return ReceiptPrivateAdapter(io, file_id="synthetic-private", folder_id="synthetic-folder",
        owner_email="owner@synthetic.invalid", service_email="service@synthetic.invalid", catalog_digest=digest(catalog))


def journal(a):
    return ValidationJournal(a, preflight=lambda: None)


def capture(a, row, rid=RID):
    _, tag = a.read_versioned()  # ETag BEFORE independent input decoding.
    c = decode_receipt_row(row, expected=a.decoder_expected(row[0]))
    snapshot = dict(case_id=c["case_id"], source=c["source"],
        operations=operations_from_cells(c["draft"], c["original_items"], c["categories"]),
        context_digest=c["context_digest"])
    snapshot["digest"] = digest(snapshot)
    return dict(request_id=rid, purpose=PURPOSE, snapshot=snapshot), tag


def test_prepare_accept_worker_reuses_existing_reader_and_final_cache_contract():
    w, row, catalog, io = fixture()
    a, before = adapter(io, catalog), deepcopy(catalog)
    j = journal(a)
    request, tag = capture(a, row)
    j.prepare(request, expected_tag=tag)
    assert io.writes == io.attempts == 1
    assert a.private_cases()[1] == j.load()[1] == '"1"'
    assert a.private_cases()[0] == w.catalog
    assert a.decoder_expected(w.case_id)["context_digest"] == digest(w.anchor)
    # Returned catalog/expected values cannot mutate the pinned state.
    returned, _ = a.read_catalog()
    returned["cases"].clear()
    assert a.read_catalog()[0] == before
    c = decode_receipt_row(row, expected=a.decoder_expected(w.case_id))
    w.row["draft"] = c["draft"]
    w.reader.private_cases = a.private_cases
    j.accept_prepared(RID, request["snapshot"]["digest"])
    result = execute_accepted_request(RID, reader=w.reader, journal=j)
    assert result["state"] == "検証済み・未記帳"
    assert result["result"]["financial_write_count"] == 0
    assert result["result"]["posting_authority"] is False
    assert json.loads(io.raw)["catalog"] == catalog == before
    assert io.writes == io.attempts == 4  # prepare, accept, claim, FINAL
    assert io.trace.count("GET") > io.writes
    from app.receipt_sheet_validation import result_cache_entry
    assert result_cache_entry(result)["state"] == result["state"]


def test_stale_expected_etag_and_stale_cached_bytes_never_merge():
    _, row, catalog, io = fixture()
    a = adapter(io, catalog)
    request, tag = capture(a, row)
    io.generation += 1
    with pytest.raises(StateError, match="preparation_stale"):
        journal(a).prepare(request, expected_tag=tag)
    assert io.attempts == 0
    # Cache stale at the write boundary, not just at prepare's load.
    value, tag = a.read_versioned()
    replay = deepcopy(value)
    replay["requests"][RID] = dict(request, state=PREPARED, result=None)
    replay["active_prepared"][row[0]] = RID
    replay["generation"] += 1
    io.generation += 1
    with pytest.raises(StateError, match="write_unknown"):
        a.replace_versioned(replay, tag)
    assert io.attempts == io.writes == 0
    a.read_versioned()
    with pytest.raises(StateError, match="write_stopped"):
        a.replace_versioned(replay, tag)


@pytest.mark.parametrize("stage", ["prepare", "accept", "claim"])
def test_atomic_if_match_competing_updates_have_at_most_one_winner(stage):
    _, row, catalog, io = fixture()
    adapters = [adapter(io, catalog), adapter(io, catalog)]
    requests = [capture(a, row, rid) for a, rid in zip(adapters, [RID, RID2])]
    if stage != "prepare":
        journal(adapters[0]).prepare(requests[0][0], expected_tag=requests[0][1])
    if stage == "claim":
        journal(adapters[0]).accept_prepared(RID, requests[0][0]["snapshot"]["digest"])
    writes, attempts = io.writes, io.attempts
    io.barrier = Barrier(2)
    def prepare(args):
        a, (request, tag) = args
        try:
            if stage == "prepare":
                journal(a).prepare(request, expected_tag=tag)
            elif stage == "accept":
                journal(a).accept_prepared(RID, requests[0][0]["snapshot"]["digest"])
            else:
                journal(a).transition(RID, "検証中")
            return True
        except StateError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(prepare, zip(adapters, requests)))
    assert sum(results) == io.writes - writes == 1
    assert io.attempts - attempts == 2
    assert len(json.loads(io.raw)["journal"]["requests"]) == 1


@pytest.mark.parametrize("fault,writes", [("before", 0), ("after", 1), ("readback", 1),
                                        ("wrong_readback", 1), ("unchanged_etag", 1)])
def test_unknown_permanently_get_only_even_after_successful_reconciliation(fault, writes):
    _, row, catalog, io = fixture()
    a = adapter(io, catalog)
    request, tag = capture(a, row)
    io.fault = fault
    with pytest.raises(StateError, match="write_unknown"):
        journal(a).prepare(request, expected_tag=tag)
    assert io.attempts == 1 and io.writes == writes
    io.fault = None
    a.read_versioned()  # Allowed reconciliation GET, never a repair.
    a.private_cases()
    with pytest.raises(StateError):
        journal(a).prepare(dict(request, request_id=RID2), expected_tag=a.read_versioned()[1])
    assert io.attempts == 1 and io.writes == writes


@pytest.mark.parametrize("fault", ["read", "metadata"])
def test_pre_save_read_unknown_also_stops_writes_and_sanitizes_errors(fault):
    _, row, catalog, io = fixture()
    a = adapter(io, catalog)
    io.fault = fault
    with pytest.raises(StateError) as exc:
        a.read_versioned()
    assert str(exc.value) == "receipt_private_read_unknown" and exc.value.__suppress_context__
    io.fault = None
    request, tag = capture(a, row)
    with pytest.raises(StateError, match="write_stopped"):
        journal(a).prepare(request, expected_tag=tag)
    assert io.attempts == 0


@pytest.mark.parametrize("raw", [b"", b"{", b"[]", b"null", b"\xff", b"\xef\xbb\xbf{}",
    b'{"schema":1,"schema":2}', b'{"x":{"a":1,"a":2}}', b'{"x":NaN}', b'{"x":Infinity}',
    b'{"x":1e999}', b'{"x":"\\ud800"}', b" "*(MAX_BYTES+1)], ids=[f"json-{i}" for i in range(13)])
def test_strict_json_rejects_without_write(raw):
    _, _, catalog, io = fixture()
    io.raw = raw
    with pytest.raises(StateError, match="read_unknown"):
        adapter(io, catalog).read_versioned()
    assert io.attempts == 0


@pytest.mark.parametrize("change", ["missing_catalog", "changed_catalog", "unconfigured", "outer_schema",
    "version_bool", "extra", "legacy_journal", "missing_active", "source_mismatch"])
def test_envelope_catalog_and_legacy_schemas_cannot_be_initialized_or_migrated(change):
    _, row, catalog, io = fixture()
    value = json.loads(io.raw)
    if change == "missing_catalog": value.pop("catalog")
    elif change == "changed_catalog": value["catalog"]["cases"][row[0]]["decoder"]["kind_label"] = "changed"
    elif change == "unconfigured": value = dict(enabled=False, status="unconfigured")
    elif change == "outer_schema": value["schema"] = "receipt-private-envelope-v0"
    elif change == "version_bool": value["version"] = True
    elif change == "extra": value["extra"] = True
    elif change == "legacy_journal": value["journal"] = dict(schema=PURPOSE, generation=0, requests={})
    elif change == "missing_active": value["journal"].pop("active_prepared")
    else:
        a = adapter(io, catalog)
        request, tag = capture(a, row)
        request["snapshot"]["source"]["version"] = "8"
        s = request["snapshot"]
        s["digest"] = digest({k: v for k, v in s.items() if k != "digest"})
        with pytest.raises(StateError, match="update_invalid"):
            journal(a).prepare(request, expected_tag=tag)
        assert io.attempts == 0
        return
    io.raw = encode(value)
    before = io.raw
    with pytest.raises(StateError): adapter(io, catalog).read_versioned()
    assert io.raw == before and io.attempts == 0


@pytest.mark.parametrize("field", ["anchor", "import_row", "candidate_rows", "categories", "rules", "decoder"])
def test_missing_trusted_data_rejected_even_if_catalog_digest_is_pinned(field):
    _, row, catalog, io = fixture()
    catalog["cases"][row[0]].pop(field)
    value = json.loads(io.raw)
    value["catalog"] = catalog
    io.raw = encode(value)
    with pytest.raises(StateError): adapter(io, catalog).read_versioned()
    assert io.attempts == 0


@pytest.mark.parametrize("change", ["confirmation", "candidate", "import", "policy", "machine", "four_items"])
def test_catalog_internal_bindings_and_capacity_are_independently_checked(change):
    _, row, catalog, io = fixture()
    entry = catalog["cases"][row[0]]
    if change == "confirmation": entry["anchor"]["total_confirmation"] = None
    elif change == "candidate": entry["candidate_rows"][0][6] = 999
    elif change == "import": entry["import_row"][3] = "wrong-source"
    elif change == "policy": entry["categories"].append(["new", "unbound"])
    elif change == "machine": entry["decoder"]["machine"][4] = "wrong-item"
    else:
        entry["candidate_rows"] *= 2  # ALL four records must be rejected, not sliced.
        entry["anchor"]["candidate_digest"] = digest(entry["candidate_rows"])
    value = json.loads(io.raw)
    value["catalog"] = catalog
    io.raw = encode(value)
    with pytest.raises(StateError): adapter(io, catalog).read_versioned()
    assert io.attempts == 0


def test_730_to_868_without_confirmation_stops_before_preparation():
    _, row, catalog, io = fixture()
    row[26] = False
    a = adapter(io, catalog)
    with pytest.raises(ValueError, match="receipt_cells_original_check_required"):
        capture(a, row)
    assert io.attempts == io.writes == 0
    assert a.read_versioned()[0] == empty_journal()


@pytest.mark.parametrize("change", ["parent", "owner", "acl", "trashed", "mime", "weak", "missing_trash"])
def test_file_and_parent_metadata_fail_closed(change):
    _, _, catalog, io = fixture()
    def mutate(fid, value):
        if change == "parent": value["parents"] = []
        elif change == "owner": value["owners"] = []
        elif change == "acl": value["permissions"].append(dict(type="anyone", role="reader"))
        elif change == "trashed": value["labels"]["trashed"] = True
        elif change == "mime": value["mimeType"] = "text/plain"
        elif change == "weak": value["etag"] = 'W/"0"'
        else: value.pop("labels")
    io.meta_change = mutate
    with pytest.raises(StateError): adapter(io, catalog).read_versioned()
    assert io.attempts == 0


def test_metadata_media_metadata_race_is_rejected():
    _, _, catalog, io = fixture()
    io.after_read = lambda transport: setattr(transport, "generation", transport.generation+1)
    with pytest.raises(StateError): adapter(io, catalog).read_versioned()
    assert io.attempts == 0


@pytest.mark.parametrize("fid", ["synthetic-private", "synthetic-folder"])
def test_parent_and_file_acl_are_checked_separately(fid):
    _, _, catalog, io = fixture()
    io.meta[fid]["permissions"] = [dict(type="user", role="owner", emailAddress="owner@synthetic.invalid")]
    with pytest.raises(StateError): adapter(io, catalog).read_versioned()
    assert io.attempts == 0


def test_transport_file_binding_mismatch_cannot_read_or_write_another_resource():
    _, _, catalog, io = fixture()
    io.config["file_id"] = "another-synthetic-file"
    with pytest.raises(StateError): adapter(io, catalog).read_versioned()
    assert io.attempts == io.reads == 0


def test_catalog_change_after_cache_cannot_be_merged_or_saved():
    _, row, catalog, io = fixture()
    a = adapter(io, catalog)
    request, tag = capture(a, row)
    value = json.loads(io.raw)
    value["catalog"]["cases"][row[0]]["decoder"]["kind_label"] = "edited"
    io.raw, io.generation = encode(value), io.generation + 1
    with pytest.raises(StateError): journal(a).prepare(request, expected_tag=tag)
    assert io.attempts == 0


def test_concurrent_change_during_cores_extra_readback_latches_get_only():
    _, row, catalog, io = fixture()
    a = adapter(io, catalog)
    request, tag = capture(a, row)
    read, changed = a._read, []
    def read_then_change():
        result = read()
        if io.writes and not changed:
            # Adapter read-back completes, then core save/load sees a newer tag.
            io.generation += 1
            changed.append(True)
        return result
    a._read = read_then_change
    with pytest.raises(StateError): journal(a).prepare(request, expected_tag=tag)
    assert io.attempts == io.writes == 1
    a.read_versioned()
    with pytest.raises(StateError, match="write_stopped"):
        journal(a).prepare(dict(request, request_id=RID2), expected_tag=a.read_versioned()[1])
    assert io.attempts == 1


@pytest.mark.parametrize("change", ["rollback", "generation", "snapshot", "delete", "active", "direct_accept", "terminal"])
def test_adapter_rejects_mutations_outside_one_existing_core_transition(change):
    _, row, catalog, io = fixture()
    a = adapter(io, catalog)
    j = journal(a)
    request, tag = capture(a, row)
    j.prepare(request, expected_tag=tag)
    if change == "terminal":
        j.accept_prepared(RID, request["snapshot"]["digest"])
        j.transition(RID, UNKNOWN)
    before, tag = a.read_versioned()
    after = deepcopy(before)
    after["generation"] += 1
    if change == "rollback": after = empty_journal()
    elif change == "generation": after["generation"] += 1
    elif change == "snapshot":
        after["requests"][RID]["snapshot"]["operations"] = []
        s = after["requests"][RID]["snapshot"]
        s["digest"] = digest({k: v for k, v in s.items() if k != "digest"})
    elif change == "delete": after["requests"].clear(); after["active_prepared"].clear()
    elif change == "active": after["active_prepared"].clear()
    elif change == "direct_accept":
        after["requests"][RID2] = dict(request, request_id=RID2, state="受付済み", result=None)
    else: after["requests"][RID]["state"] = "検証中"
    count = io.attempts
    with pytest.raises(StateError): a.replace_versioned(after, tag)
    assert io.attempts == count and a.read_versioned()[0] == before
