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


def test_scheduled_scopes_keep_gmail_cadence_and_drive_sources_independent():
    scopes = flow.DRIVE_SCHEDULE_SCOPES
    assert {"amazon", "aupay_card", "aupay_balance"} <= scopes["core"]
    assert not {"receipts", "paypay", "bank"} & scopes["core"]
    assert scopes["drive_receipts"] == {"receipts"}
    assert scopes["drive_bank"] == {"bank"}
    assert scopes["drive_paypay"] == {"paypay"}


@pytest.mark.parametrize("scope", ["core", "drive_receipts", "drive_paypay"])
def test_scheduled_scopes_reject_bank_apply(scope):
    with pytest.raises(StateError):
        flow.validate_scope(SimpleNamespace(scope=scope, mode="apply", bank_apply=True,
                                            amazon_target="", receipt_store="", receipt_manifest="",
                                            projection_bootstrap=False))


def test_isolated_empty_drive_runs_do_not_sort_other_ledgers():
    empty_receipts = {"sources": {"receipts": {"counts": {"written": 0}}}}
    new_receipts = {"sources": {"receipts": {"counts": {"written": 1}}}}
    assert not flow.needs_ledger_order("drive_bank", {}, bank_apply=False)
    assert not flow.needs_ledger_order("drive_paypay", {}, bank_apply=False)
    assert not flow.needs_ledger_order("drive_receipts", empty_receipts, bank_apply=False)
    assert flow.needs_ledger_order("drive_receipts", new_receipts, bank_apply=False)
    assert not flow.needs_ledger_order("drive_bank", {"sources": {"bank": {"counts": {"written": 0}}}}, bank_apply=True)
    assert flow.needs_ledger_order("drive_bank", {"sources": {"bank": {"counts": {"written": 1}}}}, bank_apply=True)
    assert flow.needs_ledger_order("core", {}, bank_apply=False)


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


@pytest.mark.parametrize("stderr,expected", [
    ("private traceback\nRuntimeError: bank_recurring_drive_folder_mismatch", "bank_recurring_drive_folder_mismatch"),
    ("RuntimeError: protected_bank_recurring_authority_invalid", "protected_bank_recurring_authority_invalid"),
    ("RuntimeError: bank_recurring_drive_folder_mismatch private-id", "source_command_failed"),
    ("RuntimeError: private-bank-filename", "source_command_failed"),
])
def test_bank_child_reports_only_fixed_preflight_error(monkeypatch, stderr, expected):
    monkeypatch.setattr(flow.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stdout="", stderr=stderr))
    with pytest.raises(StateError, match=f"^{expected}$"):
        flow.invoke("bank", apply=False, env={})


def test_bank_default_stays_preview_when_general_sources_apply(monkeypatch, tmp_path):
    from unittest.mock import Mock
    calls = []
    monkeypatch.setattr(flow, "source_environment", lambda source, directory, env: env.copy())
    monkeypatch.setattr("app.google_clients.read_only_drive_service", lambda: object())
    monkeypatch.setattr(flow, "run_durable_source", lambda store, directory, run, apply: calls.append(apply) or {"written": 0})
    runners = flow.assemble({"SPREADSHEET_ID": "sheet", "KAKEIBO_STATE_FOLDER_ID": "folder", "BANK_STATE_FILE_ID": "file"},
                            tmp_path, apply=True, bank_apply=False,
                            ledger=Mock(value={"sources": {"bank": {"phase": "ready", "counts": {}}}}))
    runners["bank"]()
    assert calls == [False]


def test_bank_preview_uses_last_scanned_cursor(monkeypatch, tmp_path):
    from unittest.mock import Mock
    seen = []
    monkeypatch.setattr(flow, "source_environment", lambda source, directory, env: env.copy())
    monkeypatch.setattr("app.google_clients.read_only_drive_service", lambda: object())
    monkeypatch.setattr(flow, "run_durable_source",
                        lambda store, directory, run, apply: run(directory))
    monkeypatch.setattr(flow, "invoke", lambda source, apply, env, **kw:
                        seen.append(env.get("BANK_PREVIEW_CURSOR_EPOCH")) or {"written": 0})
    ledger = Mock(value={"sources": {"bank": {"phase": "ready",
                                               "counts": {"preview_cursor_epoch": 123456}}}})
    env = {"SPREADSHEET_ID": "sheet", "KAKEIBO_STATE_FOLDER_ID": "folder",
           "BANK_STATE_FILE_ID": "file"}
    flow.assemble(env, tmp_path, apply=False, bank_apply=False, ledger=ledger)["bank"]()
    assert seen == ["123456"]


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
    target = "AM-" + "a" * 32
    args = flow.command("amazon", apply=True, canary_target=target)
    assert args[-4:] == ["--apply-limit", "1", "--approved-target", target]
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
    monkeypatch.setattr("app.ledger_order.run_ledger_order",lambda env,apply:{})
    monkeypatch.setattr(flow,"assemble",lambda *args,**kw:pytest.fail("isolated daily cannot run intake"))
    flow.main()
    assert seen==[True]
    assert json.loads(capsys.readouterr().out)=={"success":True,"scope":"daily","counts":{"corrections_applied":1}}


@pytest.mark.parametrize("apply", [False, True])
def test_ledger_order_scope_is_isolated_and_preview_does_not_apply(monkeypatch, capsys, apply):
    import json
    seen = []
    monkeypatch.setattr(flow.sys, "argv", ["production_flow", "--scope", "ledger_order", "--mode", "apply" if apply else "preview"])
    for key, value in dict(valid_env(), GITHUB_EVENT_NAME="workflow_dispatch").items(): monkeypatch.setenv(key, value)
    monkeypatch.setattr(flow.subprocess, "check_output", lambda *args, **kw: "a" * 40)
    monkeypatch.setattr("app.private_state_bindings.decode_environment", lambda env, **kw: (env, ""))
    monkeypatch.setattr("app.ledger_order.run_ledger_order", lambda env, apply: seen.append(apply) or {"ledger_order_sheets": 2})
    monkeypatch.setattr(flow, "assemble", lambda *args, **kw: pytest.fail("ordering cannot run intake"))
    flow.main()
    assert seen == [apply]
    assert json.loads(capsys.readouterr().out)["counts"] == {"ledger_order_sheets": 2}


def test_post_writer_sort_runs_after_success_only(monkeypatch):
    seen = []
    monkeypatch.setattr("app.ledger_order.run_ledger_order", lambda env, apply: seen.append(apply) or {"ledger_order_sheets": 2})
    with pytest.raises(StateError): flow.finish_ledger_updates({}, {"failure": 1}, apply=True)
    flow.finish_ledger_updates({}, {"written": 1}, apply=False)
    assert not seen
    assert flow.finish_ledger_updates({}, {"written": 1}, apply=True)["ledger_order_sheets"] == 2
    assert seen == [True]


@pytest.mark.parametrize("apply", [False, True])
def test_categories_scope_consumes_only_category_requests(monkeypatch,capsys,apply):
    import json
    seen=[]
    monkeypatch.setattr(flow.sys,"argv",["production_flow","--scope","categories","--mode","apply" if apply else "preview"])
    for key,value in dict(valid_env(),GITHUB_EVENT_NAME="workflow_dispatch").items():monkeypatch.setenv(key,value)
    monkeypatch.setattr(flow.subprocess,"check_output",lambda *args,**kw:"a"*40)
    monkeypatch.setattr("app.private_state_bindings.decode_environment",lambda env,**kw:(env,""))
    monkeypatch.setattr("app.category_operations.run_category_operations",lambda env,apply:seen.append(apply) or {"category_registration_processed":1})
    monkeypatch.setattr(flow,"assemble",lambda *args,**kw:pytest.fail("categories cannot run intake"))
    flow.main()
    assert seen==[apply]
    assert json.loads(capsys.readouterr().out)["scope"]=="categories"


@pytest.mark.parametrize("flag,value",[("bank_apply",True),("amazon_target","target"),
    ("receipt_manifest","a"*64),("receipt_store","private"),("projection_bootstrap",True)])
def test_categories_scope_rejects_other_source_inputs(flag,value):
    args=dict(scope="categories",mode="apply",bank_apply=False,amazon_target="",receipt_manifest="",receipt_store="",projection_bootstrap=False)
    args[flag]=value
    with pytest.raises(StateError,match="categories_scope_other_source_forbidden"):
        flow.validate_scope(SimpleNamespace(**args))


def diagnostic_policy(tmp_path, route="bank", **overrides):
    import json
    from datetime import datetime, timedelta, timezone
    from app.bank_pdf_pipeline import SOURCE
    now = datetime.now(timezone.utc)
    policy = dict(schema_version=1, policy_id="private-policy-name",
        source="bank_pdf_drive" if route == "bank" else "au_pay_card_gmail",
        expected_spreadsheet_id="private-sheet-id", expected_worksheet="取込データ",
        overlap_seconds=3600, max_window_seconds=604800,
        initial_start=(now-timedelta(days=2)).isoformat(),
        valid_from=(now-timedelta(days=1)).isoformat(), expires_at=(now+timedelta(days=1)).isoformat(),
        unexpected_credential="do-not-copy-this-secret")
    if route == "bank":
        policy.update(expected_drive_folder_id="private-folder-id", supported_bank_sources=[SOURCE],
            allowed_classifications=["expense"], max_files=20, max_rows=100,
            account_alias="private-account", income_enabled=True, income_worksheet="収入明細",
            income_accounts=[[SOURCE, "private-account"]])
    else:
        policy.update(allowed_statuses=["auto_expense"], max_batch_size=100, max_messages=500)
    policy.update(overrides)
    directory = tmp_path / "source-inputs" / route
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "authority.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    env = {"SPREADSHEET_ID":"private-sheet-id", "BANK_PDF_DRIVE_FOLDER_ID":"private-folder-id",
           "RUNNER_TEMP":str(tmp_path), "BANK_INCOME_WRITE_ENABLED":"false"}
    return path, env


@pytest.mark.parametrize("source", ["bank", "aupay_card"])
def test_authority_preview_keeps_details_private_and_never_changes_inputs(tmp_path, capsys, source):
    import hashlib
    import json
    path, env = diagnostic_policy(tmp_path, source)
    original, original_env = path.read_bytes(), env.copy()
    result = flow.preview_authority_summary(path.parent.parent, {source:{"status":"success"}}, env)
    assert result[source]["policy_valid"]
    assert all(type(value) is bool for value in result[source].values())
    private = json.loads(next(tmp_path.glob("kakeibo-authority-*.private.json")).read_text())
    assert private[source]["policy"]["expected_spreadsheet_id"] == "private-sheet-id"
    assert private[source]["policy_sha256"] == hashlib.sha256(original).hexdigest()
    assert private[source]["policy"]["max_window_seconds"] == 604800
    assert private["income_write_enabled"] is False
    if source == "bank":
        assert private[source]["policy"]["income_accounts"][0][1] == "private-account"
    assert "do-not-copy-this-secret" not in json.dumps(private)
    captured = capsys.readouterr()
    public = json.dumps(result) + captured.out + captured.err
    for value in ("private-policy-name", "private-sheet-id", "private-folder-id", "private-account",
                  "do-not-copy-this-secret", "income_accounts", "policy_sha256"):
        assert value not in public
    assert path.read_bytes() == original and env == original_env


@pytest.mark.parametrize("field", ["SPREADSHEET_ID", "BANK_PDF_DRIVE_FOLDER_ID"])
def test_authority_preview_detects_target_mismatch_even_if_source_failed(tmp_path, field):
    path, env = diagnostic_policy(tmp_path)
    env[field] = "different-private-id"
    result = flow.preview_authority_summary(path.parent.parent, {"bank":{"status":"failed"}}, env)
    assert not result["bank"]["policy_valid"]
    assert result["bank"]["bounds_valid"]


@pytest.mark.parametrize("overrides", [{"max_rows":101}, {"max_window_seconds":1},
    {"source":"wrong-source"}, {"allowed_classifications":["income"]}])
def test_authority_preview_rejects_invalid_bounds_or_source_without_leaking_errors(tmp_path, overrides):
    import json
    path, env = diagnostic_policy(tmp_path, **overrides)
    result = flow.preview_authority_summary(path.parent.parent, {"bank":{"status":"failed"}}, env)
    assert result == {"bank":{"policy_valid":False}}
    private = json.loads(next(tmp_path.glob("kakeibo-authority-*.private.json")).read_text())
    assert private["bank"] == {"checks":{"policy_valid":False},"error":"authority_diagnostic_unavailable"}


def test_authority_preview_detects_expiry_and_omits_unselected_sources(tmp_path):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    path, env = diagnostic_policy(tmp_path, valid_from=(now-timedelta(days=3)).isoformat(),
                                  expires_at=(now-timedelta(days=1)).isoformat())
    assert flow.preview_authority_summary(path.parent.parent, {}, env) == {}
    assert not list(tmp_path.glob("kakeibo-authority-*.private.json"))
    result = flow.preview_authority_summary(path.parent.parent, {"bank":{"status":"failed"}}, env)
    assert not result["bank"]["period_active"] and not result["bank"]["policy_valid"]


def test_authority_private_report_cannot_be_written_inside_repository(tmp_path, monkeypatch):
    path, env = diagnostic_policy(tmp_path)
    monkeypatch.setattr("app.drive_run_state.REPOSITORY_ROOT", tmp_path)
    with pytest.raises(StateError, match="state_directory_must_be_absolute_and_external"):
        flow.preview_authority_summary(path.parent.parent, {"bank":{"status":"success"}}, env)
    assert not list(tmp_path.glob("kakeibo-authority-*.private.json"))


@pytest.mark.parametrize("mode", ["preview", "apply"])
def test_authority_diagnostic_hook_is_preview_only_and_preserves_existing_boundary(monkeypatch, capsys, tmp_path, mode):
    import json
    seen = []
    monkeypatch.setattr(flow.tempfile, "tempdir", str(tmp_path))
    env = dict(valid_env(), GITHUB_EVENT_NAME="workflow_dispatch", SPREADSHEET_ID="sheet",
               KAKEIBO_STATE_FOLDER_ID="folder", KAKEIBO_RUN_LEDGER_FILE_ID="file")
    monkeypatch.setattr(flow.sys, "argv", ["production_flow", "--scope", "drive_bank", "--mode", mode])
    for key,value in env.items(): monkeypatch.setenv(key,value)
    monkeypatch.setattr(flow.subprocess, "check_output", lambda *a,**kw:"a"*40)
    monkeypatch.setattr("app.private_state_bindings.decode_environment", lambda env,**kw:(env,""))
    monkeypatch.setattr("app.google_clients.read_only_drive_service", lambda:object())
    monkeypatch.setattr("app.google_clients.drive_service", lambda:object())
    ledger = SimpleNamespace(transport=object(), value={"sources":{"bank":{"last_success":None,"phase":"ready","counts":{}}}})
    monkeypatch.setattr(flow, "ProductionLedger", lambda *a:ledger)
    monkeypatch.setattr(flow, "assemble", lambda *a,**kw:{})
    monkeypatch.setattr(flow, "execute_serial", lambda *a,**kw:{"success":True,"sources":{"bank":{"status":"success","counts":{}}}})
    monkeypatch.setattr("app.bank_review_refresh.refresh_bank_review_after_sources", lambda *a,**kw:{})
    monkeypatch.setattr(flow, "preview_authority_summary", lambda *a:seen.append(True) or {"bank":{"policy_valid":True}})
    flow.main()
    report = json.loads(capsys.readouterr().out)
    assert seen == ([True] if mode == "preview" else [])
    assert ("authority" in report) == (mode == "preview")
