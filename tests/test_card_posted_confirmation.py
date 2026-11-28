from copy import deepcopy
from pathlib import Path
import base64
import json

import pytest

from app import auto_expense_reconciliation as op
from app import aupay_card_incremental_recovery as recovery
from app.aupay_card_aliases import digest,resolve_aliases,validate_owner_expenses
from app.aupay_card_expense_stage import owner_updates,contract_sha256
from app.aupay_card_incremental_recovery import _rows
from app.aupay_card_reconciliation_diagnostics import ReconciliationDiagnostics
from app.auto_expense import AutoExpensePipeline
from app.aupay_card_recurring import SqliteRecurringRunState
from app.drive_run_state import DurableState,validate,envelope
from app.production_ledger import ProductionLedger
from test_card_selected_expenses import case,OWNER,OTHER,C1
from test_auto_expense_reconciliation import release_case


def posted_case(tmp_path,monkeypatch):
    db,state,durable,registry,args=case(tmp_path)
    plan=AutoExpensePipeline(db).plan_card_expenses([OWNER,OTHER],categories_by_id={v:['食費','食料品'] for v in (OWNER,OTHER)})
    old=durable.inspect()
    durable.transport.payload=envelope(durable.binding,old['files'],generation=130,phase='pending',attempt='b'*32)
    native=durable.inspect();_,parent,_,_,release=release_case(monkeypatch)
    value=json.loads(parent.payload);value['generation']=1423
    parent.payload=json.dumps(value,sort_keys=True,separators=(',',':')).encode()
    record=dict(financial_write_authority=False,plan=plan,checkpoint=C1,business_contract=contract_sha256(plan),
        failed_run={'id':46083853434,'conclusion':'failure'},native_generation=130,native_attempt='b'*32,
        parent_before_raw_b64=base64.b64encode(parent.payload).decode(),native_before_sha256=op.sha(durable.transport.payload),
        history_sha256=digest(_rows(native,'recurring.sqlite3','recurring_runs')),
        checkpoint_sha256=digest(_rows(native,'recurring.sqlite3','recurring_checkpoint')),
        native_file_hashes={k:v['sha256'] for k,v in native['files'].items()},owner_updates_sha256=digest(owner_updates(plan,registry)),
        financial_before_sha256='1'*64,financial_sha256='2'*64)
    monkeypatch.setattr(op,'POSTED_RECORD_SHA256',digest(record))
    monkeypatch.setattr(recovery,'preserved_unknown_proofs',lambda *a:{'synthetic':True})
    # The financial operation is already complete before the state-only entry.
    for row in plan['rows']:db.expenses.append(deepcopy(row['expense']));db.rows[row['row_num']-2]=deepcopy(row['after'])
    before=deepcopy(db.financial())
    def observe():
        assert db.financial()==before and not db.writes
        return dict(imports=deepcopy(db.rows),expenses=deepcopy(db.expenses))
    def arguments(index=0):
        return dict(record=record,durable=DurableState(durable.transport,durable.binding),
            ledger=ProductionLedger(parent,release['binding']),state_dir=tmp_path/f'attempt-{index}'/'state',
            repo_root=tmp_path/'repo',native_read=durable.transport.read,parent_read=parent.read,observe=observe,
            guard=lambda:None,operator=dict(id=70+index,head_sha='a'*40,run_attempt=1,event='workflow_dispatch',path='.github/workflows/kakeibo-production.yml'),
            diagnostics=ReconciliationDiagnostics())
    return db,durable.transport,parent,durable.binding,arguments


def test_posted_pair_pending_proof_ready_parent_fresh_alias_and_no_financial_write(tmp_path,monkeypatch):
    db,nt,pt,binding,args=posted_case(tmp_path,monkeypatch);before=json.loads(pt.payload)
    old=deepcopy(validate(nt.payload,binding));result=op.confirm_posted_expenses(**args())
    assert result['success'] and not result['financial_write_requests'] and not result['archive_write_requests']
    assert [(validate(r,binding)['generation'],validate(r,binding)['phase']) for r in nt.writes]==[(131,'pending'),(132,'ready')]
    assert len(pt.writes)==1 and json.loads(pt.payload)['generation']==1424 and not db.writes
    after=json.loads(pt.payload);expected=deepcopy(before);expected['generation']+=1
    expected['sources']['auto_expense'].update(phase='ready',attempt='',error='');assert after==expected
    fresh=tmp_path/'independent';DurableState(nt,binding).restore(fresh)
    restored=SqliteRecurringRunState(fresh/'recurring.sqlite3',repo_root=tmp_path/'repo')
    assert restored.successful_window_end().isoformat()==C1
    resolve_aliases([],db.rows,restored.alias_registry());validate_owner_expenses(restored.alias_registry(),db.expenses)
    assert all(r in _rows(validate(nt.payload,binding),'recurring.sqlite3','recurring_runs') for r in _rows(old,'recurring.sqlite3','recurring_runs'))
    replay=AutoExpensePipeline(db).plan_card_expenses([OWNER,OTHER],categories_by_id={v:['食費','食料品'] for v in (OWNER,OTHER)})
    assert len(replay['already_posted'])==2 and not replay['rows']
    op.confirm_posted_expenses(**args(1))
    assert len(nt.writes)==2 and len(pt.writes)==1


@pytest.mark.parametrize('boundary',['before_proof','after_proof','before_ready','after_ready','before_parent','after_parent'])
def test_loss_at_each_save_requires_new_entry_and_skips_finished_steps(tmp_path,monkeypatch,boundary):
    db,nt,pt,binding,args=posted_case(tmp_path,monkeypatch);native_write=nt.write;parent_write=pt.write
    def write_native(value):
        generation=validate(value,binding)['generation'];kind='proof' if generation==131 else 'ready'
        if boundary=='before_'+kind:raise TimeoutError('body-secret')
        native_write(value)
        if boundary=='after_'+kind:raise TimeoutError('body-secret')
    def write_parent(value):
        if boundary=='before_parent':raise TimeoutError('body-secret')
        parent_write(value)
        if boundary=='after_parent':raise TimeoutError('body-secret')
    nt.write=write_native;pt.write=write_parent
    with pytest.raises(TimeoutError):op.confirm_posted_expenses(**args())
    nt.write=native_write;pt.write=parent_write
    op.confirm_posted_expenses(**args(1))
    assert len(nt.writes)==2 and len(pt.writes)==1 and not db.writes
    assert validate(nt.payload,binding)['phase']=='ready'


@pytest.mark.parametrize('change',['financial','history','checkpoint','generation','attempt','owner_hash','target','guard'])
def test_unexplained_changes_never_release_or_write_native(tmp_path,monkeypatch,change):
    db,nt,pt,binding,args=posted_case(tmp_path,monkeypatch);kw=args()
    if change=='financial':db.expenses[-1][4]+=1
    if change=='history':kw['record']['history_sha256']='0'*64
    if change=='checkpoint':kw['record']['checkpoint_sha256']='0'*64
    if change=='owner_hash':kw['record']['owner_updates_sha256']='0'*64
    if change in {'history','checkpoint','owner_hash'}:monkeypatch.setattr(op,'POSTED_RECORD_SHA256',digest(kw['record']))
    if change in {'generation','attempt'}:
        n=validate(nt.payload,binding)
        nt.payload=envelope(binding,n['files'],generation=129 if change=='generation' else 130,phase='pending',attempt='c'*32 if change=='attempt' else 'b'*32)
    if change=='target':
        p=json.loads(pt.payload);p['sources']['auto_expense']['last_success']='';pt.payload=json.dumps(p).encode()
    if change=='guard':kw['guard']=lambda:(_ for _ in ()).throw(TimeoutError('secret'))
    with pytest.raises((RuntimeError,AssertionError,TimeoutError)):op.confirm_posted_expenses(**kw)
    assert not nt.writes and not pt.writes and not db.writes
