from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
from hashlib import sha256
from io import BytesIO
import base64

from PIL import Image
import pytest
from app import pdf_unit_intake as intake
from app.drive_run_state import StateError
from app.gemini_ai import GeminiAI
from app.pdf_unit_analysis import PdfUnitAnalyzer
from app.pdf_page_kind import PageKindConfirmation
from app.pdf_production_authority import DrivePdfAuthority
from app.receipt_pipeline import ReceiptPipeline
from app.drive_receipts import should_archive_result
from app import receipt_pdf_units as pdf
from pdf_production_test_support import context,request
from test_pdf_unit_processing import context as completion_context
from test_pdf_receipt_materialization import DB,result
from test_receipt_pdf_units import local_ocr


def analyzer(response=None):
    ai=object.__new__(GeminiAI);ai.model='synthetic-model'
    create=Mock(return_value=SimpleNamespace(output_text=(response or result()).model_dump_json()))
    ai.client=SimpleNamespace(interactions=SimpleNamespace(create=create),
        _api_client=SimpleNamespace(_http_options=SimpleNamespace(base_url='https://generativelanguage.googleapis.com')))
    return PdfUnitAnalyzer(ai),create


def setup(kinds=('normal','normal'),human=None,confirmed=True,group=False):
    g,live,drive=context(kinds);view=g.display('drive-source-id')
    if human:PageKindConfirmation(g).confirm(view['proposal'],human)
    if group:
        g.review(request(view,operation='edit',partition=[list(range(1,len(kinds)+1))]))
        view=g.view(next(iter(g.store.load()['records'].values())))
    if confirmed:g.review(request(view,number=2))
    provider=DrivePdfAuthority(g.store,lambda _:live.content)
    state,completion,_=completion_context();db=DB();ai,sdk=analyzer()
    service=intake.PdfUnitIntake(provider,completion,db,ai)
    return service,live,g,db,sdk,state


def test_every_page_observed_before_sdk_then_independent_duplicate_candidate_hold(local_ocr,monkeypatch):
    service,live,g,db,sdk,state=setup();observed=[];original=intake.observe_pdf
    def observe(*a,**kw):
        result=original(*a,**kw);observed.extend(result.pages);return result
    monkeypatch.setattr(intake,'observe_pdf',observe)
    def answer(**kw):
        assert len(observed)==2
        if sdk.call_count==1:assert not db.appends
        return SimpleNamespace(output_text=result().model_dump_json())
    sdk.side_effect=answer
    out=service.process(live.content,'drive-source-id')
    assert [r['status'] for r in out['units']]==['imported','needs_review']
    assert out['units'][1]['reason_code']=='pdf_receipt_duplicate_candidate'
    assert sdk.call_count==2 and len(db.appends)==3 and len(observed)==2
    for call in sdk.call_args_list:
        media=call.kwargs['input'][1];png=base64.b64decode(media['data'])
        assert media['mime_type']=='image/png' and b'PRIVATE_ATTACHMENT' not in png
        with Image.open(BytesIO(png)) as image:assert image.size==(216,216) and image.info=={}
    assert not out['all_units_terminal'] and not should_archive_result(out)
    assert out['work']['peak_live_pages']==1


def test_applied_replay_reads_intent_rows_without_new_sdk_append_or_state_write(local_ocr):
    service,live,g,db,sdk,state=setup(group=True)
    out=service.process(live.content,'drive-source-id');before=(sdk.call_count,len(db.appends),state.payload)
    replay=service.process(live.content,'drive-source-id')
    assert replay['all_units_terminal'] and replay['units'][0]['replayed']
    assert before==(sdk.call_count,len(db.appends),state.payload)
    assert out['archive_allowed'] is False and not should_archive_result(out)


def test_mixed_pdf_medical_and_human_normal_unknown_never_reach_sdk(local_ocr):
    service,live,g,db,sdk,state=setup(('medical','normal','unknown'),
        {1:'医療',2:'一般',3:'一般'})
    out=service.process(live.content,'drive-source-id')
    assert [r['status'] for r in out['units']]==['medical_pending','imported','privacy_pending']
    assert sdk.call_count==1 and len(db.appends)==3 and not out['all_units_terminal']


def test_unconfirmed_pdf_is_only_local_observation_no_accounting_or_sdk(local_ocr):
    service,live,g,db,sdk,state=setup(confirmed=False);before=state.payload
    out=service.process(live.content,'drive-source-id')
    assert [r['status'] for r in out['units']]==['grouping_required','grouping_required']
    assert sdk.call_count==0 and not db.appends and state.payload==before


def test_sticky_failure_persists_across_service_instances_and_grouping_edit(local_ocr,monkeypatch):
    service,live,g,db,sdk,state=setup();original=intake.observe_pdf
    def incomplete(*a,**kw):
        from dataclasses import replace
        observed=original(*a,**kw)
        return replace(observed,pages=tuple(replace(p,classification='sensitive_unknown',
            observation_complete=False) for p in observed.pages))
    monkeypatch.setattr(intake,'observe_pdf',incomplete)
    assert all(r['status']=='privacy_pending' for r in service.process(live.content,'drive-source-id')['units'])
    before=len(state.updates);monkeypatch.setattr(intake,'observe_pdf',original)
    view=g.view(next(iter(g.store.load()['records'].values())))
    g.review(request(view,operation='edit',partition=[[1,2]],number=3))
    g.review(request(g.view(next(iter(g.store.load()['records'].values()))),number=4))
    second=intake.PdfUnitIntake(service.authority,service.completion,db,service.analyzer)
    assert second.process(live.content,'drive-source-id')['units'][0]['status']=='privacy_pending'
    assert sdk.call_count==0 and not db.appends and len(state.updates)==before


def test_source_mismatch_before_observation_never_renders_or_sends(local_ocr,monkeypatch):
    service,live,g,db,sdk,state=setup();observe=Mock();monkeypatch.setattr(intake,'observe_pdf',observe)
    with pytest.raises(StateError,match='source_changed'):service.process(live.content+b'changed','drive-source-id')
    observe.assert_not_called();assert sdk.call_count==0 and not db.appends


def test_sdk_authority_race_stops_remaining_units_without_ai(local_ocr,monkeypatch):
    service,live,g,db,sdk,state=setup()
    monkeypatch.setattr(service.authority,'verify',Mock(return_value=False))
    out=service.process(live.content,'drive-source-id')
    assert all(r['status']=='authority_held' for r in out['units'])
    assert sdk.call_count==0 and not db.appends


def test_partial_accounting_failure_stops_next_unit_and_replay_never_resends(local_ocr):
    service,live,g,db,sdk,state=setup();db.fail=('支出明細','before')
    out=service.process(live.content,'drive-source-id')
    assert all(r['status']=='authority_held' for r in out['units'])
    assert sdk.call_count==1 and len(db.appends)==2
    before=(sdk.call_count,len(db.appends));db.fail=None
    service.process(live.content,'drive-source-id')
    assert before==(sdk.call_count,len(db.appends))


def test_sdk_payload_and_reread_stability_guards(local_ocr,monkeypatch):
    service,live,g,db,sdk,state=setup(group=True);spec=service.authority.current('drive-source-id').units[0]
    png=live.observations.page_payload(1)
    with pytest.raises(StateError,match='payload_changed'):
        service.analyzer.analyze(png,'0'*64,db.categories(),spec,verify_current=lambda _:True)
    assert sdk.call_count==0
    one=result();one.items[0].amount=90;two=result();two.date='2026-09-25'
    sdk.side_effect=[SimpleNamespace(output_text=x.model_dump_json()) for x in (one,two,two)]
    with pytest.raises(StateError,match='unstable_reread'):
        service.analyzer.analyze(png,sha256(png).hexdigest(),db.categories(),spec,verify_current=lambda _:True)
    assert sdk.call_count==3 and not db.appends


def test_existing_pipeline_pdf_dispatch_and_original_pdf_ai_block(local_ocr):
    service,live,g,db,sdk,state=setup(group=True)
    assert ReceiptPipeline(db,None,pdf_unit_intake=service).process_bytes(live.content,
        'application/pdf','drive-source-id')['units'][0]['status']=='imported'
    with pytest.raises(Exception):
        service.analyzer.ai.analyze_receipt(live.content,'application/pdf',db.categories())
    assert sdk.call_count==1


def test_sdk_wrapper_rejects_changed_bytes_even_after_the_initial_gate(local_ocr,monkeypatch):
    service,live,g,db,sdk,state=setup()
    spec=service.authority.current('drive-source-id').units[0];png=live.observations.page_payload(1)
    def malicious_parser(self,image_bytes,mime_type,categories,**kw):
        return self.client.interactions.create(model=self.model,input=[{'type':'text','text':'safe'},
            {'type':'image','mime_type':'image/png','data':base64.b64encode(image_bytes+b'x').decode()}],response_format={})
    monkeypatch.setattr(GeminiAI,'analyze_receipt',malicious_parser)
    with pytest.raises(StateError,match='payload_changed'):
        service.analyzer.analyze(png,sha256(png).hexdigest(),db.categories(),spec,verify_current=lambda _:True)
    assert sdk.call_count==0 and not db.appends


def test_unconfirmed_or_medical_only_pdf_does_not_resolve_gemini_factory(local_ocr):
    for kinds,confirmed,human in [(('normal','normal'),False,None),
            (('medical','medical'),True,{1:'医療',2:'医療'})]:
        service,live,g,db,sdk,state=setup(kinds,human,confirmed)
        factory=Mock(side_effect=AssertionError('must not resolve Gemini'))
        service.analyzer=PdfUnitAnalyzer(gemini_factory=factory)
        service.process(live.content,'drive-source-id')
        factory.assert_not_called();assert sdk.call_count==0 and not db.appends


def test_legacy_unit_marker_without_migrated_intent_holds_before_sdk(local_ocr):
    service,live,g,db,sdk,state=setup()
    s=service.authority.current('drive-source-id').units[0]
    db.rows['取込データ']=[['receipt:'+s['unit_id']]]
    # Remaining pages still have their separate, verified authority.
    out=service.process(live.content,'drive-source-id')
    assert out['units'][0]['status']=='needs_review'
    assert out['units'][0]['reason_code']=='pdf_receipt_existing_identity_without_intent'
    assert sdk.call_count==1  # second page only, never the already posted Unit
    assert len(db.appends)==3
