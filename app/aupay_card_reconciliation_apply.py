"""Approved pending-to-pending card reconciliation using the existing batch writer.

No release, financial transport, DB, retry or rollback is introduced here.
The workflow's one-dispatch fence and kakeibo-production lock are mandatory.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from uuid import UUID

from .aupay_card_aliases import confirmed_alias_updates, merge_registry
from .aupay_card_apply_plan import build_canonical_apply_plan, validate_canonical_apply_plan
from .aupay_card_batch import (
    ProtectedBatchApprovalProvider, SealedSheetsBatchTransport, SqliteBatchCapabilityStore,
    SqliteBatchJournal, SqliteBatchManifestStore, create_batch_manifest, create_batch_projection,
    execute_production_batch_once, issue_batch_capability,
)
from .aupay_card_first_window_apply import ImportOnlySheets, verify_financial
from .aupay_card_incremental_recovery import _hash, _rows, preserved_unknown_proofs
from .aupay_card_production import SqliteLeaseManager
from .aupay_card_recurring import SqliteRecurringRunState, _batch_reader, _classify_candidates
from .aupay_card_window_preview import require
from .aupay_card_writer import FixedSourceWindow, ReadOnlySheetsTargetInspector, TargetBinding
from .drive_run_state import CARD_FILES, DurableState, envelope, snapshot, validate
from .production_ledger import ProductionLedger, validate_ledger
from .production_run import COUNT_KEYS
from .aupay_card_reconciliation_diagnostics import ReconciliationDiagnostics
from .transaction_plan import reconcile_transactions


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def financial_digest(tables):
    return sha(encode({title + "|" + render: rows for (title, render), rows in tables.items()}))


def read_financial_and_review(service,sid):
    """Existing financial reader plus the existing ledger review/category sheets."""
    from .aupay_card_first_window_apply import financial_data
    result=financial_data(service,sid)
    api=service.spreadsheets()
    meta=api.get(spreadsheetId=sid,fields="spreadsheetId,sheets(properties)").execute(num_retries=0)
    titles=[s["properties"]["title"] for s in meta["sheets"] if s["properties"]["title"] in {"要確認","カテゴリ"}]
    require(meta["spreadsheetId"]==sid and set(titles)=={"要確認","カテゴリ"},"repair_review_sheet_missing")
    for render in ("UNFORMATTED_VALUE","FORMULA"):
        response=api.values().batchGet(spreadsheetId=sid,ranges=["'"+t+"'" for t in titles],
            valueRenderOption=render,dateTimeRenderOption="SERIAL_NUMBER").execute(num_retries=0)
        require(response.get("spreadsheetId")==sid and len(response.get("valueRanges",[]))==len(titles),
                "repair_review_read_incomplete")
        for title,item in zip(titles,response["valueRanges"]):
            require(any(item.get("range","").startswith(p+"!") for p in (title,"'"+title+"'")),"repair_review_range_changed")
            rows=deepcopy(item.get("values",[]))
            for row in rows:
                while row and row[-1]=="":row.pop()
            while rows and not rows[-1]:rows.pop()
            result[(title,render)]=rows
    return result


def prepare_reconciliation(record, db, state, key):
    """Project the cached, previously reconciled sources against fresh imports."""
    cp, end = map(datetime.fromisoformat, (record["checkpoint_before"], record["checkpoint_after"]))
    require(state.successful_window_end() == cp and end > cp, "repair_checkpoint_changed")
    start = cp - timedelta(days=2)
    require((end-start).total_seconds() <= 14*86400, "repair_window_exceeds_authority")
    query = ('in:anywhere from:kddi-fs.com subject:"【ご利用詳細】au PAY カード" '
             f'after:{int(start.timestamp())} before:{int(end.timestamp())}')
    window = FixedSourceWindow(start,end,"Asia/Tokyo",query)
    window.validate()
    existing = deepcopy(db.get("取込データ!A2:L"))
    registry = state.alias_registry()
    source = record["source"]
    rec = reconcile_transactions(source["transactions"],existing,alias_registry=registry)
    plan = validate_canonical_apply_plan(build_canonical_apply_plan(source["collection"],rec))
    statuses,reviews = _classify_candidates(plan,db)
    require(not reviews and statuses == record["statuses"] and statuses
            and not (plan.withheld_return_count or plan.withheld_ambiguous_count
                     or plan.withheld_review_count or plan.invalid_item_count or plan.global_withheld_count),
            "repair_candidates_changed")
    projection = create_batch_projection(plan,candidate_identities=tuple(record["candidate_identities"]),
        statuses=statuses,binding=TargetBinding(expected_spreadsheet_id=db.sid),audit_key=key)
    require(projection.batch_size == record["import_count"]
            and sum(c.amount_yen for c in projection.plan.candidates) == record["import_yen"]
            and projection.target_ref == record["historical_target_ref"]
            and key.key_id == record["audit_key_id"], "repair_approved_projection_changed")
    return dict(window=window,existing=existing,registry=registry,reconciliation=rec,projection=projection)


def _pending_cache(native, destination, repo_root):
    """Read-only materialization for explicit reconciliation; normal restore stays denied."""
    destination=Path(destination).resolve()
    root=Path(repo_root).resolve()
    require(not destination.is_relative_to(root) and not destination.exists(), "repair_cache_invalid")
    destination.mkdir(parents=True)
    for name,item in native["files"].items():
        require(name in CARD_FILES, "repair_native_file_forbidden")
        (destination/name).write_bytes(base64.b64decode(item["data"],validate=True))


def execute_reconciliation(*,record,record_sha256,run_id,db,durable,ledger,key_provider,
        approval_provider,state_dir,repo_root,native_read,parent_read,financial_read,
        documents_check,source_check,guard,clock=lambda:datetime.now(timezone.utc),
        boundary=lambda _stage:None,diagnostics=None):
    """One approved dispatch. Any uncertainty consumes it and leaves no replay path."""
    diagnostics = diagnostics or ReconciliationDiagnostics()
    diagnostics.mark("reconciliation_preflight")
    require(type(durable) is DurableState and type(ledger) is ProductionLedger
            and type(approval_provider) is ProtectedBatchApprovalProvider,
            "repair_existing_authority_required")
    require(sha(encode(record)) == record_sha256, "repair_record_changed")
    attempt=UUID(run_id).hex
    guard()
    old_raw=native_read(); old_parent_raw=parent_read()
    old=validate(old_raw,durable.binding); old_parent=validate_ledger(old_parent_raw,ledger.binding)
    require(sha(old_raw)==record["native_before_sha256"]
            and sha(old_parent_raw)==record["parent_before_sha256"]
            and old["phase"]==old_parent["sources"]["aupay_card"]["phase"]=="pending"
            and attempt not in {old["attempt"],old_parent["sources"]["aupay_card"]["attempt"]}
            and all(v["phase"]=="ready" and not v["attempt"] and not v["error"]
                    for k,v in old_parent["sources"].items() if k!="aupay_card"), "repair_state_changed")
    require(ledger.payload==old_parent_raw, "repair_parent_changed")
    diagnostics.mark("financial_preflight")
    before=financial_read()
    require(financial_digest(before)==record["financial_before_sha256"], "repair_financial_changed")
    diagnostics.mark("source_preflight")
    documents_check();source_check()
    _pending_cache(old,state_dir,repo_root)
    path=Path(state_dir)
    state=SqliteRecurringRunState(path/"recurring.sqlite3",repo_root=repo_root)
    key=key_provider.load()
    prepared=prepare_reconciliation(record,db,state,key)
    projection=prepared["projection"]
    binding=TargetBinding(expected_spreadsheet_id=db.sid)
    manifest=create_batch_manifest(projection,source_window=prepared["window"],created_at=clock(),
        run_id=run_id,audit_key=key,binding=binding)
    manifests=SqliteBatchManifestStore(path/"manifests.sqlite3",repo_root=repo_root)
    capabilities=SqliteBatchCapabilityStore(path/"capabilities.sqlite3",repo_root=repo_root)
    journal=SqliteBatchJournal(path/"journal.sqlite3",repo_root=repo_root)
    leases=SqliteLeaseManager(path/"leases.sqlite3",repo_root=repo_root,clock=clock)
    manifests.save(manifest)
    diagnostics.mark("capability_issue")
    capability=issue_batch_capability(projection,manifest,binding=binding,
        inspector=ReadOnlySheetsTargetInspector(db),key_provider=key_provider,
        approval_provider=approval_provider,store=capabilities,now=clock())
    diagnostics.mark("archive_prepare")
    archive_id=f'UNKNOWN:{record["old_failure"]["id"]}:{record["old_failure"]["run_attempt"]}'
    archive=dict(run_id=archive_id,status="UNKNOWN_NO_REPLAY",replay_allowed=False,
        archive_schema=2,native_before_raw_b64=base64.b64encode(old_raw).decode(),
        native_before_bytes_sha256=sha(old_raw),parent_before_raw_b64=base64.b64encode(old_parent_raw).decode(),
        parent_before_bytes_sha256=sha(old_parent_raw),
        source_window_start=prepared["window"].start.isoformat(),source_window_end=record["checkpoint_after"],
        native_before=old,native_sha256=_hash(old),parent_card_before=old_parent["sources"]["aupay_card"],
        parent_card_sha256=_hash(old_parent["sources"]["aupay_card"]),
        checkpoint_before=record["checkpoint_before"],checkpoint_after=record["checkpoint_after"],
        github_failure=record["old_failure"],repair_run_id=run_id,evidence_sha256=record_sha256)
    state.record(archive,advance_checkpoint=False)
    state.record(dict(run_id="RESERVATION:"+run_id,status="REPAIR_RESERVED",
        source_window_start=archive["source_window_start"],source_window_end=archive["source_window_end"],
        record_sha256=record_sha256,batch_ref=projection.batch_ref,manifest_sha256=sha(manifests._serialize(manifest).encode()),
        capability_id=capability.capability_id,replay_allowed=False),advance_checkpoint=False)
    boundary("archive_and_reservation_local")
    pending=envelope(durable.binding,snapshot(path,durable.binding),generation=old["generation"]+1,
                     phase="pending",attempt=attempt)
    diagnostics.mark("native_guard")
    guard()
    diagnostics.mark("native_save")
    durable._replace(old_raw,pending,observe=lambda event: diagnostics.state_save("native",event))
    diagnostics.mark("native_independent_readback")
    require(native_read()==pending, "repair_reservation_independent_readback")
    durable._pending=pending
    diagnostics.mark("native_reserved", "native_reservation", "readback_confirmed")
    boundary("native_reserved")
    proposed_parent=deepcopy(old_parent)
    proposed_parent["sources"]["aupay_card"].update(phase="pending",attempt=attempt,error="")
    diagnostics.mark("parent_guard")
    guard()
    diagnostics.mark("parent_save")
    ledger._save(proposed_parent,observe=lambda event: diagnostics.state_save("parent",event))
    diagnostics.mark("parent_independent_readback")
    require(parent_read()==ledger.payload, "repair_parent_reservation_independent_readback")
    diagnostics.mark("parent_reserved", "parent_reservation", "readback_confirmed")
    boundary("parent_reserved")

    def pending_guard():
        guard()
        require(native_read()==durable._pending and parent_read()==ledger.payload,
                "repair_reservation_changed")
        documents_check()

    def predispatch():
        diagnostics.mark("financial_guard")
        pending_guard();source_check()
        require(prepare_reconciliation(record,db,state,key)["projection"]==projection,
                "repair_projection_changed_before_dispatch")
        verify_financial(before,financial_read(),[])
        diagnostics.mark("before_financial_append", "financial", "append_possible")
        boundary("before_financial_append")

    diagnostics.mark("writer_prepare")
    service=db.svc
    bounded=ImportOnlySheets(service,db.sid,predispatch,projection=projection)
    db.svc=bounded
    execution=None
    try:
        transport=SealedSheetsBatchTransport(db,projection=projection,manifest=manifest,capability=capability,
            store=capabilities,journal=journal,binding=binding,inspector=ReadOnlySheetsTargetInspector(db),
            key_provider=key_provider,clock=clock)
        diagnostics.mark("writer_execute", "financial", "writer_entered")
        execution=execute_production_batch_once(projection,manifest,capability=capability,store=capabilities,
            journal=journal,leases=leases,reader=_batch_reader(db),transport=transport,binding=binding,
            inspector=ReadOnlySheetsTargetInspector(db),key_provider=key_provider,owner_id="repair-"+run_id,
            readback_attempts=1,readback_delays=(),clock=clock)
    finally:
        db.svc=service
    diagnostics.mark("financial_attempt_finished")
    boundary("financial_attempt_finished")
    pending_guard()
    history=journal.history(run_id,execution.attempt_id)
    acknowledged=any(h[:2]==("write_result","acknowledged") for h in history)
    exact=execution.exact and acknowledged and execution.write_request_count==bounded.calls==1
    if not exact:
        state.record(dict(run_id="OUTCOME:"+run_id,status="REPAIR_UNKNOWN_NO_REPLAY",
            source_window_start=archive["source_window_start"],source_window_end=archive["source_window_end"],
            replay_allowed=False,write_requests=execution.write_request_count),advance_checkpoint=False)
    outcome=envelope(durable.binding,snapshot(path,durable.binding),
        generation=validate(durable._pending,durable.binding)["generation"]+1,phase="pending",attempt=attempt)
    diagnostics.mark("outcome_save")
    durable._replace(durable._pending,outcome)
    diagnostics.mark("outcome_independent_readback")
    require(native_read()==outcome,"repair_outcome_independent_readback")
    durable._pending=outcome
    diagnostics.mark("outcome_persisted")
    boundary("outcome_persisted")
    require(exact,"repair_unknown_no_replay")
    pending_guard()
    diagnostics.mark("financial_readback")
    verify_financial(before,financial_read(),bounded.rows)
    documents_check()
    diagnostics.mark("financial_readback", "financial", "exact_readback_confirmed")
    boundary("exact_financial_readback")
    result=dict(run_id=run_id,status="complete",source_window_start=archive["source_window_start"],
        source_window_end=archive["source_window_end"],written=projection.batch_size,new_eligible=projection.batch_size,
        duplicate=prepared["reconciliation"]["summary"]["existing_identity_duplicates"],
        failure=0,write_requests=1,manifest_final="persisted",capability_final="sealed",journal_final="confirmed",
        lease_final="released",money_review=0)
    result["card_aliases"]=confirmed_alias_updates(record["source"]["transactions"],prepared["reconciliation"],
        db.get("取込データ!A2:L"),prepared["registry"],set(record["statuses"])|{
            i for item in prepared["reconciliation"]["transactions"] for i in item.existing_source_identities},
        expected_rows=bounded.rows)
    evidence=dict(files=snapshot(path,durable.binding))
    cap=next(r for r in _rows(evidence,"capabilities.sqlite3","batch_capabilities") if r["run_id"]==run_id)
    events=sorted((r for r in _rows(evidence,"journal.sqlite3","batch_events") if r["run_id"]==run_id),key=lambda r:r["seq"])
    result["checkpoint_reconciliation"]=dict(archive_id=archive_id,evidence_sha256=record_sha256,
        aliases_sha256=_hash(list(merge_registry(result["card_aliases"]))),
        manifest_sha256=sha(manifests._serialize(manifest).encode()),capability_sha256=_hash(cap),journal_sha256=_hash(events))
    diagnostics.mark("checkpoint_proof")
    state.record(result,advance_checkpoint=True)
    # Validate the proposed completion before either real ready transition.
    proposed_native=validate(envelope(durable.binding,snapshot(path,durable.binding),
        generation=validate(outcome,durable.binding)["generation"]+1),durable.binding)
    ready_parent=deepcopy(ledger.value)
    ready_parent["sources"]["aupay_card"].update(phase="ready",attempt="",error="")
    preserved_unknown_proofs(proposed_native,ready_parent,record["checkpoint_after"])
    diagnostics.mark("checkpoint_guard")
    pending_guard();verify_financial(before,financial_read(),bounded.rows)
    boundary("before_checkpoint_commit")
    diagnostics.mark("checkpoint_commit", "checkpoint", "write_possible")
    durable.commit(path)
    diagnostics.mark("checkpoint_independent_readback")
    require(native_read()==durable._loaded,"repair_checkpoint_independent_readback")
    diagnostics.mark("checkpoint_committed", "checkpoint", "readback_confirmed")
    boundary("checkpoint_committed")
    guard();documents_check();verify_financial(before,financial_read(),bounded.rows)
    diagnostics.mark("parent_completion", "parent_completion", "write_possible")
    ledger.complete("aupay_card",result,0)
    diagnostics.mark("parent_completion_readback")
    require(parent_read()==ledger.payload,"repair_parent_completion_independent_readback")
    diagnostics.mark("parent_completed", "parent_completion", "readback_confirmed")
    boundary("parent_completed")
    diagnostics.mark("final_readback")
    final_native_raw,final_parent_raw=native_read(),parent_read()
    require(final_native_raw==durable._loaded and final_parent_raw==ledger.payload,"repair_final_state_changed")
    final_native=validate(final_native_raw,durable.binding)
    final_parent=validate_ledger(final_parent_raw,ledger.binding)
    preserved_unknown_proofs(final_native,final_parent,record["checkpoint_after"])
    require(all(final_parent["sources"][k]==v for k,v in old_parent["sources"].items() if k!="aupay_card"),
            "repair_other_source_changed")
    documents_check();final_financial=financial_read()
    verify_financial(before,final_financial,bounded.rows)
    diagnostics.mark("complete")
    return dict(success=True,written=projection.batch_size,import_yen=record["import_yen"],
        expense_rows_written=0,checkpoint=record["checkpoint_after"],old_outcome="UNKNOWN_NO_REPLAY",automatic_retry=False,
        native_sha256=sha(final_native_raw),parent_sha256=sha(final_parent_raw),financial_sha256=financial_digest(final_financial),
        evidence_sha256=record_sha256,acknowledged=True,sealed_capability=True,independent_readback=True,
        alias_owners=len(result["card_aliases"]),alias_sources=sum(len(e["aliases"]) for e in result["card_aliases"]),
        unposted_ordinary_yen=sum(c.amount_yen for c in projection.plan.candidates if record["statuses"][c.identity]=="auto_expense"))
