from copy import deepcopy

import pytest

from app.aupay_card_expense_stage import verify_delta,verify_side_effects,execute_selected
from app.category_navigation import return_formula
from app.projection_store import ProjectionJournal,expected_append_journal
from test_card_selected_expenses import case


def navigation(db,count=0):
    return dict(spreadsheet_id=db.sid,workflow_sheet_id=20,
        formula=return_formula(db.sid,20),counts={row[0]:count for row in db.expenses})


def journal(append=False):
    return dict(generation=338,ranges=[[2,3]],months=['2026-11'],append=append)


@pytest.mark.parametrize('count',[0,1,2])
@pytest.mark.parametrize('already_append',[False,True])
def test_writer_owned_formula_and_journal_both_renderings(tmp_path,count,already_append):
    db,state,durable,registry,args=case(tmp_path)
    nav=navigation(db);identifier=args['plan']['rows'][0]['expense'][0];nav['counts']={identifier:count}
    docs={'journal':journal(already_append),'money':None,'money-reviews':{'unchanged':True}}
    before=db.financial();db.append_raw('支出明細',[args['plan']['rows'][0]['expense']])
    db.rows[0]=deepcopy(args['plan']['rows'][0]['after'])
    after=db.financial()
    for render in ('UNFORMATTED_VALUE','FORMULA'):
        value=nav['formula'] if render=='FORMULA' else '' if count==0 else '↩ カテゴリ操作に戻る' if count==1 else '↩ カテゴリ操作の一覧（複数候補）'
        if value:after[('支出明細',render)][-1].append(value)
    verify_delta(before,after,args['plan'],navigation=nav)
    updated=deepcopy(docs);updated['journal']=expected_append_journal(docs['journal'])
    verify_side_effects(dict(navigation=nav,documents=docs),dict(navigation=nav,documents=updated))
    assert updated['journal']['generation']==338+int(not already_append)


@pytest.mark.parametrize('change',['formula','display','old_row','amount','reason','J','other_table','journal_generation','journal_ranges','money','navigation'])
def test_unknown_formula_or_unapproved_change_still_rejected(tmp_path,change):
    db,state,durable,registry,args=case(tmp_path);before=db.financial();after=deepcopy(before)
    row=args['plan']['rows'][0];nav=navigation(db)
    for render in ('UNFORMATTED_VALUE','FORMULA'):
        changed=list(row['after'])
        while changed and changed[-1]=='':changed.pop()
        after[('取込データ',render)][row['row_num']-1]=changed
        after[('支出明細',render)].append(row['expense']+([nav['formula']] if render=='FORMULA' else []))
    docs={'journal':journal(),'money':None};expected={'journal':expected_append_journal(journal()),'money':None}
    effects=dict(navigation=deepcopy(nav),documents=deepcopy(expected))
    if change=='formula':after[('支出明細','FORMULA')][-1][-1]='=OTHER()'
    if change=='display':after[('支出明細','UNFORMATTED_VALUE')][-1].append('unapproved display')
    if change=='old_row':after[('支出明細','FORMULA')][0].append('=OTHER()')
    if change=='amount':after[('支出明細','UNFORMATTED_VALUE')][-1][4]+=1
    if change=='reason':after[('支出明細','UNFORMATTED_VALUE')][-1][11]='changed'
    if change=='J':after[('取込データ','UNFORMATTED_VALUE')][1][9]='changed'
    if change=='other_table':after[('収入明細','FORMULA')].append(['changed'])
    if change=='journal_generation':effects['documents']['journal']['generation']+=1
    if change=='journal_ranges':effects['documents']['journal']['ranges']=[]
    if change=='money':effects['documents']['money']={'changed':True}
    if change=='navigation':effects['navigation']['workflow_sheet_id']+=1
    with pytest.raises(RuntimeError):
        verify_delta(before,after,args['plan'],navigation=nav)
        verify_side_effects(dict(navigation=nav,documents=docs),effects)


@pytest.mark.parametrize('already_append',[False,True])
def test_existing_selected_writer_validates_journal_then_commits(tmp_path,already_append):
    db,state,durable,registry,args=case(tmp_path)
    class Store:
        def __init__(self):self.value=journal(already_append);self.writes=[]
        def read(self,key):return deepcopy(self.value)
        def write(self,key,value):self.writes.append(deepcopy(value));self.value=deepcopy(value)
    store=Store();append=db.append_raw
    def existing_append(sheet,rows):
        ProjectionJournal(store).mark(append=True)
        append(sheet,rows)
    db.append_raw=existing_append
    result=execute_selected(**args,side_effects_read=lambda:dict(navigation=None,documents={'journal':store.read('journal')}))
    assert result['expenses_created']==1 and len(store.writes)==int(not already_append)
    assert durable.inspect()['phase']=='ready' and len(db.writes)==2


def test_side_document_changed_before_financial_writer_is_rejected(tmp_path):
    db,state,durable,registry,args=case(tmp_path);calls=[]
    def effects():
        calls.append(1)
        return dict(navigation=None,documents={'journal':journal(),'money':len(calls)})
    with pytest.raises(RuntimeError):execute_selected(**args,side_effects_read=effects)
    assert not db.writes and durable.inspect()['phase']=='pending'
