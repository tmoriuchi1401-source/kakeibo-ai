from copy import deepcopy
from types import SimpleNamespace
import pytest

from app.drive_run_state import StateError
from app.pdf_intake_authority import page_key
from app.pdf_intake_registry import RegistryStore
from app.pdf_intake_runner import PageIntake, plan, observe_source, ledger_source_id
from app.page_receipt_model import PageUnit, PageReceiptExtraction, build_receipt_units
from app.general_receipt_completion import drafts_for_page
from app.receipt_item_review import prepare, card
from app.sheets import HEADERS
from test_pdf_intake_registry import Memory
from test_pdf_intake_authority import page, RAW

CATEGORIES=[('食費','食料品'),('日用品','洗剤')]


class DB:
    def __init__(self):self.data={t:[] for t in ('レシート','支出明細','取込データ')};self.writes=[];self.fail_after=None
    def categories(self):return deepcopy(CATEGORIES)
    def get_raw(self,region):
        title=region.split("'")[1]
        return deepcopy(([HEADERS[title]] if '!A1:' in region else [])+self.data[title])
    get_formula=get_raw
    def append_raw(self,title,rows):
        self.data[title].extend(deepcopy(rows));self.writes.append((title,deepcopy(rows)))
        if len(self.writes)==self.fail_after:raise TimeoutError()


def rig(kind='normal'):
    value=page(classification=kind,source_id='newPDFSourceFile01')
    store=RegistryStore(Memory(),'anchor');key=store.register(value);db=DB()
    p=PageUnit.model_validate(value)
    result={'date':'2026-10-07','merchant':'synthetic店','total':730,'payment_method':'','transaction_kind':'purchase','note':'',
            'items':[{'name':'牛乳','quantity':1,'amount':250,'major_category':'食費','minor_category':'食料品','note':'','confidence':.99},
                     {'name':'洗剤','quantity':1,'amount':480,'major_category':'日用品','minor_category':'洗剤','note':'','confidence':.99}]}
    reading=PageReceiptExtraction.model_validate({'receipts':[{'bbox':{'left':0,'top':0,'right':1,'bottom':1},'receipt':result,'item_boxes':[]}],
        'separation_complete':True,'mixed_page_kind_suspected':False,'cross_page_continuation_suspected':False})
    extracted=build_receipt_units(p,reading,reading,CATEGORIES)
    manifest={k:extracted[k] for k in ('page_key','segmentation_digest')}
    manifest.update(source=p.source.model_dump(),page_number=p.page_number,stable_page_identity=p.stable_page_identity,
                    units=[{k:u[k] for k in ('receipt_unit_id','receipt_index','parent_page_identity','bbox','accounting_status')}
                           for u in extracted['units']])
    draft=drafts_for_page(p,manifest,reading,reading,categories=CATEGORIES)[0]
    record=prepare(draft,CATEGORIES);uid=draft['identity']['receipt_unit_id']
    current=store.load();current['pages'][key]['units'][uid]={'status':'units_observed',
        'candidate':{'record':record,'view':card(record,categories=CATEGORIES),'manifest':manifest,'prepared_at':'2026-10-07 10:00:00'},
        'posting_authority':None,'intent':None,'readback':None}
    current['generation']+=1;store.save(current)
    runner=PageIntake(store,lambda _:RAW,db,SimpleNamespace(verify=lambda *_:pytest.fail('normal must not authenticate')),
                      lambda *_:pytest.fail('no AI in posting test'))
    return runner,key,uid,db


def test_existing_writer_plan_then_real_fake_store_exact_readback_replay():
    runner,key,uid,db=rig()
    first=runner.post_normal(key,uid)
    second=runner.post_normal(key,uid)
    assert first==second and first['status']=='ready_to_write' and db.writes==[]
    assert [len(op['rows']) for op in first['plan']['operations']]==[1,2,1]
    assert [row[5:7] for row in first['plan']['operations'][1]['rows']]==[['食費','食料品'],['日用品','洗剤']]
    posted=runner.post_normal(key,uid,apply=True)
    assert posted['status']=='imported' and posted['total']==730
    assert len(db.writes)==3
    before=deepcopy(db.data)
    assert runner.post_normal(key,uid,apply=True)['replay']
    assert db.data==before and len(db.writes)==3


def test_unknown_ai_permission_is_not_posting_authority():
    runner,key,uid,db=rig('unknown')
    assert runner.post_normal(key,uid,apply=True)['status']=='posting_authority_required'
    assert db.writes==[]


@pytest.mark.parametrize('stage',[1,2,3])
def test_write_response_lost_requires_readback_and_never_retries(stage):
    runner,key,uid,db=rig();db.fail_after=stage
    result=runner.post_normal(key,uid,apply=True)
    assert result['retry_allowed'] is False
    before=deepcopy(db.data)
    replay=runner.post_normal(key,uid,apply=True)
    assert replay['replay'] and db.data==before and len(db.writes)==stage
    assert result['status']==('complete' if stage==3 else 'partial_or_unknown')
    assert runner.store.load()['pages'][key]['units'][uid]['status']==('imported' if stage==3 else 'units_observed')


def test_duplicate_is_held_without_an_intent_or_write():
    runner,key,uid,db=rig()
    db.data['レシート']=[['existing','2026-10-07','other',730]]
    with pytest.raises(StateError,match='possible_duplicate'):runner.post_normal(key,uid,apply=True)
    assert db.writes==[] and runner.store.load()['pages'][key]['units'][uid]['intent'] is None


def test_corrupted_readback_never_marks_terminal():
    runner,key,uid,db=rig();original=db.append_raw
    def corrupt(title,rows):
        original(title,rows)
        if title=='支出明細':db.data[title][0][4]=251
    db.append_raw=corrupt
    result=runner.post_normal(key,uid,apply=True)
    assert result['status']=='partial_or_unknown'
    assert runner.store.load()['pages'][key]['units'][uid]['status']!='imported'
    assert len(db.writes)==2


def test_unit_limit_is_preserved():
    runner,key,uid,db=rig();runner.written=runner.unit_limit
    assert runner.post_normal(key,uid,apply=True)['status']=='unit_limit' and db.writes==[]
