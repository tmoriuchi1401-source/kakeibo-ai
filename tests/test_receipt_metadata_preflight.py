"""Narrow, offline-only safety tests; all identities/keys/metadata are synthetic."""
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
import socket
from urllib.parse import parse_qs

import pytest
import requests
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import jwt

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("metadata_probe", ROOT / "diagnostics/receipt_metadata_preflight.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
CANARY = "PRIVATE_TOKEN https://private.invalid/source PRIVATE_FILE private@invalid.example"
FIXED = dict(service_email="sa@synthetic.invalid", private_id="SYNTHETIC_PRIVATE_PIN", folder_id="SYNTHETIC_FOLDER_PIN")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts = []
    disabled = logging.root.manager.disable

    def deny(*a, **k):
        attempts.append(True)
        raise AssertionError("real communication forbidden")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    yield
    logging.disable(disabled)
    assert attempts == []


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


@pytest.fixture
def env(monkeypatch, key):
    pin = hashlib.sha256(json.dumps(FIXED, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    monkeypatch.setattr(probe, "BINDING_SHA256", pin)
    return dict(GITHUB_REPOSITORY=probe.REPOSITORY, GITHUB_EVENT_NAME="push",
        GITHUB_REF="refs/tags/" + probe.EXECUTION_TAG, PREFLIGHT_REF_CREATED="true",
        GITHUB_RUN_ATTEMPT="1", RECEIPT_VALIDATION_ENABLED="false",
        RECEIPT_VALIDATION_CONFIG_JSON=json.dumps({**FIXED, "unused_private_field": CANARY}),
        GOOGLE_SERVICE_ACCOUNT_JSON=json.dumps(dict(type="service_account", client_email=FIXED["service_email"],
            token_uri=probe.TOKEN_URI, private_key=key, private_key_id="SYNTHETIC_ONLY")))


def mocked_http(monkeypatch, fault=None):
    calls = []

    def request(session, method, url, **kwargs):
        calls.append((method, url))
        assert kwargs["allow_redirects"] is False and kwargs["verify"] is True
        assert kwargs["timeout"] == (10, 20) and session.trust_env is False
        assert session.get_adapter(url).max_retries.total == 0
        response = requests.Response()
        response.status_code = 200
        if method == "POST":
            assert url == probe.TOKEN_URI
            body_text = kwargs["data"].decode() if isinstance(kwargs["data"], bytes) else kwargs["data"]
            assertion = parse_qs(body_text)["assertion"][0]
            payload = jwt.decode(assertion, verify=False)
            assert payload["scope"] == probe.SCOPE and payload["aud"] == probe.TOKEN_URI and "sub" not in payload
            body = dict(access_token="SYNTHETIC_ACCESS_TOKEN", expires_in=3600)
            if fault and fault[0] == "auth":
                response.status_code, body = fault[1], {"error": "synthetic", "error_description": CANARY}
        else:
            assert method == "GET" and url.startswith("https://www.googleapis.com/drive/v2/files/")
            assert parse_qs(url.split("?", 1)[1]) == {"fields": [probe.FIELDS]}
            assert kwargs["headers"] == {"Authorization": "Bearer SYNTHETIC_ACCESS_TOKEN"}
            private = FIXED["private_id"] in url
            body = dict(id=FIXED["private_id"] if private else FIXED["folder_id"],
                mimeType="application/json" if private else "application/vnd.google-apps.folder", labels={"trashed": False})
            if fault and fault[0] == ("private" if private else "folder"):
                response.status_code, body = fault[1], {"error": {"message": CANARY}}
        response._content = json.dumps(body).encode()
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    return calls


def assert_safe(rows, capsys):
    text = json.dumps(rows)
    assert CANARY not in text and "SYNTHETIC_ACCESS_TOKEN" not in text
    assert all(set(row) == {"stage", "code", "http_status"} for row in rows)
    assert capsys.readouterr() == ("", "")


def test_success_is_one_auth_and_exactly_two_metadata_gets(env, monkeypatch, capsys):
    calls = mocked_http(monkeypatch)
    status, rows = probe.probe(env)
    assert status == 0 and len(calls) == 3
    assert [r["code"] for r in rows] == ["SA_IDENTITY_PASS", "AUTH_PASS", "METADATA_PASS", "METADATA_PASS"]
    assert [r["http_status"] for r in rows] == [0, 200, 200, 200]
    assert_safe(rows, capsys)


@pytest.mark.parametrize("field,value", [
    ("GITHUB_REF", "refs/heads/diag/receipt-metadata-preflight-20261114"),
    ("GITHUB_EVENT_NAME", "workflow_dispatch"), ("PREFLIGHT_REF_CREATED", "false"),
    ("GITHUB_RUN_ATTEMPT", "2"), ("RECEIPT_VALIDATION_ENABLED", "true"), ("ACTIONS_STEP_DEBUG", "true"),
])
def test_placement_or_unapproved_context_never_connects(env, field, value, monkeypatch, capsys):
    env[field] = value
    calls = mocked_http(monkeypatch)
    status, rows = probe.probe(env)
    assert status == 1 and calls == [] and rows[0]["code"] == "EXECUTION_NOT_APPROVED"
    assert_safe(rows, capsys)


@pytest.mark.parametrize("wrong", ["identity", "binding"])
def test_identity_and_fixed_target_rejection_before_auth(env, wrong, monkeypatch, capsys):
    name = "GOOGLE_SERVICE_ACCOUNT_JSON" if wrong == "identity" else "RECEIPT_VALIDATION_CONFIG_JSON"
    value = json.loads(env[name]);value["client_email" if wrong == "identity" else "private_id"] = CANARY
    env[name] = json.dumps(value)
    calls = mocked_http(monkeypatch);status, rows = probe.probe(env)
    assert status == 1 and calls == []
    assert rows[0]["code"] == ("SA_IDENTITY_MISMATCH" if wrong == "identity" else "BINDING_PIN_MISMATCH")
    assert_safe(rows, capsys)


@pytest.mark.parametrize("fault,count", [(('auth', 500), 1), (('auth', 302), 1),
    (('private', 503), 2), (('private', 302), 2), (('folder', 403), 3), (('private', 200), 2)])
def test_errors_stop_without_retry_redirect_or_private_body_output(env, monkeypatch, capsys, fault, count):
    calls = mocked_http(monkeypatch, fault);status, rows = probe.probe(env)
    assert status == 1 and len(calls) == count
    if count < 3:
        assert rows[3]["code"] == "NOT_RUN"
    assert_safe(rows, capsys)


def test_transport_rejects_replay_write_and_body_endpoints(monkeypatch):
    calls = mocked_http(monkeypatch)
    url = "https://www.googleapis.com/drive/v2/files/SYNTHETIC_PRIVATE_PIN?" + probe.urlencode({"fields": probe.FIELDS})
    session = probe.BoundedSession([url])
    try:
        for method, target in [("PUT", url), ("GET", url + "&alt=media"), ("GET", "https://sheets.googleapis.com/")]:
            with pytest.raises(RuntimeError, match="PREFLIGHT_TRANSPORT_BLOCKED"):
                session.request(method, target)
        assert calls == []
        # Use a generic stub for this budget-only check; JWT is tested above.
        monkeypatch.setattr(requests.Session, "request", lambda *a, **k: type("R", (), {"status_code": 200})())
        session.request("GET", url)
        with pytest.raises(RuntimeError, match="PREFLIGHT_TRANSPORT_BLOCKED"):
            session.request("GET", url)
    finally:
        session.close()


def test_new_workflow_is_inert_on_branch_placement_and_has_no_uuid_or_dispatch():
    path = ROOT / ".github/workflows/receipt-metadata-preflight-once.yml"
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    assert workflow["on"] == {"push": {"tags": [probe.EXECUTION_TAG]}}
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["metadata-only"]
    assert "github.event.created == true" in job["if"] and "github.run_attempt == 1" in job["if"]
    assert "vars.RECEIPT_VALIDATION_ENABLED == 'false'" in job["if"] and "runner.debug" not in job["if"]
    secret_steps = [s for s in job["steps"] if "secrets." in json.dumps(s)]
    assert len(secret_steps) == 1 and secret_steps[0]["run"] == "python diagnostics/receipt_metadata_preflight.py"
    assert "request_id" not in path.read_text() and "receipt_sheet_worker" not in path.read_text()


def test_cli_rejects_arguments_without_reading_secrets_or_connecting(monkeypatch, capsys):
    monkeypatch.setattr(probe.sys, "argv", ["receipt_metadata_preflight.py", CANARY])
    monkeypatch.setattr(probe, "probe", lambda *a, **k: pytest.fail("arguments must be rejected before probe"))
    assert probe.main() == 1
    output = capsys.readouterr()
    assert CANARY not in output.out and output.err == ""
    assert json.loads(output.out) == dict(stage="gate", http_status=0, code="ARGUMENTS_FORBIDDEN")
