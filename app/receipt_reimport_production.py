"""Fixed normal-receipt maintenance inside the existing production parent.

The precreated private result file is independent of the four production states.
It records request intent before Gemini and saves successful responses for replay.
Only count/error codes leave this module; normal source data stays in Drive.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re

from .drive_run_state import DriveStateTransport, StateError
from .models import ReceiptResult
from .receipt_reimport import compare_receipt

SCHEMA = "receipt-reimport-v1"
DESTINATION = "https://generativelanguage.googleapis.com"
TABLES = {"receipt_rows":"レシート", "import_rows":"取込データ",
          "expense_rows":"支出明細", "review_rows":"要確認"}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return sha256(encoded(value)).hexdigest()


@dataclass(frozen=True)
class ResultBinding:
    folder_id: str
    file_id: str


def validate_store(value, expected_digest, spreadsheet_id):
    try:
        manifest=value["manifest"]
        if (value["schema"] != SCHEMA or digest(manifest) != expected_digest
                or manifest["spreadsheet_id"] != spreadsheet_id
                or manifest["destination"] != DESTINATION
                or len(manifest["sources"]) != 10
                or len({r["source_id"] for r in manifest["sources"]}) != 10
                or set(value["records"]) != {r["source_id"] for r in manifest["sources"]}):
            raise ValueError()
        for source in manifest["sources"]:
            if (source["classification"] != "normal" or source["pages"] != 1
                    or not re.fullmatch(r"[0-9a-f]{64}",source["sha256"])
                    or source["mime_type"] not in {"application/pdf","image/png","image/jpeg","image/jpg"}
                    or not source["version"]):
                raise ValueError()
            record=value["records"][source["source_id"]]
            if record["phase"] not in {"new","requested","complete","held","failed"}:
                raise ValueError()
            if record["phase"] == "complete":
                ReceiptResult.model_validate(record["parsed"])
        return value
    except Exception:
        raise StateError("receipt_manifest_or_result_invalid") from None


class ReimportStore:
    def __init__(self, transport, expected_digest, spreadsheet_id):
        self.transport=transport
        self.expected_digest=expected_digest
        self.spreadsheet_id=spreadsheet_id
        self.payload=transport.read()
        self.value=validate_store(json.loads(self.payload),expected_digest,spreadsheet_id)

    def save(self, value):
        validate_store(value,self.expected_digest,self.spreadsheet_id)
        if self.transport.read()!=self.payload:
            raise StateError("receipt_result_changed")
        proposed=encoded(value)
        try:
            self.transport.write(proposed)
        except Exception:
            # Unknown write: GET the same ID once; never repeat the update.
            if self.transport.read()!=proposed:
                raise StateError("receipt_result_write_unknown") from None
        if self.transport.read()!=proposed:
            raise StateError("receipt_result_readback_mismatch")
        self.payload=proposed
        self.value=deepcopy(value)


def target_snapshot(tables, source_id):
    rid,iid="R-"+source_id,"receipt:"+source_id
    expenses=[r for r in tables["expense_rows"] if len(r)>10 and (r[9]==rid or r[10]==iid)]
    keys={r[0] for r in expenses}
    return {"receipt_rows":[r for r in tables["receipt_rows"] if r and r[0]==rid],
            "import_rows":[r for r in tables["import_rows"] if r and
                           (r[0]==iid or (len(r)>9 and r[9] in keys|{rid,iid}))],
            "expense_rows":expenses,
            "review_rows":[r for r in tables["review_rows"] if r and r[0]==iid]}


def error_code(error):
    # Never serialize exception messages, bodies, filenames, or credentials.
    code=getattr(error,"code",None) or getattr(error,"status_code",None)
    if code in (401,403): return "gemini_auth_rejected"
    if code==429: return "gemini_quota_rejected"
    if code in (400,404): return "gemini_api_or_model_rejected"
    if type(error).__name__ in {"ValidationError","JSONDecodeError"}: return "gemini_result_invalid"
    if (isinstance(error,(TimeoutError,ConnectionError)) or "Timeout" in type(error).__name__
            or type(error).__name__ in {"ConnectError","ReadError","WriteError","RemoteProtocolError","NetworkError"}):
        return "gemini_transport_unknown"
    return "gemini_request_failed_unknown"


def run_reimport(store, *, pipeline, db, load_source, verify_source, apply, operation, limit, run_id,
                 api_key_present, model):
    if operation not in {"reanalyze","replay"} or limit not in {1,2,3}:
        raise StateError("receipt_operation_invalid")
    manifest=store.value["manifest"]
    if model!=manifest["model"]:
        raise StateError("receipt_model_mismatch")
    if any(r["phase"] in {"requested","failed"} for r in store.value["records"].values()):
        raise StateError("receipt_analysis_reconciliation_required")
    counts={"found":10,"analyzed":0,"reused":0,"privacy_held":0,"unchanged":0,"needs_review":0,
            "written":0,"failure":0}
    tables={key:db.get(f"'{title}'!A2:T") for key,title in TABLES.items()}
    categories=db.categories()
    attempted=0
    for source in manifest["sources"]:
        sid=source["source_id"]
        record=deepcopy(store.value["records"][sid])
        if record["phase"]=="held":
            counts["privacy_held"]+=1
            continue
        if record["phase"]=="new":
            if not apply or operation=="replay" or attempted>=limit:
                continue
            if not api_key_present:
                raise StateError("gemini_secret_not_injected")
            payload=load_source(source,manifest["folder_id"])
            if sha256(payload).hexdigest()!=source["sha256"]:
                raise StateError("receipt_source_content_changed")
            record={"phase":"requested","run_id":run_id,"before":target_snapshot(tables,sid)}
            value=deepcopy(store.value); value["records"][sid]=record; store.save(value)
            attempted+=1
            try:
                result=pipeline.reanalyze_bytes(payload,source["mime_type"],sid,
                                               destination=manifest["destination"])
            except Exception as error:
                record["phase"]="failed"; record["error"]=error_code(error)
                value=deepcopy(store.value); value["records"][sid]=record; store.save(value)
                raise StateError(record["error"]) from None
            if result["status"]=="privacy_blocked":
                record.update(phase="held",classification=result["classification"])
                counts["privacy_held"]+=1
            else:
                record.update(phase="complete",parsed=result["parsed"],validation=result["status"],issues=result["issues"])
                counts["analyzed"]+=1
        else:
            verify_source(source,manifest["folder_id"])
            counts["reused"]+=1
        if record["phase"]=="complete":
            result=compare_receipt(sid,ReceiptResult.model_validate(record["parsed"]),**tables,categories=categories)
            record["comparison"]=asdict(result)
            counts[result.status]+=1
            if operation=="replay":
                record["replay_run_id"]=run_id
        if apply and record!=store.value["records"][sid]:
            value=deepcopy(store.value); value["records"][sid]=record; store.save(value)
    counts["remaining"]=sum(r["phase"]=="new" for r in store.value["records"].values())
    return counts


def execute(env, *, store_file, manifest_digest, apply, operation, limit):
    from .cli import make_receipt_pipeline
    from .google_clients import drive_service,read_only_drive_service,read_only_sheets_service,download_drive_file
    from .settings import Settings
    from .sheets import SheetsDB
    if env.get("GITHUB_EVENT_NAME")!="workflow_dispatch":
        raise StateError("receipt_reimport_manual_only")
    if store_file in [env.get(name) for name in ("AMAZON_STATE_FILE_ID","AUPAY_CARD_STATE_FILE_ID",
                                               "BANK_STATE_FILE_ID","KAKEIBO_RUN_LEDGER_FILE_ID")]:
        raise StateError("receipt_result_must_be_separate")
    settings=Settings()
    service=drive_service() if apply else read_only_drive_service()
    binding=ResultBinding(env["KAKEIBO_STATE_FOLDER_ID"],store_file)
    store=ReimportStore(DriveStateTransport(service,binding),manifest_digest,settings.spreadsheet_id)
    # Metadata commitment covers owner/permissions and inherited folder sharing.
    for fid in (binding.folder_id,binding.file_id):
        meta=service.files().get(fileId=fid,fields="owners(emailAddress),permissions(type,role,emailAddress,deleted)").execute(num_retries=0)
        owner=store.value["manifest"]["owner_email"]
        sa=store.value["manifest"]["sa_email"]
        if ([x.get("emailAddress") for x in meta.get("owners",[])]!=[owner]
                or any(p.get("type")!="user" or (p.get("emailAddress"),p.get("role")) not in
                       {(owner,"owner"),(sa,"writer")} for p in meta.get("permissions",[]) if not p.get("deleted"))
                or not meta.get("permissions")):
            raise StateError("receipt_result_permissions_mismatch")
    reader=read_only_drive_service()
    def verify_source(source,folder):
        meta=reader.files().get(fileId=source["source_id"],fields="id,mimeType,parents,version,trashed").execute(num_retries=0)
        if (meta.get("trashed") or meta.get("parents")!=[folder] or meta.get("version")!=source["version"]
                or meta.get("mimeType")!=source["mime_type"]):
            raise StateError("receipt_source_version_changed")
        return meta
    def load_source(source,folder):
        meta=verify_source(source,folder)
        data=download_drive_file(source["source_id"],reader)
        after=reader.files().get(fileId=source["source_id"],fields="id,mimeType,parents,version,trashed").execute(num_retries=0)
        if after!=meta: raise StateError("receipt_source_version_changed")
        return data
    db=SheetsDB(settings.spreadsheet_id,service=read_only_sheets_service())
    return run_reimport(store,pipeline=make_receipt_pipeline(settings,db,None),db=db,load_source=load_source,verify_source=verify_source,
                        apply=apply,operation=operation,limit=limit,run_id=env.get("GITHUB_RUN_ID",""),
                        api_key_present=bool(env.get("GEMINI_API_KEY")),model=settings.gemini_model)


def main():
    import os
    import sys
    from pathlib import Path
    from .settings import service_account_source
    from .private_state_bindings import unwrap
    try:
        from .production_flow import verify_execution_boundary, REPO
        import subprocess
        env=dict(os.environ)
        head=subprocess.check_output(["git","rev-parse","HEAD"],cwd=REPO,text=True).strip()
        verify_execution_boundary(env,head)
        if len(sys.argv)!=2 or sys.argv[1] not in {"apply","preview"}:
            raise StateError("receipt_mode_invalid")
        path,info=service_account_source()
        info=info or json.loads(Path(path).read_bytes())
        file_id=unwrap("RECEIPT_REIMPORT_FILE_ID",env.get("RECEIPT_REIMPORT_FILE",""),info["private_key"])
        result=execute(env,store_file=file_id,manifest_digest=env.get("RECEIPT_REIMPORT_MANIFEST",""),
                       apply=sys.argv[1]=="apply",operation=env.get("RECEIPT_REIMPORT_OPERATION",""),
                       limit=int(env.get("RECEIPT_REIMPORT_LIMIT","1")))
        print(json.dumps(result,sort_keys=True))
    except StateError as error:
        # Our StateError constructors use only fixed operation codes.
        print(json.dumps({"failure":1,"error":str(error)}))
        raise SystemExit(1)
    except Exception:
        print(json.dumps({"failure":1,"error":"receipt_operation_failed"}))
        raise SystemExit(1)


if __name__=="__main__":
    main()
