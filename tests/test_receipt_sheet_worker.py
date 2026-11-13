"""Synthetic real receiver -> unchanged private protocol -> actual worker.

Only Google IO is replaced. No verified live actor or iPhone success is claimed.
"""
from copy import deepcopy
import json
from pathlib import Path
import socket
import subprocess

import pytest

from app.page_receipt_model import digest
from app.receipt_private_adapter import _transition
from app.receipt_sheet_worker import run, validate_binding, binding_from_catalog
from test_receipt_private_adapter import encode
from test_receipt_sheet_connection import connected
from test_receipt_sheet_validation import RID


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    calls = []
    def deny(*args, **kwargs):
        calls.append(True)
        raise AssertionError("external or financial IO forbidden")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    from app.sheets import SheetsDB
    monkeypatch.setattr(SheetsDB, "__init__", deny)
    yield
    assert calls == []


def binding(c, w):
    expected = c.private.decoder_expected(w.case_id)
    catalog, _ = c.private.read_catalog()
    return dict(enabled=True, synthetic_only=True, repository="tmoriuchi1401-source/kakeibo-ai",
        workflow="category-sheet-request.yml", ref="feat/receipt-minimal-iphone-20261113",
        spreadsheet_id="synthetic-sheet", private_id="synthetic-private", folder_id="synthetic-folder",
        owner_email="owner@synthetic.invalid", service_email="service@synthetic.invalid",
        case_id=w.case_id, catalog_digest=digest(catalog), catalog_json=encode(catalog).decode(),
        expected_json=encode(expected).decode(), expected_digest=digest(expected))


def environment(b):
    return dict(RECEIPT_VALIDATION_CONFIG_JSON=json.dumps(b), GOOGLE_SERVICE_ACCOUNT_JSON='{}',
        RECEIPT_VALIDATION_ENABLED="true", GITHUB_REPOSITORY=b["repository"],
        GITHUB_REF="refs/heads/"+b["ref"], RECEIPT_VALIDATION_REF=b["ref"],
        GITHUB_EVENT_NAME="workflow_dispatch", GITHUB_RUN_ATTEMPT="1",
        RECEIPT_VALIDATION_SHA="a"*40, GITHUB_SHA="a"*40, RECEIPT_REQUEST_ID=RID)


def receive(c, w, p, io, **options):
    config = binding(c, w)
    script = Path("apps-script/receipt-validation/Connection.test.cjs").resolve()
    result = subprocess.run(["node", str(script)], input=json.dumps(dict(config=config,
        envelope_json=io.raw.decode(), row=p.rows[0], request_id=RID, **options)),
        text=True, encoding="utf-8", capture_output=True, check=True)
    output = json.loads(result.stdout)
    # The new JS adapter must produce transitions already accepted by #200's
    # unchanged Python protocol; it cannot create a different journal contract.
    for before, after in zip(output["history"], output["history"][1:]):
        assert before["catalog"] == after["catalog"]
        _transition(before["journal"], after["journal"])
    return config, output


def test_receiver_one_edit_one_register_uuid_worker_and_result_same_case():
    c, w, p, io = connected()
    # Real ReceiptItem output can store integral quantities as 1.0. The JS
    # adapter must preserve those catalog bytes/hash while changing journal.
    envelope = json.loads(io.raw)
    entry = envelope["catalog"]["cases"][w.case_id]
    for row in entry["candidate_rows"]:
        row[5] = float(row[5])
    entry["anchor"]["candidate_digest"] = digest(entry["candidate_rows"])
    io.raw = encode(envelope)
    c.private.catalog_digest = digest(envelope["catalog"])
    original_row = deepcopy(p.rows[0])
    b, received = receive(c, w, p, io, duplicate=True)
    assert received["attempts"] == 2 and not received["stopped"]
    assert received["dispatched"] == [dict(ref=b["ref"], inputs=dict(request_id=RID))]
    assert received["errors"] == ["receipt_validation_replay_rejected"]
    assert received["envelope"]["journal"]["requests"][RID]["state"] == "受付済み"
    assert received["rows"][0][:8] == original_row[:8]
    assert received["rows"][0][9:] == original_row[9:]
    io.raw, io.generation = received["raw_envelope"].encode(), received["generation"]
    assert digest(json.loads(io.raw)["catalog"]) == b["catalog_digest"]
    p.rows = received["rows"]
    assert run(environment(b), connect=lambda config, info: c) == (0, "登録検証済み・未記帳")
    result = c.result(RID, w.sheet["cases"])[0]
    assert result["result"]["state"] == "検証済み・未記帳"
    assert result["note"] == w.row["note"]
    assert result["original_link"] == w.row["original_link"]
    assert result["result"]["financial_write_count"] == 0


@pytest.mark.parametrize("fault", ["before", "after"])
def test_receiver_uncertain_private_write_stops_across_trigger_invocations(fault):
    c, w, p, io = connected()
    _, output = receive(c, w, p, io, fault=fault)
    assert output["attempts"] == 1
    assert output["stopped"] is True
    assert output["dispatched"] == [] and output["rule"] is None
    assert output["errors"][-1] == "receipt_connection_stopped"


@pytest.mark.parametrize("key,value", [
    ("GITHUB_RUN_ATTEMPT", "2"), ("GITHUB_REF", "refs/heads/main"),
    ("GITHUB_SHA", "b"*40), ("RECEIPT_VALIDATION_ENABLED", "false"),
    ("GITHUB_EVENT_NAME", "schedule"), ("RECEIPT_REQUEST_ID", "not-a-uuid"),
])
def test_wrong_execution_never_constructs_google_clients(key, value):
    c, w, p, io = connected()
    env = environment(binding(c, w));env[key] = value
    calls = []
    status, _ = run(env, connect=lambda *args: calls.append(True))
    assert status == 3 and calls == [] and io.writes == 0


def test_binding_rejects_altered_expected_data_without_matching_pin():
    c, w, p, io = connected()
    b = binding(c, w)
    wrong = json.loads(b["expected_json"])
    wrong["receipt_total"] += 1
    b["expected_json"] = encode(wrong).decode()
    with pytest.raises(Exception, match="binding_invalid"):
        validate_binding(b)


def test_binding_export_keeps_missing_owner_confirmation_missing():
    c, w, p, io = connected()
    catalog, _ = c.private.read_catalog()
    original = deepcopy(catalog)
    options = dict(spreadsheet_id="synthetic-sheet", private_id="synthetic-private",
        folder_id="synthetic-folder", owner_email="owner@synthetic.invalid",
        service_email="service@synthetic.invalid", ref="feat/receipt-minimal-iphone-20261113")
    assert binding_from_catalog(catalog, **options) == binding(c, w)
    catalog["cases"][w.case_id]["anchor"]["total_confirmation"] = None
    with pytest.raises(Exception, match="catalog_confirmation_required"):
        binding_from_catalog(catalog, **options)
    assert catalog["cases"][w.case_id]["anchor"]["total_confirmation"] is None
    assert original["cases"][w.case_id]["anchor"]["total_confirmation"] is not None


def test_explicit_setup_and_stop_only_manage_this_trial_trigger_and_token_copy():
    c, w, p, io = connected()
    _, received = receive(c, w, p, io, setup=True)
    assert received["errors"] == [] and len(received["dispatched"]) == 1
    assert received["setup"] == dict(trigger_count=0, enabled=False, staged_token=False, user_token=False)
