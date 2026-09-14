from pathlib import Path
from types import SimpleNamespace

import pytest

from app import production_flow as flow
from app.drive_run_state import StateError


def valid_env():
    return {"GITHUB_ACTIONS": "true", "GITHUB_REF": "refs/heads/main",
            "GITHUB_REPOSITORY": "tmoriuchi1401-source/kakeibo-ai",
            "KAKEIBO_PRODUCTION_ENABLED": "true", "GITHUB_SHA": "a" * 40,
            "KAKEIBO_VALIDATED_MAIN_SHA": "a" * 40}


@pytest.mark.parametrize("key,value", [("GITHUB_REF", "refs/heads/feature"), ("GITHUB_ACTIONS", "false"),
    ("KAKEIBO_PRODUCTION_ENABLED", ""), ("KAKEIBO_VALIDATED_MAIN_SHA", "b" * 40),
    ("GITHUB_SHA", "b" * 40), ("GITHUB_REPOSITORY", "fork/repo")])
def test_production_cannot_run_from_local_branch_or_unvalidated_main(key, value):
    env = valid_env()
    flow.verify_execution_boundary(env, "a" * 40)
    env[key] = value
    with pytest.raises(StateError):
        flow.verify_execution_boundary(env, "a" * 40)


def test_preview_commands_never_select_apply():
    for source in flow.DEPENDENCIES:
        args = flow.command(source, apply=False)
        assert "--apply" not in args
        assert args[-1] in {"--dry-run", "preview"} or args[-1].endswith("preview")


def test_legacy_output_is_captured_and_ai_key_only_goes_to_receipt_apply(monkeypatch):
    seen = []
    def subprocess_run(args, **kwargs):
        seen.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"written": 0, "amount": 1234}', stderr="private-error")
    monkeypatch.setattr(flow.subprocess, "run", subprocess_run)
    for source in ("amazon", "receipts"):
        for apply in (False, True):
            flow.invoke(source, apply=apply, env={"GEMINI_API_KEY": "synthetic", "GITHUB_STEP_SUMMARY": "forbidden"})
            args, kwargs = seen[-1]
            assert kwargs["capture_output"]
            assert "GITHUB_STEP_SUMMARY" not in kwargs["env"]
            assert ("GEMINI_API_KEY" in kwargs["env"]) == (source == "receipts" and apply)


def test_bank_default_stays_preview_when_general_sources_apply(monkeypatch, tmp_path):
    from unittest.mock import Mock
    calls = []
    monkeypatch.setattr(flow, "source_environment", lambda source, directory, env: env.copy())
    monkeypatch.setattr("app.google_clients.read_only_drive_service", lambda: object())
    monkeypatch.setattr(flow, "run_durable_source", lambda store, directory, run, apply: calls.append(apply) or {"written": 0})
    runners = flow.assemble({"SPREADSHEET_ID": "sheet", "KAKEIBO_STATE_FOLDER_ID": "folder", "BANK_STATE_FILE_ID": "file"},
                            tmp_path, apply=True, bank_apply=False, ledger=Mock())
    runners["bank"]()
    assert calls == [False]


def test_amazon_daily_policy_keeps_existing_bounds(tmp_path):
    import json
    result = flow.source_environment("amazon", tmp_path, {"SPREADSHEET_ID": "synthetic-sheet"})
    policy = json.loads(Path(result["AMAZON_RECURRING_AUTHORITY_FILE"]).read_text())
    assert (policy["max_messages"], policy["max_purchases"], policy["max_window_seconds"]) == (100, 3, 259200)
    assert policy["overlap_seconds"] == 7200
