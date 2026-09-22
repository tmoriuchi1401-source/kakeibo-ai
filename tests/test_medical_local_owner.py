from copy import deepcopy

import pytest

from app.medical_local_owner import blocked,compatible,snapshot
from app.medical_auto_posting import owner_blocked
from app.medical_local_reading import apply_local
from app.receipt_confirmation import TITLE,review_id
from test_medical_local_reading import parsed,proof
from test_receipt_confirmation import medical


def prior_positive(review,store,db,source):
    key=review_id('medical',source);old=deepcopy(review.items[key])
    old['source']['version']='older';old['status']='superseded';old['inputs'][5]='医療費を確定'
    oldkey=review_id('medical',old['source']);review.save_item(oldkey,old)
    row=deepcopy(db.rows[TITLE][0]);row[0]=oldkey;row[12]='医療費を確定';db.rows[TITLE].append(row)
    return oldkey


def test_metadata_only_version_preserves_positive_intent_and_cloud_guard():
    review,store,db,verify,source=medical();oldkey=prior_positive(review,store,db,source)
    inputs=deepcopy(review.items[oldkey]['inputs'])
    assert owner_blocked(source,store.value) and not blocked(source,store.value)
    assert apply_local(review,source,'synthetic-folder',parsed(),proof())
    assert review.items[oldkey]['inputs']==inputs and db.rows[TITLE][1][12]=='医療費を確定'


@pytest.mark.parametrize('change',['hold','note','amount','link','changed_bytes','reconfirm','closed','unknown_error'])
def test_local_guard_preserves_every_real_owner_veto(change):
    review,store,db,verify,source=medical();key=prior_positive(review,store,db,source)
    old=review.items[key]
    if change=='hold':old['inputs'][5]='保留'
    if change=='note':old['inputs'][7]='Owner note'
    if change=='amount':old['inputs'][2]='1234'
    if change=='link':old['inputs'][6]='expense-id'
    if change=='changed_bytes':old['source']['sha256']='b'*64
    if change=='reconfirm':old['require_reconfirm']=True
    if change=='closed':old['status']='closed_user'
    if change=='unknown_error':old['error']='Other conflict'
    assert blocked(source,store.value)
    assert not apply_local(review,source,'synthetic-folder',parsed(),proof())
    assert not db.rows['支出明細']


def test_live_hold_on_superseded_row_wins_before_and_after_intent():
    review,store,db,verify,source=medical();prior_positive(review,store,db,source)
    db.rows[TITLE][1][12]='保留'
    assert snapshot(review,source) is None
    assert not apply_local(review,source,'synthetic-folder',parsed(),proof())
    db.rows[TITLE][1][12]='医療費を確定'
    def change(*a):
        if verify.call_count==2:db.rows[TITLE][1][12]='保留'
    verify.side_effect=change
    assert not apply_local(review,source,'synthetic-folder',parsed(),proof())
    assert not db.rows['支出明細'] and db.rows[TITLE][1][12]=='保留'


def test_kind_answer_only_narrows_routing_not_send_authority():
    review,store,db,verify,source=medical();item=deepcopy(next(iter(review.items.values())))
    item.update(kind='intake',status='closed_user',inputs=['']*7+['医療'])
    assert compatible(item,source)
    item['inputs'][5]='保留'
    assert not compatible(item,source)
