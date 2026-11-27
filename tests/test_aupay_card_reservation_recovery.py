"""Isolated generation122/1414 recovery through the existing real writer/stores."""
import base64
from copy import deepcopy
from datetime import datetime,timedelta
import json
from pathlib import Path
import subprocess
import sys
from uuid import UUID

import pytest

from app import aupay_card_reconciliation_apply as repair
from app import aupay_card_incremental_guard as guard
from app.aupay_card_reservation_recovery import summaries,unused_reservation_proof,PROTOCOL
from app.aupay_card_incremental_recovery import _rows,preserved_unknown_proofs
from app.aupay_card_recurring import SqliteRecurringRunState
from app.drive_run_state import DurableState,validate
from app.production_ledger import ProductionLedger,validate_ledger
from test_aupay_card_reconciliation_apply import case,C0,C1,NOW,RID
from test_aupay_card_resume_recovery import NoWriteGitHub
from test_aupay_card_incremental_apply import env

NEW="d8f1a0d3-483b-4d56-a962-2acf8e94de99"


def recovery_case(tmp_path,mode="normal"):
    p=case(tmp_path)
    base_financial=p.args["financial_read"]
    p.extra_financial={"要確認":[["held","owner input"]],"カテゴリ":[["category","=1+1"]]}
    def all_tables():
        value=base_financial()
        for title,rows in p.extra_financial.items():
            for render in ("UNFORMATTED_VALUE","FORMULA"):value[(title,render)]=deepcopy(rows)
        return value
    p.args["financial_read"]=all_tables
    p.record["financial_before_sha256"]=repair.financial_digest(all_tables())
    p.record.update(native_before_metadata_digest="native-121",parent_before_metadata_digest="parent-1414")
    p.args["record_sha256"]=repair.sha(repair.encode(p.record))
    def crash(stage):
        if stage=="native_reserved":raise TimeoutError("invented original reservation loss")
    p.args["boundary"]=crash
    with pytest.raises(TimeoutError):repair.execute_reconciliation(**p.args)
    assert validate(p.nt.payload,p.cb)["generation"]==122 and json.loads(p.pt.payload)["generation"]==1414
    p.prior_record=deepcopy(p.record);p.reserved_native=p.nt.payload;p.reserved_parent=p.pt.payload
    later=NOW+timedelta(minutes=10)
    record=deepcopy(p.record)
    record.update(native_before_sha256=repair.sha(p.nt.payload),parent_before_sha256=repair.sha(p.pt.payload),
        native_before_metadata_digest="native-122",old_failure=dict(id=20,head_sha="b"*40,head_branch="main",run_attempt=1,
            status="completed",conclusion="failure",event="workflow_dispatch",
            path=".github/workflows/aupay-card-reconciliation-once.yml",created_at=(NOW+timedelta(seconds=1)).isoformat()),
        recovery=dict(previous_record=p.prior_record,failed_run_uuid=RID))
    approval=p.args["approval_provider"].path
    value=json.loads(approval.read_bytes());value["expires_at"]=(later+timedelta(seconds=300)).isoformat()
    from types import SimpleNamespace
    state=SimpleNamespace(successful_window_end=lambda:datetime.fromisoformat(C0),alias_registry=lambda:())
    value["batch_ref"]=repair.prepare_reconciliation(record,p.db,state,p.args["key_provider"].load())["projection"].batch_ref
    approval.write_text(json.dumps(value),encoding="utf-8")
    p.args.update(record=record,record_sha256=repair.sha(repair.encode(record)),run_id=NEW,
        state_dir=tmp_path/"recovery-cache",durable=DurableState(p.nt,p.cb),ledger=ProductionLedger(p.pt,p.pb),
        clock=lambda:later,boundary=lambda stage:None,execution_identity=dict(id=30,head_sha="a"*40,head_branch="main",run_attempt=1,
            event="workflow_dispatch",path=".github/workflows/aupay-card-reservation-recovery.yml",created_at=later.isoformat()),
        state_observe=lambda:(dict(sha256=record["native_before_sha256"],metadata_digest=record["native_before_metadata_digest"]),
                              dict(sha256=record["parent_before_sha256"],metadata_digest=record["parent_before_metadata_digest"])))
    p.record=record;p.now=later
    def append(**kwargs):
        rows=deepcopy(kwargs["body"]["values"])
        class Request:
            def execute(self):
                p.db.calls+=1
                native=validate(p.nt.payload,p.cb);runs=summaries(native)
                assert runs["INTENT:"+p.args["run_id"]]["protocol"]==PROTOCOL
                assert native["attempt"]==UUID(p.args["run_id"]).hex
                assert validate_ledger(p.pt.payload,p.pb)["sources"]["aupay_card"]["attempt"]==native["attempt"]
                if mode!="drop":p.db.rows.extend(rows[:1] if mode=="partial" else rows)
                if mode=="unknown":raise TimeoutError("invented response lost")
                return {}
        return Request()
    p.db.svc.api.api.append=append
    return p


def test_full_122_1414_recovery_exact_archives_revocation_c1_and_fresh_guard(tmp_path):
    p=recovery_case(tmp_path);before=deepcopy(p.db.rows)
    result=repair.execute_reconciliation(**p.args)
    assert result["success"] and result["written"]==2 and result["import_yen"]==4506
    assert result["expense_rows_written"]==0 and p.db.calls==1 and p.db.rows[:10]==before
    native=validate(p.nt.payload,p.cb);parent=validate_ledger(p.pt.payload,p.pb)
    assert native["generation"]==126 and parent["generation"]==1416
    assert parent["sources"]["aupay_card"]["phase"]=="ready"
    runs=summaries(native)
    assert base64.b64decode(runs["UNKNOWN:20:1"]["native_before_raw_b64"])==p.reserved_native
    assert base64.b64decode(runs["UNKNOWN:20:1"]["parent_before_raw_b64"])==p.reserved_parent
    assert runs["UNKNOWN:19:1"]==summaries(json.loads(p.reserved_native))["UNKNOWN:19:1"]
    assert runs["RESERVATION:"+RID]==summaries(json.loads(p.reserved_native))["RESERVATION:"+RID]
    caps=_rows(native,"capabilities.sqlite3","batch_capabilities")
    assert len(caps)==2 and all(c["state"]=="sealed" for c in caps)
    assert [c["terminal_reason"] for c in caps if c["run_id"]==RID]==["revoked_unused_no_dispatch"]
    proof=preserved_unknown_proofs(native,parent,C1)
    assert set(proof)=={19,20} and all(v["conclusion"]=="failure" for v in proof.values())
    # Execution is Nov27 while business coverage remains Nov4.
    assert datetime.fromisoformat(runs[NEW]["execution_started_at"])>datetime.fromisoformat(C1)
    fresh=DurableState(p.nt,p.cb);fresh.restore(tmp_path/"fresh")
    state=SqliteRecurringRunState(tmp_path/"fresh/recurring.sqlite3",repo_root=p.args["repo_root"])
    assert state.successful_window_end().isoformat()==C1 and len(state.alias_registry())==12
    class History(NoWriteGitHub):
        def get(self,path):
            if path=="actions/runs/30":return p.args["execution_identity"]|dict(status="completed",conclusion="success")
            for fid,item in proof.items():
                if path==f"actions/runs/{fid}":return item
            if path=="actions/runs/17":return super().get(path)|dict(created_at="2026-11-28T00:00:00Z")
            return super().get(path)
    class Recording(History):
        def __init__(self):super().__init__();self.responses={}
        def get(self,path):
            value=super().get(path);self.responses[path]=value;return value
    history=Recording();history.prior.append(proof[19])
    assert guard.controls(env()|dict(EXPECTED_CHECKPOINT=C1),"a"*40,history,reconciliation_state=(native,parent))
    # A separate process has neither the original objects nor local cache.
    payload=dict(native=base64.b64encode(p.nt.payload).decode(),parent=parent,
        sid=p.cb.spreadsheet_id,folder=p.cb.folder_id,file=p.cb.file_id,target=str(tmp_path/"fresh-process"),
        repo=str(p.args["repo_root"]),env=env()|dict(EXPECTED_CHECKPOINT=C1),responses=history.responses)
    script='''import base64,json,socket,sys,warnings
warnings.simplefilter("ignore",DeprecationWarning)
def deny(*args,**kwargs):raise AssertionError("network forbidden")
socket.socket.connect=deny
from pathlib import Path
from app.drive_run_state import DurableState,StateBinding,validate
from app.aupay_card_recurring import SqliteRecurringRunState
from app.aupay_card_incremental_guard import controls
d=json.load(sys.stdin);raw=base64.b64decode(d["native"])
class Transport:
 def read(self):return raw
 def write(self,*args):raise AssertionError("write forbidden")
binding=StateBinding("au_pay_card_gmail",d["sid"],d["folder"],d["file"])
DurableState(Transport(),binding).restore(Path(d["target"]))
state=SqliteRecurringRunState(Path(d["target"])/"recurring.sqlite3",repo_root=Path(d["repo"]))
assert state.successful_window_end().isoformat()==d["env"]["EXPECTED_CHECKPOINT"] and len(state.alias_registry())==12
class GitHub:
 def get(self,path):return d["responses"][path]
assert controls(d["env"],"a"*40,GitHub(),reconciliation_state=(validate(raw,binding),d["parent"]))
print(json.dumps({"fresh_process":True,"guard_accepted":True,"aliases":12}))
'''
    process=subprocess.run([sys.executable,"-B","-c",script],input=json.dumps(payload),text=True,capture_output=True,timeout=60)
    assert process.returncode==0,process.stderr
    assert json.loads(process.stdout)==dict(fresh_process=True,guard_accepted=True,aliases=12)
    # A rerun of either archived attempt is still refused independently.
    class Rerun(History):
        def get(self,path):
            value=super().get(path)
            return value|dict(run_attempt=2) if path=="actions/runs/20" else value
    with pytest.raises(RuntimeError):guard.controls(env()|dict(EXPECTED_CHECKPOINT=C1),"a"*40,Rerun(),reconciliation_state=(native,parent))
    class DeliveryUnknown(History):
        def get(self,path):
            value=super().get(path)
            return value|dict(conclusion="failure") if path=="actions/runs/30" else value
    with pytest.raises(RuntimeError,match="recovery_completion_execution_unproven"):
        guard.controls(env()|dict(EXPECTED_CHECKPOINT=C1),"a"*40,DeliveryUnknown(),reconciliation_state=(native,parent))


@pytest.mark.parametrize("stage",["unused_capability_revoked_local","archive_and_reservation_local","native_reserved",
    "parent_reserved","dispatch_intent_persisted","before_financial_append","financial_attempt_finished",
    "outcome_persisted","exact_financial_readback","before_checkpoint_commit","checkpoint_committed","parent_completed"])
def test_runner_loss_never_resends_or_rolls_back(tmp_path,stage):
    p=recovery_case(tmp_path)
    def crash(where):
        if where==stage:raise KeyboardInterrupt("invented runner loss")
    p.args["boundary"]=crash
    with pytest.raises(KeyboardInterrupt):repair.execute_reconciliation(**p.args)
    calls=p.db.calls
    assert calls<=1
    if stage in {"unused_capability_revoked_local","archive_and_reservation_local","native_reserved","parent_reserved","dispatch_intent_persisted","before_financial_append"}:assert calls==0
    def no_retry():raise RuntimeError("repair_dispatch_consumed_no_retry")
    p.args["guard"]=no_retry
    with pytest.raises(RuntimeError):repair.execute_reconciliation(**p.args)
    assert p.db.calls==calls
    if stage not in {"checkpoint_committed","parent_completed"}:
        assert _rows(json.loads(p.nt.payload),"recurring.sqlite3","recurring_checkpoint")[0]["successful_window_end"]==C0


@pytest.mark.parametrize("mode",["unknown","partial","drop"])
def test_unknown_financial_result_keeps_c0_pending_no_replay(tmp_path,mode):
    p=recovery_case(tmp_path,mode)
    with pytest.raises(RuntimeError,match="repair_unknown_no_replay"):repair.execute_reconciliation(**p.args)
    n=json.loads(p.nt.payload)
    assert n["phase"]=="pending" and p.db.calls==1
    assert _rows(n,"recurring.sqlite3","recurring_checkpoint")[0]["successful_window_end"]==C0
    assert "INTENT:"+NEW in summaries(n)


@pytest.mark.parametrize("change",["revision","previous_record","not_expired","parent_changed","missing_observer"])
def test_unused_proof_rejects_before_any_new_remote_state_or_financial_effect(tmp_path,change):
    p=recovery_case(tmp_path);before=(p.nt.payload,p.pt.payload)
    if change=="revision":p.record["parent_before_metadata_digest"]="different-revision"
    if change=="previous_record":p.record["recovery"]["previous_record"]["import_yen"]+=1
    if change=="not_expired":
        p.args["clock"]=lambda:NOW+timedelta(seconds=120)
        p.args["execution_identity"]["created_at"]=(NOW+timedelta(seconds=60)).isoformat()
    if change=="parent_changed":p.pt.payload=p.pt.payload+b" "
    if change=="missing_observer":p.args["state_observe"]=None
    p.args["record_sha256"]=repair.sha(repair.encode(p.record))
    with pytest.raises(RuntimeError,match="recovery_unused_capability_unproven" if change=="not_expired" else None):
        repair.execute_reconciliation(**p.args)
    assert p.nt.payload==before[0] and p.db.calls==0


def next_independent(p,tmp_path):
    previous=deepcopy(p.record);record=deepcopy(previous)
    identity=p.args["execution_identity"]
    record.update(native_before_sha256=repair.sha(p.nt.payload),parent_before_sha256=repair.sha(p.pt.payload),
        native_before_metadata_digest="next-native-revision",parent_before_metadata_digest=(
            previous["parent_before_metadata_digest"] if p.pt.payload==p.reserved_parent else "next-parent-revision"),
        old_failure=identity|dict(status="completed",conclusion="failure"),
        recovery=dict(previous_record=previous,failed_run_uuid=p.args["run_id"]))
    later=p.now+timedelta(minutes=10)
    p.args.update(record=record,record_sha256=repair.sha(repair.encode(record)),run_id="e8f1a0d3-483b-4d56-a962-2acf8e94de99",
        durable=DurableState(p.nt,p.cb),ledger=ProductionLedger(p.pt,p.pb),state_dir=tmp_path/"next-cache",clock=lambda:later,
        boundary=lambda stage:None,execution_identity=dict(id=31,head_sha="c"*40,head_branch="main",run_attempt=1,
            event="workflow_dispatch",path=".github/workflows/aupay-card-reservation-recovery.yml",created_at=later.isoformat()),
        state_observe=lambda:(dict(sha256=record["native_before_sha256"],metadata_digest=record["native_before_metadata_digest"]),
                              dict(sha256=record["parent_before_sha256"],metadata_digest=record["parent_before_metadata_digest"])))
    from types import SimpleNamespace
    state=SimpleNamespace(successful_window_end=lambda:datetime.fromisoformat(C0),alias_registry=lambda:())
    projection=repair.prepare_reconciliation(record,p.db,state,p.args["key_provider"].load())["projection"]
    path=p.args["approval_provider"].path;approval=json.loads(path.read_bytes())
    approval.update(batch_ref=projection.batch_ref,expires_at=(later+timedelta(seconds=300)).isoformat())
    path.write_text(json.dumps(approval),encoding="utf-8")
    p.record=record;p.now=later


@pytest.mark.parametrize("stage",["native_reserved","parent_reserved"])
def test_same_kind_failed_reservation_can_use_a_distinct_approved_record_and_run(tmp_path,stage):
    p=recovery_case(tmp_path)
    def crash(where):
        if where==stage:raise TimeoutError("reservation boundary")
    p.args["boundary"]=crash
    with pytest.raises(TimeoutError):repair.execute_reconciliation(**p.args)
    assert p.db.calls==0
    stopped=p.nt.payload
    next_independent(p,tmp_path)
    result=repair.execute_reconciliation(**p.args)
    assert result["written"]==2 and p.db.calls==1
    native=json.loads(p.nt.payload);parent=json.loads(p.pt.payload)
    proofs=preserved_unknown_proofs(native,parent,C1)
    assert set(proofs)=={19,20,30}
    assert base64.b64decode(summaries(native)["UNKNOWN:30:1"]["native_before_raw_b64"])==stopped
    assert len([c for c in _rows(native,"capabilities.sqlite3","batch_capabilities") if c["terminal_reason"]=="revoked_unused_no_dispatch"])==2


@pytest.mark.parametrize("stage",["dispatch_intent_persisted","financial_attempt_finished"])
def test_issued_and_zero_persisted_journal_do_not_prove_unsent_after_intent(tmp_path,stage):
    p=recovery_case(tmp_path,"drop")
    def crash(where):
        if where==stage:raise TimeoutError("intent boundary")
    p.args["boundary"]=crash
    with pytest.raises(TimeoutError):repair.execute_reconciliation(**p.args)
    n=json.loads(p.nt.payload)
    assert not [e for e in _rows(n,"journal.sqlite3","batch_events") if e["run_id"]==NEW]
    assert next(c for c in _rows(n,"capabilities.sqlite3","batch_capabilities") if c["run_id"]==NEW)["state"]=="issued"
    count=p.db.calls;stopped=p.nt.payload
    next_independent(p,tmp_path)
    with pytest.raises(RuntimeError,match="recovery_financial_dispatch_possible"):repair.execute_reconciliation(**p.args)
    assert p.db.calls==count and p.nt.payload==stopped


@pytest.mark.parametrize("target,index",[("native",1),("native",2),("native",3),("native",4),("parent",1),("parent",2)])
def test_state_response_loss_is_unknown_even_when_server_saved(tmp_path,target,index):
    p=recovery_case(tmp_path);transport=p.nt if target=="native" else p.pt
    start=len(transport.writes);original=transport.write
    def unknown(payload):
        original(payload)
        if len(transport.writes)-start==index:raise TimeoutError("reply lost")
    transport.write=unknown
    with pytest.raises(TimeoutError):repair.execute_reconciliation(**p.args)
    calls=p.db.calls
    assert calls<=1 and len(transport.writes)-start==index
    # No retry, including native ready / parent pending or final ack loss.
    p.args["guard"]=lambda:(_ for _ in ()).throw(RuntimeError("no_retry"))
    with pytest.raises(RuntimeError):repair.execute_reconciliation(**p.args)
    assert p.db.calls==calls
    if target=="native" and index==4:
        assert json.loads(p.nt.payload)["phase"]=="ready" and json.loads(p.pt.payload)["sources"]["aupay_card"]["phase"]=="pending"
        with pytest.raises(RuntimeError):preserved_unknown_proofs(json.loads(p.nt.payload),json.loads(p.pt.payload),C1)


@pytest.mark.parametrize("change",["certificate","archive_chain","revocation_hash","revocation_time","old_archive","intent"])
def test_tampered_completion_never_relaxes_existing_guards(tmp_path,change):
    p=recovery_case(tmp_path);repair.execute_reconciliation(**p.args)
    n=json.loads(p.nt.payload);runs=summaries(n)
    from test_aupay_card_checkpoint_reconciliation import replace_summary
    if change=="archive_chain":runs[NEW]["checkpoint_reconciliation"]["archive_chain_ids"].pop()
    elif change=="certificate":runs[NEW]["checkpoint_reconciliation"]["archive_chain_sha256"]="0"*64
    elif change=="old_archive":runs["UNKNOWN:19:1"]["native_before_bytes_sha256"]="0"*64
    elif change=="intent":runs["INTENT:"+NEW]["protocol"]="other"
    else:
        cert=next(s for s in runs.values() if s.get("status")=="CAPABILITY_REVOKED_UNUSED")
        if change=="revocation_hash":cert["after_sha256"]="0"*64
        else:cert["revoked_at"]=NOW.isoformat()
    for rid,summary in runs.items():replace_summary(n,rid,summary)
    with pytest.raises(RuntimeError):preserved_unknown_proofs(n,json.loads(p.pt.payload),C1)
    assert p.db.calls==1


@pytest.mark.parametrize("table",["要確認","カテゴリ"])
def test_review_or_formula_change_holds_checkpoint_after_single_append(tmp_path,table):
    p=recovery_case(tmp_path)
    def changed(stage):
        if stage=="financial_attempt_finished":p.extra_financial[table][0][1]="unexpected external edit"
    p.args["boundary"]=changed
    with pytest.raises(RuntimeError,match="financial_exact_delta_failed"):repair.execute_reconciliation(**p.args)
    assert p.db.calls==1 and _rows(json.loads(p.nt.payload),"recurring.sqlite3","recurring_checkpoint")[0]["successful_window_end"]==C0


def test_revocation_and_completion_use_execution_clock_not_business_checkpoint(tmp_path):
    from itertools import count
    p=recovery_case(tmp_path);ticks=count()
    p.args["clock"]=lambda:p.now+timedelta(milliseconds=next(ticks))
    assert repair.execute_reconciliation(**p.args)["success"]
    runs=summaries(json.loads(p.nt.payload))
    revoked=next(r for r in runs.values() if r.get("status")=="CAPABILITY_REVOKED_UNUSED")
    cap=next(r for r in _rows(json.loads(p.nt.payload),"capabilities.sqlite3","batch_capabilities") if r["run_id"]==NEW)
    assert datetime.fromisoformat(C1)<datetime.fromisoformat(revoked["revoked_at"])<=datetime.fromisoformat(cap["issued_at"])
    parent=json.loads(p.pt.payload);parent["sources"]["aupay_card"]["counts"]["written"]+=1
    with pytest.raises(RuntimeError,match="reconciliation_parent_counts_changed"):
        preserved_unknown_proofs(json.loads(p.nt.payload),parent,C1)


@pytest.mark.parametrize("field",["checkpoint_after","candidate_identities","financial_before_sha256"])
def test_recovery_cannot_change_the_approved_business_scope(tmp_path,field):
    p=recovery_case(tmp_path);stopped=p.nt.payload
    p.record[field]="different" if field!="candidate_identities" else ["different"]
    p.args["record_sha256"]=repair.sha(repair.encode(p.record))
    with pytest.raises(RuntimeError,match="recovery_business_plan_changed"):repair.execute_reconciliation(**p.args)
    assert p.nt.payload==stopped and p.db.calls==0


@pytest.mark.parametrize("change",["claimed","dispatching","attempt","terminal","expiry","journal"])
def test_unused_authority_requires_every_negative_condition(tmp_path,change):
    import hashlib,sqlite3
    p=recovery_case(tmp_path);n=json.loads(p.nt.payload)
    file="journal.sqlite3" if change=="journal" else "capabilities.sqlite3"
    item=n["files"][file]
    with sqlite3.connect(":memory:") as con:
        con.deserialize(base64.b64decode(item["data"]))
        if change=="journal":
            cap=_rows(n,"capabilities.sqlite3","batch_capabilities")[0]
            con.execute("INSERT INTO batch_events (run_id,attempt_id,batch_ref,stage,state,reason,identity_count,candidate_digest,timestamp) VALUES (?,?,?,?,?,?,?,?,?)",
                (RID,"invented",cap["batch_ref"],"pre_read","started","invented",2,"0"*64,p.now.isoformat()))
        else:
            field,value={"claimed":("state","claimed"),"dispatching":("state","dispatching"),"attempt":("attempt_id","invented"),
                "terminal":("terminal_reason","ended"),"expiry":("expires_at",(p.now+timedelta(days=1)).isoformat())}[change]
            con.execute("UPDATE batch_capabilities SET "+field+"=? WHERE run_id=?",(value,RID))
        con.commit();raw=con.serialize()
    item.update(data=base64.b64encode(raw).decode(),sha256=hashlib.sha256(raw).hexdigest())
    p.nt.payload=repair.encode(n);stopped=p.nt.payload
    p.record["native_before_sha256"]=repair.sha(stopped)
    p.args["record_sha256"]=repair.sha(repair.encode(p.record))
    with pytest.raises(RuntimeError,match="recovery_(unused_capability_unproven|financial_dispatch_possible)"):
        repair.execute_reconciliation(**p.args)
    assert p.nt.payload==stopped and p.db.calls==0
