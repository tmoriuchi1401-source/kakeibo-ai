from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import pdf_duplicate_comparison as d
from app import pdf_receipt_write_canary as c
from app.drive_run_state import StateError
from app.models import ReceiptResult
from test_pdf_receipt_write_canary import context


def fixture(monkeypatch):
    store, db, fresh, verify, transport, _ = context(monkeypatch)
    old='old-source';rid='R-'+old
    db.rows['レシート']=[[rid,'2026-09-21','店A',159,'',f'https://drive.google.com/file/d/{old}/view']]
    db.rows['取込データ']=[['receipt:'+old,'','receipt',old,'2026-09-21','店A',159,'','','','parsed-fingerprint']]
    db.rows['支出明細']=[['old-1','2026-09-21','店A','item',59,'食費','食品','','receipt',rid,'receipt:'+old,'','active'],
        ['old-2','2026-09-21','店A','item',100,'食費','食品','','receipt',rid,'receipt:'+old,'','active']]
    result=ReceiptResult(date='2026-09-26',merchant='店B',total=159,
        items=[dict(name='商品',amount=159,major_category='食費',minor_category='食品')])
    original=Mock(return_value=(b'synthetic-original','image/png'))
    monkeypatch.setattr(d,'original_png',lambda *args:(b'old-png','image/png'))
    texts=[SimpleNamespace(text='店A 2026/9/21',status='extracted',observation_complete=True),
        SimpleNamespace(text='店B 2026/9/26',status='extracted',observation_complete=True)]
    extraction=Mock(side_effect=texts);monkeypatch.setattr(d,'_extract_receipt_text',extraction)
    gate=Mock(return_value=SimpleNamespace(classification='normal',gemini_allowed=True))
    monkeypatch.setattr(d,'evaluate_receipt_privacy',gate)
    return db,rid,result,original,texts,gate,extraction


def compare(db,rid,result,original):
    return d.compare_distinct(db,rid,result,b'new-png',original,merchant_terms=(('店A',),('店B',)))


def writer_evidence(fresh):
    from hashlib import sha256
    _,png,result,_,_=fresh(11)
    return dict(schema='receipt-distinct-originals-v1',decision='distinct_transactions',
        receipt_id='R-old',import_id='receipt:old',source_file_id='old',source_content_hash='a'*64,
        rows_digest='b'*64,candidate_digest=c.digest(result.model_dump()),payload_sha256=sha256(png).hexdigest(),
        old_date='2026-09-25',new_date='2026-09-26',old_item_count=2,new_item_count=1,
        date_original_verified=True,merchant_original_verified=True)


def test_originals_not_same_amount_establish_distinct_then_recheck(monkeypatch):
    db,rid,r,get,*_=fixture(monkeypatch)
    evidence=compare(db,rid,r,get)
    assert d.verify_comparison(db,evidence,r,b'new-png',get)=={rid}
    assert evidence['old_item_count']==2 and evidence['new_item_count']==1
    assert all(k not in evidence for k in ('text','image','ocr','raw_response'))
    c.check_duplicates(db,'new-unit',r,verified_distinct={rid})
    with pytest.raises(StateError,match='possible_duplicate'):c.check_duplicates(db,'new-unit',r)
    db.rows['レシート'][0][2]='changed'
    with pytest.raises(StateError,match='comparison_changed'):d.verify_comparison(db,evidence,r,b'new-png',get)


@pytest.mark.parametrize('change',['same_date','same_merchant','same_items','missing_original_date',
    'missing_original_merchant','incomplete_ocr','privacy_hold','wrong_import','wrong_item_identity'])
def test_uncertain_or_conflicting_comparison_cannot_clear_hold(monkeypatch,change):
    db,rid,r,get,texts,gate,_=fixture(monkeypatch)
    if change=='same_date':r.date='2026-09-21'
    if change=='same_merchant':r.merchant='店A'
    if change=='same_items':r.items=[r.items[0].model_copy(update={'amount':59}),r.items[0].model_copy(update={'amount':100})]
    if change=='missing_original_date':texts[0].text='店A'
    if change=='missing_original_merchant':texts[0].text='2026/9/21'
    if change=='incomplete_ocr':texts[0].observation_complete=False
    if change=='privacy_hold':gate.return_value=SimpleNamespace(classification='medical',gemini_allowed=False)
    if change=='wrong_import':db.rows['取込データ'][0][3]='other-source'
    if change=='wrong_item_identity':db.rows['支出明細'][0][10]='receipt:other'
    with pytest.raises(StateError):compare(db,rid,r,get)


def test_original_payload_and_rows_are_frozen(monkeypatch):
    db,rid,r,get,*_=fixture(monkeypatch);evidence=compare(db,rid,r,get)
    get.return_value=(b'changed-original','image/png')
    with pytest.raises(StateError,match='comparison_changed'):d.verify_comparison(db,evidence,r,b'new-png',get)
    with pytest.raises(StateError,match='comparison_changed'):d.verify_comparison(db,evidence,r,b'new-png!',get)


def test_applied_replay_render_fingerprint_is_diagnostic_not_identity(monkeypatch):
    db,rid,r,get,*_=fixture(monkeypatch);evidence=compare(db,rid,r,get)
    assert d.verify_comparison(db,evidence,r,b'different-encoding',get,replay=True)=={rid}
    # Only completed replay may omit old encode equality; pre-write never may.
    with pytest.raises(StateError,match='comparison_changed'):
        d.verify_comparison(db,evidence,r,b'different-encoding',get)
    get.return_value=(b'changed-original','image/png')
    with pytest.raises(StateError,match='comparison_changed'):
        d.verify_comparison(db,evidence,r,b'different-encoding',get,replay=True)


def test_unrelated_duplicate_still_blocks(monkeypatch):
    db,rid,r,get,*_=fixture(monkeypatch)
    evidence=compare(db,rid,r,get)
    db.rows['レシート'].append(['another','2026-09-26','other',159])
    with pytest.raises(StateError,match='possible_duplicate'):
        c.check_duplicates(db,'new-unit',r,verified_distinct=d.verify_comparison(db,evidence,r,b'new-png',get))


def test_writer_rechecks_comparison_before_each_append_and_replay(monkeypatch):
    store,db,fresh,verify,t,_=context(monkeypatch)
    db.rows['レシート']=[['R-old','2026-09-25','old merchant',159]]
    resolver=SimpleNamespace(resolve=Mock(return_value=writer_evidence(fresh)),verify=Mock(return_value={'R-old'}))
    outcome=c.run_canary(store,db,11,fresh,verify,distinct_originals=resolver)
    assert outcome['status']=='imported' and resolver.verify.call_count>=4
    before=deepcopy(db.appends);writes=t.writes
    assert c.run_canary(store,db,11,fresh,verify,distinct_originals=resolver)['status']=='replayed'
    assert db.appends==before and t.writes==writes and resolver.resolve.call_count==1
    resolver.verify.side_effect=StateError('duplicate_comparison_changed')
    with pytest.raises(StateError,match='comparison_changed'):
        c.run_canary(store,db,11,fresh,verify,distinct_originals=resolver)
    assert db.appends==before and t.writes==writes


def test_changed_comparison_after_intent_prevents_all_accounting_write(monkeypatch):
    store,db,fresh,verify,t,_=context(monkeypatch)
    resolver=SimpleNamespace(resolve=Mock(return_value=writer_evidence(fresh)),
        verify=Mock(side_effect=[set(),StateError('duplicate_comparison_changed')]))
    with pytest.raises(StateError,match='comparison_changed'):
        c.run_canary(store,db,11,fresh,verify,distinct_originals=resolver)
    assert not db.appends and t.writes==1
    assert store.value['records']['stable-unit']['phase']=='pending'


def test_original_multpage_cannot_be_assumed_one_receipt(monkeypatch):
    class Document:
        def __len__(self):return 2
        def close(self):pass
        def __getitem__(self,index):raise AssertionError('No page may be rendered')
    monkeypatch.setattr(d.pdfium,'PdfDocument',lambda _:Document())
    with pytest.raises(StateError,match='original_held'):d.original_png(b'synthetic','application/pdf')
