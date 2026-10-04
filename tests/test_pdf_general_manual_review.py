from copy import deepcopy
from unittest.mock import Mock
import pytest
from app.drive_run_state import StateError
from app.pdf_general_manual_review import GeneralManualReview
from app.pdf_page_kind import current_answer
from app.receipt_pdf_units import _digest
from test_pdf_production_authority import setup as authority_setup
from test_pdf_unit_processing import context
from test_pdf_general_manual import PDFDB
from test_manual_entry import FIRST,CANCEL
from test_receipt_pdf_units import local_ocr,synthetic_pdf

INPUT={'date':'2026-09-24','amount':500,'major':'食費','minor':'外食','merchant':'','payment':'','note':''}

def setup():
    g,live,drive,authority=authority_setup(('unknown','normal'),{1:'一般'})
    processing,store,_=context();db=PDFDB()
    p=g.store.load()['records'][_digest('drive-source-id')]['proposal'];page=p['pages'][0]
    a=current_answer(g.store.load(),page)
    key='pdf-general-manual-view-v1:'+_digest(['drive-source-id',p['source_content_hash'],1,
        page['page_hash'],a['confirmation_digest'],p['proposal_digest'],p['grouping_version']])
    identity={'schema':'pdf-page-review-v1','kind':'general_manual','source_file_id':'drive-source-id',
        'source_content_hash':p['source_content_hash'],'page_numbers':[1],'page_hashes':[page['page_hash']],
        'kind_digests':[a['confirmation_digest']],'review_id':key,'proposal_digest':p['proposal_digest'],
        'grouping_version':p['grouping_version']}
    review=GeneralManualReview(authority,store,db)
    return review,identity,db,processing,live,Mock(return_value=deepcopy(INPUT))

def run(args):return args[0](args[1],FIRST,INPUT,args[5])

def test_shared_pdf_request_uses_existing_writer_without_extra_sheet_or_queue_and_replays(local_ocr):
    args=setup();review,identity,db,drive,live,owner=args;queue=deepcopy(db.queue)
    assert run(args)=='一般手入力済み'
    assert len(db.appends)==1 and db.appends[0][0]=='支出明細' and db.queue==queue
    assert not db.writes and db.ledger[0][2]==db.ledger[0][7]==''
    before=(drive.payload,deepcopy(db.ledger),len(db.appends))
    assert run(args)=='一般手入力済み'
    assert before==(drive.payload,db.ledger,len(db.appends)) and not db.writes
    assert review(identity,CANCEL,INPUT,owner)=='一般手入力済み'
    assert before==(drive.payload,db.ledger,len(db.appends)) and not db.writes

@pytest.mark.parametrize('field,value',[('source_content_hash','f'*64),('page_numbers',[2]),
    ('page_numbers',[True]),('page_numbers',[1.0]),
    ('page_hashes',['f'*64]),('kind_digests',['f'*64]),('grouping_version',99),
    ('proposal_digest','f'*64),('kind','medical'),('review_id','forged')])
def test_ui_identity_cannot_authorize_a_different_unit(field,value,local_ocr):
    args=setup();args[1][field]=value
    with pytest.raises(StateError):run(args)
    assert not args[2].appends and not args[3].updates

def test_source_change_or_page_count_change_never_writes(local_ocr):
    for content in (b'changed',synthetic_pdf(('unknown','normal','normal'))):
        args=setup();args[4].content=content
        with pytest.raises(StateError):run(args)
        assert not args[2].appends and not args[3].updates

def test_owner_snapshot_changed_before_append_is_not_applied(local_ocr):
    args=setup();args[5].side_effect=[INPUT,INPUT,{**INPUT,'amount':501}]
    with pytest.raises(StateError):run(args)
    assert not args[2].appends

def test_strong_medical_hold_never_reaches_general_writer(local_ocr):
    args=setup();r=args[0];s=r.authority.current('drive-source-id').units[0]
    r.completion.block(s,'medical','exact_payload_sensitive',verify_current=r.authority.verify)
    with pytest.raises(StateError,match='strong_sensitive'):run(args)
    assert not args[2].appends

def test_ambiguous_accounting_delivery_requires_readback_and_never_resends(local_ocr):
    args=setup();db=args[2]
    db.append_raw=Mock(side_effect=RuntimeError('synthetic unacknowledged append'))
    with pytest.raises(Exception):run(args)
    assert db.append_raw.call_count==1
    with pytest.raises(StateError,match='requires_reconciliation'):
        args[0](args[1],CANCEL,INPUT,args[5])
    assert db.append_raw.call_count==1
    with pytest.raises(StateError,match='requires_reconciliation'):run(args)
    assert db.append_raw.call_count==1

def test_second_request_with_changed_input_never_overwrites_existing_accounting(local_ocr):
    args=setup();run(args);before=(args[3].payload,deepcopy(args[2].ledger),len(args[2].appends))
    changed={**INPUT,'amount':501}
    with pytest.raises(StateError,match='existing_intent_changed'):
        args[0](args[1],CANCEL,changed,lambda:changed)
    assert before==(args[3].payload,args[2].ledger,len(args[2].appends))
