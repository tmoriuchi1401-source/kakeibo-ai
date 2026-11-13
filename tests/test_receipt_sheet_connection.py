"""One synthetic ordinary-cell receipt through the actual composition.

Ports replace Google IO; the decoder, pinned private adapter, reader, journal,
validator and result projection are real. No real owner confirmation is claimed.
"""
from copy import deepcopy
import json
import socket
import subprocess
from pathlib import Path

import pytest

from app.drive_run_state import StateError
from app.receipt_sheet_connection import (CACHE_RANGE, ReceiptSheetConnection,
                                         GoogleReceiptProjection, cache_row)
from app.receipt_sheet_validation import UNKNOWN
from test_receipt_private_adapter import fixture, adapter
from test_receipt_sheet_validation import RID


@pytest.fixture(autouse=True)
def deny_external_and_financial_io(monkeypatch):
    calls = []
    def deny(*args, **kwargs):
        calls.append(True)
        raise AssertionError("unexpected live or financial IO")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    from app.sheets import SheetsDB
    monkeypatch.setattr(SheetsDB, "__init__", deny)
    yield
    assert calls == []


class Projection:
    def __init__(self, row):
        self.spreadsheet_id = "synthetic-sheet"
        self.rows, self.results = [deepcopy(row)], []
        self.append_calls, self.lose_response = 0, False

    def read_rows(self): return deepcopy(self.rows)
    def read_results(self): return deepcopy(self.results)

    def append_result(self, entry):
        self.append_calls += 1
        self.results.append(deepcopy(entry))
        if self.lose_response:
            raise TimeoutError("synthetic committed append response lost")


def connected():
    w, row, catalog, io = fixture()
    private, projection = adapter(io, catalog), Projection(row)
    def metadata(fid):
        value = deepcopy(w.metadata[fid])
        if fid == "synthetic-private":
            value["version"] = str(io.generation + 1)
        return value
    connection = ReceiptSheetConnection(private=private, projection=projection,
        metadata=metadata, source_bytes=lambda fid: w.raw,
        spreadsheet_id="synthetic-sheet", private_id="synthetic-private",
        source_folder="synthetic-folder", owner_email="owner@synthetic.invalid",
        service_email="service@synthetic.invalid")
    return connection, w, projection, io


def accept(c, w):
    ticket = c.prepare(w.case_id, RID)
    c.journal.accept_prepared(RID, ticket["snapshot"]["digest"])
    return ticket


def test_cells_js_prepare_accept_worker_and_same_case_result():
    c, w, p, io = connected()
    before = deepcopy(p.rows)
    # Use #200's actual Apps Script preparer; its output is the Python journal
    # input, not an independently hand-built expected request.
    _, tag = c.journal.load(writable=True)
    candidate = c.sheet_values()["cases"][0]
    script = Path("apps-script/receipt-validation/Code.test.cjs").resolve()
    process = subprocess.run(["node", str(script), "--prepare"],
        input=json.dumps(dict(candidate=candidate, tag=tag)), text=True,
        encoding="utf-8", capture_output=True, check=True)
    captured = json.loads(process.stdout)
    assert captured["error"] is None
    request = captured["request"]
    assert request["snapshot"] == c.reader(w.case_id)["request_snapshot"]
    c.journal.prepare(request, **captured["options"])
    c.journal.accept_prepared(RID, request["snapshot"]["digest"])
    output = c.run(RID)
    assert output["record"]["state"] == "検証済み・未記帳"
    assert output["display_pending"] is False
    assert output["record"]["result"]["posting_authority"] is False
    assert output["record"]["result"]["financial_write_count"] == 0
    writes = io.writes
    logical = deepcopy(w.sheet["cases"])
    logical[0]["draft"] = candidate["draft"]
    logical[0]["draft_snapshot_digest"] = request["snapshot"]["digest"]
    logical[0]["submission"] = dict(request_id=RID, snapshot_digest=request["snapshot"]["digest"])
    displayed = c.result(RID, logical)
    assert displayed[0]["result"]["state"] == "検証済み・未記帳"
    assert displayed[0]["note"] == logical[0]["note"]
    assert displayed[0]["draft"] == logical[0]["draft"]
    assert displayed[0]["original_link"] == logical[0]["original_link"]
    assert p.rows == before and io.writes == writes == 4
    with pytest.raises(StateError, match="replay_rejected"):
        c.run(RID)
    assert io.writes == writes and p.append_calls == 1


def test_lost_cache_response_can_be_queried_without_rerun_or_write():
    c, w, p, io = connected()
    accept(c, w)
    p.lose_response = True
    output = c.run(RID)
    assert output["record"]["state"] == "検証済み・未記帳"
    assert output["display_pending"] is True
    writes = io.writes
    assert c.result(RID, w.sheet["cases"])[0]["result"]["state"] == "検証済み・未記帳"
    assert io.writes == writes and p.append_calls == 1


def test_conflicting_cache_never_displays_success_or_reexecutes_validation():
    c, w, p, io = connected()
    accept(c, w)
    c.run(RID)
    p.results.append(dict(deepcopy(p.results[0]), state="保留"))
    writes = io.writes
    with pytest.raises(StateError, match="display_pending"):
        c.result(RID, w.sheet["cases"])
    assert io.writes == writes and p.append_calls == 1


@pytest.mark.parametrize("damage", ["source", "duplicate", "edit"])
def test_changed_source_duplicate_case_or_later_edit_holds_without_posting(damage):
    c, w, p, io = connected()
    accept(c, w)
    if damage == "source":
        w.raw = b"changed original"
    elif damage == "duplicate":
        p.rows.append(deepcopy(p.rows[0]))
    else:
        p.rows[0][20] = 869
        p.rows[0][4:6] = [1369, -1]
    output = c.run(RID)
    assert output["record"]["state"] == "保留"
    assert output["record"]["result"]["financial_write_count"] == 0
    assert io.writes == 4


def test_lost_claim_response_stops_without_result_append_or_retry():
    c, w, p, io = connected()
    accept(c, w)
    io.fault = "after"
    output = c.run(RID)
    assert output["record"]["state"] == UNKNOWN
    assert p.append_calls == 0 and io.attempts == 3
    io.fault = None
    assert c.journal.query(RID)["state"] == "検証中"
    with pytest.raises(StateError, match="replay_rejected"):
        c.run(RID)
    assert io.attempts == 3


def test_google_projection_reads_native_cells_and_writes_only_fixed_raw_cache():
    class Values:
        def __init__(self): self.calls = []; self.reply = {}
        def spreadsheets(self): return self
        def values(self): return self
        def get(self, **kwargs): self.calls.append(("GET", kwargs)); return self
        def append(self, **kwargs): self.calls.append(("APPEND", kwargs)); return self
        def execute(self, **kwargs):
            assert kwargs == dict(num_retries=0)
            return deepcopy(self.reply)
    api = Values()
    port = GoogleReceiptProjection(api, spreadsheet_id="synthetic-sheet",
                                  cache_range=CACHE_RANGE)
    api.reply = {"values": [["case-id", "一般"]]}
    assert len(port.read_rows()[0]) == 46
    assert api.calls[-1][1]["valueRenderOption"] == "UNFORMATTED_VALUE"
    c, w, _, _ = connected()
    accept(c, w)
    entry = c.run(RID)["record"]
    from app.receipt_sheet_validation import result_cache_entry
    entry = result_cache_entry(entry)
    port.append_result(entry)
    method, options = api.calls[-1]
    assert method == "APPEND" and options["range"] == CACHE_RANGE
    assert options["valueInputOption"] == "RAW"
    assert options["body"]["values"] == [cache_row(entry)]
    api.reply = {"values": options["body"]["values"]}
    assert port.read_results() == [entry]


def test_mismatched_projection_cannot_redirect_reads_or_results_to_another_book():
    c, w, p, io = connected()
    p.spreadsheet_id = "another-synthetic-sheet"
    with pytest.raises(StateError, match="resource_binding_mismatch"):
        ReceiptSheetConnection(private=c.private, projection=p,
            metadata=c.reader.metadata, source_bytes=c.reader.source_bytes,
            spreadsheet_id="synthetic-sheet", private_id="synthetic-private",
            source_folder="synthetic-folder", owner_email="owner@synthetic.invalid",
            service_email="service@synthetic.invalid")
    assert io.writes == 0


def test_live_cache_write_uses_existing_no_replay_session_without_sdk_write():
    class Session:
        def __init__(self): self.calls = []
        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return type("Reply", (), {"status_code": 503})()
    session = Session()
    port = GoogleReceiptProjection(None, spreadsheet_id="synthetic-sheet",
        cache_range=CACHE_RANGE, write_http=session)
    c, w, _, _ = connected()
    accept(c, w)
    from app.receipt_sheet_validation import result_cache_entry
    entry = result_cache_entry(c.run(RID)["record"])
    with pytest.raises(StateError, match="cache_write_unknown"):
        port.append_result(entry)
    assert len(session.calls) == 1
    url, options = session.calls[0]
    assert url.startswith("https://sheets.googleapis.com/v4/spreadsheets/synthetic-sheet/values/")
    assert options["allow_redirects"] is False
    assert options["json"] == {"values": [cache_row(entry)]}
