from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.receipt_confirmation import ReceiptConfirmation,TITLE,review_id
from app.receipt_reimport_production import target_snapshot
from app.receipt_review_policy import manual_expense_already_recorded
from app.auto_expense import expense_id
from app.drive_run_state import StateError
from tests.test_receipt_confirmation import Store,DB,medical
from tests.test_receipt_reimport import fixture


def normal_service(manual=False):
    parsed,tables=fixture();categories=tables.pop('categories')
    if manual:
        eid=expense_id('receipt:s1')
        tables['import_rows'][0][8:10]=['manual_expense',eid]
        tables['receipt_rows'][0][6]='要確認'
        tables['expense_rows'][0][0]=eid;tables['expense_rows'][0][3]='手動計上';tables['expense_rows'][0][9]=''
    else:
        parsed.merchant='Synthetic shop branch';parsed.items[0].name='Synthetic   item'
    store=Store();db=DB();db.categories=lambda:categories
    for field,title in [('receipt_rows','レシート'),('import_rows','取込データ'),('expense_rows','支出明細'),('review_rows','要確認')]:db.rows[title]=deepcopy(tables[field])
    source={'source_id':'s1','version':'1','sha256':'a'*64,'mime_type':'image/png'};key=review_id('normal',source)
    store.value['manifest']={'sources':[source],'folder_id':'synthetic-folder'}
    store.value['records']={'s1':{'phase':'complete','parsed':parsed.model_dump(),'before':target_snapshot(tables,'s1')}}
    store.value['confirmation_items'][key]={'kind':'normal','source':source,'folder_id':'synthetic-folder','status':'waiting',
        'inputs':['']*8,'reason':'old question','before':target_snapshot(tables,'s1')}
    review=ReceiptConfirmation(store,db,Mock());review.render()
    return review,store,db,key,parsed,tables,categories


@pytest.mark.parametrize('manual',[False,True])
def test_existing_labels_or_confirmed_manual_post_close_without_accounting_changes(manual):
    review,store,db,key,*_=normal_service(manual)
    accounting=deepcopy({k:v for k,v in db.rows.items() if k!=TITLE})
    assert review.resolve_general_without_writes()==1
    assert store.value['confirmation_items'][key]['status']=='closed_machine'
    assert not review.needs_attention(key)
    review.render();assert review.resolve_general_without_writes()==0
    assert {k:v for k,v in db.rows.items() if k!=TITLE}==accounting


@pytest.mark.parametrize('mutation',[
    lambda r,s,d,k:s.value['records']['s1']['parsed']['items'][0].update(name='Another product'),
    lambda r,s,d,k:s.value['records']['s1']['parsed']['items'][0].update(minor_category='別分類'),
    lambda r,s,d,k:s.value['records']['s1']['parsed'].update(total=121),
    lambda r,s,d,k:d.rows['支出明細'][0].__setitem__(11,'new owner note'),
    lambda r,s,d,k:d.rows[TITLE][0].__setitem__(14,'owner typing'),
    lambda r,s,d,k:s.value['confirmation_items'][k].update(require_reconfirm=True),
])
def test_meaningful_difference_or_new_owner_input_stays_visible(mutation):
    review,store,db,key,*_=normal_service()
    mutation(review,store,db,key)
    assert review.resolve_general_without_writes()==0 and review.needs_attention(key)


def test_changed_original_cannot_close_an_old_question():
    review,store,db,key,*_=normal_service()
    review.verify_source.side_effect=StateError('confirmation_source_changed')
    assert review.resolve_general_without_writes()==0
    assert review.needs_attention(key)


def test_unchanged_held_difference_does_not_reread_entire_review_ui():
    review,store,db,key,*_=normal_service()
    store.value['records']['s1']['parsed']['items'][0]['name']='Different product'
    before=deepcopy(store.value)
    review.ui_rows=Mock(side_effect=AssertionError('unnecessary full UI read'))
    assert review.resolve_general_without_writes()==0
    assert store.value==before and review.needs_attention(key)


@pytest.mark.parametrize('mutation',[
    lambda t:t['expense_rows'].append(deepcopy(t['expense_rows'][0])),
    lambda t:t['expense_rows'][0].__setitem__(12,'duplicate_excluded'),
    lambda t:t['expense_rows'][0].__setitem__(4,999),
    lambda t:t['expense_rows'][0].__setitem__(10,'receipt:other'),
    lambda t:t['import_rows'][0].__setitem__(9,'wrong-target'),
    lambda t:t['review_rows'].append(['receipt:s1']+['']*9+['保留']),
])
def test_manual_closure_requires_exact_complete_owner_posting(mutation):
    _,_,_,_,parsed,tables,categories=normal_service(True)
    mutation(tables)
    assert not manual_expense_already_recorded('s1',parsed,**tables,categories=categories)


def test_seventeen_inbox_files_and_seven_legacy_rows_reconcile_without_losing_history():
    store=Store();db=DB();review=ReceiptConfirmation(store,db,Mock())
    sources=[{'source_id':f'synthetic-{n}','version':'1','sha256':f'{n:064x}','mime_type':'image/png'} for n in range(17)]
    for source in sources[:2]:review.observe_medical(source,'synthetic-inbox')
    old=deepcopy(sources[0]);old['version']='0'
    key=review_id('medical',old);current=deepcopy(review.items[review_id('medical',sources[0])])
    current.update(source=old,status='superseded');store.value['confirmation_items'][key]=current
    # Four historical general reviews refer to processed files outside the inbox.
    for n in range(4):
        sample,s,d,k,*_=normal_service()
        item=deepcopy(s.value['confirmation_items'][k]);item['source']['source_id']=f'processed-{n}'
        rid=review_id('normal',item['source']);store.value['confirmation_items'][rid]=item
        store.value['records'][item['source']['source_id']]=deepcopy(s.value['records']['s1'])
    assert len(review.items)==7
    for source in sources[2:]:review.observe_intake_hold(source,'synthetic-inbox',SimpleNamespace(classification='sensitive_unknown'))
    review.render()
    assert len(review.ui_rows())==22 and review.review_counts()['review_pending']==21
    assert not review.needs_attention(key)
    assert review.review_counts()=={'review_pending':21,'normal_review_pending':4,'medical_review_pending':2,'intake_review_pending':15}
    # No anonymous/orphan intake rows, duplicated retries, or accounting writes.
    for source in sources[2:]:assert not review.observe_intake_hold(source,'synthetic-inbox',SimpleNamespace(classification='sensitive_unknown'))
    assert len(review.ui_rows())==22 and not db.rows['支出明細']


def test_intake_cannot_authorize_posting_and_owner_hold_blocks_later_medical_automation():
    from app.medical_auto_posting import owner_blocked
    store=Store();db=DB();review=ReceiptConfirmation(store,db,Mock())
    source={'source_id':'synthetic','version':'1','sha256':'a'*64,'mime_type':'image/png'}
    review.observe_intake_hold(source,'synthetic-inbox',SimpleNamespace(classification='sensitive_unknown'))
    review.render();db.rows[TITLE][0][12]='候補明細で確定';review.capture_inputs()
    assert review.apply_confirmations()==0 and not db.rows['支出明細']
    assert owner_blocked(source,store.value)


def test_fifteen_intake_rows_use_one_bounded_write_and_reconcile_lost_response():
    review,store,db,_,source=medical();db.rows[TITLE][0][14]='owner note'
    review.capture_inputs()
    for n in range(15):
        review.observe_intake_hold(dict(source,source_id=f'held-{n}'),'synthetic-inbox',SimpleNamespace(classification='sensitive_unknown'))
    original=db.set_raw_range;calls=[]
    def write(rng,values):
        calls.append(rng);original(rng,values)
        if len(values)==15:raise OSError('lost response after complete write')
    db.set_raw_range=write
    review.render()
    assert calls==[f"'{TITLE}'!A2:G2",f"'{TITLE}'!A3:P17"]
    assert len(review.ui_rows())==16 and db.rows[TITLE][0][14]=='owner note'
    assert review.review_counts()['intake_review_pending']==15


def test_hiding_history_changes_only_ui_metadata():
    from app.receipt_confirmation_production import sync_review_visibility
    review,store,db,verify,source=medical()
    db.rows[TITLE][0][14]='owner note';review.capture_inputs()
    review.observe_medical(dict(source,version='2'),'synthetic-inbox');review.render()
    values=deepcopy(db.rows);calls=[]
    class Request:
        def __init__(self,result):self.result=result
        def execute(self,**kw):return self.result
    class Sheets:
        def get(self,**kw):return Request({'sheets':[{'properties':{'sheetId':7},'data':[{'startRow':0,'rowMetadata':[{}, {}, {}]}]}]})
        def batchUpdate(self,**kw):calls.extend(kw['body']['requests']);return Request({})
    db.sid='synthetic';db.svc=SimpleNamespace(spreadsheets=lambda:Sheets())
    sync_review_visibility(review)
    hides=[r['updateDimensionProperties'] for r in calls if 'updateDimensionProperties' in r and r['updateDimensionProperties']['range']['dimension']=='ROWS']
    assert len(hides)==1 and hides[0]['range']['startIndex']==1
    assert all(set(r)<={'updateDimensionProperties','setDataValidation','repeatCell'} for r in calls)
    assert all('userEnteredValue' not in r.get('repeatCell',{}).get('fields','') for r in calls)
    assert db.rows==values and db.rows[TITLE][0][14]=='owner note'
