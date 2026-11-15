"""Existing fixture, actual worker composition, A-F single-attempt faults.

No live IDs, credentials, new case resources, or Google/writer IO. Deliberate
failures are evidence of distinct possible stops, not diagnoses of past runs.
"""
from copy import deepcopy
import json
import logging
import socket

import pytest

from app import receipt_sheet_validation as validation
from app.receipt_sheet_worker import run
from test_receipt_sheet_connection import connected, accept
from test_receipt_sheet_worker import binding, environment
from test_receipt_sheet_worker import receive
from test_receipt_sheet_validation import RID


PRIVATE = "secret/input/original/exception-must-never-be-logged"
PRIVATE_CODES = {
    "receipt_private_ack_readback_begin", "receipt_private_ack_readback_ok",
    "receipt_private_ack_readback_failed", "receipt_private_prewrite_read_begin",
    "receipt_private_prewrite_read_ok", "receipt_private_prewrite_read_failed",
    "receipt_private_cas_begin", "receipt_private_cas_ok", "receipt_private_cas_failed",
    "receipt_private_adapter_readback_begin", "receipt_private_adapter_readback_ok",
    "receipt_private_adapter_readback_failed",
    "receipt_private_read_body_failed",
}
WORKER_CODES = {
    "receipt_worker_claim_begin", "receipt_worker_claim_verified", "receipt_worker_claim_unknown",
    "receipt_worker_source_check_begin", "receipt_worker_source_check_ok", "receipt_worker_source_check_failed",
    "receipt_worker_registration_plan_begin", "receipt_worker_registration_plan_ok", "receipt_worker_registration_plan_failed",
    "receipt_worker_source_recheck_begin", "receipt_worker_source_recheck_ok", "receipt_worker_source_recheck_failed",
    "receipt_worker_terminal_begin", "receipt_worker_terminal_verified", "receipt_worker_terminal_unknown",
    "receipt_worker_terminal_query_ok",
}


@pytest.fixture(autouse=True)
def no_live_or_financial_io(monkeypatch):
    calls = []
    def deny(*args, **kwargs):
        calls.append(True)
        raise AssertionError("unexpected live or financial IO")
    for method in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, method, deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    from app.sheets import SheetsDB
    monkeypatch.setattr(SheetsDB, "__init__", deny)
    yield
    assert calls == []


def observed_worker(monkeypatch, fault):
    c, w, p, io = connected()
    config = binding(c, w)
    accept(c, w)
    trace = []
    pending, core_done = [], set()
    replace, read = io.replace_versioned, io.read_versioned
    reader, plan = c.reader, validation.registration_plan
    reader_calls = []

    def put(before, tag, after):
        state = json.loads(after)["journal"]["requests"][RID]["state"]
        phase = "A" if state == "検証中" else "E"
        trace.append(phase + "_CAS")
        if fault == "claim_before" and phase == "A" or fault == "terminal_before" and phase == "E":
            io.fault = "before"
        elif fault == "claim_response" and phase == "A" or fault == "terminal_response" and phase == "E":
            io.fault = "after"
        replace(before, tag, after)
        pending.append("B" if phase == "A" else "F")

    def get():
        if pending:
            phase = pending.pop()
            trace.append(phase + "_ADAPTER_READBACK")
            if fault == "claim_adapter_readback" and phase == "B" or fault == "terminal_adapter_readback" and phase == "F":
                raise TimeoutError(PRIVATE)
        elif c.private._ack is not None:
            phase = "B" if io.generation == 3 else "F"
            trace.append(phase + "_CORE_READBACK")
            if fault == "claim_core_readback" and phase == "B" or fault == "terminal_core_readback" and phase == "F":
                raise TimeoutError(PRIVATE)
            core_done.add(phase)
        elif "F" in core_done:
            core_done.remove("F")
            trace.append("F_QUERY")
            if fault == "terminal_query":
                raise TimeoutError(PRIVATE)
        return read()

    def revalidate(case_id):
        trace.append("C_REVALIDATE")
        reader_calls.append(True)
        if fault == "source" or fault == "source_recheck" and len(reader_calls) == 2:
            raise ValueError(PRIVATE)
        if fault == "source_private_read":
            # A private-read fault must remain write-stopped even when a later
            # GET succeeds. Do not 'fix' it by resetting/reconstructing adapter.
            io.fault = "read"
            try:
                c.private.read_catalog()
            finally:
                io.fault = None
        return reader(case_id)

    def registration_plan(*args):
        trace.append("D_PLAN")
        if fault == "plan":
            raise ValueError(PRIVATE)
        return plan(*args)

    monkeypatch.setattr(io, "replace_versioned", put)
    monkeypatch.setattr(io, "read_versioned", get)
    monkeypatch.setattr(c, "reader", revalidate)
    monkeypatch.setattr(validation, "registration_plan", registration_plan)
    return c, w, p, io, config, trace


@pytest.mark.parametrize("fault,status,generation,state,attempts,appends,code", [
    (None, 0, 4, "検証済み・未記帳", 4, 1, "receipt_worker_terminal_query_ok"),
    ("claim_before", 3, 2, "受付済み", 3, 0, "receipt_private_cas_failed"),
    ("claim_response", 3, 3, "検証中", 3, 0, "receipt_private_cas_failed"),
    ("claim_adapter_readback", 3, 3, "検証中", 3, 0, "receipt_private_adapter_readback_failed"),
    ("claim_core_readback", 3, 3, "検証中", 3, 0, "receipt_private_ack_readback_failed"),
    ("source", 2, 4, "保留", 4, 1, "receipt_worker_source_check_failed"),
    ("source_private_read", 3, 3, "検証中", 3, 0, "receipt_worker_terminal_unknown"),
    ("plan", 2, 4, "保留", 4, 1, "receipt_worker_registration_plan_failed"),
    ("source_recheck", 2, 4, "保留", 4, 1, "receipt_worker_source_recheck_failed"),
    ("terminal_before", 3, 3, "検証中", 4, 0, "receipt_private_cas_failed"),
    ("terminal_response", 3, 4, "検証済み・未記帳", 4, 0, "receipt_private_cas_failed"),
    ("terminal_adapter_readback", 3, 4, "検証済み・未記帳", 4, 0, "receipt_private_adapter_readback_failed"),
    ("terminal_core_readback", 3, 4, "検証済み・未記帳", 4, 0, "receipt_private_ack_readback_failed"),
    ("terminal_query", 3, 4, "検証済み・未記帳", 4, 0, "receipt_worker_terminal_unknown"),
])
def test_actual_worker_stage_order_and_failure_persistence(monkeypatch, caplog, fault, status, generation, state, attempts, appends, code):
    c, w, p, io, config, trace = observed_worker(monkeypatch, fault)
    rows_before = deepcopy(p.rows)
    caplog.clear()
    caplog.set_level(logging.WARNING)
    result_status, _ = run(environment(config), connect=lambda *_: c)
    durable = json.loads(io.raw)["journal"]
    record = durable["requests"][RID]
    assert result_status == status
    assert durable["generation"] == generation
    assert record["state"] == state
    assert io.attempts == attempts and p.append_calls == appends
    assert len(durable["requests"]) == 1 and p.rows == rows_before
    if state in {"受付済み", "検証中"}:
        assert record["result"] is None
    else:
        assert record["result"]["financial_write_count"] == 0
        assert record["result"]["posting_authority"] is False
    messages = [r.getMessage() for r in caplog.records]
    assert code in messages
    assert set(messages) <= PRIVATE_CODES | WORKER_CODES
    assert all(PRIVATE not in message and RID not in message for message in messages)
    assert trace.count("A_CAS") == 1 and trace.count("E_CAS") <= 1
    if fault in {"claim_before", "claim_response", "claim_adapter_readback", "claim_core_readback"}:
        assert "C_REVALIDATE" not in trace and "E_CAS" not in trace
        assert "receipt_worker_claim_unknown" in messages
    if fault is None:
        assert trace == ["A_CAS", "B_ADAPTER_READBACK", "B_CORE_READBACK", "C_REVALIDATE", "D_PLAN", "C_REVALIDATE", "E_CAS", "F_ADAPTER_READBACK", "F_CORE_READBACK", "F_QUERY"]
        private_save = [
            "receipt_private_prewrite_read_begin", "receipt_private_prewrite_read_ok",
            "receipt_private_cas_begin", "receipt_private_cas_ok",
            "receipt_private_adapter_readback_begin", "receipt_private_adapter_readback_ok",
            "receipt_private_ack_readback_begin", "receipt_private_ack_readback_ok",
        ]
        assert messages == [
            "receipt_worker_claim_begin", *private_save, "receipt_worker_claim_verified",
            "receipt_worker_source_check_begin", "receipt_worker_source_check_ok",
            "receipt_worker_registration_plan_begin", "receipt_worker_registration_plan_ok",
            "receipt_worker_source_recheck_begin", "receipt_worker_source_recheck_ok",
            "receipt_worker_terminal_begin", *private_save,
            "receipt_worker_terminal_verified", "receipt_worker_terminal_query_ok",
        ]
    if fault == "terminal_before":
        assert "receipt_worker_claim_verified" in messages
        assert "D_PLAN" in trace and "E_CAS" in trace
        assert "receipt_worker_terminal_unknown" in messages
    if fault == "source_private_read":
        assert c.private._stopped and "E_CAS" not in trace


@pytest.mark.parametrize("fault,status,generation,writes,appends", [
    (None, 0, 4, 2, 1),
    ("claim_response", 3, 3, 1, 0),
])
def test_receiver_to_actual_google_composition_uses_only_fake_http(monkeypatch, fault, status, generation, writes, appends):
    """Exercise the real connection factory and inherited DriveRegistryIO too.

    Previous worker tests inject an already composed connection. Here only
    credentials/Google clients/HTTP responses are fake; all receipt code runs.
    """
    from types import SimpleNamespace
    from urllib.parse import unquote
    from google.auth.transport import requests
    from google.oauth2 import service_account
    from googleapiclient import discovery
    import google_auth_httplib2
    import httplib2
    from app import receipt_sheet_worker as worker
    from test_receipt_private_adapter import encode

    c, w, p, io = connected()
    config, received = receive(c, w, p, io)
    assert received["errors"] == [] and len(received["dispatched"]) == 1
    io.raw = received["raw_envelope"].encode()
    io.generation = received["generation"]
    p.rows = received["rows"]
    before_rows = deepcopy(p.rows)
    calls = []

    class Session:
        def request(self, method, url, **kwargs):
            calls.append(method)
            assert kwargs["allow_redirects"] is False
            if method == "GET":
                assert url.startswith("https://www.googleapis.com/drive/v2/files/")
                fid = url.rsplit("/", 1)[1]
                if kwargs["params"].get("alt") == "media":
                    assert fid == "synthetic-private"
                    raw, _ = io.read_versioned()
                    return SimpleNamespace(status_code=200, content=raw)
                data = io.metadata(fid, kwargs["params"]["fields"])
                return SimpleNamespace(status_code=200, content=encode(data), json=lambda: data)
            if method == "PUT":
                assert url == "https://www.googleapis.com/upload/drive/v2/files/synthetic-private"
                assert kwargs["params"] == {"uploadType":"media", "fields":"id,etag"}
                assert kwargs["headers"]["If-Match"] == f'"{io.generation}"'
                if fault == "claim_response":
                    io.fault = "after"
                io.replace_versioned(io.raw, kwargs["headers"]["If-Match"], kwargs["data"])
                return SimpleNamespace(status_code=200, content=b"{}", json=lambda: {})
            assert method == "POST"
            assert unquote(url) == "https://sheets.googleapis.com/v4/spreadsheets/synthetic-sheet/values/'_receipt_validation_results'!A2:M1000:append"
            assert kwargs["params"] == {"valueInputOption":"RAW", "insertDataOption":"INSERT_ROWS"}
            assert len(kwargs["json"]["values"]) == 1
            p.results.extend(kwargs["json"]["values"])
            p.append_calls += 1
            return SimpleNamespace(status_code=200, content=b"{}")

        def post(self, url, **kwargs):
            return self.request("POST", url, **kwargs)

    class Execute:
        def __init__(self, data): self.data = data
        def execute(self, **kwargs):
            assert kwargs == {"num_retries":0}
            # Actual SDK JSON shapes, not Python tuple/category identity.
            return json.loads(json.dumps(self.data)) if not isinstance(self.data, bytes) else self.data

    class Drive:
        def files(self): return self
        def get(self, *, fileId, fields):
            data = deepcopy(w.metadata[fileId])
            if fileId == "synthetic-private": data["version"] = str(io.generation + 1)
            return Execute(data)
        def get_media(self, *, fileId):
            assert fileId == w.source["source_id"]
            return Execute(w.raw)

    class Sheets:
        def spreadsheets(self): return self
        def values(self): return self
        def get(self, *, spreadsheetId, range, valueRenderOption):
            assert spreadsheetId == "synthetic-sheet" and valueRenderOption == "UNFORMATTED_VALUE"
            assert range in {"'確認'!A2:AT1000", "'_receipt_validation_results'!A2:M1000"}
            return Execute({"values":p.rows if range.startswith("'確認'") else p.results})

    session = Session()
    monkeypatch.setattr(service_account.Credentials, "from_service_account_info", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(requests, "AuthorizedSession", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(google_auth_httplib2, "AuthorizedHttp", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(httplib2, "Http", lambda **_kwargs: object())
    monkeypatch.setattr(discovery, "build", lambda name, *_args, **_kwargs: Drive() if name == "drive" else Sheets())
    env = environment(config)
    env["GOOGLE_SERVICE_ACCOUNT_JSON"] = json.dumps({"client_email":config["service_email"]})
    result_status, _ = worker.run(env)
    journal = json.loads(io.raw)["journal"]
    assert result_status == status and journal["generation"] == generation
    assert io.attempts == io.writes == writes and p.append_calls == appends
    assert calls.count("PUT") == writes and calls.count("POST") == appends
    assert p.rows == before_rows
    if status == 0:
        assert journal["requests"][RID]["result"]["financial_write_count"] == 0
    else:
        assert journal["requests"][RID]["state"] == "検証中"
        assert journal["requests"][RID]["result"] is None
