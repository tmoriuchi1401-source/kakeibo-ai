from copy import deepcopy
from dataclasses import replace
import base64
import json
from types import SimpleNamespace

import pytest

from app import auto_expense_reconciliation as op, production_flow as flow
from app.aupay_card_aliases import digest
from app.aupay_card_expense_stage import selected_plan, contract_sha256, run_cli
from app.aupay_card_expense_stage import planning_profile
from app.drive_run_state import StateBinding, validate
from app.production_ledger import initial_ledger
from app.production_run import SourceFailure
from test_card_selected_expenses import DB, OWNER, C1, Transport, case


def release_case(monkeypatch):
    binding=StateBinding('production_run','selected-sheet','folder','parent')
    value=json.loads(initial_ledger(binding));value['generation']=1420
    value['sources']['auto_expense'].update(phase='pending',attempt='a'*32,error='source_execution_failed',
        last_success='2026-11-04T00:40:52+00:00',counts={'expenses_created':0},duration_seconds=3.129)
    raw=json.dumps(value,sort_keys=True,separators=(',',':')).encode();transport=Transport(raw)
    record=dict(failed_run={'id':46013990252,'conclusion':'failure','run_attempt':1},
        failed_attempt='a'*32,parent_before_raw_b64=base64.b64encode(raw).decode(),
        parent_before_revision='synthetic-revision',native_before_sha256='1'*64,checkpoint=C1)
    monkeypatch.setattr(op,'RECORD_SHA256',digest(record));saved=[];order=[]
    def save(archive):order.append('archive');saved.append(deepcopy(archive))
    def read():order.append('archive_readback');return saved[-1]
    def observe(*,parent_after=None):
        assert transport.read()==(parent_after or raw)
        return {'financial_sha256':'2'*64}
    args=dict(record=record,parent_raw=raw,binding=binding,parent_transport=transport,
        save_archive=save,read_archive=read,observe=observe,guard=lambda:None,apply=True)
    return value,transport,saved,order,args


def test_existing_release_api_preserves_all_other_fields_and_archives_first(monkeypatch):
    old,t,saved,order,args=release_case(monkeypatch)
    result=op.release_with_archive(**args);new=json.loads(t.read())
    assert len(t.writes)==1 and new['generation']==1421
    for source,item in old['sources'].items():
        if source!='auto_expense':assert new['sources'][source]==item
        else:
            for key in ('last_success','counts','duration_seconds'):assert new['sources'][source][key]==item[key]
    assert order[:2]==['archive','archive_readback'] and result['financial_write_requests']==0
    assert base64.b64decode(saved[0]['original_parent_exact_b64'])==args['parent_raw']
    assert saved[0]['failed_run']['conclusion']=='failure'
    assert 'native_before' not in saved[0] and len(json.dumps(saved[0]))<op.MAX_ARCHIVE_BYTES


@pytest.mark.parametrize('fault',['save','readback','snapshot','guard','parent_unknown'])
def test_failure_boundaries_never_retry_or_release_without_archive(monkeypatch,fault):
    old,t,saved,order,args=release_case(monkeypatch)
    def fail(*a,**k):raise TimeoutError('secret body must not be printed')
    if fault=='save':args['save_archive']=fail
    if fault=='readback':args['read_archive']=lambda:{'wrong':True}
    if fault=='snapshot':
        calls=[]
        def changed(**k):calls.append(1);return {'financial_sha256':'2'*64 if len(calls)==1 else '3'*64}
        args['observe']=changed
    if fault=='guard':args['guard']=fail
    if fault=='parent_unknown':
        original=t.write
        def unknown(raw):original(raw);fail()
        t.write=unknown
    with pytest.raises((RuntimeError,TimeoutError)):op.release_with_archive(**args)
    assert len(t.writes)==(1 if fault=='parent_unknown' else 0)


@pytest.mark.parametrize('change',['source','attempt','record','archive_size'])
def test_fixed_scope_and_capacity_fail_before_any_write(monkeypatch,change):
    _,t,saved,_,args=release_case(monkeypatch)
    if change=='source':args['record']['parent_before_raw_b64']='wrong'
    if change=='attempt':
        args['record']['failed_attempt']='b'*32;monkeypatch.setattr(op,'RECORD_SHA256',digest(args['record']))
    if change=='record':args['record']['failed_run']['id']=1
    if change=='archive_size':monkeypatch.setattr(op,'MAX_ARCHIVE_BYTES',1)
    with pytest.raises(RuntimeError):op.release_with_archive(**args)
    assert not saved and not t.writes


def test_operator_preview_is_pure(monkeypatch):
    _,t,saved,_,args=release_case(monkeypatch);args['apply']=False
    result=op.release_with_archive(**args)
    assert result['mode']=='preview' and not saved and not t.writes


def test_compact_archive_survives_real_durable_restore_without_checkpoint_change(tmp_path):
    db,state,durable,registry,args=case(tmp_path)
    archive={'failed_run':{'id':46013990252},'parent_exact_b64':'synthetic-parent','financial_sha256':'a'*64}
    new_path=tmp_path/'archive-state'
    raw=op.persist_archive(durable,new_path,{'checkpoint':C1},archive,lambda:None)
    native=validate(raw,durable.binding)
    from app.aupay_card_reservation_recovery import summaries
    assert summaries(native)[op.ARCHIVE_RUN]['operator_reconciliation']==archive
    from app.aupay_card_recurring import SqliteRecurringRunState
    assert SqliteRecurringRunState(new_path/'recurring.sqlite3',repo_root=tmp_path/'repo').successful_window_end().isoformat()==C1
    restored=tmp_path/'another-runner'
    from app.drive_run_state import DurableState
    DurableState(durable.transport,durable.binding).restore(restored)
    assert SqliteRecurringRunState(restored/'recurring.sqlite3',repo_root=tmp_path/'repo').alias_registry()==registry


@pytest.mark.parametrize('change',['rule_flag','reason','contract'])
def test_contract_rejection_precedes_parent_reservation(monkeypatch,tmp_path,change):
    db=DB();categories={OWNER:['食費','食料品']}
    expected=contract_sha256(selected_plan(db,[OWNER],categories,False))
    original=__import__('app.auto_expense',fromlist=['AutoExpensePipeline']).AutoExpensePipeline._with_approved_rule
    if change in {'rule_flag','reason'}:
        def rules(self,decisions,cats):
            if change=='reason' or self.category_rule_auto_apply_enabled:
                return [replace(d,reason=d.reason+'; approved rule held') for d in decisions]
            return decisions
        monkeypatch.setattr('app.auto_expense.AutoExpensePipeline._with_approved_rule',rules)
    else:expected='0'*64
    calls=[]
    def invoke(source,*,apply,env,**kwargs):
        calls.append(apply)
        selected_plan(db,[OWNER],categories,change=='rule_flag',expected)
        return {'failure':0}
    monkeypatch.setattr(flow,'invoke',invoke)
    ledger=SimpleNamespace(begin=lambda *a:pytest.fail('pending before valid contract'),complete=lambda *a:None,fail=lambda *a:pytest.fail('must not leave pending'))
    runner=flow.assemble({'CARD_EXPENSE_REQUEST':'fixed'},tmp_path,apply=True,bank_apply=False,ledger=ledger)['auto_expense']
    with pytest.raises(RuntimeError,match='card_expense_approved_plan_changed'):runner()
    assert calls==[False] and not db.writes


@pytest.mark.parametrize('message,expected',[('card_expense_approved_plan_changed','card_expense_approved_plan_changed'),('RAW SECRET 4386','card_expense_failed')])
def test_safe_child_error_code_without_exception_body(monkeypatch,capsys,message,expected):
    def fail(*a,**k):raise RuntimeError(message)
    monkeypatch.setattr('app.aupay_card_expense_stage.run_from_environment',fail)
    with pytest.raises(SystemExit):run_cli(None,{},apply=False)
    value=json.loads(capsys.readouterr().out)
    assert value['error']==expected and value['stage']=='card_expense_preflight'
    assert 'RAW SECRET' not in str(value)


def test_fixed_child_code_reaches_existing_parent_diagnostic(monkeypatch):
    failure={'error':'card_expense_approved_plan_changed','stage':'card_expense_preflight','failure_class':'RuntimeError'}
    monkeypatch.setattr(flow.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=1,stdout=json.dumps(failure),stderr='SECRET'))
    with pytest.raises(SourceFailure) as exc:flow.invoke('auto_expense',apply=False,env={})
    assert str(exc.value)==failure['error'] and exc.value.stage==failure['stage']


@pytest.mark.parametrize('change',['flag_without_matching_rule','rules','master'])
def test_effective_profile_mismatch_alone_leaves_no_parent_pending(monkeypatch,tmp_path,change):
    db=DB();env=dict(CATEGORY_RULE_AUTO_APPLY_ENABLED='false',KAKEIBO_AMAZON_MONEY_MODE='confirmed-v1')
    expected=digest(planning_profile(db,env));categories={OWNER:['食費','食料品']}
    contract=contract_sha256(selected_plan(db,[OWNER],categories,False))
    if change=='flag_without_matching_rule':env['CATEGORY_RULE_AUTO_APPLY_ENABLED']='true'
    if change=='rules':db.category_rules=lambda:[['changed-rule-metadata']]
    if change=='master':db.categories=lambda:[('その他','未分類'),('食費','食料品'),('交通','電車')]
    def invoke(*a,**k):return selected_plan(db,[OWNER],categories,True,contract,expected_profile=expected,env=env)
    monkeypatch.setattr(flow,'invoke',invoke)
    ledger=SimpleNamespace(begin=lambda *a:pytest.fail('pending created'),complete=lambda *a:None,fail=lambda *a:pytest.fail('pending left behind'))
    runner=flow.assemble({'CARD_EXPENSE_REQUEST':'fixed'},tmp_path,apply=True,bank_apply=False,ledger=ledger)['auto_expense']
    with pytest.raises(RuntimeError,match='card_expense_profile_changed'):runner()
    assert not db.writes
