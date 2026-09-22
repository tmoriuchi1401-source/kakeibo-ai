from copy import deepcopy
from unittest.mock import Mock

import pytest

from app.medical_local_duplicate import decide,verify_saved_targets
from app.medical_local_reading import apply_local
from app.receipt_confirmation import review_id
from app.drive_run_state import StateError
from test_medical_document_identity import evidence
from test_medical_local_reading import parsed,proof
from test_medical_payment_units import receipt,load
from test_receipt_confirmation import medical


def setup(monkeypatch):
    review,store,db,verify,source=medical()
    old=receipt((1234,),identity='existing-medical')
    for table,merchant_col in [('receipt_rows',2),('import_rows',5),('expense_rows',2)]:
        old[table][0][merchant_col]='Synthetic clinic'
    old['expense_rows'][0][5:7]=['医療・保険','病院'];load(db,old)
    p=proof();p['document_identity']=evidence(monkeypatch)
    target={'source':{'source_id':'existing-medical','version':'2','mime_type':'image/png','sha256':'b'*64},
            'identity':evidence(monkeypatch,source='b')}
    reader=Mock(return_value=target)
    return review,store,db,verify,source,p,reader


def test_exact_document_links_once_without_new_or_changed_expense(monkeypatch):
    review,store,db,verify,source,p,reader=setup(monkeypatch)
    before=deepcopy(db.rows['支出明細'])
    assert apply_local(review,source,'synthetic-folder',parsed(),p,read_existing=reader)
    item=review.items[review_id('medical',source)]
    assert db.rows['支出明細']==before
    assert len(db.rows['レシート'])==len(db.rows['取込データ'])==2
    assert db.rows['取込データ'][-1][8:10]==['matched_receipt',before[0][0]]
    assert item['decision_origin']=='automatic' and 'confirmation_hash' not in item
    assert item['local_decision']['reconciliation']['linked_expense_id']==before[0][0]
    assert reader.call_count==2
    assert not apply_local(review,source,'synthetic-folder',parsed(),p,read_existing=reader)
    assert db.rows['支出明細']==before


@pytest.mark.parametrize('change',['other_key','no_evidence','amount','unposted','two_targets','unrelated_card','incomplete_receipt'])
def test_ambiguous_or_unverified_duplicate_cannot_link_or_post(monkeypatch,change):
    review,store,db,verify,source,p,reader=setup(monkeypatch)
    record=deepcopy(reader.return_value)
    if change=='other_key':record['identity']=evidence(monkeypatch,source='b',key=b'y'*32)
    if change=='no_evidence':p.pop('document_identity')
    if change=='amount':record['identity']['amount']=1235
    if change=='unposted':db.rows['支出明細'][0][12]='inactive'
    if change=='two_targets':
        extra=receipt((1234,),identity='other-medical');extra['expense_rows'][0][5:7]=['医療・保険','病院']
        for k,t in [('receipt_rows','レシート'),('import_rows','取込データ'),('expense_rows','支出明細')]:db.rows[t].extend(extra[k])
        def same(sid):return {'source':dict(record['source'],source_id=sid),'identity':record['identity']}
        reader.side_effect=same
    if change=='unrelated_card':db.rows['取込データ'].append(['card','','card','card','2026-09-01','Unknown merchant',1234,'','auto_expense'])
    if change=='incomplete_receipt':db.rows['レシート']=[]
    reader.return_value=record;before=deepcopy(db.rows)
    assert not apply_local(review,source,'synthetic-folder',parsed(),p,read_existing=reader)
    assert db.rows==before


def test_verified_distinct_same_day_same_amount_bill_posts_once(monkeypatch):
    review,store,db,verify,source,p,reader=setup(monkeypatch)
    reader.return_value['identity']=evidence(monkeypatch,number='765432',source='b')
    before=deepcopy(db.rows['支出明細'])
    assert apply_local(review,source,'synthetic-folder',parsed(),p,read_existing=reader)
    assert len(db.rows['支出明細'])==2 and db.rows['支出明細'][0]==before[0]
    item=review.items[review_id('medical',source)]
    assert item['local_decision']['reconciliation']['operation']=='post_distinct'
    assert not apply_local(review,source,'synthetic-folder',parsed(),p,read_existing=reader)


@pytest.mark.parametrize('change',['source','target','new_duplicate','owner','reader_error'])
def test_freshness_change_after_intent_aborts_without_accounting(monkeypatch,change):
    review,store,db,verify,source,p,reader=setup(monkeypatch)
    calls=0;record=deepcopy(reader.return_value)
    def read(sid):
        nonlocal calls
        calls+=1
        if calls==2:
            if change=='source':return dict(record,source=dict(record['source'],version='3'))
            if change=='target':db.rows['支出明細'][0][12]='inactive'
            if change=='new_duplicate':db.rows['取込データ'].append(['new-card','','card','card','2026-09-01','Unknown',1234,'','auto_expense'])
            if change=='owner':db.rows['領収書確認'][0][12]='保留'
            if change=='reader_error':raise OSError('unavailable')
        return record
    reader.side_effect=read
    assert not apply_local(review,source,'synthetic-folder',parsed(),p,read_existing=reader)
    assert len(db.rows['レシート'])==1 and not any(r[0]=='receipt:'+source['source_id'] for r in db.rows['取込データ'])
    assert review.items[review_id('medical',source)]['status']=='waiting'


def test_capability_does_not_replay_from_json_or_changed_snapshot(monkeypatch):
    review,store,db,verify,source,p,reader=setup(monkeypatch)
    item=review.items[review_id('medical',source)]
    decision=decide(source,parsed(),p,review.tables(),reader)
    with pytest.raises(ValueError):review._plan(item,parsed(),decision.linked_expense_id,automatic=True,local_duplicate=decision.audit())
    db.rows['支出明細'][0][11]='Changed note'
    with pytest.raises(ValueError):review._plan(item,parsed(),decision.linked_expense_id,automatic=True,local_duplicate=decision)


def test_target_change_after_accounting_is_detected_in_recovery(monkeypatch):
    review,store,db,verify,source,p,reader=setup(monkeypatch)
    assert apply_local(review,source,'synthetic-folder',parsed(),p,read_existing=reader)
    item=review.items[review_id('medical',source)]
    db.rows['支出明細'][0][4]=999
    with pytest.raises(StateError,match='medical_duplicate_target_changed'):verify_saved_targets(item,review.tables())
    item['status']='pending'
    with pytest.raises(StateError,match='medical_duplicate_target_changed'):review.apply_confirmations()


def test_existing_reader_checks_current_bytes_and_preserves_historical_binding(monkeypatch):
    from hashlib import sha256
    from app.medical_local_duplicate import existing_reader
    from app import medical_candidate_preparation as prep
    review,store,db,verify,source,p,reader=setup(monkeypatch)
    monkeypatch.setenv('KAKEIBO_LOCAL_OCR_ENABLED','true')
    payload=b'synthetic-original';checksum=sha256(payload).hexdigest()
    meta={'id':'existing-medical','parents':['processed'],'mimeType':'image/png','version':'2'}
    api=Mock();api.files().get().execute.side_effect=lambda **kw:deepcopy(meta)
    prepare=Mock(return_value=({'status':'local_ready','local_provenance':{'document_identity':{'source_sha256':checksum}}},None))
    monkeypatch.setattr(prep,'prepare',prepare)
    download=Mock(return_value=payload)
    read=existing_reader(api,download,'processed',b'x'*32,review)
    first=read('existing-medical');assert first['source']['sha256']==checksum
    assert read('existing-medical')==first and prepare.call_count==1 and download.call_count==2
    # Cache skips repeat OCR only. Every call still verifies source metadata
    # and fresh downloaded bytes, including after a process-external edit.
    old=deepcopy(next(iter(review.items.values())))
    old['source']=deepcopy(first['source']);old['status']='applied'
    review.save_item(review_id('medical',old['source']),old)
    download.return_value=b'changed-original';meta['version']='3'
    assert read('existing-medical') is None and prepare.call_count==1
    meta['parents']=['unrelated-folder'];download.reset_mock()
    assert read('existing-medical') is None
    download.assert_not_called()


def test_duplicate_target_change_blocks_archiving_before_drive_call(monkeypatch):
    from app.receipt_confirmation_archive import archive_confirmations
    review,store,db,verify,source,p,reader=setup(monkeypatch)
    assert apply_local(review,source,'synthetic-folder',parsed(),p,read_existing=reader)
    db.rows['支出明細'][0][12]='inactive';drive=Mock()
    with pytest.raises(StateError,match='medical_duplicate_target_changed'):
        archive_confirmations(review,'synthetic-folder','processed',drive,Mock())
    drive.files.assert_not_called()


def test_an_existing_verified_alias_does_not_hide_canonical_receipt(monkeypatch):
    review,store,db,verify,source,p,reader=setup(monkeypatch)
    root=deepcopy(db.rows['支出明細'])
    db.rows['レシート'].append(['R-prior-copy','2026-09-01','Synthetic clinic',1234,'','','解析済','',''])
    db.rows['取込データ'].append(['receipt:prior-copy','','receipt','prior-copy','2026-09-01','Synthetic clinic',1234,'',
                                'matched_receipt',root[0][0],'b'*64,''])
    assert apply_local(review,source,'synthetic-folder',parsed(),p,read_existing=reader)
    assert db.rows['支出明細']==root
    assert db.rows['取込データ'][-1][9]==root[0][0]
