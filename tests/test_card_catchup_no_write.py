"""Synthetic coverage/state and fixed run metadata; no credentials/network."""
from copy import deepcopy
from datetime import datetime,timedelta
from types import SimpleNamespace
import json

import pytest

from app import aupay_card_incremental_recovery as recovery
from app import aupay_card_incremental_guard as guard
from app.aupay_card_incremental_coverage import successful_coverage_proofs
from app.drive_run_state import validate
from test_card_catchup_cutoff import completed_historical
from test_aupay_card_incremental_apply import env
from test_aupay_card_resume_recovery import NoWriteGitHub


def audited(monkeypatch,tmp_path):
    p=completed_historical(tmp_path)
    native=validate(p.durable.transport.read(),p.durable.binding);parent=deepcopy(p.ledger.value)
    cp=p.prepared['window'].end.isoformat()
    summary=next(json.loads(r['summary_json']) for r in recovery._rows(native,'recurring.sqlite3','recurring_runs')
                 if json.loads(r['summary_json']).get('coverage_protocol'))
    financial=p.execution['financial_read']()
    documents={k:deepcopy(p.store.read(k)) for k in recovery.NO_WRITE_MONEY_KEYS}
    for key,value in dict(CATCHUP_NO_WRITE_CHECKPOINT=cp,CATCHUP_NO_WRITE_NATIVE_SHA256=recovery._hash(native),
        CATCHUP_NO_WRITE_SOURCES_SHA256=recovery._hash({k:v for k,v in parent['sources'].items() if k!='bank'}),
        CATCHUP_NO_WRITE_FINANCIAL_SHA256=recovery._hash({t+'|'+v:r for (t,v),r in financial.items()}),
        CATCHUP_NO_WRITE_DOCUMENTS_SHA256=recovery._hash(documents),CATCHUP_NO_WRITE_ANCHOR_SHA256=recovery._hash(summary)).items():
        monkeypatch.setattr(recovery,key,value)
    return native,parent,cp,financial,documents,p


@pytest.mark.parametrize('change',[None,'native','other_source','bank_pending','bank_write','bank_missing_counts','finance','formula','document','anchor'])
def test_exact_audit_allows_bank_noop_but_rejects_unexplained_changes(monkeypatch,tmp_path,change):
    n,p,cp,finance,docs,_=audited(monkeypatch,tmp_path)
    original=deepcopy((n,p,finance,docs));bank=p['sources']['bank']
    p['generation']+=2;bank.update(phase='ready',attempt='',error='',last_success='2026-11-28T05:00:00+00:00')
    bank['counts'].update(written=0,write_requests=0,failure=0)
    if change=='native':n['generation']+=1
    if change=='other_source':p['sources']['aupay_card']['duration_seconds']+=1
    if change=='bank_pending':bank.update(phase='pending',attempt='f'*32)
    if change=='bank_write':bank['counts']['written']=1
    if change=='bank_missing_counts':bank['counts'].pop('written')
    if change in {'finance','formula'}:
        finance[('支出明細','FORMULA')].append(['synthetic-extra', '=1' if change=='formula' else 1])
    if change=='document':docs['journal']={'unexpected':True}
    if change=='anchor':monkeypatch.setattr(recovery,'CATCHUP_NO_WRITE_ANCHOR_SHA256','0'*64)
    action=lambda:recovery.verify_catchup_no_write_state(n,p,cp,financial=lambda:finance,money=SimpleNamespace(read=docs.get))
    if change:
        with pytest.raises(RuntimeError):action()
    else:
        before=deepcopy((n,p,finance,docs));action();assert (n,p,finance,docs)==before
        assert successful_coverage_proofs(n,p,cp)[100]['conclusion']=='success'
        assert n==original[0]


class AuditedGitHub(NoWriteGitHub):
    def __init__(self):
        super().__init__();self.bad=None;self.requests=[];self.preview=False
        self.prior.insert(0,dict(recovery.CATCHUP_NO_WRITE_EXECUTION))
    def get(self,path):
        self.requests.append(path)
        if path==f'actions/runs/{recovery.CATCHUP_NO_WRITE_RUN}':
            value=dict(recovery.CATCHUP_NO_WRITE_EXECUTION)
            if self.bad in value:value[self.bad]='unproved'
            return value
        if path==f'actions/runs/{recovery.CATCHUP_BANK_EXECUTION["id"]}':
            return dict(recovery.CATCHUP_BANK_EXECUTION,conclusion='failure' if self.bad=='bank' else 'success')
        if path==f'actions/runs/{recovery.CATCHUP_NO_WRITE_RUN}/jobs?per_page=100':
            value=super().get(f'actions/runs/{recovery.NO_WRITE_RUN}/jobs?per_page=100')
            job=value['jobs'][0];job.update(id=recovery.CATCHUP_NO_WRITE_JOB,run_id=recovery.CATCHUP_NO_WRITE_RUN)
            if self.bad=='job':job['id']+=1
            if self.bad=='step':job['steps'][2]['conclusion']='success'
            return value
        value=super().get(path)
        if path=='actions/runs/17':value['created_at']='2026-11-28T05:00:00Z'
        if self.preview and path.startswith('actions/workflows/'):
            runs=[r for r in value['workflow_runs'] if r['id']!=17]
            value=dict(total_count=len(runs),workflow_runs=runs)
        return value


@pytest.mark.parametrize('bad',[None,'head_sha','run_attempt','head_branch','created_at','conclusion','job','step','bank'])
def test_fixed_failure_remains_failure_and_unproven_identity_is_rejected(bad):
    g=AuditedGitHub();g.bad=bad
    if bad:
        with pytest.raises(RuntimeError):recovery.verify_no_write_run(g,recovery.CATCHUP_NO_WRITE_RUN)
    else:
        recovery.verify_no_write_run(g,recovery.CATCHUP_NO_WRITE_RUN)
        assert g.get(f'actions/runs/{recovery.CATCHUP_NO_WRITE_RUN}')['conclusion']=='failure'
    with pytest.raises(RuntimeError):recovery.verify_no_write_run(g,999)


@pytest.mark.parametrize('preview',[False,True])
@pytest.mark.parametrize('unproved',[False,True])
def test_preview_and_apply_share_history_validation(monkeypatch,preview,unproved):
    from app import aupay_card_incremental_coverage as coverage
    from app import aupay_card_reservation_recovery as reservation
    monkeypatch.setattr(recovery,'preserved_unknown_proofs',lambda *a:{})
    monkeypatch.setattr(coverage,'successful_coverage_proofs',lambda *a:{})
    monkeypatch.setattr(reservation,'summaries',lambda *a:{})
    checked=[]
    monkeypatch.setattr(guard,'verify_catchup_no_write_state',lambda *a:checked.append(a))
    g=AuditedGitHub();g.preview=preview
    if unproved:g.prior.insert(0,dict(recovery.CATCHUP_NO_WRITE_EXECUTION,id=999))
    values=env()|dict(EXPECTED_CHECKPOINT=recovery.CATCHUP_NO_WRITE_CHECKPOINT)
    state=({'protected':'native'},{'protected':'parent'})
    action=(lambda:guard.history_controls(g,'a'*40,checkpoint_value=values['EXPECTED_CHECKPOINT'],reconciliation_state=state)) if preview else (
        lambda:guard.controls(values,'a'*40,g,reconciliation_state=state))
    if unproved:
        with pytest.raises(RuntimeError,match='previous_dispatch_no_retry'):action()
    else:
        assert action() is True and checked
    assert any('/'+guard.WORKFLOW+'/runs?' in r for r in g.requests)


def test_new_fixed_failure_needs_state_even_with_valid_job():
    g=AuditedGitHub();values=env()|dict(EXPECTED_CHECKPOINT=recovery.CATCHUP_NO_WRITE_CHECKPOINT)
    assert guard.controls(values,'a'*40,g,defer_reconciliation=True) is True
    with pytest.raises(RuntimeError,match='no_write_state_required'):guard.controls(values,'a'*40,g)


@pytest.mark.parametrize('preview',[False,True])
def test_fixed_failure_history_cannot_be_deleted_or_hidden(preview):
    g=AuditedGitHub();g.preview=preview
    g.prior=[r for r in g.prior if r['id']!=recovery.CATCHUP_NO_WRITE_RUN]
    values=env()|dict(EXPECTED_CHECKPOINT=recovery.CATCHUP_NO_WRITE_CHECKPOINT)
    action=(lambda:guard.history_controls(g,'a'*40,checkpoint_value=values['EXPECTED_CHECKPOINT'],defer_reconciliation=True)) if preview else (
        lambda:guard.controls(values,'a'*40,g,defer_reconciliation=True))
    with pytest.raises(RuntimeError,match='no_write_history_missing'):action()


def test_later_business_checkpoint_does_not_replace_execution_proof(monkeypatch,tmp_path):
    n,p,cp,finance,docs,_=audited(monkeypatch,tmp_path)
    later=(datetime.fromisoformat(cp)+timedelta(days=1)).isoformat()
    from app import aupay_card_incremental_coverage as coverage
    monkeypatch.setattr(coverage,'successful_coverage_proofs',lambda *a:{})
    with pytest.raises(RuntimeError,match='no_write_followup_unproven'):
        recovery.verify_catchup_no_write_state(n,p,later)


@pytest.mark.parametrize('execution_before_failure',[False,True])
def test_real_completed_successor_unlocks_later_coverage_without_comparing_business_to_wall_time(monkeypatch,tmp_path,execution_before_failure):
    from app import aupay_card_first_window_apply as apply
    n,parent,cp,finance,docs,p=audited(monkeypatch,tmp_path)
    wall=p.execution['clock']();later=datetime.fromisoformat(cp)+timedelta(days=1)
    p.args.update(expected_checkpoint=cp,now=wall,cutoff=later)
    p.args['gmail'].stamp=str(int((datetime.fromisoformat(cp)-timedelta(days=1)).timestamp())*1000)
    prepared=apply.prepare(**p.args)
    prepared['github_execution']=dict(id=101,head_sha='a'*40,head_branch='main',run_attempt=1,
        event='workflow_dispatch',path='.github/workflows/'+guard.WORKFLOW,created_at=wall.isoformat())
    p.execution.update(prepared=prepared,final_read=lambda **kw:None)
    apply.execute_fixed(**p.execution)
    after=validate(p.durable.transport.read(),p.durable.binding)
    failed_at=wall+timedelta(seconds=1 if execution_before_failure else -1)
    monkeypatch.setattr(recovery,'CATCHUP_NO_WRITE_EXECUTION',dict(recovery.CATCHUP_NO_WRITE_EXECUTION,created_at=failed_at.isoformat()))
    assert later<failed_at  # Historical catch-up cutoff remains before execution.
    action=lambda:recovery.verify_catchup_no_write_state(after,p.ledger.value,later.isoformat())
    if execution_before_failure:
        with pytest.raises(RuntimeError,match='no_write_followup_unproven'):action()
    else:
        action()
        assert successful_coverage_proofs(after,p.ledger.value,later.isoformat())[101]['conclusion']=='success'
