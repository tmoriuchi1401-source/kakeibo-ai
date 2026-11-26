"""New execution-boundary regression only; existing 319-case run is reused."""
import base64
from copy import deepcopy
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from app import aupay_card_reconciliation_apply as repair
from app.aupay_card_apply_plan import build_canonical_apply_plan
from app.aupay_card_batch import (
    ProtectedBatchApprovalProvider, SqliteBatchManifestStore, SqliteBatchCapabilityStore, SqliteBatchJournal,
    create_batch_projection,
)
from app.aupay_card_production import ProtectedAuditKeyProvider, SqliteLeaseManager
from app.aupay_card_recurring import SqliteRecurringRunState
from app.aupay_card_window_preview import checkpoint
from app.aupay_card_writer import TargetBinding
from app.canonical_import import materialize_import_row
from app.drive_run_state import DurableState, StateBinding, envelope, snapshot, validate
from app.production_ledger import ProductionLedger, initial_ledger, validate_ledger, _encode
from app.transaction_plan import reconcile_transactions
from test_aupay_card_batch import DB


C0="2026-11-03T14:45:43+09:00"
C1="2026-11-04T21:00:33+09:00"
NOW=datetime.fromisoformat("2026-11-27T06:00:00+09:00")
RID="c8f1a0d3-483b-4d56-a962-2acf8e94de99"


class Transport:
    def __init__(self,payload): self.payload,self.writes=payload,[];self.mode="normal"
    def read(self): return self.payload
    def write(self,payload):
        self.payload=payload;self.writes.append(payload)
        if self.mode=="unknown":raise TimeoutError("synthetic state delivery unknown")


def case(tmp_path,mode="normal"):
    repo=tmp_path/"repo";repo.mkdir(parents=True)
    initial=tmp_path/"initial";initial.mkdir()
    keyfile=tmp_path/"key.json"
    keyfile.write_text(json.dumps(dict(key_id="synthetic-repair",key_b64=base64.b64encode(b"k"*32).decode())))
    key=ProtectedAuditKeyProvider(keyfile,repo_root=repo)
    state=SqliteRecurringRunState(initial/"recurring.sqlite3",repo_root=repo)
    state.record(dict(run_id="baseline",status="noop",source_window_start="2026-11-01T14:45:43+09:00",
        source_window_end=C0),advance_checkpoint=True)
    SqliteBatchManifestStore(initial/"manifests.sqlite3",repo_root=repo)
    SqliteBatchCapabilityStore(initial/"capabilities.sqlite3",repo_root=repo)
    SqliteBatchJournal(initial/"journal.sqlite3",repo_root=repo)
    SqliteLeaseManager(initial/"leases.sqlite3",repo_root=repo,clock=lambda:NOW)
    raw=[]
    for i in range(12):
        for char in "ab":
            raw.append(dict(import_id=f'aupaycard-mail:{char*24}:{i+1:03d}',date="2026-11-01",
                merchant="au PAY 残高チャージ" if i==11 else f"合成店{i}",
                amount=(4386 if i==10 else 120 if i==11 else 1000+i),payment_type="メール通知",
                member="本会員",memo=f"メール明細No.{i+1:03d}",transaction_kind="purchase"))
    collection=dict(collection_complete=True,listing_complete=True,collection_truncated=False,gmail_list_failed=0,
        gmail_read_failed=0,needs_review=0,found=6,fetched=6)
    initial_plan=build_canonical_apply_plan(collection,reconcile_transactions(raw,[]))
    db=DB()
    db.rows=[materialize_import_row(item,imported_at=NOW,status="auto_expense")
             for item in initial_plan.candidates if item.amount_yen not in (4386,120)]
    full=build_canonical_apply_plan(collection,reconcile_transactions(raw,db.rows))
    statuses={c.identity:("transfer_aupay_charge" if c.amount_yen==120 else "auto_expense") for c in full.candidates}
    projection=create_batch_projection(full,candidate_identities=tuple(statuses),statuses=statuses,
        binding=TargetBinding(expected_spreadsheet_id=db.sid),audit_key=key.load())
    cb=StateBinding("au_pay_card_gmail",db.sid,"folder","card-file")
    pb=StateBinding("production_run",db.sid,"folder","parent-file")
    old=envelope(cb,snapshot(initial,cb),generation=121,phase="pending",attempt="a"*32)
    oldparent=json.loads(initial_ledger(pb));oldparent["generation"]=1414
    oldparent["sources"]["aupay_card"].update(phase="pending",attempt="b"*32,error="source_execution_failed")
    nt,pt=Transport(old),Transport(_encode(oldparent))
    durable=DurableState(nt,cb);ledger=ProductionLedger(pt,pb)
    def financial():
        return {(title,render):[["header"]]+deepcopy(db.rows if title=="取込データ" else [])
                for title in ("取込データ","支出明細","収入明細","レシート","Amazon注文")
                for render in ("UNFORMATTED_VALUE","FORMULA")}
    record=dict(checkpoint_before=C0,checkpoint_after=C1,native_before_sha256=repair.sha(old),
        parent_before_sha256=repair.sha(pt.payload),financial_before_sha256=repair.financial_digest(financial()),
        source=dict(transactions=raw,collection=collection),statuses=statuses,candidate_identities=list(statuses),import_count=2,import_yen=4506,
        audit_key_id=key.load().key_id,historical_target_ref=projection.target_ref,old_failure=dict(id=19,head_sha="f"*40,
        head_branch="main",run_attempt=1,status="completed",conclusion="failure",event="workflow_dispatch",
        path=".github/workflows/aupay-card-incremental-apply.yml",created_at="2026-11-04T11:58:12Z"))
    approval_file=tmp_path/"approval.json"
    approval_file.write_text(json.dumps(dict(approval_reference="owner-fixed-repair",batch_ref=projection.batch_ref,
        target_ref=projection.target_ref,batch_size=2,expires_at=(NOW+timedelta(seconds=240)).isoformat())))
    approval=ProtectedBatchApprovalProvider(approval_file,repo_root=repo)
    original=db.svc.api.api
    def append(**kwargs):
        body=deepcopy(kwargs["body"])
        class Request:
            def execute(self):
                db.calls+=1
                assert nt.payload!=old and pt.payload!=_encode(oldparent)
                n=validate(nt.payload,cb)
                rows=repair._rows(n,"recurring.sqlite3","recurring_runs")
                assert any(r["status"]=="UNKNOWN_NO_REPLAY" for r in rows)
                assert any(r["status"]=="REPAIR_RESERVED" for r in rows)
                assert n["phase"]=="pending" and n["attempt"]==UUID(RID).hex
                values=body["values"]
                if mode!="drop":db.rows.extend(values[:1] if mode=="partial" else values)
                if mode=="corrupt":db.rows[-1][6]+=1
                if mode=="unknown":raise TimeoutError("synthetic financial unknown")
                return {}
        return Request()
    original.append=append
    documents={"money":{"records":{"old":{"state":"review"}}},"review":{"old":"input"}}
    expected=deepcopy(documents)
    def documents_check(): assert documents==expected
    stages=[]
    def boundary(stage):stages.append(stage)
    args=dict(record=record,record_sha256=repair.sha(repair.encode(record)),run_id=RID,db=db,durable=durable,
        ledger=ledger,key_provider=key,approval_provider=approval,state_dir=tmp_path/"cache",repo_root=repo,
        native_read=nt.read,parent_read=pt.read,financial_read=financial,documents_check=documents_check,
        source_check=lambda:None,guard=lambda:None,clock=lambda:NOW,boundary=boundary)
    return SimpleNamespace(args=args,record=record,db=db,nt=nt,pt=pt,old=old,oldparent=oldparent,
        cb=cb,pb=pb,stages=stages,documents=documents)


def test_existing_writer_exact_two_rows_archive_alias_and_c1(tmp_path):
    p=case(tmp_path);original=deepcopy(p.db.rows)
    result=repair.execute_reconciliation(**p.args)
    assert result["written"]==2 and result["import_yen"]==4506 and result["expense_rows_written"]==0
    assert p.db.calls==1 and p.db.rows[:10]==original and len(p.db.rows)==12
    native=validate(p.nt.payload,p.cb);parent=validate_ledger(p.pt.payload,p.pb)
    assert checkpoint(native,C1)==C1 and native["generation"]==124
    assert parent["generation"]==1416 and parent["sources"]["aupay_card"]["phase"]=="ready"
    assert repair.preserved_unknown_proofs(native,parent,C1)[19]["conclusion"]=="failure"
    archive=json.loads(next(r["summary_json"] for r in repair._rows(native,"recurring.sqlite3","recurring_runs")
                            if r["status"]=="UNKNOWN_NO_REPLAY"))
    assert base64.b64decode(archive["native_before_raw_b64"])==p.old
    assert base64.b64decode(archive["parent_before_raw_b64"])==_encode(p.oldparent)
    assert result["acknowledged"] and result["sealed_capability"] and result["independent_readback"]
    assert result["alias_owners"]==12 and result["alias_sources"]==24 and result["unposted_ordinary_yen"]==4386
    assert all(parent["sources"][k]==v for k,v in p.oldparent["sources"].items() if k!="aupay_card")
    with pytest.raises(RuntimeError):repair.execute_reconciliation(**p.args)
    assert p.db.calls==1


@pytest.mark.parametrize("stage",["archive_and_reservation_local","native_reserved","parent_reserved",
    "before_financial_append","financial_attempt_finished","outcome_persisted","exact_financial_readback",
    "before_checkpoint_commit","checkpoint_committed","parent_completed"])
def test_crash_at_each_boundary_never_reappends(tmp_path,stage):
    p=case(tmp_path)
    reached=[]
    def fail(where):
        reached.append(where)
        if where==stage:raise RuntimeError("synthetic crash")
    p.args["boundary"]=fail
    with pytest.raises(RuntimeError):repair.execute_reconciliation(**p.args)
    assert stage in reached
    count=p.db.calls
    # A second dispatch is refused by workflow history, even if a failure
    # happened before the very first Google reservation was acknowledged.
    def consumed():raise RuntimeError("repair_dispatch_consumed_no_retry")
    p.args["guard"]=consumed
    with pytest.raises(RuntimeError,match="consumed_no_retry"):
        repair.execute_reconciliation(**p.args)
    assert p.db.calls==count and count<=1
    if stage in {"archive_and_reservation_local","native_reserved","parent_reserved","before_financial_append"}:
        assert count==0
    if stage not in {"archive_and_reservation_local","checkpoint_committed","parent_completed"}:
        assert validate(p.nt.payload,p.cb)["phase"]=="pending"


@pytest.mark.parametrize("mode",["unknown","partial","drop","corrupt"])
def test_unknown_or_partial_retains_both_pending_and_old_checkpoint(tmp_path,mode):
    p=case(tmp_path,mode)
    with pytest.raises(RuntimeError,match="unknown_no_replay"):
        repair.execute_reconciliation(**p.args)
    native=validate(p.nt.payload,p.cb)
    assert p.db.calls==1 and native["phase"]=="pending"
    assert repair._rows(native,"recurring.sqlite3","recurring_checkpoint")[0]["successful_window_end"]==C0
    assert validate_ledger(p.pt.payload,p.pb)["sources"]["aupay_card"]["phase"]=="pending"
    assert any(r["status"]=="REPAIR_UNKNOWN_NO_REPLAY" for r in repair._rows(native,"recurring.sqlite3","recurring_runs"))


@pytest.mark.parametrize("target",["native","parent"])
def test_reservation_response_loss_is_not_retried_and_never_appends(tmp_path,target):
    p=case(tmp_path)
    (p.nt if target=="native" else p.pt).mode="unknown"
    with pytest.raises(TimeoutError):repair.execute_reconciliation(**p.args)
    assert p.db.calls==0
    assert len(p.nt.writes)==1 and len(p.pt.writes)==(target=="parent")
