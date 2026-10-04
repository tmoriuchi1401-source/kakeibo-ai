from copy import deepcopy
from unittest.mock import Mock
import json

import pytest
from app.drive_run_state import StateError
from app import manual_entry
from app.pdf_general_manual import confirm
from app.pdf_unit_processing import digest
from app.sheets import HEADERS
from test_manual_entry import FakeDB, FIRST, CANCEL
from test_pdf_unit_processing import context, spec


class PDFDB(FakeDB):
    def __init__(self):
        super().__init__(); self.receipts=[]; self.appends=[]

    def get_raw(self, range_):
        if range_=="'支出明細'!A1:M1":return [HEADERS['支出明細'][:]]
        if range_=="'支出明細'!A2:M":return deepcopy(self.ledger)
        if range_=="'レシート'!A2:I":return deepcopy(self.receipts)
        return super().get_raw(range_)

    def append_raw(self, title, rows):
        self.appends.append((title,deepcopy(rows)));super().append_raw(title,rows)


def setup():
    drive,store,acl=context();s=spec();db=PDFDB()
    db.add(payload={'date':'2026-09-24','amount':500,'merchant':'','major':'食費',
        'minor':'外食','note':'','payment':'','pdf_unit':digest(s)})
    return db,store,s,Mock(return_value=True),Mock(return_value=True),drive


def run(args,project=lambda *_:None):
    db,store,s,fresh,owner,_=args
    return confirm(db,store,s,FIRST,verify_current=fresh,
        verify_owner_request=owner,project=project)


def test_explicit_input_only_uses_existing_writer_and_replay_is_readback_only():
    args=setup();db,store,s,fresh,owner,drive=args
    out=run(args)
    assert out['status']=='manual_imported' and out['accounting_appends']==1
    assert db.ledger[0][2]=='' and db.ledger[0][7]==''
    before=(drive.payload,deepcopy(db.writes),deepcopy(db.appends))
    replay=run(args)
    assert replay['replayed'] and replay['accounting_appends']==0
    assert before==(drive.payload,db.writes,db.appends)
    assert owner.call_count>=4 and fresh.call_count>=8
    assert set(db.appends[0][0:1])=={'支出明細'}


def test_ordinary_manual_worker_rejects_pdf_marker_without_source_verifier():
    args=setup();db=args[0]
    with pytest.raises(StateError,match='pdf_verifier_required'):manual_entry.execute(db,FIRST)
    assert not db.ledger and not db.appends


def test_cancel_cannot_void_or_delete_a_pdf_bound_manual_expense():
    args=setup();run(args);db=args[0];db.add(CANCEL,{'target':FIRST})
    with pytest.raises(StateError,match='pdf_cancel_forbidden'):manual_entry.execute(db,CANCEL)
    assert db.ledger[0][12]=='active' and len(db.appends)==1


@pytest.mark.parametrize('field,value',[('date',''),('amount',0),('major',''),
    ('minor',''),('pdf_unit','f'*64)])
def test_incomplete_or_replaced_owner_input_never_claims_or_writes(field,value):
    args=setup();db=args[0];payload=json.loads(db.queue[1][2]);payload[field]=value
    db.queue[1][2]=json.dumps(payload)
    with pytest.raises(StateError,match='owner_input_invalid'):run(args)
    assert not args[-1].updates and not db.ledger


@pytest.mark.parametrize('barrier_index',[1,2,3])
def test_source_change_before_existing_writer_append_blocks_accounting(barrier_index):
    args=setup();args[3].side_effect=[True]*(barrier_index-1)+[False]
    with pytest.raises(StateError):run(args)
    assert not args[0].ledger


def test_missing_explicit_owner_confirmation_is_not_accounting_authority():
    args=setup();args[4].return_value=False
    with pytest.raises(StateError,match='source_or_owner'):run(args)
    assert not args[-1].updates and not args[0].ledger


@pytest.mark.parametrize('automatic,human',[('medical','normal'),('payroll','normal'),('normal','medical')])
def test_medical_or_payroll_cannot_enter_general_manual_backend(automatic,human):
    args=list(setup());args[2]=spec(automatic=automatic,human=human)
    payload=json.loads(args[0].queue[1][2]);payload['pdf_unit']=digest(args[2]);args[0].queue[1][2]=json.dumps(payload)
    with pytest.raises(StateError,match='state_invalid'):run(args)
    assert not args[0].ledger and not args[-1].updates


def test_possible_duplicate_is_a_hold_not_an_amount_only_duplicate_confirmation():
    args=setup();args[0].receipts=[['R-other','2026-09-21','別店舗',500,'','','解析済','','']]
    with pytest.raises(StateError,match='duplicate_candidate'):run(args)
    assert not args[0].ledger and not args[-1].updates


def test_ledger_content_mismatch_is_not_overwritten_or_deleted_on_replay():
    args=setup();run(args);args[0].ledger[0][4]=501;writes=len(args[0].writes)
    with pytest.raises(StateError,match='accounting_readback'):run(args)
    assert args[0].ledger[0][4]==501 and len(args[0].appends)==1 and len(args[0].writes)==writes


def test_same_id_duplicate_rows_are_not_accepted_as_exact_readback():
    args=setup();run(args);args[0].ledger.append(args[0].ledger[0][:])
    with pytest.raises(StateError,match='accounting_readback'):run(args)
    assert len(args[0].appends)==1


def test_projection_failure_replays_projection_only_without_drive_reapply():
    args=setup();projection=Mock(side_effect=RuntimeError('synthetic projection failure'))
    with pytest.raises(RuntimeError):run(args,project=projection)
    before=(args[-1].payload,len(args[-1].updates),len(args[0].writes))
    projection.side_effect=None
    assert run(args,project=projection)['replayed']
    assert before==(args[-1].payload,len(args[-1].updates),len(args[0].writes))
    assert len(args[0].appends)==1 and projection.call_count==2


def test_queue_update_failure_after_append_reconciles_without_second_accounting_write():
    args=setup();db=args[0];original=db.set_raw_range
    def fail_complete(range_,rows):
        if rows[0][1]=='complete':raise RuntimeError('synthetic queue failure')
        return original(range_,rows)
    db.set_raw_range=fail_complete
    with pytest.raises(RuntimeError):run(args)
    assert len(db.appends)==1 and db.queue[1][1]=='running'
    assert run(args)['replayed'] and len(db.appends)==1


def test_pending_without_exact_existing_rows_requires_manual_reconciliation():
    args=setup();db=args[0];db.append_raw=Mock(side_effect=ConnectionError('synthetic ambiguous write'))
    with pytest.raises(ConnectionError):run(args)
    assert not db.ledger
    with pytest.raises(StateError,match='pending_requires_reconciliation'):run(args)
    assert db.append_raw.call_count==1


def test_payload_change_between_confirmation_and_append_is_rejected():
    args=setup();db=args[0]
    def owner(request,payload):
        if args[4].call_count==2:
            changed=json.loads(db.queue[1][2]);changed['amount']=501;db.queue[1][2]=json.dumps(changed)
        return True
    args[4].side_effect=owner
    with pytest.raises(StateError,match='owner_snapshot_changed'):run(args)
    assert not db.ledger


def test_source_change_after_append_leaves_pending_not_applied_or_retried():
    args=setup();args[3].side_effect=[True,True,True,False]
    with pytest.raises(StateError,match='authority_or_source'):run(args)
    assert len(args[0].appends)==1
    record=next(iter(args[1].load()['records'].values()))
    assert record['phase']=='pending'
    args[3].side_effect=None;args[3].return_value=False
    with pytest.raises(StateError):run(args)
    assert len(args[0].appends)==1


def test_new_other_transaction_does_not_revoke_completed_manual_replay():
    args=setup();run(args)
    args[0].receipts=[['R-other','2026-09-21','別店舗',500,'','','解析済','','']]
    assert run(args)['replayed'] and len(args[0].appends)==1


def test_processing_state_change_before_append_is_rejected():
    args=setup();store=args[1]
    store.verify_pending=Mock(side_effect=StateError('pdf_processing_pending_intent_changed'))
    with pytest.raises(StateError,match='pending_intent_changed'):run(args)
    assert not args[0].ledger
    record=next(iter(args[1].load()['records'].values()))
    assert record['phase']=='pending'
    args[3].side_effect=None;args[3].return_value=False
    with pytest.raises(StateError):run(args)
    assert len(args[0].appends)==0
