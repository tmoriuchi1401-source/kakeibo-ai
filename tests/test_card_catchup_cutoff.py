"""Historical normal Apply coverage is proved, never inferred from run time."""
from copy import deepcopy
from datetime import datetime,timedelta
import json

import pytest

from app import aupay_card_first_window_apply as apply
from app import aupay_card_incremental_guard as guard
from app.aupay_card_incremental_coverage import successful_coverage_proofs
from app.drive_run_state import validate
from test_aupay_card_incremental_apply import followup,env,GitHub,NOW


def test_fixed_cutoff_uses_real_now_without_expanding_or_truncating_window(tmp_path):
    p=followup(tmp_path)
    p.args.update(now=NOW+timedelta(days=30),cutoff=NOW)
    prepared=apply.prepare(**p.args)
    assert prepared['window'].end==NOW
    assert prepared['window'].start==datetime.fromisoformat(apply.END)-timedelta(days=2)
    assert p.db.write_calls==0


@pytest.mark.parametrize('change',['future','too_long','no_advance','precision'])
def test_invalid_cutoff_never_starts_write(tmp_path,change):
    p=followup(tmp_path)
    end=NOW
    if change=='future':end=NOW+timedelta(seconds=1)
    if change=='too_long':end=datetime.fromisoformat(apply.END)+timedelta(days=12,seconds=1);p.args['now']=end+timedelta(days=1)
    if change=='no_advance':end=datetime.fromisoformat(apply.END)
    if change=='precision':end=NOW.replace(microsecond=1)
    p.args['cutoff']=end
    with pytest.raises(RuntimeError):apply.prepare(**p.args)
    assert p.db.write_calls==0


def completed_historical(tmp_path):
    p=followup(tmp_path)
    wall=NOW+timedelta(days=30)
    provider=p.execution['authority_provider'];raw=json.loads(provider.path.read_bytes())
    raw['expires_at']=(wall+timedelta(days=1)).isoformat();provider.path.write_text(json.dumps(raw))
    p.execution.update(policy=provider.load(),clock=lambda:wall)
    p.prepared['github_execution']=dict(id=100,head_sha='a'*40,head_branch='main',run_attempt=1,
        event='workflow_dispatch',path='.github/workflows/'+guard.WORKFLOW,created_at=(wall-timedelta(seconds=1)).isoformat())
    p.execution['prepared']=p.prepared
    apply.execute_fixed(**p.execution)
    return p


def test_success_created_after_business_cutoff_has_independent_completion_certificate(tmp_path):
    p=completed_historical(tmp_path)
    native=validate(p.durable.transport.read(),p.durable.binding);parent=p.ledger.value
    proofs=successful_coverage_proofs(native,parent,NOW.isoformat())
    assert proofs[100]['conclusion']=='success' and datetime.fromisoformat(proofs[100]['created_at'])>NOW
    assert proofs[100]['run_attempt']==1 and p.db.write_calls==1


@pytest.mark.parametrize('change',['native_pending','parent_pending','certificate','window','journal'])
def test_unknown_or_changed_completion_never_proves_coverage(tmp_path,change):
    p=completed_historical(tmp_path);native=validate(p.durable.transport.read(),p.durable.binding);parent=deepcopy(p.ledger.value)
    if change=='native_pending':native['phase']='pending';native['attempt']='f'*32
    elif change=='parent_pending':parent['sources']['aupay_card']['phase']='pending'
    else:
        import base64,sqlite3,hashlib
        con=sqlite3.connect(':memory:');con.deserialize(base64.b64decode(native['files']['recurring.sqlite3']['data']))
        rows=con.execute('SELECT run_id,summary_json FROM recurring_runs').fetchall();rid,encoded=next((k,v) for k,v in rows if json.loads(v).get('coverage_protocol'))
        value=json.loads(encoded)
        if change=='certificate':value['coverage_certificate']['journal_sha256']='0'*64
        if change=='window':value['checkpoint_before']=(NOW-timedelta(days=20)).isoformat()
        if change=='journal':value['written']+=1
        con.execute('DROP TRIGGER recurring_run_no_update');con.execute('UPDATE recurring_runs SET summary_json=? WHERE run_id=?',(json.dumps(value),rid));con.commit();raw=con.serialize();con.close()
        native['files']['recurring.sqlite3']=dict(data=base64.b64encode(raw).decode(),sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(RuntimeError):successful_coverage_proofs(native,parent,NOW.isoformat())


def test_precredential_deferral_never_accepts_unproved_historical_success_at_runtime():
    values=env();history=GitHub();history.prior.append(dict(id=100,head_sha='a'*40,status='completed',conclusion='success',
        run_attempt=1,created_at='2026-11-27T00:00:00Z'))
    assert guard.controls(values,'a'*40,history,defer_reconciliation=True) is False
    with pytest.raises(RuntimeError,match='previous_dispatch_no_retry'):guard.controls(values,'a'*40,history)


def test_real_protected_coverage_accepts_next_window_but_rejects_changed_run(tmp_path):
    p=completed_historical(tmp_path);native=validate(p.durable.transport.read(),p.durable.binding)
    proof=successful_coverage_proofs(native,p.ledger.value,NOW.isoformat())[100]
    class History(GitHub):
        tamper={}
        def get(self,path):
            if path=='actions/runs/100':return proof|self.tamper
            value=super().get(path)
            if path=='actions/runs/17':value['created_at']=(NOW+timedelta(days=31)).isoformat()
            return value
    history=History([proof]);values=env()|dict(EXPECTED_CHECKPOINT=NOW.isoformat())
    assert guard.controls(values,'a'*40,history,reconciliation_state=(native,p.ledger.value)) is False
    for changed in (dict(run_attempt=2),dict(head_sha='b'*40),dict(conclusion='failure')):
        history.tamper=changed
        with pytest.raises(RuntimeError):guard.controls(values,'a'*40,history,reconciliation_state=(native,p.ledger.value))


def test_schedule_never_uses_manual_cutoff(tmp_path):
    from test_aupay_card_daily import schedule_env,DailyGitHub
    with pytest.raises(RuntimeError,match='scheduled_cutoff_forbidden'):
        guard.controls(schedule_env(tmp_path)|dict(CUTOFF=NOW.isoformat()),'a'*40,DailyGitHub())


@pytest.mark.parametrize('change',['amount','source_hash','status','review'])
def test_maintenance_contract_binds_full_purchase_and_review_scope(tmp_path,change):
    from app.aupay_card_incremental_coverage import plan_contract_sha256
    p=followup(tmp_path);expected=plan_contract_sha256(p.prepared)
    altered=dict(p.prepared,transactions=deepcopy(p.prepared['transactions']),
        statuses=dict(p.prepared['statuses']),decisions=list(p.prepared['decisions']))
    if change=='amount':altered['transactions'][0]['amount']+=1
    if change=='source_hash':altered['transactions'][0]['source_hash']='0'*64
    if change=='status':altered['statuses'][next(iter(altered['statuses']))]='transfer_aupay_charge'
    if change=='review':altered['decisions']=altered['decisions'][1:]
    assert plan_contract_sha256(altered)!=expected
    p.prepared['transactions'].reverse()
    assert plan_contract_sha256(p.prepared)==expected


def test_historical_cutoff_requires_reviewed_plan_hash_before_credentials():
    values=env()|dict(CUTOFF=NOW.isoformat())
    with pytest.raises(RuntimeError,match='cutoff_plan_required'):guard.controls(values,'a'*40,GitHub())
