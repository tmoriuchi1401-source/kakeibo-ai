"""Process the existing independent category approvals inside the main writer."""
from .category_rule_ui import CategoryRuleUIPipeline
from .category_backfill_ui import CategoryBackfillUIPipeline
from .category_rule_choices import checked
from .drive_run_state import StateError


class CategoryOperationFailure(StateError):
    """Report only code locations/status, never private API error bodies."""
    def __init__(self, cause):
        from pathlib import Path
        super().__init__("category_operation_failed")
        self.details={"category_exception":type(cause).__name__}
        status=getattr(getattr(cause,"resp",None),"status",None)
        if type(status) is int:self.details["category_http_status"]=status
        frame=cause.__traceback__
        while frame is not None:
            source=Path(frame.tb_frame.f_code.co_filename)
            if source.parent == Path(__file__).parent:
                self.details["category_code_location"]=source.name+":"+str(frame.tb_lineno)
            frame=frame.tb_next


def process_category_operations(db, *, apply, rule_enabled, save_enabled,
                                preview_enabled, backfill_enabled):
    # Preview is a real read-only path: even display refreshes are writes.
    rows=db.category_rule_ui_rows()
    counts={"category_registration_pending":sum(checked(row[4]) for row in rows if len(row)>4),
            "category_past_choices":sum(checked(row[5]) for row in rows if len(row)>5)}
    if not apply:
        return counts
    rules=CategoryRuleUIPipeline(db,ui_enabled=rule_enabled,save_enabled=save_enabled)
    past=CategoryBackfillUIPipeline(db,ui_enabled=preview_enabled,apply_enabled=backfill_enabled)
    rules.refresh()  # Rebind first edits; invalidate stale source approvals.
    saved=rules.apply_checked()
    past.refresh()
    previews=past.preview_checked()
    past.refresh_confirmations()
    applied=past.apply_confirmed()
    past.refresh_confirmations()
    rules.refresh()  # Show consumed decisions and registered/held states.
    counts.update(category_registration_processed=saved.get("checked",0),
                  category_previews_processed=len(previews.get("results",[])),
                  category_confirmations_processed=len(applied.get("results",[])))
    return counts


def run_category_operations(env, *, apply=False):
    from .production_flow import verify_execution_boundary
    from .google_clients import sheets_service, read_only_sheets_service
    from .sheets import SheetsDB
    verify_execution_boundary(env,env.get("GITHUB_SHA",""))
    enabled=lambda name:env.get(name,"false").lower() == "true"
    if not enabled("CATEGORY_RULE_UI_ENABLED"):
        return {"category_operations_disabled":1}
    db=SheetsDB(env.get("SPREADSHEET_ID",""),
                service=sheets_service() if apply else read_only_sheets_service())
    try:
        result=process_category_operations(db,apply=apply,rule_enabled=True,
            save_enabled=enabled("CATEGORY_RULE_SAVE_ENABLED"),
            preview_enabled=enabled("CATEGORY_BACKFILL_PREVIEW_ENABLED"),
            backfill_enabled=enabled("CATEGORY_BACKFILL_APPLY_ENABLED"))
        if apply:
            from .projection_runtime import run_projection
            result.update(run_projection(env,apply=True))
    except Exception as exc:
        raise CategoryOperationFailure(exc) from None
    return result
