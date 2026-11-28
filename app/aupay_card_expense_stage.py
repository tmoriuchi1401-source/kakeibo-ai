"""Existing AutoExpensePipeline under the existing hosted accounting stage.

No new writer/store/workflow. Manual selection is a fixed reviewed contract;
ordinary core runs discover only Google-registered ordinary card owners.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import tempfile
import time
from uuid import uuid4

from .auto_expense import AutoExpensePipeline
from .aupay_card_aliases import digest, import_row, merge_registry, resolve_aliases, validate_owner_expenses
from .aupay_card_window_preview import require


def hosted_parent_context(env):
    return (env.get("GITHUB_ACTIONS")=="true" and env.get("KAKEIBO_PRODUCTION_ENABLED")=="true"
            and env.get("WORKFLOW_REF")=="tmoriuchi1401-source/kakeibo-ai/.github/workflows/kakeibo-production.yml@refs/heads/main")


ERROR_CODES = frozenset({'card_expense_approved_plan_changed','card_expense_plan_changed',
    'card_expense_requires_review','card_expense_category_invalid','card_expense_owner_changed',
    'card_expense_native_pending','card_expense_profile_changed','card_expense_failed',
    'card_expense_exact_delta_failed','card_expense_navigation_changed','card_expense_journal_changed'})


def run_cli(db, env, *, apply):
    """Fixed child diagnostic only; never expose an exception message or rows."""
    from .aupay_card_reconciliation_diagnostics import ReconciliationDiagnostics
    diagnostics=ReconciliationDiagnostics()
    try:
        return run_from_environment(db, env, apply=apply, diagnostics=diagnostics)
    except Exception as error:
        from .production_run import failure_diagnostic
        code=str(error) if str(error) in ERROR_CODES else 'card_expense_failed'
        print(json.dumps(dict(success=False,error=code,stage='card_expense_apply' if apply
            else 'card_expense_preflight',diagnostic=diagnostics.report(error),**failure_diagnostic(error))))
        raise SystemExit(1) from None


def contract_sha256(plan):
    # A fresh import timestamp belongs to execution premises, not the purchase.
    return digest(dict(kind=plan["kind"], rows=[dict(owner=r["owner"],
        before=r["before"][:1]+r["before"][2:], expense=r["expense"])
        for r in sorted(plan["rows"]+plan["completed_rows"], key=lambda v:v["owner"])]))


def planning_profile(db, env):
    ranges=["'カテゴリ'!A:B","'カテゴリ自動分類ルール'!A:R"]
    rows=db.get_raw_ranges(ranges) if hasattr(db,'get_raw_ranges') else [db.get(r) for r in ranges]
    return dict(effective_settings_sha256=digest({k:env.get(k,'') for k in
        ('CATEGORY_RULE_AUTO_APPLY_ENABLED','KAKEIBO_AMAZON_MONEY_MODE')}),
        category_master_sha256=digest(rows[0]),category_rules_sha256=digest(rows[1]))


def selected_plan(db, ids, categories, rule_enabled, expected=None, *, expected_profile=None, env=None):
    if expected_profile is not None:
        require(digest(planning_profile(db,env))==expected_profile,'card_expense_profile_changed')
    plan=AutoExpensePipeline(db,category_rule_auto_apply_enabled=rule_enabled).plan_card_expenses(
        ids,categories_by_id=categories)
    if expected is not None:require(contract_sha256(plan)==expected,'card_expense_approved_plan_changed')
    return plan


def owner_updates(plan, registry):
    owners={v["owner"]:v for v in merge_registry(registry)}
    updates=[]
    for row in plan["rows"]:
        entry=deepcopy(owners[row["owner"]])
        require(entry["owner_row_sha256"]==digest(row["before"]), "card_expense_owner_changed")
        entry["owner_row_sha256"]=digest(row["after"])
        entry.setdefault("owner_changes",[]).append(dict(before=row["before"],after=row["after"],
            expense=row["expense"],plan_sha256=contract_sha256(plan)))
        updates.append(entry)
    merge_registry((*registry,*updates))
    return updates


def navigation_context(db):
    """Only the installed writer-owned formula and its stable-ID display inputs."""
    from collections import Counter
    from .category_navigation import installed, HEADER, WORKFLOW, return_formula, source_rows
    metadata = db._sheet_metadata()
    if not installed(metadata): return None
    require(db.get_raw("'支出明細'!N1:N1") == [[HEADER]], 'card_expense_navigation_changed')
    props = {s['properties']['title']: s['properties'] for s in metadata['sheets']}
    return dict(spreadsheet_id=db.sid, workflow_sheet_id=props[WORKFLOW]['sheetId'],
        formula=return_formula(db.sid,props[WORKFLOW]['sheetId']),
        counts=dict(Counter(identifier for _,identifier in source_rows(db.get_raw("'カテゴリ操作'!A:H")))))


def read_side_effects(env, sid):
    from .google_clients import read_only_sheets_service
    from .sheets import SheetsDB
    from .projection_store import store_from_environment
    store = store_from_environment(sid,env,read_only=True)
    keys = ('journal','money','money-reviews','money-migration','money-notices')
    return dict(navigation=navigation_context(SheetsDB(sid,service=read_only_sheets_service())),
        documents={k:store.read(k) for k in keys} if store is not None else {})


def verify_side_effects(before, after, *, appended=True):
    from .projection_store import expected_append_journal
    require(before['navigation']==after['navigation'],'card_expense_navigation_changed')
    expected=deepcopy(before['documents'])
    if appended and 'journal' in expected: expected['journal']=expected_append_journal(expected['journal'])
    require(after['documents']==expected,'card_expense_journal_changed')


def verify_delta(before, after, plan, *, navigation=None):
    expected=deepcopy(before)
    if navigation is not None:
        from .category_navigation import return_formula
        require(navigation['formula']==return_formula(navigation['spreadsheet_id'],navigation['workflow_sheet_id']),
                'card_expense_navigation_changed')
    for render in ("UNFORMATTED_VALUE", "FORMULA"):
        for row in plan["rows"]:
            require(import_row(expected[("取込データ",render)][row["row_num"]-1])==row["before"], "card_expense_owner_changed")
            changed=list(row["after"])
            while changed and changed[-1]=="": changed.pop()
            expected[("取込データ",render)][row["row_num"]-1]=changed
            expense=list(row["expense"])
            if navigation is not None:
                count=navigation['counts'].get(expense[0],0)
                require(type(count) is int and count>=0,'card_expense_navigation_changed')
                display='' if count==0 else '↩ カテゴリ操作に戻る' if count==1 else '↩ カテゴリ操作の一覧（複数候補）'
                expense.append(navigation['formula'] if render=='FORMULA' else display)
            while expense and expense[-1]=="": expense.pop()
            expected[("支出明細",render)].append(expense)
    require(after==expected,"card_expense_exact_delta_failed")


def batch_owner_links(db, rows, *, guard, diagnostics=None):
    """Existing Sheets RAW batch API, J only; one attempt, no write retry."""
    if not rows:return
    require(len({r['row_num'] for r in rows})==len(rows)
        and all(type(r['row_num']) is int and r['row_num']>=2 for r in rows),
        'card_expense_scope_forbidden')
    from .auto_expense import expense_id
    for r in rows:
        before,after=r['before'],r['after']
        require(len(before)==len(after)==12 and not before[9]
            and after[:9]+after[10:]==before[:9]+before[10:]
            and after[9]==expense_id(before[0]),'card_expense_scope_forbidden')
    if diagnostics:diagnostics.mark('owner_links_preflight')
    guard()
    ranges=[f"'取込データ'!A{r['row_num']}:L{r['row_num']}" for r in rows]
    current=db.get_raw_ranges(ranges)
    require(len(current)==len(rows) and all(len(v)==1 and import_row(v[0])==r['before']
        for r,v in zip(rows,current)),'card_expense_owner_moved_or_changed')
    guard()
    if diagnostics:
        if diagnostics.effects['financial']=='not_reached':
            diagnostics.mark('owner_links_write','financial','writer_entered')
        else:diagnostics.mark('owner_links_write')
    db.svc.spreadsheets().values().batchUpdate(spreadsheetId=db.sid,
        body=dict(valueInputOption='RAW',data=[dict(range=f"'取込データ'!J{r['row_num']}",
            values=[[r['after'][9]]]) for r in rows])).execute(num_retries=0)
    if diagnostics:diagnostics.mark('owner_links_readback')
    guard()
    current=db.get_raw_ranges(ranges)
    require(len(current)==len(rows) and all(len(v)==1 and import_row(v[0])==r['after']
        for r,v in zip(rows,current)),'card_expense_link_readback_failed')


class SelectedDB:
    """Forward only the already planned calls to the existing SheetsDB writer."""
    def __init__(self, db, plan, guard=lambda:None, diagnostics=None):
        self.db,self.plan,self.append_calls,self.link_calls,self.guard=db,plan,0,0,guard
        self.diagnostics=diagnostics
    def __getattr__(self,name): return getattr(self.db,name)
    def ensure_expense_status_column(self):
        from .sheets import HEADERS
        require(self.db.get("支出明細!A1:M1")==[HEADERS["支出明細"]],"card_expense_header_changed")
    def append(self,sheet,rows):
        if not rows:return
        require(sheet=="支出明細" and self.append_calls==0 and rows==[r["expense"] for r in self.plan["rows"]],"card_expense_scope_forbidden")
        self.append_calls+=1
        self.guard()
        if self.diagnostics:self.diagnostics.mark('before_financial_append','financial','append_possible')
        # Existing RAW writer preserves the exact ISO date/source fields and
        # cannot interpret a merchant or note as a formula.
        return self.db.append_raw(sheet,rows)
    def update_rows(self,sheet,rows):
        if not rows:return
        require(sheet=="取込データ" and self.link_calls==0 and rows==[(r["row_num"],r["after"]) for r in self.plan["rows"]],"card_expense_scope_forbidden")
        self.link_calls+=1
        batch_owner_links(self.db,self.plan['rows'],guard=self.guard,diagnostics=self.diagnostics)


def execute_selected(*, db, plan, state, durable, state_dir, registry, financial_read, guard,
                     category_rule_auto_apply_enabled=False, side_effects_read=None, diagnostics=None):
    """Normal parent auto_expense pending + card native pending dominate writes."""
    if diagnostics:diagnostics.mark('financial_preflight')
    before=financial_read()
    effects_before=side_effects_read() if side_effects_read is not None else None
    guard()
    require(durable.binding.source=="au_pay_card_gmail" and durable.binding.spreadsheet_id==db.sid,
            "card_expense_target_changed")
    require(AutoExpensePipeline(db,category_rule_auto_apply_enabled=category_rule_auto_apply_enabled).plan_card_expenses(
        plan["import_ids"],categories_by_id=plan["categories_by_id"])==plan,"card_expense_plan_changed")
    if not plan["rows"]: return dict(expenses_created=0,expenses_updated=0,imports_updated=0)
    require(durable.inspect()["phase"]=="ready","card_expense_native_pending")
    state.require_no_unknown_write()
    updates=owner_updates(plan,registry)
    if diagnostics:diagnostics.mark('native_save','native_reservation','write_possible')
    durable.begin(uuid4().hex)
    guard()
    require(durable.transport.read()==durable._pending,"card_expense_native_changed")
    if diagnostics:diagnostics.mark('native_independent_readback','native_reservation','readback_confirmed')
    require(financial_read()==before,"card_expense_financial_changed")
    if effects_before is not None:require(side_effects_read()==effects_before,'card_expense_journal_changed')
    bounded=SelectedDB(db,plan,guard,diagnostics)
    result=AutoExpensePipeline(bounded,category_rule_auto_apply_enabled=category_rule_auto_apply_enabled).apply(card_plan=plan)
    guard()
    require(durable.transport.read()==durable._pending,"card_expense_native_changed")
    if diagnostics:diagnostics.mark('financial_readback')
    after=financial_read();verify_delta(before,after,plan,navigation=effects_before['navigation'] if effects_before else None)
    if effects_before is not None:verify_side_effects(effects_before,side_effects_read())
    if diagnostics:diagnostics.mark('financial_readback','financial','exact_readback_confirmed')
    require(result["expenses_created"]==len(plan["rows"]) and result["expenses_updated"]==0
            and result["imports_updated"]==len(plan["rows"]) and bounded.append_calls==bounded.link_calls==1,
            "card_expense_write_unknown")
    resolve_aliases([],db.get("取込データ!A2:L"),merge_registry((*registry,*updates)))
    validate_owner_expenses(merge_registry((*registry,*updates)),db.get("支出明細!A2:M"))
    cp=state.successful_window_end().isoformat()
    state.record(dict(run_id="CARD-EXPENSE:"+uuid4().hex,status="noop",source_window_start=cp,source_window_end=cp,
        card_aliases=updates,expense_contract_sha256=contract_sha256(plan),expense_rows=len(plan["rows"]),
        expense_yen=plan["expense_yen"],financial_before_sha256=digest({str(k):v for k,v in before.items()}),
        financial_after_sha256=digest({str(k):v for k,v in after.items()}),failure=0),advance_checkpoint=False)
    guard();verify_delta(before,financial_read(),plan,navigation=effects_before['navigation'] if effects_before else None)
    if effects_before is not None:verify_side_effects(effects_before,side_effects_read())
    if diagnostics:diagnostics.mark('checkpoint_commit','checkpoint','write_possible')
    durable.commit(state_dir)
    require(durable.transport.read()==durable._loaded,"card_expense_native_readback_failed")
    if diagnostics:diagnostics.mark('complete','checkpoint','readback_confirmed')
    return dict(result,write_requests=2)


def run_from_environment(db, env, *, apply, diagnostics=None):
    from .drive_run_state import DurableState,DriveStateTransport,StateBinding,validate
    from .production_ledger import validate_ledger
    from .production_flow import verify_execution_boundary
    from .google_clients import drive_service,read_only_drive_service,read_only_sheets_service
    from .aupay_card_recurring import SqliteRecurringRunState
    from .aupay_card_first_window_apply import financial_data
    from .aupay_card_reservation_recovery import summaries
    require(hosted_parent_context(env),"card_expense_parent_boundary_required")
    head=env.get("GITHUB_SHA","");verify_execution_boundary(env,head)
    deadline=time.monotonic()+900
    rule_enabled=env.get("CATEGORY_RULE_AUTO_APPLY_ENABLED")=="true"
    request=json.loads(env.get("CARD_EXPENSE_REQUEST") or "null")
    if request is not None:
        require(env.get("GITHUB_EVENT_NAME")=="workflow_dispatch" and env.get("MANUAL_SCOPE")=="card_expenses"
                and env.get("GITHUB_ACTOR")==env.get("GITHUB_TRIGGERING_ACTOR")=="tmoriuchi1401-source"
                and set(request)=={"imports","plan_sha256","profile_sha256"},"card_expense_owner_approval_required")
        require(len(env["CARD_EXPENSE_REQUEST"])<=60000,"card_expense_request_too_large")
    from .aupay_card_aliases import LiteralRows
    db=LiteralRows(db)
    b=StateBinding("au_pay_card_gmail",db.sid,env["KAKEIBO_STATE_FOLDER_ID"],env["AUPAY_CARD_STATE_FILE_ID"])
    pb=StateBinding("production_run",db.sid,b.folder_id,env["KAKEIBO_RUN_LEDGER_FILE_ID"])
    ro=read_only_drive_service();transport=DriveStateTransport(ro,b)
    raw=transport.read();native=validate(raw,b)
    require(native["phase"]=="ready" and not native["attempt"],"card_expense_native_pending")
    registry=merge_registry(e for s in summaries(native).values() if s.get("status") in {"complete","noop"} for e in s.get("card_aliases",[]))
    resolve_aliases([],db.get("取込データ!A2:L"),registry);validate_owner_expenses(registry,db.get("支出明細!A2:M"))
    if request is None:
        # No scan of unregistered legacy auto_expense rows acquires authority.
        owners={v["owner"] for v in registry}
        ids=[r[0] for r in db.get("取込データ!A2:L") if r and r[0] in owners
             and len(r)>8 and r[8]=="auto_expense" and not (len(r)>9 and r[9])]
        categories={}
    else:
        require(isinstance(request["imports"],list) and 0<len(request["imports"])<=100
                and all(set(v)=={"id","category"} for v in request["imports"]),"card_expense_selection_invalid")
        from .private_state_bindings import unwrap
        key=json.loads(env["GOOGLE_SERVICE_ACCOUNT_JSON"])["private_key"]
        decoded=[(unwrap("CARD_EXPENSE_IMPORT_ID",v["id"],key),v["category"]) for v in request["imports"]]
        ids=[i for i,_ in decoded];categories=dict(decoded)
        require(set(ids).issubset({v["owner"] for v in registry}),"card_expense_owner_unregistered")
    if not ids:
        pipeline=AutoExpensePipeline(db,category_rule_auto_apply_enabled=rule_enabled)
        return (pipeline.apply() if apply else pipeline.preview()) if request is None else dict(expenses_created=0,expenses_updated=0,imports_updated=0)
    profile=planning_profile(db,env) if request is not None else None
    plan=selected_plan(db,ids,categories,rule_enabled,request['plan_sha256'] if request is not None else None,
        expected_profile=request['profile_sha256'] if request is not None else None,env=env)
    if request is not None:require(planning_profile(db,env)==profile,'card_expense_profile_changed')
    if not apply:
        effects=read_side_effects(env,db.sid)
        if plan['rows']:
            from .projection_store import expected_append_journal
            if 'journal' in effects['documents']:expected_append_journal(effects['documents']['journal'])
        return dict(expense_rows=plan["expense_rows"],expense_yen=plan["expense_yen"],plan_sha256=contract_sha256(plan),
            profile_sha256=digest(profile),planning_profile=profile,side_effects_sha256=digest(effects))
    pending_profile_checked=False
    def guard():
        nonlocal pending_profile_checked
        require(time.monotonic()<deadline,"card_expense_timeout")
        verify_execution_boundary(env,head)
        parent=validate_ledger(DriveStateTransport(ro,pb).read(),pb)
        require(parent["sources"]["auto_expense"]["phase"]=="pending"
                and all(v["phase"]=="ready" and not v["attempt"] and not v["error"]
                        for k,v in parent["sources"].items() if k!="auto_expense"),"card_expense_parent_changed")
        if profile is not None and durable._pending is not None and not pending_profile_checked:
            require(planning_profile(db,env)==profile,'card_expense_profile_changed')
            pending_profile_checked=True
    with tempfile.TemporaryDirectory(prefix="card-expense-",dir=env.get("RUNNER_TEMP")) as directory:
        path=Path(directory)/"state"
        durable=DurableState(DriveStateTransport(drive_service(),b),b)
        require(durable.transport.read()==raw,"card_expense_native_changed")
        durable.restore(path)
        state=SqliteRecurringRunState(path/"recurring.sqlite3",repo_root=Path(__file__).resolve().parents[1])
        from .aupay_card_incremental_recovery import preserved_unknown_proofs
        parent=validate_ledger(DriveStateTransport(ro,pb).read(),pb)
        preserved_unknown_proofs(native,parent,state.successful_window_end().isoformat())
        result=execute_selected(db=db,plan=plan,state=state,durable=durable,state_dir=path,registry=registry,
            financial_read=lambda:financial_data(read_only_sheets_service(),db.sid,include_controls=True),guard=guard,
            category_rule_auto_apply_enabled=rule_enabled,side_effects_read=lambda:read_side_effects(env,db.sid),diagnostics=diagnostics)
    # Routine core remains autonomous under its existing authority; the fixed
    # maintenance scope never runs unrelated AutoExpensePipeline candidates.
    if request is None:
        ordinary=AutoExpensePipeline(db,category_rule_auto_apply_enabled=rule_enabled).apply()
        return {k:result.get(k,0)+ordinary.get(k,0) for k in set(result)|set(ordinary)}
    return result
