"""Existing expense writer: one owner link, immutable aliases, no replay."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import json

import pytest

from app.auto_expense import AutoExpensePipeline,expense_id
from app.aupay_card_aliases import digest,merge_registry,resolve_aliases,validate_owner_expenses
from app.aupay_card_expense_stage import execute_selected,owner_updates,contract_sha256
from app.aupay_card_recurring import SqliteRecurringRunState
from app.drive_run_state import StateBinding,DurableState,envelope,snapshot,validate
from app.sheets import HEADERS

OWNER='aupaycard-mail:'+'a'*24+':001'
OTHER='aupaycard-mail:'+'b'*24+':001'
TRANSFER='aupaycard-mail:'+'c'*24+':001'
C1='2026-11-04T21:00:33+09:00'


class DB:
    sid='selected-sheet'
    def __init__(self):
        self.rows=[[i,'2026-11-27T12:00:00+09:00','au PAYカード',i,'2026-11-01','合成店',amount,'1回払い',status,'','f'*64,'元メモ']
                   for i,amount,status in [(OWNER,4386,'auto_expense'),(OTHER,145,'auto_expense'),(TRANSFER,120,'transfer_aupay_charge')]]
        self.expenses=[];self.writes=[];self.mode='normal'
    def get(self,rng):
        if rng=="'カテゴリ'!A:B":return [['大','小']]+[list(v) for v in self.categories()]
        if rng=="'カテゴリ自動分類ルール'!A:R":return [['rule']]+self.category_rules()
        if rng=='取込データ!A2:L':return deepcopy(self.rows)
        if rng=='支出明細!A2:M':return deepcopy(self.expenses)
        if rng=='支出明細!A1:M1':return [HEADERS['支出明細']]
        if rng.startswith('取込データ!A') and ':L' in rng:
            number=int(rng.split('!A')[1].split(':')[0])
            if self.mode=='sort_before_link':self.rows.reverse()
            return [deepcopy(self.rows[number-2])]
        raise AssertionError(rng)
    get_raw=get
    def get_raw_ranges(self,ranges):
        return [self.get(r.replace("'",'') if r.startswith("'取込データ'") else r) for r in ranges]
    @property
    def svc(self):return self
    def spreadsheets(self):return self
    def values(self):return self
    def batchUpdate(self,*,spreadsheetId,body):
        from types import SimpleNamespace
        assert spreadsheetId==self.sid and body['valueInputOption']=='RAW'
        def execute(*,num_retries):
            assert num_retries==0
            rows=[]
            for cell in body['data']:
                number=int(cell['range'].split('!J')[1]);row=deepcopy(self.rows[number-2])
                row[9]=cell['values'][0][0];rows.append((number,row))
            return self.update_rows('取込データ',rows)
        return SimpleNamespace(execute=execute)
    def categories(self):return [('その他','未分類'),('食費','食料品')]
    def category_rules(self):return []
    def expense_index(self):return {r[0]:i for i,r in enumerate(self.expenses,2)}
    def expense_records(self):return {r[0]:(i,list(r)) for i,r in enumerate(self.expenses,2)}
    def append(self,sheet,rows):
        if not rows:return
        assert sheet=='支出明細';self.writes.append(('append',deepcopy(rows)));self.expenses.extend(deepcopy(rows))
        if self.mode=='unknown':raise TimeoutError('synthetic lost append response')
        if self.mode=='wrong_amount':self.expenses[-1][4]+=1
    def update_rows(self,sheet,rows):
        if not rows:return
        assert sheet=='取込データ';self.writes.append(('link',deepcopy(rows)))
        for number,row in rows:self.rows[number-2]=deepcopy(row)
        if self.mode=='after_link':raise TimeoutError('synthetic runner loss')
    append_raw=append
    def set_raw_range(self,rng,rows):
        number=int(rng.split('!J')[1]);assert len(rows)==len(rows[0])==1
        row=deepcopy(self.rows[number-2]);row[9]=rows[0][0]
        self.update_rows('取込データ',[(number,row)])
    def financial(self):
        return {(title,render):[HEADERS.get(title,['header'])]+deepcopy(rows) for title,rows in
                [('取込データ',self.rows),('支出明細',self.expenses),('収入明細',[])]
                for render in ('UNFORMATTED_VALUE','FORMULA')}


class Transport:
    def __init__(self,payload):self.payload=payload;self.writes=[]
    def read(self):return self.payload
    def write(self,value):self.writes.append(value);self.payload=value


def case(tmp_path):
    db=DB();path=tmp_path/'state';state=SqliteRecurringRunState(path/'recurring.sqlite3',repo_root=tmp_path/'repo')
    registry=tuple(dict(owner=r[0],owner_row_sha256=digest(r),business_fingerprint=hex(10+i)[2:].zfill(24),aliases={r[0]:r[10]}) for i,r in enumerate(db.rows))
    state.record(dict(run_id='initial',status='noop',source_window_start=C1,source_window_end=C1,card_aliases=list(registry)),advance_checkpoint=True)
    binding=StateBinding('au_pay_card_gmail',db.sid,'folder','file')
    transport=Transport(envelope(binding,snapshot(path,binding)));durable=DurableState(transport,binding)
    restored=tmp_path/'fresh';durable.restore(restored)
    state=SqliteRecurringRunState(restored/'recurring.sqlite3',repo_root=tmp_path/'repo')
    plan=AutoExpensePipeline(db).plan_card_expenses([OWNER],categories_by_id={OWNER:['食費','食料品']})
    args=dict(db=db,plan=plan,state=state,durable=durable,state_dir=restored,registry=registry,financial_read=db.financial,guard=lambda:None)
    return db,state,durable,registry,args


def test_one_4386_expense_correct_J_link_and_fresh_alias_restore(tmp_path):
    db,state,durable,registry,args=case(tmp_path);before=deepcopy(db.rows)
    old=deepcopy(registry);result=execute_selected(**args)
    assert result['expenses_created']==result['imports_updated']==1 and result['expenses_updated']==0
    assert len(db.expenses)==1 and db.expenses[0][0]==expense_id(OWNER) and db.expenses[0][4]==4386
    assert db.expenses[0][5:7]==['食費','食料品'] and db.expenses[0][10]==OWNER
    assert db.rows[0][9]==expense_id(OWNER) and db.rows[0][:9]==before[0][:9] and db.rows[0][10:]==before[0][10:]
    assert db.rows[1:]==before[1:] and state.successful_window_end().isoformat()==C1 and registry==old
    fresh_dir=tmp_path/'another-runner';DurableState(durable.transport,durable.binding).restore(fresh_dir)
    fresh=SqliteRecurringRunState(fresh_dir/'recurring.sqlite3',repo_root=tmp_path/'repo')
    merged=fresh.alias_registry();resolve_aliases([],db.rows,merged);validate_owner_expenses(merged,db.expenses)
    assert merged[0]['owner_changes'][0]['before']==before[0]
    replay=AutoExpensePipeline(db).plan_card_expenses([OWNER],categories_by_id={OWNER:['食費','食料品']})
    assert replay['expense_rows']==0 and replay['already_posted']==[OWNER]
    assert contract_sha256(replay)==contract_sha256(args['plan'])
    result=execute_selected(**(args|dict(plan=replay,state=fresh,state_dir=fresh_dir,registry=merged)))
    assert result['expenses_created']==0 and len(db.expenses)==1 and len(db.writes)==2


@pytest.mark.parametrize('mode',['unknown','after_link','wrong_amount'])
def test_unknown_partial_or_wrong_delta_stops_with_pending_and_no_resend(tmp_path,mode):
    db,state,durable,registry,args=case(tmp_path);db.mode=mode
    with pytest.raises(RuntimeError if mode=='wrong_amount' else TimeoutError):execute_selected(**args)
    assert durable.inspect()['phase']=='pending' and state.successful_window_end().isoformat()==C1
    assert len(db.expenses)==1 and len(durable.transport.writes)==1
    with pytest.raises(RuntimeError):execute_selected(**args)
    assert len(db.expenses)==1


@pytest.mark.parametrize('change',['transfer','category','duplicate_expense','receipt','source','owner_J'])
def test_unapproved_or_ambiguous_selection_does_not_write(tmp_path,change):
    db,state,durable,registry,args=case(tmp_path)
    ids=[OWNER];cats={OWNER:['食費','食料品']}
    if change=='transfer':ids=[TRANSFER];cats={}
    if change=='category':cats={OWNER:['invalid','invalid']}
    if change=='duplicate_expense':db.expenses.append(args['plan']['rows'][0]['expense'])
    if change=='receipt':db.rows.append(['receipt','','receipt','receipt','2026-11-01','合成店',4386,'','canonical_receipt','','',''])
    if change=='source':db.rows[0][2]='PayPay'
    if change=='owner_J':db.rows[0][9]='unrelated'
    with pytest.raises(RuntimeError):AutoExpensePipeline(db).plan_card_expenses(ids,categories_by_id=cats)
    assert not db.writes and not durable.transport.writes


@pytest.mark.parametrize('change',['J','amount','source_hash','expense','proof_chain'])
def test_forged_owner_successor_or_missing_actual_expense_is_denied(tmp_path,change):
    db,state,durable,registry,args=case(tmp_path);updates=owner_updates(args['plan'],registry)
    if change=='J':updates[0]['owner_changes'][0]['after'][9]='wrong'
    if change=='amount':updates[0]['owner_changes'][0]['after'][6]+=1
    if change=='source_hash':updates[0]['owner_changes'][0]['after'][10]='0'*64
    if change=='proof_chain':updates[0]['owner_changes'][0]['before'][6]+=1
    if change=='expense':
        merged=merge_registry((*registry,*updates))
        with pytest.raises(RuntimeError):validate_owner_expenses(merged,[])
    else:
        with pytest.raises(RuntimeError):merge_registry((*registry,*updates))
    assert not db.writes


def test_owner_link_preserves_existing_three_unknown_checkpoint_proofs(tmp_path,monkeypatch):
    from test_aupay_card_reservation_reference import reference_case
    from app import aupay_card_reconciliation_apply as repair
    from app.aupay_card_incremental_recovery import preserved_unknown_proofs
    p=reference_case(tmp_path,monkeypatch)
    repair.execute_reconciliation(**p.args)
    db=DB();db.sid=p.db.sid;db.rows=deepcopy(p.db.rows)
    owner=next(r[0] for r in db.rows if r[8]=='auto_expense' and r[6]==4386)
    path=Path(p.args['state_dir']);state=SqliteRecurringRunState(path/'recurring.sqlite3',repo_root=p.args['repo_root'])
    registry=state.alias_registry();before_native=validate(p.nt.payload,p.cb)
    oldproofs=preserved_unknown_proofs(before_native,json.loads(p.pt.payload),C1)
    plan=AutoExpensePipeline(db).plan_card_expenses([owner],categories_by_id={owner:['食費','食料品']})
    execute_selected(db=db,plan=plan,state=state,durable=p.args['durable'],state_dir=path,registry=registry,
        financial_read=db.financial,guard=lambda:None)
    after_native=validate(p.nt.payload,p.cb)
    assert preserved_unknown_proofs(after_native,json.loads(p.pt.payload),C1)==oldproofs
    assert set(oldproofs)=={19,20,30} and state.successful_window_end().isoformat()==C1
    validate_owner_expenses(state.alias_registry(),db.expenses)


@pytest.mark.parametrize('source',['expense','receipt','expense_without_M'])
def test_numeric_sheet_date_cannot_hide_an_existing_posting_or_receipt(tmp_path,source):
    from datetime import date
    db,state,durable,registry,args=case(tmp_path)
    serial=(date(2026,11,1)-date(1899,12,30)).days
    if source in {'expense','expense_without_M'}:
        row=deepcopy(args['plan']['rows'][0]['expense']);row[0]='manual-existing';row[1]=serial;row[10]='another-source'
        db.expenses.append(row)
        if source=='expense_without_M':db.expenses[-1]=row[:12]
    else:
        db.rows.append(['receipt','','receipt','receipt',serial,'合成店',4386,'','canonical_receipt','','',''])
    with pytest.raises(RuntimeError):AutoExpensePipeline(db).plan_card_expenses([OWNER],categories_by_id={OWNER:['食費','食料品']})
    assert not db.writes and not durable.transport.writes


def test_normal_category_edit_does_not_reset_aliases_or_repost_expense(tmp_path):
    db,state,durable,registry,args=case(tmp_path);execute_selected(**args)
    db.expenses[0][5:7]=['その他','未分類'];db.expenses[0][11]='user note'
    validate_owner_expenses(state.alias_registry(),db.expenses)
    resolve_aliases([],db.rows,state.alias_registry())
    replay=AutoExpensePipeline(db).plan_card_expenses([OWNER],categories_by_id={OWNER:['食費','食料品']})
    assert replay['expense_rows']==0 and contract_sha256(replay)==contract_sha256(args['plan'])
    execute_selected(**(args|dict(plan=replay,registry=state.alias_registry())))
    assert len(db.expenses)==1 and db.expenses[0][5:7]==['その他','未分類'] and db.expenses[0][11]=='user note'


def test_independent_reader_includes_review_and_category_controls():
    from app.aupay_card_first_window_apply import financial_data
    from app.aupay_card_window_preview import FINANCIAL_SHEETS
    from app.aupay_card_expense_stage import verify_delta
    class Request:
        def __init__(self,value):self.value=value
        def execute(self,**kwargs):return deepcopy(self.value)
    class API:
        def spreadsheets(self):return self
        def values(self):return self
        def get(self,**kwargs):return Request(dict(spreadsheetId='sheet',sheets=[dict(properties=dict(title=t)) for t in (*FINANCIAL_SHEETS,'要確認','カテゴリ')]))
        def batchGet(self,**kwargs):return Request(dict(spreadsheetId='sheet',valueRanges=[dict(range=r+'!A1:B2',values=[['header'],['preserve']]) for r in kwargs['ranges']]))
    before=financial_data(API(),'sheet',include_controls=True)
    assert {('要確認',r) for r in ('UNFORMATTED_VALUE','FORMULA')}<=set(before)
    assert {('カテゴリ',r) for r in ('UNFORMATTED_VALUE','FORMULA')}<=set(before)
    after=deepcopy(before);after[('カテゴリ','UNFORMATTED_VALUE')][1][0]='unapproved category change'
    with pytest.raises(RuntimeError,match='exact_delta'):verify_delta(before,after,dict(rows=[]))


def test_owner_row_movement_before_J_write_never_links_another_transaction(tmp_path):
    db,state,durable,registry,args=case(tmp_path);db.mode='sort_before_link'
    with pytest.raises(RuntimeError,match='owner_moved_or_changed'):execute_selected(**args)
    assert len(db.expenses)==1 and all(not row[9] for row in db.rows)
    assert durable.inspect()['phase']=='pending' and state.successful_window_end().isoformat()==C1
    assert len(db.writes)==1
