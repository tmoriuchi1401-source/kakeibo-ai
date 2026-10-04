from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.bank_review_replay import WORKFLOW_REF, require_request_context, run_bank_review_replay
from app.drive_run_state import StateError
from app.production_ledger import initial_ledger
from test_bank_review_requests import REQUEST

HEAD = "a" * 40
ENV = {
    "GITHUB_ACTIONS": "true", "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": HEAD,
    "GITHUB_REPOSITORY": "tmoriuchi1401-source/kakeibo-ai", "GITHUB_EVENT_NAME": "workflow_dispatch",
    "GITHUB_WORKFLOW_REF": WORKFLOW_REF, "KAKEIBO_PRODUCTION_ENABLED": "true",
    "KAKEIBO_VALIDATED_MAIN_SHA": HEAD, "KAKEIBO_LEGACY_DISABLED": "true",
    "BANK_REVIEW_ENABLED": "true", "BANK_REVIEW_REPLAY_ENABLED": "true", "CATEGORY_REQUEST_ID": REQUEST,
    "SPREADSHEET_ID": "synthetic-sheet", "KAKEIBO_STATE_FOLDER_ID": "F" * 20,
    "KAKEIBO_RUN_LEDGER_FILE_ID": "L" * 20,
}


class MemoryTransport:
    def __init__(self, binding):
        self.payload = initial_ledger(binding)
        self.writes = []
    def read(self): return self.payload
    def write(self, payload):
        self.payload = payload
        self.writes.append(payload)


def rig(monkeypatch, *, fail=False, pending=False):
    import app.bank_review_replay as replay
    seen = SimpleNamespace(calls=[], transport=None, env=None)
    monkeypatch.setattr(replay.subprocess, "check_output", lambda *a, **kw: HEAD)
    monkeypatch.setattr("app.private_state_bindings.decode_environment", lambda env: (deepcopy(env), ""))
    monkeypatch.setattr("app.google_clients.drive_service", lambda: object())
    def transport(service, binding):
        if seen.transport is None:
            seen.transport = MemoryTransport(binding)
        return seen.transport
    monkeypatch.setattr(replay, "DriveStateTransport", transport)
    def assemble(env, directory, **kwargs):
        assert kwargs["apply"] is True and kwargs["bank_apply"] is True
        assert env["BANK_REVIEW_REQUEST_ID"] == REQUEST
        seen.env = env
        ledger = kwargs["ledger"]
        def bank():
            ledger.begin("bank", "b" * 32)
            seen.calls.append("bank")
            if fail:
                ledger.fail("bank")
                raise StateError("source_reconciliation_required")
            result = {"status": "complete", "written": 2, "deposit_imports_created": 1,
                      "files_processed": 1, "files_withheld": 3, "duplicate": 185,
                      "file_statuses": [{"file_id": "private", "description": "private"}],
                      "secret": "private", "failure": 0}
            if not pending:
                ledger.complete("bank", result, 1)
            return result
        def other(): pytest.fail("unrelated source invoked")
        return {"bank": bank, "amazon": other, "receipts": other, "auto_expense": other}
    monkeypatch.setattr(replay, "assemble", assemble)
    return seen


def test_captured_request_reuses_bank_runner_and_durable_parent_marker_only(monkeypatch):
    seen = rig(monkeypatch)
    result = run_bank_review_replay(ENV, request_id=REQUEST, confirmed_groups=1)
    assert seen.calls == ["bank"] and len(seen.transport.writes) == 2
    assert result["bank_replay_completed"] == 1 and result["bank_ledger_writes"] == 3
    assert result["bank_replay_duplicate"] == 185 and result["bank_replay_files_withheld"] == 3
    assert "secret" not in str(result) and "private" not in str(result)


@pytest.mark.parametrize("fail,pending", [(True, False), (False, True)])
def test_failed_or_unverified_replay_keeps_pending_and_is_not_retried(monkeypatch, fail, pending):
    seen = rig(monkeypatch, fail=fail, pending=pending)
    with pytest.raises(StateError, match="source_reconciliation_required"):
        run_bank_review_replay(ENV, request_id=REQUEST, confirmed_groups=1)
    assert seen.calls == ["bank"]
    assert b'"phase":"pending"' in seen.transport.payload


def test_disabled_followup_does_not_open_credentials_state_or_invoke_a_runner():
    assert run_bank_review_replay({}, request_id="", confirmed_groups=0) == {"bank_replay_disabled": 1}


@pytest.mark.parametrize("name,value", [
    ("GITHUB_ACTIONS", "false"), ("GITHUB_REF", "refs/heads/diagnostic"),
    ("GITHUB_SHA", "b" * 40), ("KAKEIBO_VALIDATED_MAIN_SHA", "b" * 40),
    ("GITHUB_REPOSITORY", "another/repo"), ("GITHUB_EVENT_NAME", "schedule"),
    ("GITHUB_WORKFLOW_REF", "unrelated@refs/heads/main"), ("KAKEIBO_LEGACY_DISABLED", "false"),
    ("BANK_REVIEW_ENABLED", "false"), ("BANK_REVIEW_REPLAY_ENABLED", "false"),
    ("CATEGORY_REQUEST_ID", "87654321-1234-1234-1234-123456789abc"),
])
def test_shared_request_context_has_no_main_flag_or_uuid_fallback(name, value):
    with pytest.raises(StateError):
        require_request_context({**ENV, name: value}, HEAD, REQUEST)


@pytest.mark.parametrize("request_id", ["", "not-a-uuid", None, REQUEST.upper()])
def test_request_id_must_be_the_canonical_captured_uuid(request_id):
    with pytest.raises(StateError):
        require_request_context(ENV, HEAD, request_id)


def test_income_writer_accepts_shared_request_only_with_all_request_gates(monkeypatch):
    from app.bank_income_recurring import require_income_actions
    for name, value in ENV.items(): monkeypatch.setenv(name, value)
    monkeypatch.setenv("BANK_REVIEW_REQUEST_ID", REQUEST)
    monkeypatch.setattr("app.bank_income_recurring.subprocess.check_output", lambda *a, **kw: HEAD)
    require_income_actions(".", HEAD)
    monkeypatch.delenv("BANK_REVIEW_REQUEST_ID")
    with pytest.raises(StateError, match="request_invalid"):
        require_income_actions(".", HEAD)
