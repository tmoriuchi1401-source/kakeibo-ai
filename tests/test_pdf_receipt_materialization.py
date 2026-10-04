from copy import deepcopy
from hashlib import sha256
from unittest.mock import Mock

import pytest
from app.drive_run_state import StateError
from app.models import ReceiptResult
from app.pdf_bounded_rendering import WorkBudget
from app.pdf_receipt_materialization import materialize,reconcile
from app.pdf_unit_payload import rendered_unit
from app.sheets import HEADERS
from test_pdf_unit_processing import context
from test_pdf_unit_payload import setup
from test_receipt_pdf_units import local_ocr


class DB:
    def __init__(self):
        self.rows={t:[] for t in ('レシート','支出明細','取込データ')};self.appends=[]
        self.cats=[('食費','外食')];self.fail=None
    def categories(self):return deepcopy(self.cats)
    def get_raw(self,r):
        title=r.split("'")[1]
        return [HEADERS[title]] if '1:' in r else deepcopy(self.rows[title])
    def append_raw(self,title,rows):
        self.appends.append((title,deepcopy(rows)))
        if self.fail==(title,'before'):raise RuntimeError('synthetic timeout')
        self.rows[title].extend(deepcopy(rows))
        if self.fail==(title,'after'):raise RuntimeError('synthetic lost acknowledgement')


def result():
    return ReceiptResult(date='2026-09-24',total=100,merchant='',payment_method='',
        items=[dict(name='商品',amount=100,major_category='食費',minor_category='外食')])


def run(db,store,s,png,h,fresh=None):
    return materialize(db,store,s,png,h,result(),verify_current=fresh or Mock(return_value=True),
        clock=lambda:'2026-10-04 12:00:00')


def test_existing_writer_exact_three_tables_then_readback_only_replay(local_ocr):
    content,s,obs=setup();_,store,_=context();db=DB();fresh=Mock(return_value=True)
    with rendered_unit(content,s,obs,WorkBudget()) as (png,h):out=run(db,store,s,png,h,fresh)
    assert out['status']=='imported' and out['appended']==3
    assert [t for t,_ in db.appends]==['レシート','支出明細','取込データ']
    assert db.rows['レシート'][0][2]=='' and db.rows['支出明細'][0][7]==''
    before=(deepcopy(db.rows),len(db.appends),store.payload)
    assert reconcile(db,store,s,verify_current=fresh)['replayed']
    assert before==(db.rows,len(db.appends),store.payload)
    assert fresh.call_count>=7


def test_lost_append_acknowledgement_exact_readback_does_not_resend(local_ocr):
    content,s,obs=setup();_,store,_=context();db=DB();db.fail=('支出明細','after')
    with rendered_unit(content,s,obs,WorkBudget()) as (png,h):assert run(db,store,s,png,h)['appended']==3
    assert len(db.appends)==3


@pytest.mark.parametrize('table',['レシート','支出明細','取込データ'])
def test_partial_write_is_pending_and_never_automatically_resumed(table,local_ocr):
    content,s,obs=setup();_,store,_=context();db=DB();db.fail=(table,'before')
    with rendered_unit(content,s,obs,WorkBudget()) as (png,h):
        with pytest.raises(StateError,match='readback_missing'):run(db,store,s,png,h)
    before=(deepcopy(db.rows),len(db.appends),store.payload)
    db.fail=None
    with pytest.raises(StateError,match='readback_missing'):
        reconcile(db,store,s,verify_current=Mock(return_value=True))
    assert before==(db.rows,len(db.appends),store.payload)


def test_completion_save_failure_is_reconciled_without_second_accounting_write(local_ocr,monkeypatch):
    content,s,obs=setup();drive,store,_=context();db=DB();save=store._save;calls=0
    def fail_second(value):
        nonlocal calls
        calls+=1
        if calls==2:raise StateError('synthetic_state_save_failure')
        save(value)
    monkeypatch.setattr(store,'_save',fail_second)
    with rendered_unit(content,s,obs,WorkBudget()) as (png,h):
        with pytest.raises(StateError,match='save_failure'):run(db,store,s,png,h)
    assert len(db.appends)==3 and store.load()['records'][s['unit_id']]['phase']=='pending'
    assert reconcile(db,store,s,verify_current=Mock(return_value=True))['appended']==0
    assert len(db.appends)==3


def test_conflicting_existing_row_is_never_updated_on_replay(local_ocr):
    content,s,obs=setup();_,store,_=context();db=DB()
    with rendered_unit(content,s,obs,WorkBudget()) as (png,h):run(db,store,s,png,h)
    db.rows['支出明細'][0][4]=101
    with pytest.raises(StateError,match='content_conflict'):
        reconcile(db,store,s,verify_current=Mock(return_value=True))
    assert len(db.appends)==3 and db.rows['支出明細'][0][4]==101


def test_extra_row_with_same_import_is_not_accepted_as_terminal_readback(local_ocr):
    content,s,obs=setup();_,store,_=context();db=DB()
    with rendered_unit(content,s,obs,WorkBudget()) as (png,h):run(db,store,s,png,h)
    extra=deepcopy(db.rows['支出明細'][0]);extra[0]='unplanned-expense';db.rows['支出明細'].append(extra)
    with pytest.raises(StateError,match='content_conflict'):
        reconcile(db,store,s,verify_current=Mock(return_value=True))
    assert len(db.appends)==3 and len(db.rows['支出明細'])==2


@pytest.mark.parametrize('change',['payload','source','medical','privacy_hold','duplicate','legacy_parent','categories'])
def test_safety_hold_never_appends(change,local_ocr):
    content,s,obs=setup();drive,store,_=context();db=DB();fresh=Mock(return_value=True)
    with rendered_unit(content,s,obs,WorkBudget()) as (png,h):
        if change=='payload':png+=b'changed'
        elif change=='source':fresh.return_value=False
        elif change=='medical':s['human_classifications']=['medical']
        elif change=='privacy_hold':store.block(s,'sensitive_unknown','synthetic_hold',verify_current=fresh)
        elif change=='duplicate':db.rows['レシート']=[['R-other','2026-09-21','別店舗',100,'','','解析済','','']]
        elif change=='legacy_parent':db.rows['取込データ']=[['receipt:'+s['source_file_id']]]
        elif change=='categories':db.cats=[('その他','未分類')]
        with pytest.raises(StateError):run(db,store,s,png,h,fresh)
    assert db.appends==[]
