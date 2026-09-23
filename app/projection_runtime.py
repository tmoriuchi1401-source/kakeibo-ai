"""Existing Actions projection-only scope; never invokes intake/OCR/accounting."""
from .monthly_projection import ProjectionError
from .monthly_projection_sheets import SheetsLedgerReader
from .projection_refresh import ProjectionRefresh
from .projection_store import ProjectionJournal, store_from_environment


def run_projection(env, *, apply=False, bootstrap=False, read_pacer=None):
    from .google_clients import read_only_sheets_service
    from .sheets import SheetsDB
    if bootstrap and not apply:
        raise ProjectionError("projection_bootstrap_requires_apply")
    sid=env.get("SPREADSHEET_ID","")
    store=store_from_environment(sid,env)
    if store is None:
        raise ProjectionError("projection_binding_missing")
    # This scope always has read-only ledger credentials, including bootstrap.
    db=SheetsDB(sid,service=read_only_sheets_service())
    if read_pacer is not None:
        db._read_pacer=read_pacer
        db._read_retry_base=20
    reader=SheetsLedgerReader(db)
    refresh=ProjectionRefresh(store,reader)
    if not apply:
        journal=ProjectionJournal(store).read()
        return {"projection_pending_ranges":len(journal["ranges"]),
                "projection_pending_months":len(journal["months"]),
                "projection_pending_append":int(journal["append"])}
    result=(refresh.bootstrap(db.categories()) if bootstrap else refresh.refresh(db.categories()))
    from .daily_runtime import refresh_daily
    result.update(refresh_daily(db,store,env))
    return {**result,"projection_sheet_requests":reader.metrics.requests,
            "projection_sheet_cells":reader.metrics.returned_cells,
            "projection_drive_reads":store.metrics["reads"],"projection_drive_writes":store.metrics["writes"]}
