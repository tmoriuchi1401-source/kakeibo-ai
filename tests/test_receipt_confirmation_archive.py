from copy import deepcopy
from hashlib import sha256
from unittest.mock import Mock

import pytest

from app.drive_run_state import StateError
from app.receipt_confirmation import review_id
from app.receipt_confirmation_archive import archive_confirmations
from tests.test_receipt_confirmation import medical,confirm


def setup_archive():
    review,store,db,verify,source=medical()
    # Rebuild the synthetic identity using real content bytes.
    old_key=review_id('medical',source);source['sha256']=sha256(b'original').hexdigest()
    item=store.value['confirmation_items'].pop(old_key);item['source']=deepcopy(source)
    key=review_id('medical',source);store.value['confirmation_items'][key]=item
    db.rows['領収書確認'][0][0]=key;review.render()
    confirm(db);review.capture_inputs();assert review.apply_confirmations()==1
    state={'parents':['synthetic-folder'],'version':'1','mimeType':'application/pdf','appProperties':{'unrelated':'keep'}}
    drive=Mock();drive.files().get().execute.side_effect=lambda **kw:deepcopy(state)
    def move(**kw):
        state.update(parents=['processed'],version='2',appProperties=kw['body']['appProperties'])
        return Mock(execute=Mock(return_value={'parents':['processed']}))
    drive.files().update.side_effect=move
    drive.reset_mock()
    return review,store,db,source,key,state,drive


def test_confirmed_medical_moves_once_after_readback_and_keeps_identity():
    review,store,db,source,key,state,drive=setup_archive()
    financial=deepcopy(db.rows);identity=deepcopy(source)
    assert archive_confirmations(review,'synthetic-folder','processed',drive,lambda _:b'original')==1
    assert state['parents']==['processed'] and state['appProperties']['unrelated']=='keep'
    assert state['appProperties']['kakeiboReceiptClass']=='medical'
    assert review.items[key]['source']==identity and db.rows==financial
    assert archive_confirmations(review,'synthetic-folder','processed',drive,lambda _:b'original')==0
    assert drive.files().update.call_count==1
    assert review.apply_confirmations()==0


def test_lost_move_response_is_read_back_without_resending():
    review,store,db,source,key,state,drive=setup_archive()
    def moved(**kw):
        state.update(parents=['processed'],version='2')
        return Mock(execute=Mock(side_effect=OSError('lost response')))
    drive.files().update.side_effect=moved
    assert archive_confirmations(review,'synthetic-folder','processed',drive,lambda _:b'original')==1
    assert drive.files().update.call_count==1
    assert review.items[key]['archive']['status']=='complete'


def test_move_restarts_after_completed_move_before_store_save():
    review,store,db,source,key,state,drive=setup_archive()
    review.items[key]['archive']={'status':'pending','destination':'processed'}
    state.update(parents=['processed'],version='2')
    assert archive_confirmations(review,'synthetic-folder','processed',drive,lambda _:b'original')==0
    drive.files().update.assert_not_called()
    assert review.items[key]['archive']['status']=='complete'


@pytest.mark.parametrize('change',['ledger','version','folder','bytes','trash'])
def test_archive_rejects_changed_accounting_or_source(change):
    review,store,db,source,key,state,drive=setup_archive()
    payload=b'original'
    if change=='ledger':db.rows['支出明細'][0][4]=999
    if change=='version':state['version']='9'
    if change=='folder':state['parents']=['other']
    if change=='bytes':payload=b'changed'
    if change=='trash':state['trashed']=True
    with pytest.raises(StateError):archive_confirmations(review,'synthetic-folder','processed',drive,lambda _:payload)
    drive.files().update.assert_not_called()


@pytest.mark.parametrize('status',['waiting','pending','superseded'])
def test_unconfirmed_or_incomplete_receipts_never_move(status):
    review,store,db,source,key,state,drive=setup_archive();review.items[key]['status']=status
    assert archive_confirmations(review,'synthetic-folder','processed',drive,lambda _:b'original')==0
    drive.files().get.assert_not_called();drive.files().update.assert_not_called()


def test_failed_move_leaves_applied_accounting_and_pending_archive():
    review,store,db,source,key,state,drive=setup_archive();financial=deepcopy(db.rows)
    drive.files().update.side_effect=None
    drive.files().update.return_value.execute.side_effect=OSError('failure')
    with pytest.raises(StateError,match='archive_readback_required'):
        archive_confirmations(review,'synthetic-folder','processed',drive,lambda _:b'original')
    assert review.items[key]['status']=='applied' and review.items[key]['archive']['status']=='pending'
    assert db.rows==financial and review.apply_confirmations()==0
