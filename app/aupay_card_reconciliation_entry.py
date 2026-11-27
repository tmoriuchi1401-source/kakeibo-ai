"""Manual hosted entry for one signed, fixed card reconciliation only."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import base64
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import zlib

from .aupay_card_window_preview import FLAGS, REPO, ROOT, GitHub, require
from .aupay_card_reconciliation_diagnostics import ReconciliationDiagnostics

WORKFLOW="aupay-card-reconciliation-once.yml"
CONFIRM="APPLY_FIXED_CARD_RECONCILIATION_ONCE"
LABEL=b"kakeibo-card-reconciliation-v1"
# Fixed private record digest, populated after fresh GET-only preparation.
RECORD_SHA256="f1350433e560c216b824f84fe6686f960423fbd75a76d899bad0dc7dc5854d6a"


def encode(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=True,separators=(",",":")).encode()


def boundary(env,head,github,*,workflow=WORKFLOW,confirm=CONFIRM,per_release=False):
    require(env.get("GITHUB_ACTIONS")=="true" and env.get("RUNNER_ENVIRONMENT")=="github-hosted"
        and env.get("RUNNER_OS")=="Linux" and env.get("GITHUB_REPOSITORY")==REPO
        and env.get("GITHUB_EVENT_NAME")=="workflow_dispatch" and env.get("GITHUB_REF")=="refs/heads/main"
        and env.get("GITHUB_ACTOR")==env.get("GITHUB_TRIGGERING_ACTOR")==REPO.split("/")[0]
        and env.get("GITHUB_RUN_ATTEMPT")=="1" and env.get("CONFIRM")==confirm,
        "repair_hosted_owner_once_required")
    require(head==env.get("EXPECTED_SHA")==env.get("GITHUB_SHA")==env.get("GITHUB_WORKFLOW_SHA")
        ==env.get("KAKEIBO_VALIDATED_MAIN_SHA")
        and env.get("GITHUB_WORKFLOW_REF")==f"{REPO}/.github/workflows/{workflow}@refs/heads/main"
        and env.get("RUNNER_DEBUG","0")!="1" and not env.get("ACTIONS_STEP_DEBUG")
        and not env.get("ACTIONS_RUNNER_DEBUG") and not env.get("GEMINI_API_KEY")
        and all(env.get(k)==v for k,v in FLAGS.items())
        and all(env.get(k)=="false" for k in ("KAKEIBO_CARD_SCHEDULE_ENABLED","KAKEIBO_CORE_SCHEDULE_ENABLED",
            "KAKEIBO_RECEIPT_SCHEDULE_ENABLED","KAKEIBO_PAYPAY_SCHEDULE_ENABLED")),"repair_release_boundary_changed")
    require(github.get("git/ref/heads/main")["object"]["sha"]==head,"repair_main_changed")
    rid=int(env["GITHUB_RUN_ID"])
    current=github.get(f"actions/runs/{rid}")
    require(current["id"]==rid and current["head_sha"]==head and current["run_attempt"]==1
        and current["status"]=="in_progress" and current["event"]=="workflow_dispatch"
        and current["path"]==".github/workflows/"+workflow,"repair_run_changed")
    history=github.get(f"actions/workflows/{workflow}/runs?per_page=100")
    # Consumes the entry even on a crash before the first Google acknowledgement.
    # No second dispatch or rerun can reuse the same fixed repair authority.
    runs=history["workflow_runs"]
    if per_release:
        same=[r for r in runs if r["head_sha"]==head]
        require(history["total_count"]==len(runs)<=100 and len(same)==1 and same[0]["id"]==rid
                and all(r["id"]==rid or (r["status"]=="completed" and r["conclusion"]=="failure"
                    and r["run_attempt"]==1) for r in runs),"repair_dispatch_consumed_no_retry")
    else:
        require(history["total_count"]==len(runs)==1 and runs[0]["id"]==rid,"repair_dispatch_consumed_no_retry")
    for status in ("in_progress","queued","waiting","pending","requested"):
        runs=github.get(f"actions/runs?status={status}&per_page=100")
        require(runs["total_count"]==len(runs["workflow_runs"])<=100
            and all(r["id"]==rid for r in runs["workflow_runs"]),"repair_competing_run")


def decode_capsule(text,key,tree,now,*,record_sha256=None,label=None,scope="fixed_card_import_reconciliation_only"):
    """Existing RSA/AES/PSS capsule pattern and existing SA key; no new grant."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    label=LABEL if label is None else label
    record_sha256=RECORD_SHA256 if record_sha256 is None else record_sha256
    require(len(text)<=60000,"repair_capsule_too_large")
    sealed=json.loads(base64.b64decode(text,validate=True))
    b64=lambda name:base64.b64decode(sealed[name],validate=True)
    aes=key.decrypt(b64("key"),padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=label))
    decoder=zlib.decompressobj()
    plain=decoder.decompress(AESGCM(aes).decrypt(b64("nonce"),b64("data"),label),1024*1024)
    require(decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail,"repair_capsule_invalid")
    value=json.loads(plain)
    record_bytes=base64.b64decode(value["record"],validate=True)
    att=value["attestation"]
    key.public_key().verify(base64.b64decode(value["signature"],validate=True),encode(att),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),salt_length=padding.PSS.MAX_LENGTH),hashes.SHA256())
    require(hashlib.sha256(record_bytes).hexdigest()==record_sha256==att["record_sha256"]
        and att["code_tree_sha256"]==tree and att["owner_approved"] is True
        and att["scope"]==scope
        and 0<(datetime.fromisoformat(att["approval"]["expires_at"])-now).total_seconds()<=300,
        "repair_signed_approval_invalid")
    record=json.loads(record_bytes)
    require(encode(record)==record_bytes,"repair_record_not_canonical")
    return record,att


def verify_failure_ancestry(record,env,github):
    expected={}
    ancestor=record
    for _ in range(32):
        failure=ancestor["old_failure"]
        require(failure["id"] not in expected,"repair_old_failure_changed")
        expected[failure["id"]]=failure
        prior=github.get(f'actions/runs/{failure["id"]}')
        require(all(prior.get(k)==v for k,v in failure.items()),"repair_old_failure_changed")
        if not ancestor.get("recovery"):break
        ancestor=ancestor["recovery"]["previous_record"]
    else:require(False,"repair_old_failure_changed")
    if record.get("recovery"):
        from .aupay_card_reservation_recovery import RECOVERY_WORKFLOW
        history=github.get(f"actions/workflows/{RECOVERY_WORKFLOW}/runs?per_page=100")
        require(history["total_count"]==len(history["workflow_runs"])<=100
                and all(r["id"]==int(env["GITHUB_RUN_ID"]) or (r["id"] in expected
                    and all(r.get(k)==v for k,v in expected[r["id"]].items())) for r in history["workflow_runs"]),
                "repair_old_failure_changed")


def run(env,head,github,diagnostics=None,*,boundary_checker=None,record_sha256=None,label=None,scope="fixed_card_import_reconciliation_only"):
    diagnostics = diagnostics or ReconciliationDiagnostics()
    check=boundary_checker or boundary
    record_sha256=RECORD_SHA256 if record_sha256 is None else record_sha256
    diagnostics.mark("release_guard")
    check(env,head,github)
    diagnostics.mark("dependencies")
    from cryptography.hazmat.primitives import serialization
    from .aupay_card_reconciliation_apply import execute_reconciliation,read_financial_and_review,prepare_reconciliation
    from .aupay_card_batch import ProtectedBatchApprovalProvider
    from .aupay_card_production import ProtectedAuditKeyProvider
    from .aupay_card_window_evidence import GetOnly
    from .aupay_card_window_preview import ReadOnlyState
    from .amazon_gmail_preview import gmail_readonly_service
    from .drive_run_state import DurableState,DriveStateTransport,StateBinding,validate
    from .production_ledger import ProductionLedger
    from .google_clients import read_only_drive_service,read_only_sheets_service,drive_service,sheets_service
    from .projection_store import DriveProjectionStore
    from .projection_cache import RollingProjectionStore
    from .sheets import SheetsDB
    from .private_state_bindings import unwrap
    from .aupay_card_incremental_recovery import _rows
    from .aupay_card_aliases import merge_registry
    from types import SimpleNamespace
    info=json.loads(env["GOOGLE_SERVICE_ACCOUNT_JSON"])
    key=serialization.load_pem_private_key(info["private_key"].encode(),password=None)
    tree=subprocess.check_output(["git","rev-parse","HEAD^{tree}"],cwd=ROOT,text=True).strip()
    diagnostics.mark("capsule_decode")
    record,att=decode_capsule(env["REPAIR_CAPSULE"],key,tree,datetime.now(timezone.utc),record_sha256=record_sha256,label=label,scope=scope)
    diagnostics.mark("bindings")
    bindings=record["bindings"]
    require(bindings["spreadsheet_id"]==env["SPREADSHEET_ID"],"repair_target_changed")
    for name,field in (("KAKEIBO_STATE_FOLDER_ID","folder_id"),("AUPAY_CARD_STATE_FILE_ID","card_id"),
                       ("KAKEIBO_RUN_LEDGER_FILE_ID","parent_id"),("KAKEIBO_PROJECTION_FOLDER_ID","projection_id")):
        require(unwrap(name,env[name],info["private_key"])==bindings[field],"repair_binding_changed")
    deadline=time.monotonic()+900
    ro_drive=GetOnly(read_only_drive_service(),deadline)
    ro_sheets=GetOnly(read_only_sheets_service(),deadline)
    cb=StateBinding("au_pay_card_gmail",bindings["spreadsheet_id"],bindings["folder_id"],bindings["card_id"])
    pb=StateBinding("production_run",bindings["spreadsheet_id"],bindings["folder_id"],bindings["parent_id"])
    card_read,parent_read=ReadOnlyState(ro_drive,cb),ReadOnlyState(ro_drive,pb)
    diagnostics.mark("state_revision")
    require(card_read.observe()[1]["metadata_digest"]==record["native_before_metadata_digest"]
        and parent_read.observe()[1]["metadata_digest"]==record["parent_before_metadata_digest"],
        "repair_state_revision_changed")
    backing=RollingProjectionStore(DriveProjectionStore(ro_drive,bindings["projection_id"],bindings["spreadsheet_id"],precreated=True))
    def documents_check():
        require(hashlib.sha256(encode({k:backing.read(k) for k in record["protected_document_keys"]})).hexdigest()
            ==record["documents_sha256"],"repair_documents_changed")
    def acl_check():
        acl={}
        for label,file_id in (("card",cb.file_id),("parent",pb.file_id),("folder",cb.folder_id),("ledger",env["SPREADSHEET_ID"])):
            acl[label]=ro_drive.files().get(fileId=file_id,supportsAllDrives=True,
                fields="id,mimeType,trashed,owners(emailAddress,permissionId),permissions(id,type,role,emailAddress)").execute()
            grants=acl[label].get("permissions",[])
            require(not acl[label].get("trashed") and grants and not any(p.get("type") in {"anyone","domain"} for p in grants)
                and any(p.get("emailAddress")==info["client_email"] and p.get("role") in {"writer","owner"} for p in grants),
                "repair_acl_invalid")
        require(hashlib.sha256(encode(acl)).hexdigest()==record["acl_sha256"],"repair_acl_changed")
    gmail=GetOnly(gmail_readonly_service(env["GOOGLE_GMAIL_TOKEN_JSON"]),deadline)
    def source_check():
        # Exact fixed messages only. No listing, full original reconciliation,
        # AI, parser fork or fetch-window expansion.
        for evidence in record["selected_source_messages"]:
            result=gmail.users().messages().get(userId="me",id=evidence["gmail_id"],format="raw").execute()
            raw=base64.b64decode(result["raw"]+"="*(-len(result["raw"])%4),altchars=b"-_",validate=True)
            require(result["id"]==evidence["gmail_id"] and result["internalDate"]==evidence["internal_date_ms"]
                and hashlib.sha256(raw).hexdigest()==evidence["raw_sha256"],"repair_fixed_source_changed")
    def guard():
        check(env,head,github)
        require(time.monotonic()<deadline,"repair_timeout")
        verify_failure_ancestry(record,env,github)
        acl_check()
        for item in record["other_native"]:
            file_id=unwrap(item["variable"],env[item["variable"]],info["private_key"])
            require(file_id==item["file_id"],"repair_peer_binding_changed")
            binding=StateBinding(item["source"],db.sid,bindings["folder_id"],file_id)
            raw=ReadOnlyState(ro_drive,binding).read()
            value=validate(raw,binding)
            require(hashlib.sha256(raw).hexdigest()==item["sha256"] and value["phase"]=="ready"
                and not value["attempt"],"repair_peer_native_pending_or_changed")
    diagnostics.mark("runtime_clients")
    readonly=SheetsDB(env["SPREADSHEET_ID"],service=ro_sheets,read_retry_attempts=1)
    class LiteralDB:
        sid=readonly.sid
        svc=sheets_service()
        def get(self,rng):return readonly.get_raw(rng)
    db=LiteralDB()
    durable=DurableState(DriveStateTransport(drive_service(),cb),cb)
    ledger=ProductionLedger(DriveStateTransport(drive_service(),pb),pb)
    with tempfile.TemporaryDirectory(prefix="card-fixed-repair-",dir=env["RUNNER_TEMP"]) as temp:
        private=Path(temp)
        (private/"audit.json").write_text(env["AUPAY_CARD_AUDIT_KEY_JSON"],encoding="utf-8")
        audit=ProtectedAuditKeyProvider(private/"audit.json",repo_root=ROOT)
        original_raw=card_read.read()
        require(hashlib.sha256(original_raw).hexdigest()==record["native_before_sha256"],"repair_state_changed")
        original=validate(original_raw,cb)
        cp=_rows(original,"recurring.sqlite3","recurring_checkpoint")
        require(len(cp)==1,"repair_checkpoint_invalid")
        summaries=[json.loads(r["summary_json"]) for r in _rows(original,"recurring.sqlite3","recurring_runs")
                   if r["status"] in {"complete","noop"}]
        cached=SimpleNamespace(successful_window_end=lambda:datetime.fromisoformat(cp[0]["successful_window_end"]),
            alias_registry=lambda:merge_registry(e for s in summaries for e in s.get("card_aliases",[])))
        diagnostics.mark("cached_projection")
        projected=prepare_reconciliation(record,db,cached,audit.load())["projection"]
        require(att["approval"]["batch_size"]==record["import_count"],"repair_signed_count_changed")
        # The owner's signed fixed record authorizes these exact candidates.
        # HMAC refs are derived inside the hosted runner with the existing
        # Secret; its key/target must match the protected historical manifests.
        approval=dict(att["approval"],batch_ref=projected.batch_ref,target_ref=projected.target_ref)
        (private/"approval.json").write_text(json.dumps(approval),encoding="utf-8")
        current=github.get(f'actions/runs/{env["GITHUB_RUN_ID"]}')
        identity={k:current[k] for k in ("id","head_sha","head_branch","run_attempt","event","path","created_at")}
        return execute_reconciliation(record=record,record_sha256=record_sha256,run_id=att["run_id"],db=db,
            durable=durable,ledger=ledger,key_provider=audit,
            approval_provider=ProtectedBatchApprovalProvider(private/"approval.json",repo_root=ROOT),
            state_dir=private/"state",repo_root=ROOT,native_read=card_read.read,parent_read=parent_read.read,
            financial_read=lambda:read_financial_and_review(ro_sheets,db.sid),documents_check=documents_check,
            source_check=source_check,guard=guard,diagnostics=diagnostics,
            state_observe=lambda:(card_read.observe()[1],parent_read.observe()[1]),execution_identity=identity)


def main(*,run_profile=None,guard_profile=None):
    logging.disable(logging.CRITICAL);socket.setdefaulttimeout(30)
    diagnostics=ReconciliationDiagnostics()
    try:
        with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            env=dict(os.environ)
            head=subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip()
            require(sys.argv[1:] in ([],["--guard-only"]),"repair_arguments_forbidden")
            github=GitHub(env)
            if sys.argv[1:]:
                diagnostics.mark("release_guard")
                (guard_profile or boundary)(env,head,github);result=dict(success=True,guard=True)
            else:result=(run_profile or run)(env,head,github,diagnostics=diagnostics)
        result["diagnostics"]=diagnostics.report()
    except BaseException as error:
        result=dict(success=False,outcome="UNKNOWN_NO_REPLAY",automatic_retry=False,rollback=False,
            diagnostics=diagnostics.report(error))
    print(json.dumps(result,sort_keys=True))
    return 0 if result["success"] else 1


if __name__=="__main__":raise SystemExit(main())
