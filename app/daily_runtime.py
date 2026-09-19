"""Daily display binding for the existing validated Actions writer."""
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .monthly_projection import ProjectionError


def daily_from_environment(source_db,store,env=None,*,read_only=False):
    from .private_state_bindings import unwrap
    from .settings import service_account_source
    from .sheets import SheetsDB
    from .daily_sheets import DailySheets
    env=os.environ if env is None else env
    encrypted=env.get("KAKEIBO_DAILY_SPREADSHEET_ID","")
    if not encrypted:return None
    if store is None:raise ProjectionError("daily_projection_binding_required")
    if (env.get("GITHUB_ACTIONS")!="true" or env.get("GITHUB_REF")!="refs/heads/main"
            or env.get("GITHUB_REPOSITORY")!="tmoriuchi1401-source/kakeibo-ai"
            or not env.get("KAKEIBO_VALIDATED_MAIN_SHA")
            or env.get("GITHUB_SHA")!=env.get("KAKEIBO_VALIDATED_MAIN_SHA")):
        raise ProjectionError("daily_validated_main_required")
    path,info=service_account_source()
    info=info or json.loads(Path(path).read_bytes())
    sid=unwrap("KAKEIBO_DAILY_SPREADSHEET_ID",encrypted,info["private_key"])
    if sid==source_db.sid:raise ProjectionError("daily_copy_required")
    # Compare permission actors before copying financial output into the view.
    source=store.service.files().get(fileId=source_db.sid,supportsAllDrives=True,
        fields="permissions(id,type)").execute(num_retries=0)
    target=store.service.files().get(fileId=sid,supportsAllDrives=True,
        fields="mimeType,trashed,permissions(id,type)").execute(num_retries=0)
    allowed={p["id"] for p in source.get("permissions",[]) if p.get("type") in {"user","group"}}
    grants=target.get("permissions",[])
    if (target.get("mimeType")!="application/vnd.google-apps.spreadsheet" or target.get("trashed")
            or not grants or any(p.get("type") not in {"user","group"} or p.get("id") not in allowed for p in grants)):
        raise ProjectionError("daily_sharing_mismatch")
    if read_only:
        from .google_clients import read_only_sheets_service
        return DailySheets(SheetsDB(sid,service=read_only_sheets_service()),source_db.sid,store)
    return DailySheets(SheetsDB(sid),source_db.sid,store)


def run_daily_requests(env,*,apply=False):
    """Independent fixed-ID inbox stage inside the existing production lock.

    It needs no native source checkpoint and does not loosen their pending
    gates. Inbox intents and the projection journal make this stage replayable.
    Display-only scope never calls it.
    """
    mode=env.get("KAKEIBO_DAILY_CORRECTIONS_MODE","")
    if not mode:return {}
    if mode!="fixed-id-v1":raise ProjectionError("daily_correction_mode_invalid")
    from .production_flow import verify_execution_boundary
    verify_execution_boundary(env,env.get("GITHUB_SHA",""))
    from .projection_store import store_from_environment
    from .sheets import SheetsDB
    from .google_clients import sheets_service, read_only_sheets_service
    from .settings import service_account_source
    from .daily_edit_cutover import verify_cutover
    from .daily_corrections import DailyCorrections
    from .monthly_projection_sheets import SheetsLedgerReader
    from .projection_refresh import ProjectionRefresh
    sid=env.get("SPREADSHEET_ID","")
    store=store_from_environment(sid,env)
    if store is None:raise ProjectionError("daily_projection_binding_required")
    source=SheetsDB(sid,service=(sheets_service() if apply else read_only_sheets_service()))
    daily=daily_from_environment(source,store,env,read_only=not apply)
    if daily is None:raise ProjectionError("daily_edit_binding_required")
    metadata=source._execute_sheet_read(lambda:source.svc.spreadsheets().get(
        spreadsheetId=sid,fields="sheets(properties,protectedRanges,developerMetadata),developerMetadata"))
    path,info=service_account_source()
    info=info or json.loads(Path(path).read_bytes())
    verify_cutover(metadata,daily.db.sid,info["client_email"])
    daily.verify()
    inbox=DailyCorrections(store,source)
    before=inbox._requests()["requests"]
    pending=sum(item["state"] in {"queued","pending"} for item in before.values())
    from .daily_money_review import run_money_reviews
    from .daily_coverage import run_coverage
    current_month=datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m")
    if not apply:
        return {"corrections_pending":pending,"correction_form_ready":int(daily.form()[0][7] is True),
                **run_money_reviews(daily,source,env,apply=False),
                **run_coverage(daily,apply=False,current_month=current_month)}
    projection=ProjectionRefresh(store,SheetsLedgerReader(source))
    # Refresh indexes for prior ledger writes before resolving any target ID.
    projection.refresh(source.categories())
    inbox.apply_pending(limit=20)
    remaining=sum(item["state"] in {"queued","pending"} for item in inbox._requests()["requests"].values())
    submitted=daily.submit(source) if not remaining else {"corrections_submitted":0}
    money_counts=run_money_reviews(daily,source,env,apply=True)
    projection.refresh(source.categories())
    # A failure here does not lose the intent or trigger a second ledger write.
    coverage_counts=run_coverage(daily,apply=True,current_month=current_month)
    output=refresh_daily(source,store,env)
    after=inbox._requests()["requests"]
    counts={"corrections_submitted":submitted.get("corrections_submitted",0),
        "correction_input_changed":submitted.get("correction_input_changed",0),
        "corrections_pending":sum(item["state"] in {"queued","pending"} for item in after.values())}
    for state in ("applied","failed"):
        counts["corrections_"+state]=sum(item["state"]==state and before.get(key,{}).get("state")!=state
                                       for key,item in after.items())
    return {**counts,**money_counts,**coverage_counts,**output}


def refresh_daily(source_db,store,env=None):
    from .daily_sheets import read_existing_reviews
    from .amazon_money_runtime import money_review_items
    daily=daily_from_environment(source_db,store,env)
    if daily is None:return {}
    props=[s["properties"] for s in source_db._sheet_metadata().get("sheets",[])]
    ledger=next((p for p in props if p["title"]=="支出明細"),None)
    if ledger is None:raise ProjectionError("daily_ledger_missing")
    now=datetime.now(ZoneInfo("Asia/Tokyo"))
    return daily.refresh(current_month=now.strftime("%Y-%m"),updated_at=now.strftime("%Y-%m-%d %H:%M"),
        reviews=read_existing_reviews(source_db)+money_review_items(store),ledger_sheet_id=ledger["sheetId"])
