from copy import deepcopy
from hashlib import sha256

import pytest

from app.drive_run_state import StateError
from app.receipt_confirmation import ReceiptConfirmation, TITLE, review_id
from app.receipt_confirmation_production import verify_receipt_source
from app.receipt_reimport_production import TABLES, target_snapshot
from tests.test_receipt_confirmation import DB, Store
from tests.test_receipt_reimport import fixture


def identity(version='1', folder='inbox', payload=b'original', file_id='same-file'):
    source={'source_id':'same-file','version':'1','sha256':sha256(b'original').hexdigest(),
            'mime_type':'application/pdf'}
    metadata={'id':file_id,'parents':[folder],'version':version,'mimeType':'application/pdf'}
    return source,lambda _:deepcopy(metadata),lambda _:payload


def check(source, read_metadata, read_bytes, original='inbox'):
    return verify_receipt_source(source,original,'inbox','processed',read_metadata,read_bytes)


def test_same_file_and_bytes_pass_after_inbox_to_processed_move():
    source,metadata,download=identity(version='2',folder='processed')
    assert check(source,metadata,download)['parents']==['processed']


@pytest.mark.parametrize('change',[
    {'payload':b'changed'},
    {'file_id':'different-file'},
    {'folder':'unexpected'},
])
def test_changed_content_identity_or_location_blocks(change):
    source,metadata,download=identity(**{'version':'2','folder':'processed',**change})
    with pytest.raises(StateError,match='confirmation_source_changed'):
        check(source,metadata,download)


def test_missing_hash_and_metadata_race_block():
    source,metadata,download=identity(version='2',folder='processed')
    source.pop('sha256')
    with pytest.raises(StateError,match='confirmation_source_changed'):
        check(source,metadata,download)
    source['sha256']=sha256(b'original').hexdigest()
    calls=0
    def changed_metadata(sid):
        nonlocal calls
        calls+=1
        result=metadata(sid)
        if calls==2:result['version']='3'
        return result
    with pytest.raises(StateError,match='confirmation_source_changed'):
        check(source,changed_metadata,download)


def test_verified_superseded_choice_updates_existing_rows_once_then_replay_writes_zero():
    parsed,rows=fixture()
    parsed.items[0].name='Corrected candidate name'
    rows.pop('categories')
    store,db=Store(),DB()
    for key,title in TABLES.items():db.rows[title]=deepcopy(rows[key])
    source={'source_id':'s1','version':'1','sha256':sha256(b'original').hexdigest(),
            'mime_type':'application/pdf'}
    store.value['manifest']={'sources':[source],'folder_id':'processed'}
    store.value['records']={'s1':{'phase':'complete','parsed':parsed.model_dump(),
        'before':target_snapshot(rows,'s1')}}
    live={'id':'s1','parents':['processed'],'version':'2','mimeType':'application/pdf'}
    verify=lambda s,f:verify_receipt_source(s,f,'inbox','processed',
        lambda _:deepcopy(live),lambda _:b'original')
    review=ReceiptConfirmation(store,db,verify)
    review.prepare_general();review.render()
    db.rows[TITLE][0][12]='候補明細で確定'
    review.capture_inputs()
    key=review_id('normal',source)
    store.value['confirmation_items'][key].update(status='superseded',
        error='原本の版が変更。新しい対象で再確認してください')
    expense_count=len(db.rows['支出明細'])
    assert review.apply_confirmations()==1
    assert len(db.rows['支出明細'])==expense_count
    assert db.rows['支出明細'][0][3]=='Corrected candidate name'
    assert review.items[key]['status']=='applied'
    writes=db.writes
    assert ReceiptConfirmation(store,db,verify).apply_confirmations()==0
    assert db.writes==writes and len(db.rows['支出明細'])==expense_count
