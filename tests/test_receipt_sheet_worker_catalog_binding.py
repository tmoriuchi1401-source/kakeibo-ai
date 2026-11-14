"""Only the live composition's catalog guard; synthetic data and fake Google IO."""
from copy import deepcopy
import json
import socket
from types import SimpleNamespace

import pytest

from app.drive_run_state import StateError
from app.page_receipt_model import digest
from app import receipt_sheet_worker as worker
from test_receipt_private_adapter import encode
from test_receipt_sheet_connection import connected
from test_receipt_sheet_worker import binding


@pytest.fixture(autouse=True)
def no_external_or_financial_io(monkeypatch):
    attempts = []

    def deny(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("external or financial IO forbidden")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket.socket, "sendto", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    from app.sheets import SheetsDB
    monkeypatch.setattr(SheetsDB, "__init__", deny)
    yield
    assert attempts == []


@pytest.fixture
def composition(monkeypatch):
    from google.auth.transport import requests
    from google.oauth2 import service_account
    from googleapiclient import discovery
    import google_auth_httplib2
    import httplib2

    connection, world, _, _ = connected()
    raw = binding(connection, world)
    catalog = json.loads(raw["catalog_json"])
    calls = dict(expected=0, clients=[], compose=0)
    private = SimpleNamespace(read_catalog=lambda: (deepcopy(catalog), '"synthetic"'))
    result = object()
    original_expected = worker._expected

    def expected(*args):
        calls["expected"] += 1
        return original_expected(*args)

    def build(name, version, **kwargs):
        calls["clients"].append((name, version))
        return object()

    def compose(**kwargs):
        calls["compose"] += 1
        assert kwargs["private"] is private
        return result

    monkeypatch.setattr(service_account.Credentials, "from_service_account_info",
                        lambda *args, **kwargs: object())
    monkeypatch.setattr(requests, "AuthorizedSession", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(google_auth_httplib2, "AuthorizedHttp", lambda *args, **kwargs: object())
    monkeypatch.setattr(httplib2, "Http", lambda *args, **kwargs: object())
    monkeypatch.setattr(discovery, "build", build)
    monkeypatch.setattr(worker, "ReceiptPrivateAdapter", lambda *args, **kwargs: private)
    monkeypatch.setattr(worker, "_expected", expected)
    monkeypatch.setattr(worker, "connect_existing_google_services", compose)

    def connect(config):
        return worker.existing_google_connection(worker.validate_binding(config),
                                                {"client_email": config["service_email"]})

    return raw, catalog, calls, connect, result, original_expected


def test_json_roundtrip_equivalent_expected_passes_actual_catalog_guard(composition):
    raw, catalog, calls, connect, result, expected = composition
    derived = expected(raw["case_id"], catalog["cases"][raw["case_id"]])
    restored = json.loads(raw["expected_json"])
    assert derived != restored
    assert type(derived["original_items"][0]["category"]) is tuple
    assert type(restored["original_items"][0]["category"]) is list
    assert digest(derived) == raw["expected_digest"]
    before = deepcopy(raw)
    assert connect(raw) is result
    assert raw == before
    assert calls == dict(expected=1, clients=[("drive", "v3"), ("sheets", "v4")], compose=1)


@pytest.mark.parametrize("damage", ["amount", "category", "source_id", "source_hash",
                                    "source_version", "case_id", "extra_case"])
def test_real_value_and_case_changes_still_reject_before_composition(composition, damage):
    raw, catalog, calls, connect, _, _ = composition
    changed = json.loads(raw["expected_json"])
    if damage == "amount":
        changed["original_items"][0]["amount"] += 1
    elif damage == "category":
        changed["original_items"][0]["category"][0] += " altered"
    elif damage == "source_id":
        changed["source"]["source_id"] += "-altered"
    elif damage == "source_hash":
        changed["source"]["sha256"] = "f" * 64
    elif damage == "source_version":
        changed["source"]["version"] += "-altered"
    elif damage == "case_id":
        raw["case_id"] += "-altered"
        changed["case_id"] = raw["case_id"]
    else:
        catalog["cases"][raw["case_id"] + "-extra"] = deepcopy(catalog["cases"][raw["case_id"]])
    # Keep each changed binding internally canonical: the live guard must still
    # compare it to independently reconstructed catalog evidence.
    raw["expected_json"] = encode(changed).decode()
    raw["expected_digest"] = digest(changed)
    with pytest.raises(StateError, match="^receipt_worker_catalog_binding_mismatch$"):
        connect(raw)
    assert calls["clients"] == [] and calls["compose"] == 0
    assert calls["expected"] == (0 if damage in {"case_id", "extra_case"} else 1)


@pytest.mark.parametrize("pin", ["expected_digest", "catalog_digest"])
def test_wrong_digest_still_rejects_at_binding_validation(composition, pin):
    raw, _, calls, connect, _, _ = composition
    raw[pin] = "0" * 64
    with pytest.raises(StateError, match="^receipt_worker_binding_invalid$"):
        connect(raw)
    assert calls == dict(expected=0, clients=[], compose=0)


@pytest.mark.parametrize("damage,code", [
    ("candidate", "receipt_private_catalog_evidence_mismatch"),
    ("confirmation", "receipt_private_catalog_confirmation_required"),
])
def test_expected_catalog_validation_is_not_bypassed(composition, damage, code):
    raw, catalog, calls, connect, _, _ = composition
    entry = catalog["cases"][raw["case_id"]]
    if damage == "candidate":
        entry["candidate_rows"][0][5] += 1
    else:
        entry["anchor"]["total_confirmation"] = None
    with pytest.raises(StateError, match="^" + code + "$"):
        connect(raw)
    assert calls == dict(expected=1, clients=[], compose=0)
