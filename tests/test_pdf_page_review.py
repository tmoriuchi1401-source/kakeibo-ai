"""Synthetic source/pages, fake durable Drive, real existing manual writer."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID
import json

import pytest

from app.drive_run_state import StateError
from app.pdf_page_kind import PageKindConfirmation, current_answer, kind_key
from app.pdf_page_medical import PdfManualConfirmation, manual_source, manual_values
from app.pdf_page_review import cards, check_snapshot, original_uri, process_page_request, publish_current
from app.pdf_page_review_worker import medical_factory, medical_results
from app.receipt_confirmation import ReceiptConfirmation, review_id
from app.receipt_confirmation_archive import archive_confirmations
from app.receipt_pdf_units import _digest
from test_pdf_grouping_authority import context, NOW
from test_receipt_pdf_units import local_ocr, synthetic_pdf
from test_receipt_confirmation import Store, DB
from test_pdf_grouping_transport_ui import FakeSheets,SheetRequest

CATEGORIES=[('医療・保険','病院'),('医療・保険','薬'),('医療・保険','その他')]
FIELDS={'date':'2026/09/01','facility':'Synthetic manually typed clinic','amount':'100',
        'category':'医療費','medical_action':'医療費を確定','payment':'現金','memo':''}


def setup():
    g,live,transport=context(('unknown','normal'))
    view=g.display('drive-source-id')
    return g,live,transport,PageKindConfirmation(g),view


def snapshot(card, fields=None):
    value=deepcopy(card)
    value['rows']=[list(row) for row in value['rows']]
    value['original_link']=original_uri(value['identity']['source_file_id'],value['identity']['page_numbers'][0])
    for row in value['rows']:
        if fields and row[0] in fields:row[2]=fields[row[0]]
    return value


class Sheet:
    categories=CATEGORIES
    def __init__(self,capture):
        self.capture=capture;self.live=deepcopy(capture);self.published=[];self.finished=[];self.fail=False
        self.load_medical_results=lambda:{}
    def request(self,id):return 2,deepcopy(self.capture)
    def read_card(self,token):return deepcopy(self.live)
    def publish_cards(self,views):
        if self.fail:raise OSError('Synthetic projection failure')
        self.published=deepcopy(views)
    def finish_card(self,id,request,result,now):self.finished.append(result)


def medical_context():
    g,live,transport,kinds,view=setup()
    kinds.confirm(view['proposal'],{1:'医療'})
    _,p,answers=kinds.current('drive-source-id')
    card=next(c for c in cards(view,answers,CATEGORIES) if c['identity']['kind']=='medical')
    sheet=Sheet(snapshot(card,FIELDS));store,db=Store(),DB()
    factory=medical_factory(kinds,store,db,'synthetic-folder')
    sheet.load_medical_results=lambda:medical_results(kinds,store,db,'synthetic-folder')
    return kinds,live,transport,sheet,store,db,factory


@pytest.mark.parametrize('selection,kind',[('医療','medical'),('一般','normal'),('給与','payroll'),('判定不能','sensitive_unknown')])
def test_unknown_human_kind_is_identity_bound_and_never_ai_or_posting_authority(local_ocr,selection,kind):
    g,live,t,kinds,view=setup();auto=deepcopy(view['proposal'])
    answer=kinds.confirm(view['proposal'],{1:selection})[1]
    assert answer['human_classification']==kind and answer['automatic_classification']=='sensitive_unknown'
    assert answer['authority_scope']==['page_kind_only']
    assert all(answer[k] is False for k in ('gemini_allowed','accounting_allowed','medical_handoff_allowed','archive_allowed'))
    value=g.store.load()
    assert value['records'][_digest('drive-source-id')]['proposal']==auto
    assert value['records'][_digest('drive-source-id')]['confirmation'] is None
    assert 'PRIVATE' not in json.dumps(value)


def test_medical_fields_blank_normal_not_asked_and_single_mobile_entry(local_ocr):
    g,live,t,kinds,view=setup()
    before=cards(view,{},CATEGORIES)
    assert sum(c['identity']['kind']=='page_kind' for c in before)==1
    answers=kinds.confirm(view['proposal'],{1:'医療'})
    projected=cards(view,answers,CATEGORIES)
    med=next(c for c in projected if c['identity']['kind']=='medical')
    fields={f:v for f,l,v in med['rows']}
    assert fields['state']=='医療入力待ち' and all(fields[f]=='' for f in ('date','facility','amount','payment','memo'))
    assert fields['category']=='医療費'
    general=next(c for c in projected if c['identity']['kind']=='grouping')
    assert general['identity']['page_numbers']==[2]


def test_human_normal_routing_never_downgrades_privacy_or_grouping(local_ocr):
    g,live,t,kinds,view=setup()
    answers=kinds.confirm(view['proposal'],{1:'一般'})
    general=cards(view,answers,CATEGORIES)[0]
    assert general['identity']['kind']=='grouping' and general['identity']['page_numbers']==[1,2]
    assert live.observations.pages[0].classification=='sensitive_unknown'
    assert view['proposal']['pages'][0]['classification']=='sensitive_unknown'
    with pytest.raises(ValueError,match='privacy'):live.observations.page_payload(1)


@pytest.mark.parametrize('changed',['source','page','automatic'])
def test_source_or_page_change_invalidates_kind_and_manual_confirmation(local_ocr,changed):
    kinds,live,t,sheet,store,db,factory=medical_context()
    pages=list(live.observations.pages)
    if changed=='source':live.observations=replace(live.observations,source_content_hash='b'*64)
    elif changed=='page':
        pages[0]=replace(pages[0],page_hash='c'*64);live.observations=replace(live.observations,pages=tuple(pages))
    else:
        pages[0]=replace(pages[0],classification='medical');live.observations=replace(live.observations,pages=tuple(pages))
    with pytest.raises(StateError):process_page_request(kinds,sheet,str(UUID(int=1)),medical_factory=factory)
    assert db.writes==0 and not store.value['confirmation_items']


def test_kind_replay_keeps_timestamp_digest_and_drive_generation(local_ocr):
    g,live,t,kinds,view=setup()
    first=kinds.confirm(view['proposal'],{1:'医療'});writes=t.writes
    g.clock=lambda:'2026-10-02T12:00:00+00:00'
    assert kinds.confirm(view['proposal'],{1:'医療'})==first and t.writes==writes


def test_kind_projection_failure_replay_recovers_without_authority_resave(local_ocr):
    g,live,t,kinds,view=setup();card=cards(view,{},CATEGORIES)[0]
    sheet=Sheet(snapshot(card,{'kind_choice':'医療','kind_action':'種別を確定'}));sheet.fail=True
    id=str(UUID(int=9))
    with pytest.raises(OSError):process_page_request(kinds,sheet,id)
    writes=t.writes;answer=g.store.load()['page_kinds'][kind_key('drive-source-id',1)]
    sheet.fail=False
    assert process_page_request(kinds,sheet,id)=='page_kind_confirmed'
    assert t.writes==writes and g.store.load()['page_kinds'][kind_key('drive-source-id',1)]==answer


@pytest.mark.parametrize('field',['date','facility','amount','category'])
def test_incomplete_medical_confirm_never_reaches_common_writer(local_ocr,field):
    kinds,live,t,sheet,store,db,factory=medical_context()
    for where in (sheet.capture,sheet.live):
        next(r for r in where['rows'] if r[0]==field)[2]=''
    result=process_page_request(kinds,sheet,str(UUID(int=1)),medical_factory=factory)
    assert result!='医療確定済み' and db.writes==0


def test_complete_typing_without_explicit_confirm_cannot_post(local_ocr):
    kinds,live,t,sheet,store,db,factory=medical_context()
    for where in (sheet.capture,sheet.live):next(r for r in where['rows'] if r[0]=='medical_action')[2]=''
    with pytest.raises(StateError,match='medical_manual_input_required'):
        process_page_request(kinds,sheet,str(UUID(int=1)),medical_factory=factory)
    assert db.writes==0 and not store.value['confirmation_items']


def test_complete_confirm_uses_existing_writer_readback_and_original_page_link(local_ocr):
    kinds,live,t,sheet,store,db,factory=medical_context()
    # Capture's status/result can change to "受付中" without changing owner input.
    next(r for r in sheet.live['rows'] if r[0]=='result')[2]='受付中・保存待ち'
    assert process_page_request(kinds,sheet,str(UUID(int=1)),medical_factory=factory)=='医療確定済み'
    assert db.writes==3 and len(db.rows['支出明細'])==1
    assert db.rows['レシート'][0][5]=='https://drive.google.com/file/d/drive-source-id/view#page=1'
    item=next(iter(store.value['confirmation_items'].values()))
    assert item['decision_origin']=='human' and item['status']=='applied' and item['confirmation_hash']
    assert medical_results(kinds,store,db,'synthetic-folder')=={review_id('medical',item['source']):'医療確定済み'}


def test_confirmation_replay_and_projection_failure_never_double_write(local_ocr):
    kinds,live,t,sheet,store,db,factory=medical_context();sheet.fail=True
    id=str(UUID(int=2))
    with pytest.raises(OSError):process_page_request(kinds,sheet,id,medical_factory=factory)
    writes=db.writes;item=deepcopy(next(iter(store.value['confirmation_items'].values())))
    sheet.fail=False
    assert process_page_request(kinds,sheet,id,medical_factory=factory)=='医療確定済み'
    assert db.writes==writes and next(iter(store.value['confirmation_items'].values()))==item
    assert process_page_request(kinds,sheet,id,medical_factory=factory)=='医療確定済み' and db.writes==writes


def test_duplicate_is_held_by_existing_payment_writer(local_ocr):
    kinds,live,t,sheet,store,db,factory=medical_context()
    db.rows['支出明細']=[['existing','2026-09-01','Synthetic other','医療費',100,'医療・保険','病院','','manual','','','','active']]
    result=process_page_request(kinds,sheet,str(UUID(int=1)),medical_factory=factory)
    assert '重複' in result and db.writes==0 and len(db.rows['支出明細'])==1


def test_changed_owner_snapshot_or_original_link_is_rejected(local_ocr):
    kinds,live,t,sheet,store,db,factory=medical_context()
    next(r for r in sheet.live['rows'] if r[0]=='amount')[2]='101'
    with pytest.raises(StateError,match='owner_inputs_changed'):
        process_page_request(kinds,sheet,str(UUID(int=1)),medical_factory=factory)
    assert db.writes==0
    sheet.live=deepcopy(sheet.capture);sheet.live['original_link']='https://example.com/spoof'
    with pytest.raises(StateError,match='original_link_changed'):
        process_page_request(kinds,sheet,str(UUID(int=2)),medical_factory=factory)
    assert db.writes==0


def test_no_saved_candidate_can_fill_manual_input(local_ocr):
    kinds,live,t,sheet,store,db,factory=medical_context()
    _,p,a=kinds.current('drive-source-id');source=manual_source(p['pages'][0],a[1])
    review=factory(source,lambda:manual_values({},CATEGORIES));review.prepare()
    item=store.value['confirmation_items'][review.key]
    item['medical_candidates']={'date':'2026-09-01','issuer':'Synthetic automatic','amount_yen':100,'category':'医療・保険｜病院'}
    assert review.confirm()=='医療入力待ち' and db.writes==0


def test_pdf_page_items_are_invisible_to_whole_file_backend_and_archiver(local_ocr):
    kinds,live,t,sheet,store,db,factory=medical_context()
    process_page_request(kinds,sheet,str(UUID(int=1)),medical_factory=factory)
    legacy=ReceiptConfirmation(store,db,Mock())
    assert legacy.items=={} and legacy.apply_confirmations()==0
    source=next(iter(store.value['confirmation_items'].values()))['source']
    review=factory(source,lambda:['']*8);drive=Mock(side_effect=AssertionError('no source move'))
    assert archive_confirmations(review,'synthetic-folder','processed',drive,Mock())==0
    assert not drive.mock_calls


def test_normal_page_request_cannot_reach_medical_backend(local_ocr):
    g,live,t,kinds,view=setup();kinds.confirm(view['proposal'],{1:'一般'})
    _,p,a=kinds.current('drive-source-id')
    card=cards(view,a,CATEGORIES)[0];sheet=Sheet(snapshot(card,{'group_action':'確定'}))
    factory=Mock(side_effect=AssertionError('no medical'))
    assert process_page_request(kinds,sheet,str(UUID(int=1)),medical_factory=factory)=='confirmed'
    factory.assert_not_called()
    c=g.store.load()['records'][_digest('drive-source-id')]['confirmation']
    assert c['medical_handoff_allowed'] is False and c['accounting_allowed'] is False
    assert c['unit_statuses'][0]!='normal_ready'


def test_group_edit_cannot_include_manual_medical_page(local_ocr):
    kinds,live,t,sheet,store,db,factory=medical_context()
    value,p,a=kinds.current('drive-source-id');g=kinds.grouping
    card=next(c for c in cards(g.view(value['records'][_digest('drive-source-id')]),a,CATEGORIES) if c['identity']['kind']=='grouping')
    request=Sheet(snapshot(card,{'group_action':'結合','group_target':'1+2'}))
    with pytest.raises(StateError,match='medical_target_forbidden'):process_page_request(kinds,request,str(UUID(int=1)))
    assert db.writes==0


def test_kind_store_save_failure_never_displays_medical_confirmed(local_ocr):
    g,live,t,kinds,view=setup();t.fail_write=True
    with pytest.raises(StateError):kinds.confirm(view['proposal'],{1:'医療'})
    assert not g.store.load().get('page_kinds')


@pytest.mark.parametrize('known',['medical','payroll'])
def test_known_sensitive_cannot_be_human_downgraded(local_ocr,known):
    g,live,t=context((known,'normal'));view=g.display('drive-source-id')
    with pytest.raises(StateError,match='restriction_conflict'):PageKindConfirmation(g).confirm(view['proposal'],{1:'一般'})


def test_manual_write_intent_save_failure_makes_zero_accounting_calls(local_ocr):
    kinds,live,t,sheet,store,db,factory=medical_context();save=store.save
    def fail_pending(value):
        if any(i['status']=='pending' for i in value['confirmation_items'].values()):raise StateError('synthetic_state_failure')
        save(value)
    store.save=fail_pending
    with pytest.raises(StateError):process_page_request(kinds,sheet,str(UUID(int=1)),medical_factory=factory)
    assert db.writes==0


def test_mobile_projection_uses_two_columns_and_same_tabs_only(local_ocr):
    from app.pdf_page_review import PageReviewSheet,SCHEMA
    from app.pdf_grouping_ui import TITLE,QUEUE,SHEET_ID,QUEUE_ID,HEADERS
    kinds,live,t,owner,store,db,factory=medical_context()
    native=FakeSheets()
    native.metadata += [{'properties':{'sheetId':SHEET_ID,'title':TITLE}},
                        {'properties':{'sheetId':QUEUE_ID,'title':QUEUE,'hidden':True}}]
    native.value_data[f"'{TITLE}'!A1:P1"]=[HEADERS]
    sheet=PageReviewSheet(native,'management-sheet',CATEGORIES)
    sheet.install()
    sheet._rows=lambda:[]
    publish_current(kinds,sheet)
    assert not any('addSheet' in r for r in native.formats)
    columns=[r['updateDimensionProperties'] for r in native.formats if 'updateDimensionProperties' in r and r['updateDimensionProperties']['range']['dimension']=='COLUMNS']
    assert any(c['range']['startIndex']==2 and c['properties'].get('hiddenByUser') is True for c in columns)
    assert all(entry['range'].startswith("'PDFページ確認'!") for batch in native.writes for entry in batch)
    rows=native.writes[-1][0]['values']
    assert next(r for r in rows if len(r)>5 and r[5]=='date')[1]==''
    assert next(r for r in rows if len(r)>5 and r[5]=='facility')[1]==''
    operation=[r['setDataValidation'] for r in native.formats if 'setDataValidation' in r and r['setDataValidation'].get('rule')]
    medical_row=next(i+1 for i,r in enumerate(rows) if len(r)>5 and r[5]=='medical_action')
    rule=next(r['rule'] for r in operation if r['range']['startRowIndex']==medical_row)
    assert rule['condition']['values']==[{'userEnteredValue':'保留'}]
    before=len(native.writes);native.value_data[f"'{QUEUE}'!A2:F1001"]=[['id','accepted']]
    with pytest.raises(StateError,match='pending_legacy_request'):sheet.install()
    assert len(native.writes)==before


@pytest.mark.parametrize('change',['none','source','page','count'])
def test_fresh_page_verification_uses_only_local_png_and_read_only_drive(local_ocr,change):
    from app.pdf_grouping_review import DrivePdfReader
    original=synthetic_pdf(('unknown','normal'))
    g,live,t,kinds,view=setup();p=deepcopy(view['proposal'])
    service=Mock()
    service.files.return_value.get.return_value.execute.return_value={'id':'drive-source-id','parents':['inbox'],
        'mimeType':'application/pdf','version':'1','trashed':False}
    service.files.return_value.get_media.return_value.execute.return_value=original
    if change=='source':service.files.return_value.get_media.return_value.execute.return_value=original+b'changed'
    if change=='page':p['pages'][0]['page_hash']='0'*64
    if change=='count':p['page_count']=3
    reader=DrivePdfReader(service,'inbox')
    if change=='none':
        current=reader.verify_pages('drive-source-id',p,numbers=[1])
        assert [page.metadata() for page in current.pages]==p['pages']
    else:
        with pytest.raises(StateError):reader.verify_pages('drive-source-id',p,numbers=[1])
    service.files.return_value.update.assert_not_called()
    service.files.return_value.create.assert_not_called()


def test_backend_partial_unknown_write_is_reconciled_without_retry(local_ocr):
    kinds,live,t,sheet,store,db,factory=medical_context()
    original=db.append_raw
    def first_only(title,rows):
        if title=='支出明細':raise OSError('unknown before accounting commit')
        original(title,rows)
    db.append_raw=first_only
    id=str(UUID(int=10))
    with pytest.raises(StateError,match='write_unknown'):process_page_request(kinds,sheet,id,medical_factory=factory)
    writes=db.writes
    with pytest.raises(StateError,match='reconciliation_required'):process_page_request(kinds,sheet,id,medical_factory=factory)
    assert db.writes==writes and not db.rows['支出明細']


def test_existing_whole_pdf_accounting_prevents_new_page_double_post(local_ocr):
    kinds,live,t,sheet,store,db,factory=medical_context()
    db.rows['レシート']=[['R-drive-source-id','2026-09-01','Synthetic previously posted',9999,'','','解析済','','']]
    with pytest.raises(StateError,match='parent_accounting_requires_review'):
        process_page_request(kinds,sheet,str(UUID(int=12)),medical_factory=factory)
    assert db.writes==0 and not db.rows['支出明細']


def test_explicit_manual_adapter_never_reaches_medical_auto_or_gemini(local_ocr,monkeypatch):
    spies=[]
    for path in ('app.gemini_ai.GeminiAI.analyze_receipt','app.medical_auto_posting.apply_automatic',
                 'app.medical_local_reading.apply_local','app.medical_candidate_runtime.run_prepared'):
        spy=Mock(side_effect=AssertionError('No Medical AUTO or external AI'))
        monkeypatch.setattr(path,spy);spies.append(spy)
    kinds,live,t,sheet,store,db,factory=medical_context()
    assert process_page_request(kinds,sheet,str(UUID(int=13)),medical_factory=factory)=='医療確定済み'
    assert len(db.rows['支出明細'])==1
    for spy in spies:spy.assert_not_called()


def test_page_change_between_durable_intent_and_accounting_aborts_before_writer(local_ocr):
    kinds,live,t,sheet,store,db,factory=medical_context();save=store.save
    def change_after_intent(value):
        save(value)
        if any(i['status']=='pending' for i in value['confirmation_items'].values()):
            pages=list(live.observations.pages);pages[0]=replace(pages[0],page_hash='c'*64)
            live.observations=replace(live.observations,pages=tuple(pages))
    store.save=change_after_intent
    with pytest.raises(StateError):process_page_request(kinds,sheet,str(UUID(int=14)),medical_factory=factory)
    item=next(iter(store.value['confirmation_items'].values()))
    assert item['status']=='waiting' and item['aborted_before_accounting'] is True
    assert db.writes==0


@pytest.mark.parametrize('answer,restricted',[('医療','medical'),('給与','payroll'),('判定不能','sensitive_unknown')])
def test_human_restriction_tightens_future_units_without_rewriting_automatic_pages(local_ocr,answer,restricted):
    from test_pdf_grouping_authority import request
    g,live,t=context(('normal','normal'));view=g.display('drive-source-id')
    g.review(request(view))
    before=g.confirmed_units('drive-source-id')[0]
    PageKindConfirmation(g).confirm(view['proposal'],{1:answer})
    after=g.confirmed_units('drive-source-id')[0]
    assert after.unit_id==before.unit_id and after.classification==restricted
    assert live.observations.pages[0].classification=='normal'
    assert view['proposal']['pages'][0]['classification']=='normal'
