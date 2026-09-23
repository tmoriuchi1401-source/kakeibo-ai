"""Actions-only parent assembly; reuse the existing source CLIs and policies."""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
from time import monotonic
from uuid import uuid4

from .drive_run_state import DriveStateTransport, DurableState, StateBinding, StateError
from .production_run import COUNT_KEYS, DEPENDENCIES, execute_serial, require_success, run_durable_source
from .production_ledger import ProductionLedger


REPO = Path(__file__).resolve().parents[1]
STATE_SOURCES = {"amazon": "amazon_gmail", "aupay_card": "au_pay_card_gmail", "bank": "bank_pdf_drive"}
STATE_ID_ENV = {"amazon": "AMAZON_STATE_FILE_ID", "aupay_card": "AUPAY_CARD_STATE_FILE_ID", "bank": "BANK_STATE_FILE_ID"}
COMMON_COMMANDS = {"review_apply": "review-apply", "reconcile": "reconcile", "auto_expense": "auto-expense",
                   "review_refresh": "review-refresh", "expenses_refresh": "expenses-refresh"}
COMMON_PREVIEWS = {"review_apply": "review-apply-preview", "reconcile": "reconcile-preview",
                  "auto_expense": "auto-expense-preview", "review_refresh": "review-preview",
                  "expenses_refresh": "expenses-preview"}


def verify_execution_boundary(env: dict, head: str) -> None:
    if (env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_REF") != "refs/heads/main"
            or env.get("GITHUB_REPOSITORY") != "tmoriuchi1401-source/kakeibo-ai"
            or env.get("KAKEIBO_PRODUCTION_ENABLED") != "true"
            or env.get("KAKEIBO_VALIDATED_MAIN_SHA") != head
            or env.get("GITHUB_SHA") != head):
        raise StateError("production_main_boundary_required")


def command(source: str, *, apply: bool, canary_target: str = "", money_canary: bool = False) -> list[str]:
    if canary_target and (source not in {"amazon","aupay_card"} or not apply or
                          not re.fullmatch(r"AM-[0-9a-f]{32}", canary_target)):
        raise StateError("amazon_canary_target_invalid")
    if money_canary and source not in {"amazon","aupay_card"}:raise StateError("money_canary_source_invalid")
    if money_canary and apply and not canary_target:raise StateError("money_canary_target_required")
    if source in {"receipts", "paypay"}:
        return [sys.executable, "-m", "app.production_source", source, "apply" if apply else "preview"]
    if source == "receipt_reimport":
        return [sys.executable, "-m", "app.receipt_reimport_production", "apply" if apply else "preview"]
    if source == "receipt_confirmation":
        return [sys.executable, "-m", "app.receipt_confirmation_production", "apply" if apply else "preview"]
    cli = [sys.executable, "-m", "app.cli"]
    if source == "amazon":
        args = cli + ["amazon-gmail-recurring", "--apply" if apply else "--dry-run"]
        if canary_target:
            args += ["--apply-limit", "1", "--approved-target", canary_target]
        return args
    if source == "aupay_card":
        return cli + ["card-gmail-recurring"] + ([] if apply else ["--dry-run"]) + (
            ["--money-canary"]+(["--money-target",canary_target] if canary_target else []) if money_canary or canary_target else [])
    if source == "bank":
        return cli + ["bank-pdf-recurring", "--apply" if apply else "--dry-run"]
    if source == "aupay_balance":
        return cli + ["aupay-gmail"] + ([] if apply else ["--dry-run"])
    return cli + [(COMMON_COMMANDS if apply else COMMON_PREVIEWS)[source]]


def invoke(source: str, *, apply: bool, env: dict, canary_target: str = "", money_canary: bool = False) -> dict:
    if (source=='receipt_confirmation' and apply and env.get('MEDICAL_DERIVED_AI_POLICY')
            and not env.get('MEDICAL_PREPARE_DIR') and not env.get('MEDICAL_FINALIZE_ONLY')):
        from .medical_auto_posting import AUTO_POLICIES
        if env['MEDICAL_DERIVED_AI_POLICY'] not in {'prepare-only','reviewed-v1:paid','reviewed-v1:free'}|AUTO_POLICIES:
            raise StateError('medical_service_terms_not_verified')
        import base64
        from .medical_candidate_runtime import run_prepared
        with tempfile.TemporaryDirectory(prefix='medical-derived-',dir=env.get('RUNNER_TEMP')) as directory:
            prepared=dict(env,MEDICAL_PREPARE_DIR=directory,
                MEDICAL_CROP_ATTESTATION_KEY=base64.b64encode(os.urandom(32)).decode())
            scan=invoke(source,apply=True,env=prepared)
            scan.update(run_prepared(prepared,directory))
            final=invoke(source,apply=True,env=dict(env,MEDICAL_FINALIZE_ONLY='true',MEDICAL_LOCAL_WRITTEN=str(scan.get('medical_local_written',0))))
            scan['written']=scan.get('written',0)+final['written']
            scan['archived']=scan.get('archived',0)+final.get('archived',0)
            scan['medical_pending']=final['medical_pending']
            scan['medical_auto_written']=final.get('medical_auto_written',0)
            for name in ('review_pending','normal_review_pending','medical_review_pending','intake_review_pending'):
                if name in final:scan[name]=final[name]
            return scan
    if source=='receipts' and apply and env.get('GITHUB_ACTIONS')=='true' and not env.get('RECEIPT_CONFIRMATION_BINDING'):
        raise StateError('receipt_confirmation_binding_required')
    if source=='receipts' and apply and env.get('RECEIPT_CONFIRMATION_BINDING') and not env.get('RECEIPT_SCAN_PLAN'):
        with tempfile.TemporaryDirectory(prefix='receipt-intake-',dir=env.get('RUNNER_TEMP')) as directory:
            prepared=dict(env,RECEIPT_SCAN_PLAN=str(Path(directory)/'plan.json'))
            scan=invoke('receipt_confirmation',apply=True,env=prepared)
            # Validate the intake contract before the ordinary receipt writer.
            if any(type(scan.get(key)) is not int or scan[key] < 0 for key in
                   ('found','medical_pending','blocked','medical_detected','written')):
                raise StateError('receipt_intake_summary_invalid')
            result=invoke('receipts',apply=True,env=prepared)
            result['found']=scan['found']
            result['needs_review']=result.get('needs_review',0)+scan['medical_pending']+scan['blocked']
            result['medical_detected']=scan['medical_detected']
            result['confirmed_written']=scan['written']
            result['archived']=scan.get('archived',0)
            for name in ('medical_ai_requests','medical_ai_reused','medical_ai_candidates','medical_ai_held','medical_auto_written','medical_local_written',
                         'review_pending','normal_review_pending','medical_review_pending','intake_review_pending'):
                if name in scan:result[name]=scan[name]
            return result
    # Child stdout/stderr can contain legacy filenames, totals and API errors.
    # Keep both in memory, never tee/upload/cache them. Disable legacy job summary.
    child_env = dict(env)
    child_env.pop("GITHUB_STEP_SUMMARY", None)
    if source in {"receipt_reimport", "receipt_confirmation"}:
        for name in ("GOOGLE_GMAIL_TOKEN_JSON", "AUPAY_CARD_AUDIT_KEY_JSON",
                     "AUPAY_CARD_RECURRING_AUTHORITY_JSON", "BANK_AUDIT_KEY_JSON",
                     "BANK_PDF_RECURRING_AUTHORITY_JSON"):
            child_env.pop(name,None)
    if source not in {"receipts", "receipt_reimport"} or not apply or (
            source == "receipt_reimport" and env.get("RECEIPT_REIMPORT_OPERATION") == "replay"):
        child_env.pop("GEMINI_API_KEY", None)
    try:
        result = subprocess.run(command(source, apply=apply, canary_target=canary_target,money_canary=money_canary), cwd=REPO, env=child_env,
                                capture_output=True, text=True, encoding="utf-8", timeout=900)
    except subprocess.TimeoutExpired:
        raise StateError('source_command_timed_out') from None
    if result.returncode != 0:
        if source in {"receipt_confirmation", "receipts", "paypay"}:
            from .production_run import SAFE_SOURCE_ERRORS, SourceFailure
            try:
                failure=json.loads(result.stdout)
                code=failure.get("error")
            except (ValueError,AttributeError):
                code=None
            if isinstance(code,str) and code in SAFE_SOURCE_ERRORS:
                raise SourceFailure(code, failure.get('stage', ''), failure)
        if source == "receipt_reimport":
            try:
                code=json.loads(result.stdout).get("error")
            except Exception:
                code=None
            if code in {"gemini_secret_not_injected", "gemini_auth_rejected", "gemini_quota_rejected",
                        "gemini_api_or_model_rejected", "gemini_result_invalid", "gemini_transport_unknown",
                        "gemini_request_failed_unknown", "receipt_analysis_reconciliation_required",
                        "receipt_manifest_or_result_invalid", "receipt_source_version_changed",
                        "receipt_source_content_changed", "receipt_result_permissions_mismatch",
                        "receipt_result_write_unknown", "receipt_result_readback_mismatch"}:
                raise StateError(code)
        raise StateError("source_command_failed")
    try:
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError:
            value = ast.literal_eval(result.stdout.strip())
    except Exception:
        raise StateError("source_result_invalid") from None
    require_success(value)
    return value


def _secret_file(directory: Path, name: str, payload: str) -> str:
    if not payload:
        raise StateError("source_configuration_missing")
    path = directory / name
    with path.open("x", encoding="utf-8") as stream:
        stream.write(payload)
    path.chmod(0o600)
    return str(path)


def source_environment(source: str, directory: Path, env: dict) -> dict:
    result = dict(env)
    if source == "amazon":
        # Exact current daily policy from amazon-daily-import.yml; no widening.
        authority = {
            "policy_id": "amazon-daily-v1", "source": "amazon_gmail",
            "expected_spreadsheet_id": env.get("SPREADSHEET_ID", ""), "max_messages": 100,
            "max_purchases": 3, "overlap_seconds": 7200, "max_window_seconds": 259200,
            "initial_start": "2026-09-12T12:26:44+09:00",
            "valid_from": "2026-08-01T00:00:00+09:00", "expires_at": "2027-09-13T00:00:00+09:00",
        }
        result["AMAZON_RECURRING_AUTHORITY_FILE"] = _secret_file(directory, "authority.json", json.dumps(authority))
    elif source == "aupay_card":
        result["AUPAY_CARD_RECURRING_AUTHORITY_FILE"] = _secret_file(directory, "authority.json", env.get("AUPAY_CARD_RECURRING_AUTHORITY_JSON", ""))
        result["AUPAY_CARD_AUDIT_KEY_FILE"] = _secret_file(directory, "audit-key.json", env.get("AUPAY_CARD_AUDIT_KEY_JSON", ""))
    elif source == "bank":
        result["BANK_PDF_RECURRING_AUTHORITY_FILE"] = _secret_file(directory, "authority.json", env.get("BANK_PDF_RECURRING_AUTHORITY_JSON", ""))
        if env.get("BANK_AUDIT_KEY_JSON"):
            result["BANK_AUDIT_KEY_FILE"] = _secret_file(directory, "audit-key.json", env["BANK_AUDIT_KEY_JSON"])
    return result


def assemble(env: dict, directory: Path, *, apply: bool, bank_apply: bool,
             ledger: ProductionLedger | None = None, canary_target: str = "", canary_source: str = "amazon", money_canary: bool = False) -> dict:
    if canary_source not in {"amazon","aupay_card"} or (canary_source!="amazon" and not money_canary):raise StateError("money_canary_source_invalid")
    if money_canary and apply and not canary_target:raise StateError("money_canary_target_required")
    if canary_target:
        command(canary_source, apply=apply, canary_target=canary_target,money_canary=money_canary)
        if bank_apply:
            raise StateError("canary_bank_apply_forbidden")
    def execute(source):
        effective_apply = apply and (source != "bank" or bank_apply)
        if source not in STATE_SOURCES:
            return invoke(source, apply=effective_apply, env=env)
        private = directory / source
        private.mkdir()
        prepared = source_environment(source, private, env)
        binding = StateBinding(STATE_SOURCES[source], env.get("SPREADSHEET_ID", ""),
                               env.get("KAKEIBO_STATE_FOLDER_ID", ""), env.get(STATE_ID_ENV[source], ""))
        from .google_clients import drive_service, read_only_drive_service
        service = drive_service() if effective_apply else read_only_drive_service()
        store = DurableState(DriveStateTransport(service, binding), binding)
        local = private / "state"
        state_env = {"amazon": "AMAZON_STATE_DIR", "aupay_card": "AUPAY_CARD_STATE_DIR", "bank": "BANK_PDF_STATE_DIR"}[source]
        prepared[state_env] = str(local)
        extra = ({"canary_target": canary_target,"money_canary":True} if source==canary_source and (canary_target or money_canary) else {})
        return run_durable_source(store, local, lambda _path: invoke(source, apply=effective_apply, env=prepared, **extra), apply=effective_apply)
    def run(source):
        if not apply:
            return execute(source)
        if ledger is None:
            raise StateError("production_ledger_required")
        # A projection stage has no accounting writer. Its own durable dirty
        # markers make replay safe even after an ambiguous display/save result.
        # Native accounting stages retain their existing pending-state gate.
        projection_replay=(source=="expenses_refresh" and env.get("KAKEIBO_PROJECTION_FOLDER_ID")
                           and ledger.value["sources"][source]["phase"]=="pending")
        if not projection_replay:
            ledger.begin(source, uuid4().hex)
        started = monotonic()
        try:
            result = execute(source)
            ledger.complete(source, result, monotonic() - started)
            return result
        except Exception:
            ledger.fail(source)
            raise
    return {source: lambda source=source: run(source) for source in DEPENDENCIES}


def validate_scope(args) -> None:
    canary_source=getattr(args,"canary_source","amazon")
    if canary_source not in {"amazon","aupay_card"} or (canary_source!="amazon" and args.scope!="amazon_canary"):
        raise StateError("money_canary_source_invalid")
    if args.scope in {"daily", "ledger_order", "categories"}:
        if (args.bank_apply or args.amazon_target or getattr(args,"receipt_store","")
                or getattr(args,"receipt_manifest","") or getattr(args,"projection_bootstrap",False)):
            raise StateError(args.scope + "_scope_other_source_forbidden")
    if args.scope == "projection":
        if args.bank_apply or args.amazon_target or getattr(args,"receipt_store","") or getattr(args,"receipt_manifest",""):
            raise StateError("projection_scope_other_source_forbidden")
    if getattr(args,"projection_bootstrap",False) and (args.scope!="projection" or args.mode!="apply"):
        raise StateError("projection_bootstrap_requires_isolated_apply")
    if args.scope in {'receipt_confirmation', 'receipts'} and (args.bank_apply or args.amazon_target):
        raise StateError('receipt_scope_other_source_forbidden')
    if args.scope == "receipt_reimport":
        if args.bank_apply or args.amazon_target:
            raise StateError("receipt_scope_other_source_forbidden")
        if not getattr(args,"receipt_store","") or not re.fullmatch(r"[0-9a-f]{64}",getattr(args,"receipt_manifest","")):
            raise StateError("receipt_fixed_manifest_required")
    elif getattr(args,"receipt_store","") or getattr(args,"receipt_manifest",""):
        raise StateError("receipt_inputs_require_reimport_scope")
    if args.scope == "amazon_canary":
        if args.bank_apply:
            raise StateError("canary_bank_apply_forbidden")
        if args.mode == "apply":
            if not args.amazon_target:
                raise StateError("amazon_canary_target_required")
            command(canary_source, apply=True, canary_target=args.amazon_target,money_canary=True)
        elif args.amazon_target:
            raise StateError("preview_has_no_approved_write_target")
    elif args.amazon_target:
        raise StateError("amazon_target_requires_canary_scope")


def finish_ledger_updates(env, result, *, apply):
    """Order only after all successful writers have released their row hints."""
    require_success(result or {"errors": 0})
    if apply:
        from .ledger_order import run_ledger_order
        return {**result, **run_ledger_order(env, apply=True)}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preview", "apply"), default="preview")
    parser.add_argument("--bank-apply", action="store_true")
    parser.add_argument("--scope", choices=("all", "amazon_canary", "receipt_reimport", "receipt_confirmation", "receipts", "projection", "daily", "ledger_order", "categories"), default="all")
    parser.add_argument("--projection-bootstrap", action="store_true")
    parser.add_argument("--amazon-target", default="")
    parser.add_argument("--canary-source",choices=("amazon","aupay_card"),default="amazon")
    parser.add_argument("--receipt-store", default="")
    parser.add_argument("--receipt-manifest", default="")
    parser.add_argument("--receipt-operation", choices=("reanalyze","replay"), default="reanalyze")
    parser.add_argument("--receipt-limit", type=int, choices=(1,2,3), default=1)
    args = parser.parse_args()
    try:
        env = dict(os.environ)
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
        verify_execution_boundary(env, head)
        from .private_state_bindings import decode_environment
        env, args.amazon_target = decode_environment(env, canary_target=args.amazon_target)
        validate_scope(args)
        if args.scope == "categories":
            if env.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
                raise StateError("categories_manual_scope_required")
            from .category_operations import run_category_operations
            result=run_category_operations(env,apply=args.mode=="apply")
            print(json.dumps({"success":True,"scope":args.scope,"counts":result},sort_keys=True))
            return
        if args.scope == "ledger_order":
            if env.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
                raise StateError("ledger_order_manual_scope_required")
            from .ledger_order import run_ledger_order
            result = run_ledger_order(env, apply=args.mode == "apply")
            print(json.dumps({"success": True, "scope": args.scope, "counts": result}, sort_keys=True))
            return
        if args.scope=="amazon_canary":
            if env.get("GITHUB_EVENT_NAME")!="workflow_dispatch":raise StateError("money_canary_manual_required")
            from .amazon_money_runtime import money_enabled
            if not money_enabled(env):raise StateError("money_canary_migration_required")
        if args.scope == "daily":
            if env.get("GITHUB_EVENT_NAME") != "workflow_dispatch":raise StateError("daily_manual_scope_required")
            from .daily_runtime import run_daily_requests
            result=run_daily_requests(env,apply=args.mode=="apply")
            result=finish_ledger_updates(env,result,apply=args.mode=="apply")
            print(json.dumps({"success":True,"scope":args.scope,"counts":result},sort_keys=True))
            return
        if args.scope == "projection":
            if env.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
                raise StateError("projection_manual_scope_required")
            from .projection_runtime import run_projection
            result=run_projection(env,apply=args.mode=="apply",bootstrap=args.projection_bootstrap)
            print(json.dumps({"success":True,"scope":args.scope,"counts":result},sort_keys=True))
            return
        if args.scope=='receipts' and env.get('GITHUB_EVENT_NAME')!='workflow_dispatch':
            raise StateError('receipt_manual_scope_required')
        if args.scope=='receipt_confirmation':
            if env.get('GITHUB_EVENT_NAME')!='workflow_dispatch':raise StateError('confirmation_manual_scope_required')
            result=invoke('receipt_confirmation',apply=args.mode=='apply',env=env)
            result=finish_ledger_updates(env,result,apply=args.mode=="apply")
            print(json.dumps({'success':True,'scope':args.scope,'counts':result},sort_keys=True))
            return
        if args.scope == "receipt_reimport":
            if env.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
                raise StateError("receipt_reimport_manual_only")
            # The new scope is isolated from all daily accounting stages and from
            # the four native/ledger state files; the shared Workflow lock holds.
            env.update(RECEIPT_REIMPORT_FILE=args.receipt_store,
                       RECEIPT_REIMPORT_MANIFEST=args.receipt_manifest,
                       RECEIPT_REIMPORT_OPERATION=args.receipt_operation,
                       RECEIPT_REIMPORT_LIMIT=str(args.receipt_limit))
            result=invoke("receipt_reimport",apply=args.mode=="apply",env=env)
            result=finish_ledger_updates(env,result,apply=args.mode=="apply")
            print(json.dumps({"success":True,"scope":args.scope,"counts":result},sort_keys=True))
            return
        if args.bank_apply and args.mode != "apply":
            raise StateError("bank_apply_requires_approved_apply")
        from .google_clients import drive_service, read_only_drive_service
        ledger_binding = StateBinding("production_run", env.get("SPREADSHEET_ID", ""),
                                       env.get("KAKEIBO_STATE_FOLDER_ID", ""), env.get("KAKEIBO_RUN_LEDGER_FILE_ID", ""))
        ledger_service = drive_service() if args.mode == "apply" else read_only_drive_service()
        ledger = ProductionLedger(DriveStateTransport(ledger_service, ledger_binding), ledger_binding)
        history = ledger.value["sources"]
        daily_counts={}
        category_counts={}
        if args.scope == "all":
            from .daily_runtime import run_daily_requests
            from .category_operations import run_category_operations
            category_counts=run_category_operations(env,apply=args.mode=="apply")
            # The fixed-ID inbox is replayable independently of the native
            # accounting stages. Their ledger/checkpoint gates are unchanged.
            daily_counts=run_daily_requests(env,apply=args.mode=="apply")
        with tempfile.TemporaryDirectory(prefix="kakeibo-production-") as directory:
            runners = assemble(env, Path(directory), apply=args.mode == "apply", bank_apply=args.bank_apply,
                               ledger=ledger, canary_target=args.amazon_target,canary_source=args.canary_source,money_canary=args.scope=="amazon_canary")
            report = execute_serial(runners, history=history, preview=args.mode == "preview",
                                    amazon_canary=args.scope == "amazon_canary", canary_source=args.canary_source,receipts_only=args.scope == "receipts")
        if report["success"] and args.mode == "apply":
            report["ledger_order"] = finish_ledger_updates(env, {}, apply=True)
        # Re-read after ambiguous responses instead of reporting stale in-memory
        # markers. This is inspection only, never a retry of a source/write.
        ledger_confirmed = True
        try:
            ledger = ProductionLedger(ledger.transport, ledger_binding)
        except Exception:
            ledger_confirmed = False
            report["success"] = False
            report["error"] = "ledger_final_read_failed"
        # Success means a completed source operation, including bank preview.
        # bank_mode and written counts distinguish preview/no-op/new writes.
        for source, outcome in report["sources"].items():
            outcome["last_success"] = ledger.value["sources"][source]["last_success"]
            outcome["confirmation_pending"] = (ledger.value["sources"][source]["phase"] == "pending") if ledger_confirmed else None
        report["mode"] = args.mode
        if category_counts:report["category_operations"]=category_counts
        if daily_counts:report["daily_requests"]={key:value for key,value in daily_counts.items()
            if key in COUNT_KEYS and type(value) is int and value>=0}
        report["scope"] = args.scope
        report["bank_mode"] = "not_run" if args.scope in {"amazon_canary", "receipts"} else "apply" if args.bank_apply else "preview"
    except Exception as exc:
        report = {"schema": 1, "success": False, "error": "production_preflight_failed"}
        from .category_operations import CategoryOperationFailure
        if isinstance(exc,CategoryOperationFailure):
            report.update(error="category_operation_failed",**exc.details)
    rendered = json.dumps(report, ensure_ascii=True, sort_keys=True)
    print(rendered)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write("## Kakeibo production\n\n```json\n" + rendered + "\n```\n")
    if not report["success"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
