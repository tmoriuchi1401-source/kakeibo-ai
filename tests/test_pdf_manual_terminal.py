"""Read-only verification against actual manual backend logic on synthetic data."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from app.drive_run_state import DriveStateTransport,StateError
from app.pdf_manual_terminal import ManualTerminalReadback
from app.pdf_unit_processing import digest
from app.pdf_page_kind import current_answer
from app.receipt_confirmation import ReceiptConfirmation,review_id,TITLE
from app.receipt_pdf_units import DocumentUnit,PageObservation,_digest
from test_pdf_general_manual_review import setup as general_context, run as general_confirm
from test_pdf_production_authority import setup as authority_context
from test_pdf_unit_processing import context as completion_context
from test_receipt_reimport_production import fixtures,MemoryTransport
from test_receipt_confirmation import DB,confirm
from test_receipt_pdf_units import local_ocr


def general():
    args=general_context();assert general_confirm(args)=='一般手入力済み'
    review,identity,db,drive,live,owner=args
    spec=review.current(identity);record=review.completion.load()['records'][spec['unit_id']]
    reader=ManualTerminalReadback(review.authority,review.completion,db)
    return reader,spec,record,db,drive,live


def medical():
    g,live,ad,provider=authority_context(('unknown','normal'),{1:'医療'})
    value=g.store.load();page=value['records'][_digest('drive-source-id')]['proposal']['pages'][0]
    answer=current_answer(value,page)
    # Existing page Unit/review identity; independent of the new completion ID.
    source={'source_id':DocumentUnit(PageObservation(**page)).source_id,
        'version':page['page_hash'],'sha256':page['source_content_hash'],'mime_type':'application/pdf',
        'pdf_page':{'original_file_id':'drive-source-id','page_number':1,'page_hash':page['page_hash'],
            'page_kind_confirmation_digest':answer['confirmation_digest']}}
    store,original_transport,_=fixtures();db=DB()
    class SyntheticPrivateDrive(MemoryTransport,DriveStateTransport):pass
    backend_transport=SyntheticPrivateDrive(original_transport.payload)
    backend_transport.binding=SimpleNamespace(folder_id='private-folder',file_id='separate-medical-state')
    store.transport=backend_transport
    class PageManual(ReceiptConfirmation):
        @property
        def items(self):return self.store.value.get('confirmation_items',{})
    backend=PageManual(store,db,Mock())
    backend.observe_medical(source,'synthetic-folder');backend.render();confirm(db)
    db.rows[TITLE][0][8]='本人が完全入力した架空施設'
    backend.capture_inputs();assert backend.apply_confirmations()==1
    key=review_id('medical',source);item=store.value['confirmation_items'][key]
    spec=provider.current('drive-source-id').medical_pages[0]
    drive,completion,_=completion_context()
    record,_=completion.reserve(spec,'medical_manual',item['confirmation_hash'],{},
        digest(['existing-medical-manual-v1',key,item['confirmation_hash']]),verify_current=provider.verify)
    completion.complete(spec['unit_id'],record['intent_digest'],verify_current=provider.verify,
        verify_readback=lambda _:backend._complete(item['plan']))
    reader=ManualTerminalReadback(provider,completion,db,medical_store=store)
    return reader,spec,record,db,drive,live,store,backend_transport,key


def test_general_terminal_requires_exact_existing_manual_rows_and_replay_is_get_only(local_ocr):
    reader,spec,record,db,drive,live=general()
    before=(drive.payload,len(drive.updates),deepcopy(db.ledger),len(db.appends))
    assert reader.verify(spec,record['intent_digest']) is True
    assert reader.verify(spec,record['intent_digest']) is True
    assert before==(drive.payload,len(drive.updates),db.ledger,len(db.appends))
    assert db.ledger[0][2]==db.ledger[0][7]==''


@pytest.mark.parametrize('change',['source','accounting','duplicate_row','missing_row','intent','category'])
def test_general_terminal_refuses_stale_incomplete_or_changed_evidence(change,local_ocr):
    reader,spec,record,db,drive,live=general()
    token=record['intent_digest']
    if change=='source':live.content+=b'changed'
    if change=='accounting':db.ledger[0][4]=501
    if change=='duplicate_row':db.ledger.append(deepcopy(db.ledger[0]))
    if change=='missing_row':db.ledger.clear()
    if change=='intent':token='f'*64
    if change=='category':db.categories=lambda:[]
    before=(drive.payload,len(drive.updates),deepcopy(db.ledger),len(db.appends))
    with pytest.raises(StateError):reader.verify(spec,token)
    assert before==(drive.payload,len(drive.updates),db.ledger,len(db.appends))


def test_medical_terminal_reuses_complete_manual_backend_without_any_new_write(local_ocr):
    reader,spec,record,db,drive,live,store,t,key=medical()
    before=(drive.payload,len(drive.updates),store.payload,t.writes,deepcopy(db.rows),db.writes)
    assert reader.verify(spec,record['intent_digest']) is True
    assert reader.verify(spec,record['intent_digest']) is True
    assert before==(drive.payload,len(drive.updates),store.payload,t.writes,db.rows,db.writes)
    assert next(iter(reader.completion.load()['records'].values()))['planned_rows']=={}


@pytest.mark.parametrize('change',['source','missing_backend','pending','automatic','input','reference',
    'source_record','duplicate_row','accounting','backend_bytes','unrelated_plan','changed_plan_amount'])
def test_medical_terminal_cannot_be_inferred_from_status_or_completion_marker_alone(change,local_ocr):
    reader,spec,record,db,drive,live,store,t,key=medical()
    if change=='source':live.content+=b'changed'
    if change=='missing_backend':reader.medical_store=None
    if change in {'pending','automatic','input','source_record'}:
        value=deepcopy(store.value);item=value['confirmation_items'][key]
        if change=='pending':item['status']='pending'
        if change=='automatic':item['decision_origin']='automatic'
        if change=='input':item['inputs'][2]='999'
        if change=='source_record':item['source']['pdf_page']['page_number']=2
        store.save(value)
    if change=='reference':
        token='f'*64
    else:token=record['intent_digest']
    if change=='duplicate_row':db.rows['支出明細'].append(deepcopy(db.rows['支出明細'][0]))
    if change=='accounting':db.rows['支出明細'][0][4]=999
    if change=='backend_bytes':t.payload=b'changed'
    if change in {'unrelated_plan','changed_plan_amount'}:
        value=deepcopy(store.value);item=value['confirmation_items'][key]
        if change=='unrelated_plan':item['plan']=[['支出明細',deepcopy(db.rows['支出明細'][0])]]
        else:
            # Even exact read-back of a changed plan is not the owner input.
            for title,row in item['plan']:
                amount=3 if title=='レシート' else 6 if title=='取込データ' else 4
                row[amount]=999
                for actual in db.rows[title]:
                    if actual[0]==row[0]:actual[amount]=999
        store.save(value)
    before=(drive.payload,len(drive.updates),t.payload,t.writes,deepcopy(db.rows),db.writes)
    with pytest.raises(StateError):reader.verify(spec,token)
    assert before==(drive.payload,len(drive.updates),t.payload,t.writes,db.rows,db.writes)


def test_a_ui_or_local_snapshot_is_never_a_terminal_authority():
    with pytest.raises(StateError,match='durable_dependencies'):
        ManualTerminalReadback(Mock(),Mock(),Mock(),medical_store=Mock())


def test_local_medical_json_is_never_a_durable_backend(local_ocr):
    reader,spec,record,db,drive,live,store,t,key=medical()
    store.transport=MemoryTransport(store.payload)
    with pytest.raises(StateError,match='durable_dependencies'):
        ManualTerminalReadback(reader.authority,reader.completion,db,medical_store=store)


@pytest.mark.parametrize('classification',['medical','payroll'])
def test_new_strong_sensitive_hold_cannot_be_ignored_by_general_terminal_plan(classification,local_ocr):
    reader,spec,record,db,drive,live=general()
    reader.completion.block(spec,classification,'exact_payload_sensitive',verify_current=reader.authority.verify)
    before=(drive.payload,len(drive.updates),deepcopy(db.ledger),len(db.appends))
    with pytest.raises(StateError,match='strong_sensitive_hold'):reader.verify(spec,record['intent_digest'])
    assert before==(drive.payload,len(drive.updates),db.ledger,len(db.appends))
