"""Exercise the actual Apps Script capture against the unchanged v1 runner."""
from copy import deepcopy
import json,subprocess
from pathlib import Path
import pytest
from test_category_sheet_requests import RequestDB,Store,blocks,physical,ENV,REQUEST
from app.category_rule_ui import CategoryRuleUIPipeline
from app.category_sheet_requests import parse_snapshot,execute_request
from app.category_backfill import CategoryBackfillPipeline
from app.category_operations import process_category_operations
from test_category_backfill import import_row

BRIDGE=Path(__file__).parents[1]/'apps-script/category-submit/confirmation-bridge.cjs'

def prepare():
    db=RequestDB();db.expenses={};db.imports=[]
    for index,(day,merchant,category) in enumerate([
        ('2026-07-20','請求名',['その他','未分類']),
        ('2026-08-10','請求名',['その他','未分類']),
        ('2026-08-11','請求名',['食費','外食']),
        ('2026-08-12','別店舗',['その他','未分類']),
    ]):
        key=f'M-{index}';import_id=f'p{index}'
        db.expenses[key]=(index+2,[key,day,merchant,'自動計上',100+index,*category,'','PayPay','',import_id,'','active'])
        tx=import_row(import_id,merchant,100+index,key);tx[4]=day;db.imports.append(tx)
    CategoryRuleUIPipeline(db,ui_enabled=True,save_enabled=True).refresh()
    row=next(r for r in db.ui if json.loads(r[11]).get('merchant')=='請求名' and r[6].startswith('group:'))
    row[2:4]=['食費','外食']
    other=next(r for r in db.ui if json.loads(r[11]).get('merchant')=='別店舗')
    other[2:6]=['食費','外食',True,True] # An unsent decision must survive.
    state={'month':'2026-08','scope':'反映しない','future':'OFF','proof':json.loads(row[11])}
    return db,row,state

def capture(db,row,state,stage):
    payload={'rows':physical(blocks(db)),'key':row[6],'category':'食費｜外食','stage':stage,'state':state}
    output=subprocess.run(['node',str(BRIDGE)],input=json.dumps(payload,ensure_ascii=False),encoding='utf-8',stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True).stdout
    return parse_snapshot(json.loads(output))

@pytest.mark.parametrize('future',['OFF','ON'])
@pytest.mark.parametrize('scope',['反映しない','対象月のみ','全期間'])
def test_scope_and_future_are_independent_and_replay_is_ignored(scope,future):
    db,row,state=prepare();state.update(scope=scope,future=future)
    preview=Store(capture(db,row,state,'preview'))
    result=execute_request(db,REQUEST,env=ENV,store=preview,refresh_projection=lambda:{})
    assert result['category_expenses_applied']==0
    assert not db.rule_rows and not db.category_updates
    assert len(db.requests)==1
    request=db.requests[0]
    expected=2 if scope=='全期間' else 1
    assert request[6]==expected
    assert request[7]==(101 if expected==1 else 201)
    assert request[4:6]==(['2026-08-01','2026-08-31'] if expected==1 else ['',''])
    assert execute_request(db,REQUEST,env=ENV,store=preview,refresh_projection=lambda:{})=={'category_request_ignored':1}
    assert len(db.requests)==1 and not db.category_updates
    state['fixed']={'id':request[0],'count':expected,'amount':request[7],'digest':request[8]}
    confirm=Store(capture(db,row,state,'confirm'))
    result=execute_request(db,REQUEST,env=ENV,store=confirm,refresh_projection=lambda:{})
    assert result['category_expenses_applied']==expected
    assert len(db.requests)==1 # Confirm reuses the preview audit request.
    assert db.requests[0][0]==state['fixed']['id']
    assert db.requests[0][8]==state['fixed']['digest']
    assert len(db.rule_rows)==(future=='ON')
    assert db.expenses['M-1'][1][5:7]==['食費','外食']
    assert db.expenses['M-0'][1][5:7]==(['食費','外食'] if scope=='全期間' else ['その他','未分類'])
    assert db.expenses['M-2'][1][5:7]==['食費','外食'] # already classified
    assert db.expenses['M-3'][1][5:7]==['その他','未分類'] # unrelated unsent
    other=next(r for r in db.ui if json.loads(r[11]).get('merchant')=='別店舗')
    assert other[4:6]==[True,True]
    before=deepcopy(db.category_updates)
    audit_before=deepcopy((db.requests,db.targets,db.rule_rows,confirm.meta))
    assert execute_request(db,REQUEST,env=ENV,store=confirm,refresh_projection=lambda:{})=={'category_request_ignored':1}
    assert db.category_updates==before
    assert (db.requests,db.targets,db.rule_rows,confirm.meta)==audit_before


def test_no_history_preview_is_non_executing_until_explicit_confirmation():
    db,row,state=prepare()
    other=next(r for r in db.ui if json.loads(r[11]).get('merchant')=='別店舗')
    other[4:6]=[False,False]
    preview=Store(capture(db,row,state,'preview'))
    execute_request(db,REQUEST,env=ENV,store=preview,refresh_projection=lambda:{})
    request=db.requests[0]
    assert request[2]=='previewed' and request[9] is False
    assert request[4:6]==['2026-08-01','2026-08-31']
    assert [r[1] for r in db.targets]==['M-1']
    assert all(r[10]=='previewed' for r in db.targets)
    assert all(not r[2] for r in db.confirmations)
    audit=deepcopy((db.requests,db.targets))
    pipe=CategoryBackfillPipeline(db,preview_enabled=True,apply_enabled=True)
    assert pipe.apply(request[0],expected_count=1)=={'state':'held','reason':'explicit_confirmation_required'}
    # Exercise later ordinary workers, not just replay of the original UUID.
    for _ in range(2):
        result=process_category_operations(db,apply=True,rule_enabled=True,save_enabled=True,
            preview_enabled=True,backfill_enabled=True)
        assert result['category_expenses_applied']==0
        assert (db.requests,db.targets)==audit
        assert not db.category_updates and not db.rule_rows
    state['fixed']={'id':request[0],'count':1,'amount':request[7],'digest':request[8]}
    confirm=Store(capture(db,row,state,'confirm'))
    result=execute_request(db,REQUEST,env=ENV,store=confirm,refresh_projection=lambda:{})
    assert result['category_expenses_applied']==1
    assert len(db.requests)==1 and db.requests[0][2]=='complete'
    assert db.requests[0][0]==state['fixed']['id'] and db.requests[0][8]==state['fixed']['digest']
    completed=deepcopy((db.requests,db.targets,db.category_updates))
    for _ in range(2):
        result=process_category_operations(db,apply=True,rule_enabled=True,save_enabled=True,
            preview_enabled=True,backfill_enabled=True)
        assert result['category_expenses_applied']==0
        assert (db.requests,db.targets,db.category_updates)==completed
        assert not db.rule_rows
    assert db.expenses['M-0'][1][5:7]==['その他','未分類']


def test_no_history_fixed_ids_do_not_expand_after_preview():
    db,row,state=prepare()
    preview=Store(capture(db,row,state,'preview'))
    execute_request(db,REQUEST,env=ENV,store=preview,refresh_projection=lambda:{})
    request=db.requests[0];state['fixed']={'id':request[0],'count':1,'digest':request[8]}
    confirm=Store(capture(db,row,state,'confirm'))
    for key,day in [('M-later','2026-08-30'),('M-past','2026-07-30')]:
        db.expenses[key]=(10,[''+key,day,'請求名','自動計上',500,'その他','未分類','','PayPay','',key,'','active'])
        tx=import_row(key,'請求名',500,key);tx[4]=day;db.imports.append(tx)
    result=execute_request(db,REQUEST,env=ENV,store=confirm,refresh_projection=lambda:{})
    assert result['category_expenses_applied']==1
    assert [r[1] for r in db.targets]==['M-1']
    assert len(db.requests)==1 and not db.rule_rows
    assert db.expenses['M-later'][1][5:7]==['その他','未分類']
    assert db.expenses['M-past'][1][5:7]==['その他','未分類']
    before=deepcopy((db.category_updates,db.requests,db.targets,db.rule_rows))
    assert execute_request(db,REQUEST,env=ENV,store=confirm,refresh_projection=lambda:{})=={'category_request_ignored':1}
    assert (db.category_updates,db.requests,db.targets,db.rule_rows)==before

def test_retry_preview_replaces_same_displayed_row_instead_of_duplicate_key():
    db,row,state=prepare();state['scope']='対象月のみ'
    store=Store(capture(db,row,state,'preview'))
    execute_request(db,REQUEST,env=ENV,store=store,refresh_projection=lambda:{})
    again=capture(db,row,state,'preview')
    assert sum(r[6]=='displayed:'+row[6] for r in again['backfill'][1])==1

def test_captured_confirmation_does_not_expand_to_new_matching_expense():
    db,row,state=prepare();state['scope']='全期間'
    store=Store(capture(db,row,state,'preview'))
    execute_request(db,REQUEST,env=ENV,store=store,refresh_projection=lambda:{})
    request=db.requests[0];state['fixed']={'id':request[0],'count':2}
    confirm=Store(capture(db,row,state,'confirm'))
    db.expenses['M-later']=(10,['M-later','2026-08-30','請求名','自動計上',500,'その他','未分類','','PayPay','','p-later','','active'])
    db.imports.append(import_row('p-later','請求名',500,'M-later'))
    result=execute_request(db,REQUEST,env=ENV,store=confirm,refresh_projection=lambda:{})
    assert result['category_expenses_applied']==2
    assert db.expenses['M-later'][1][5:7]==['その他','未分類']
