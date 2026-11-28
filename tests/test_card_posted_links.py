"""Real existing APIs in memory; partial append is never an append permit."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import base64
import json

import pytest

from app import auto_expense_reconciliation as op
from app import aupay_card_incremental_recovery as recovery
from app.aupay_card_aliases import digest,resolve_aliases,validate_owner_expenses
from app.aupay_card_expense_stage import (batch_owner_links,selected_plan,contract_sha256,
    owner_updates,verify_delta,execute_selected)
from app.aupay_card_reconciliation_diagnostics import ReconciliationDiagnostics
from app.aupay_card_recurring import SqliteRecurringRunState
from app.auto_expense import AutoExpensePipeline
from app.drive_run_state import DurableState,StateBinding,envelope,snapshot,validate
from app.production_ledger import ProductionLedger
from test_card_selected_expenses import DB,Transport
from test_auto_expense_reconciliation import release_case

C3='2026-11-26T21:43:48+09:00'


def partial_case(tmp_path,monkeypatch):
    db=DB();template=deepcopy(db.rows[0]);db.rows=[]
    for i in range(18):
        row=deepcopy(template);row[0]=row[3]='aupaycard-mail:'+format(i+1,'024x')+':001'
        row[6]=3000 if i<17 else 13116;row[5]=f'合成店{i}'
        db.rows.append(row)
    ids=[r[0] for r in db.rows];categories={v:['食費','食料品'] for v in ids}
    plan=selected_plan(db,ids,categories,True)
    path=tmp_path/'baseline';state=SqliteRecurringRunState(path/'recurring.sqlite3',repo_root=tmp_path/'repo')
    registry=[dict(owner=r[0],owner_row_sha256=digest(r),business_fingerprint=format(i+1,'024x'),aliases={r[0]:r[10]}) for i,r in enumerate(db.rows)]
    state.record(dict(run_id='existing',status='noop',source_window_start=C3,source_window_end=C3,card_aliases=registry),advance_checkpoint=True)
    cb=StateBinding('au_pay_card_gmail',db.sid,'folder','native');nt=Transport(envelope(cb,snapshot(path,cb),generation=139,phase='pending',attempt='b'*32))
    _,pt,_,_,release=release_case(monkeypatch);parent=json.loads(pt.payload);parent['generation']=1434
    pt.payload=json.dumps(parent,sort_keys=True,separators=(',',':')).encode();native=validate(nt.payload,cb)
    before=db.financial()
    for r in plan['rows']:db.expenses.append(deepcopy(r['expense']))
    for r in plan['rows'][:8]:db.rows[r['row_num']-2]=deepcopy(r['after'])
    stopped=db.financial();full=deepcopy(stopped)
    for render in ('UNFORMATTED_VALUE','FORMULA'):
        for r in plan['rows']:full[('取込データ',render)][r['row_num']-1]=deepcopy(r['after'])
    # Financial reader matches Sheets' trailing-blank representation.
    def trim(value):
        value=deepcopy(value)
        for rows in value.values():
            for row in rows:
                while row and row[-1]=='':row.pop()
        return value
    before,stopped,full=map(trim,(before,stopped,full))
    original=db.financial;db.financial=lambda:trim(original())
    h=lambda data:digest({str(k):v for k,v in data.items()})
    record=dict(financial_write_authority=False,plan=plan,checkpoint=C3,business_contract=contract_sha256(plan),
        failed_run=dict(id=46132550495,conclusion='failure'),native_generation=139,native_attempt='b'*32,
        parent_before_raw_b64=base64.b64encode(pt.payload).decode(),native_before_sha256=op.sha(nt.payload),
        history_sha256=digest(recovery._rows(native,'recurring.sqlite3','recurring_runs')),
        checkpoint_sha256=digest(recovery._rows(native,'recurring.sqlite3','recurring_checkpoint')),
        native_file_hashes={k:v['sha256'] for k,v in native['files'].items()},owner_updates_sha256=digest(owner_updates(plan,registry)),
        financial_before_sha256=h(before),financial_sha256=h(full),partial_financial_sha256=h(stopped),
        expense_before_rows=len(before[('支出明細','UNFORMATTED_VALUE')]),missing_link_rows=[r['row_num'] for r in plan['rows'][8:]])
    monkeypatch.setattr(op,'POSTED_LINKS_RECORD_SHA256',digest(record))
    monkeypatch.setattr(recovery,'preserved_unknown_proofs',lambda *a:{'synthetic':True})
    def observe():
        complete,projected=op.posted_financial(record,db.financial(),None)
        assert complete
        return dict(imports=deepcopy(db.rows),expenses=deepcopy(db.expenses))
    def args(index=0):
        return dict(record=record,durable=DurableState(nt,cb),ledger=ProductionLedger(pt,release['binding']),
            state_dir=tmp_path/f'entry-{index}'/'state',repo_root=tmp_path/'repo',native_read=nt.read,
            parent_read=pt.read,observe=observe,guard=lambda:None,operator=dict(id=70+index,head_sha='a'*40,
                run_attempt=1,event='workflow_dispatch',path='.github/workflows/kakeibo-production.yml'),diagnostics=ReconciliationDiagnostics())
    missing=[r for r in plan['rows'] if r['row_num'] in record['missing_link_rows']]
    return SimpleNamespace(db=db,nt=nt,pt=pt,cb=cb,record=record,plan=plan,missing=missing,args=args,before=before)


def test_partial_eighteen_links_ten_only_then_existing_confirmation_fresh_restore(tmp_path,monkeypatch):
    p=partial_case(tmp_path,monkeypatch);old=deepcopy(p.db.rows);expenses=deepcopy(p.db.expenses)
    complete,projected=op.posted_financial(p.record,p.db.financial(),None);assert not complete
    with pytest.raises(RuntimeError,match='existing_unlinked'):selected_plan(p.db,p.plan['import_ids'],p.plan['categories_by_id'],True)
    batch_owner_links(p.db,p.missing,guard=lambda:None)
    assert p.db.rows[:8]==old[:8] and p.db.expenses==expenses
    assert len(p.db.writes)==1 and len(p.db.writes[0][1])==10
    result=op.confirm_posted_expenses(**p.args());assert result['reconciled_expenses']==18
    assert len(p.nt.writes)==2 and len(p.pt.writes)==1
    assert validate(p.nt.payload,p.cb)['generation']==141 and json.loads(p.pt.payload)['generation']==1435
    fresh=tmp_path/'fresh';DurableState(p.nt,p.cb).restore(fresh)
    state=SqliteRecurringRunState(fresh/'recurring.sqlite3',repo_root=tmp_path/'repo')
    assert state.successful_window_end().isoformat()==C3
    resolve_aliases([],p.db.rows,state.alias_registry());validate_owner_expenses(state.alias_registry(),p.db.expenses)
    assert len([v for v in state.alias_registry() if v.get('owner_changes')])==18
    plan=selected_plan(p.db,p.plan['import_ids'],p.plan['categories_by_id'],True,p.record['business_contract'])
    assert not plan['rows'] and len(plan['already_posted'])==18
    op.confirm_posted_expenses(**p.args(1))
    assert len(p.nt.writes)==2 and len(p.pt.writes)==1 and len(p.db.writes)==1


@pytest.mark.parametrize('boundary',['before_proof','after_proof','before_ready','after_ready','before_parent','after_parent'])
def test_completed_link_write_not_repeated_after_state_loss(tmp_path,monkeypatch,boundary):
    p=partial_case(tmp_path,monkeypatch);batch_owner_links(p.db,p.missing,guard=lambda:None)
    nw,pw=p.nt.write,p.pt.write
    def native_write(value):
        stage='proof' if validate(value,p.cb)['generation']==140 else 'ready'
        if boundary=='before_'+stage:raise TimeoutError('secret')
        nw(value)
        if boundary=='after_'+stage:raise TimeoutError('secret')
    def parent_write(value):
        if boundary=='before_parent':raise TimeoutError('secret')
        pw(value)
        if boundary=='after_parent':raise TimeoutError('secret')
    p.nt.write=native_write;p.pt.write=parent_write
    with pytest.raises(TimeoutError):op.confirm_posted_expenses(**p.args())
    p.nt.write=nw;p.pt.write=pw;op.confirm_posted_expenses(**p.args(1))
    assert len(p.db.writes)==1 and len(p.nt.writes)==2 and len(p.pt.writes)==1


@pytest.mark.parametrize('change',['id','multiple','amount','category','reason','source','N','other','J','partial_J'])
def test_changed_or_ambiguous_finance_never_allows_confirmation(tmp_path,monkeypatch,change):
    p=partial_case(tmp_path,monkeypatch)
    if change=='id':p.db.expenses[-1][0]='other'
    if change=='multiple':p.db.expenses.append(deepcopy(p.db.expenses[-1]))
    if change=='amount':p.db.expenses[-1][4]+=1
    if change=='category':p.db.expenses[-1][5]='changed'
    if change=='reason':p.db.expenses[-1][11]='changed'
    if change=='source':p.db.expenses[-1][10]='other'
    if change=='N':p.db.expenses[-1].append('=BAD()')
    if change=='other':p.db.rows[0][11]='changed'
    if change=='J':p.db.rows[8][9]='other'
    if change=='partial_J':p.db.rows[8][9]=p.plan['rows'][8]['after'][9]
    with pytest.raises(RuntimeError):op.posted_financial(p.record,p.db.financial(),None)
    assert not p.db.writes and not p.nt.writes and not p.pt.writes


@pytest.mark.parametrize('delivery',['before','after'])
def test_unknown_batch_response_stops_without_state_or_append_and_no_automatic_resend(tmp_path,monkeypatch,delivery):
    p=partial_case(tmp_path,monkeypatch);original=p.db.update_rows
    def unknown(sheet,rows):
        if delivery=='after':original(sheet,rows)
        raise TimeoutError('secret response')
    p.db.update_rows=unknown
    with pytest.raises(TimeoutError):batch_owner_links(p.db,p.missing,guard=lambda:None)
    assert not p.nt.writes and not p.pt.writes and len(p.db.expenses)==18
    complete,_=op.posted_financial(p.record,p.db.financial(),None)
    assert complete==(delivery=='after') and len(p.db.writes)==int(delivery=='after')
    if complete:
        # A future explicit approval recognizes completed J state. It never
        # calls batch_owner_links again; confirmation is the state-only API.
        op.confirm_posted_expenses(**p.args(1));assert len(p.db.writes)==1


@pytest.mark.parametrize('change',['last_row','duplicate','guard'])
def test_batch_checks_every_owner_before_first_write(tmp_path,monkeypatch,change):
    p=partial_case(tmp_path,monkeypatch);rows=deepcopy(p.missing)
    if change=='last_row':p.db.rows[-1][6]+=1
    if change=='duplicate':rows.append(rows[-1])
    def guard():
        if change=='guard':raise TimeoutError('secret')
    with pytest.raises((RuntimeError,TimeoutError)):batch_owner_links(p.db,rows,guard=guard)
    assert not p.db.writes and not p.nt.writes and not p.pt.writes


def test_http_status_and_fixed_stage_are_bounded_through_parent():
    from googleapiclient.errors import HttpError
    import httplib2
    from app.production_run import SourceFailure,card_diagnostic_fields
    d=ReconciliationDiagnostics();d.mark('owner_links_readback')
    error=HttpError(httplib2.Response({'status':'503'}),b'secret raw body')
    value=d.report(error)
    report=SourceFailure('card_expense_failed','card_expense_apply',card_diagnostics=value).report()
    assert report['diagnostic']['http_status']==503 and report['diagnostic']['last_stage']=='owner_links_readback'
    assert 'secret' not in json.dumps(report)
    assert card_diagnostic_fields(dict(last_stage='secret',http_status=True,error_code='secret',body='secret'))=={}


def test_child_status_and_stage_reach_existing_parent_transport(monkeypatch,capsys):
    import httplib2
    from googleapiclient.errors import HttpError
    from app import aupay_card_expense_stage as stage,production_flow as flow
    from app.production_run import SourceFailure
    def failed(db,env,*,apply,diagnostics):
        diagnostics.mark('owner_links_readback')
        raise HttpError(httplib2.Response({'status':'503'}),b'PRIVATE SOURCE BODY')
    monkeypatch.setattr(stage,'run_from_environment',failed)
    with pytest.raises(SystemExit):stage.run_cli(None,{},apply=True)
    output=capsys.readouterr().out;assert 'PRIVATE SOURCE BODY' not in output
    monkeypatch.setattr(flow.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=1,stdout=output,stderr='PRIVATE'))
    with pytest.raises(SourceFailure) as error:flow.invoke('auto_expense',apply=True,env={})
    assert error.value.report()['diagnostic']['http_status']==503
    assert error.value.report()['diagnostic']['last_stage']=='owner_links_readback'


def test_shared_bank_code_contract_is_pinned():
    from app.aupay_card_bank_premises import code_contract,REVIEWED_BANK_CODE_SHA256
    assert digest(code_contract()) in REVIEWED_BANK_CODE_SHA256


@pytest.mark.parametrize('apply',[False,True])
def test_signed_hosted_preview_then_j_only_confirmation(tmp_path,monkeypatch,key,apply):
    from test_card_posted_hosted import hosted
    saved={}
    def fixture(path,mp):
        p=partial_case(path,mp);saved['p']=p
        return p.db,p.nt,p.pt,p.cb,p.args
    env,nt,pt,factories,*_=hosted(tmp_path,monkeypatch,key,apply,posted_fixture=fixture)
    p=saved['p'];before=deepcopy(p.db.rows);expenses=deepcopy(p.db.expenses)
    result=op.run_from_environment(env,apply=apply)
    assert result['success'] and p.db.expenses==expenses and p.db.rows[:8]==before[:8]
    if not apply:
        assert result['missing_links']==10 and result['already_appended']==18
        assert not p.db.writes and not nt.writes and not pt.writes and not factories
    else:
        assert result['reconciled_expenses']==18 and result['import_link_cells']==10 and result['expense_rows_written']==0
        assert len(p.db.writes)==1 and len(p.db.writes[0][1])==10
        assert len(nt.writes)==2 and len(pt.writes)==1 and factories.count('J-only')==1


@pytest.mark.parametrize('change',['finance','partial_response','unknown','expired','competing'])
def test_hosted_link_failure_never_finalizes_state(tmp_path,monkeypatch,key,change):
    from test_card_posted_hosted import hosted
    from test_aupay_card_reconciliation_entry import capsule
    saved={}
    def fixture(path,mp):
        p=partial_case(path,mp);saved['p']=p
        return p.db,p.nt,p.pt,p.cb,p.args
    env,nt,pt,factories,history,active,att,record,rsa=hosted(tmp_path,monkeypatch,key,True,posted_fixture=fixture)
    p=saved['p'];update=p.db.update_rows
    if change=='finance':p.db.expenses[-1][4]+=1
    if change=='competing':active.append(dict(id=99))
    if change=='expired':
        att['approval']['expires_at']='2020-01-01T00:00:00+00:00'
        env['CARD_EXPENSE_RECONCILIATION']=op.POSTED_LINKS_PREFIX+capsule(record,att,rsa)
    if change in {'unknown','partial_response'}:
        def fail(sheet,rows):
            if change=='partial_response':return update(sheet,rows[:1])
            update(sheet,rows);raise TimeoutError('secret response')
        p.db.update_rows=fail
    with pytest.raises(op.AutoExpenseReconciliationFailure) as error:op.run_from_environment(env,apply=True)
    assert not nt.writes and not pt.writes and len(p.db.expenses)==18
    if change=='unknown':assert error.value.diagnostic['last_stage']=='owner_links_write'
    if change=='partial_response':assert error.value.diagnostic['last_stage']=='owner_links_readback'
    if change in {'unknown','partial_response'}:
        assert error.value.diagnostic['effects']['financial']=='writer_entered'
    assert len(p.db.writes)<=1


from test_private_state_bindings import key


@pytest.mark.parametrize('mode',['normal','late_mismatch','unknown','readback_failure'])
def test_remaining_twenty_two_one_batch_or_stop_without_resend(tmp_path,mode):
    db=DB();template=deepcopy(db.rows[0]);db.rows=[]
    for i in range(22):
        row=deepcopy(template);row[0]=row[3]='aupaycard-mail:'+format(i+100,'024x')+':001'
        row[5]=f'合成店{i}';row[6]=8000 if i<21 else 11335;db.rows.append(row)
    ids=[r[0] for r in db.rows];cats={i:['食費','食料品'] for i in ids};plan=selected_plan(db,ids,cats,True)
    registry=[dict(owner=r[0],owner_row_sha256=digest(r),business_fingerprint=format(i+100,'024x'),aliases={r[0]:r[10]}) for i,r in enumerate(db.rows)]
    path=tmp_path/'state';state=SqliteRecurringRunState(path/'recurring.sqlite3',repo_root=tmp_path/'repo')
    state.record(dict(run_id='initial',status='noop',source_window_start=C3,source_window_end=C3,card_aliases=registry),advance_checkpoint=True)
    cb=StateBinding('au_pay_card_gmail',db.sid,'folder','native');nt=Transport(envelope(cb,snapshot(path,cb)))
    durable=DurableState(nt,cb);path=tmp_path/'restored';durable.restore(path)
    state=SqliteRecurringRunState(path/'recurring.sqlite3',repo_root=tmp_path/'repo')
    initial=deepcopy(db.financial());append=db.append_raw;update=db.update_rows;reads=db.get_raw_ranges
    def after_append(sheet,rows):
        append(sheet,rows)
        if mode=='late_mismatch':db.rows[-1][10]='changed'
    db.append_raw=after_append
    def update_once(sheet,rows):
        update(sheet,rows)
        if mode=='unknown':raise TimeoutError('secret')
    db.update_rows=update_once
    calls=[]
    def read(ranges):
        calls.append(1)
        if mode=='readback_failure' and len(calls)==2:raise TimeoutError('secret')
        return reads(ranges)
    db.get_raw_ranges=read
    diagnostic=ReconciliationDiagnostics()
    args=dict(db=db,plan=plan,state=state,durable=durable,state_dir=path,registry=registry,financial_read=db.financial,guard=lambda:None,category_rule_auto_apply_enabled=True,diagnostics=diagnostic)
    if mode=='normal':
        result=execute_selected(**args);assert result['write_requests']==2
        verify_delta(initial,db.financial(),plan)
        assert len(db.writes)==2 and len(db.writes[1][1])==22 and len(calls)==2
    else:
        with pytest.raises((RuntimeError,TimeoutError)):execute_selected(**args)
        assert diagnostic.effects['financial']=='append_possible'
        count=len(db.writes)
        with pytest.raises(RuntimeError):execute_selected(**args)
        assert durable.inspect()['phase']=='pending' and len(db.writes)==count
        assert len(db.writes)==(1 if mode=='late_mismatch' else 2)
    assert len(db.expenses)==22 and state.successful_window_end().isoformat()==C3
