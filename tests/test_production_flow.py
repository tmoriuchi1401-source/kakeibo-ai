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


@pytest.mark.parametrize('bank,target,manifest', [(True, '', ''), (False, 'amazon-order:'+'a'*16, ''), (False, '', 'a'*64)])
def test_receipts_scope_rejects_unrelated_authorities(bank, target, manifest):
    with pytest.raises(StateError):
        flow.validate_scope(SimpleNamespace(scope='receipts', mode='apply', bank_apply=bank,
                                          amazon_target=target, receipt_manifest=manifest, receipt_store=''))


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


@pytest.mark.parametrize("mode,scope,target,bank", [
    ("apply", "amazon_canary", "", False),
    ("apply", "amazon_canary", "invalid", False),
    ("apply", "amazon_canary", "amazon-order:" + "a" * 16, True),
    ("apply", "all", "amazon-order:" + "a" * 16, False),
    ("preview", "amazon_canary", "amazon-order:" + "a" * 16, False),
])
def test_canary_scope_rejects_missing_or_misplaced_approval(mode, scope, target, bank):
    with pytest.raises(StateError):
        flow.validate_scope(SimpleNamespace(mode=mode, scope=scope, amazon_target=target, bank_apply=bank))


def test_canary_uses_existing_exact_target_and_one_purchase_bounds():
    target = "amazon-order:" + "a" * 16
    args = flow.command("amazon", apply=True, canary_target=target)
    assert args[-8:] == ["--apply-limit", "1", "--approved-target", target,
                         "--expected-event-rows", "1", "--expected-header-rows", "1"]
    flow.validate_scope(SimpleNamespace(mode="preview", scope="amazon_canary", amazon_target="", bank_apply=False))


@pytest.mark.parametrize("mode,scope,bank,target", [
    ("preview","projection",False,""), ("apply","all",False,""),
    ("apply","projection",True,""), ("apply","projection",False,"amazon-order:"+"a"*16),
])
def test_bootstrap_cannot_share_a_scope_with_accounting(mode,scope,bank,target):
    with pytest.raises(StateError):
        flow.validate_scope(SimpleNamespace(mode=mode,scope=scope,bank_apply=bank,amazon_target=target,
                                           projection_bootstrap=True,receipt_store="",receipt_manifest=""))


@pytest.mark.parametrize("flag,value",[("bank_apply",True),("amazon_target","target"),
    ("receipt_manifest","a"*64),("receipt_store","private"),("projection_bootstrap",True)])
def test_daily_scope_rejects_other_source_inputs(flag,value):
    args=dict(scope="daily",mode="apply",bank_apply=False,amazon_target="",receipt_manifest="",receipt_store="",projection_bootstrap=False)
    args[flag]=value
    with pytest.raises(StateError,match="daily_scope_other_source_forbidden"):
        flow.validate_scope(SimpleNamespace(**args))


def test_isolated_daily_scope_runs_only_the_fixed_id_inbox(monkeypatch,capsys):
    import json
    seen=[]
    monkeypatch.setattr(flow.sys,"argv",["production_flow","--scope","daily","--mode","apply"])
    for key,value in dict(valid_env(),GITHUB_EVENT_NAME="workflow_dispatch").items():monkeypatch.setenv(key,value)
    monkeypatch.setattr(flow.subprocess,"check_output",lambda *args,**kw:"a"*40)
    monkeypatch.setattr("app.private_state_bindings.decode_environment",lambda env,**kw:(env,""))
    monkeypatch.setattr("app.daily_runtime.run_daily_requests",lambda env,apply:seen.append(apply) or {"corrections_applied":1})
    monkeypatch.setattr(flow,"assemble",lambda *args,**kw:pytest.fail("isolated daily cannot run intake"))
    flow.main()
    assert seen==[True]
    assert json.loads(capsys.readouterr().out)=={"success":True,"scope":"daily","counts":{"corrections_applied":1}}
