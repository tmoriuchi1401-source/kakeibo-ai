from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import pdf_confirmed_source_reader as reader
from app import pdf_grouping_authority_v2 as v2
from app.pdf_grouping_authority import DurablePdfGrouping
from app.pdf_page_kind import PageKindConfirmation
from app.pdf_page_medical import manual_source
from app.drive_run_state import StateError
from test_pdf_grouping_authority_v2 import setup_v2, BINDING
from test_receipt_pdf_units import local_ocr, synthetic_pdf
from test_private_state_bindings import key


def context(key,monkeypatch):
    value,expected,g,t,source,legacy,payload,old=setup_v2(key,monkeypatch)
    transport=SimpleNamespace(read_versioned=Mock(return_value=(v2.encoded(value),'"synthetic"')))
    store=v2.DriveGroupingV2Store(transport,BINDING,g.store,'old-v1-file',preflight=Mock())
    p=legacy['records'][v2._digest(old['source_file_id'])]['proposal']
    return store,source,p,g,t,value,transport


def test_manual_freshness_preserves_review_and_sticky_privacy_without_render_or_ocr(local_ocr,key,monkeypatch):
    store,source,p,g,t,value,transport=context(key,monkeypatch)
    from app import receipt_pdf_units, receipt_text_extraction
    monkeypatch.setattr(receipt_pdf_units,'_render_png',Mock(side_effect=AssertionError('Never render')))
    monkeypatch.setattr(receipt_text_extraction,'_extract_receipt_text',Mock(side_effect=AssertionError('Never OCR')))
    sid=p['source_file_id'];before=t.payload;writes=t.writes
    old_kinds=PageKindConfirmation(g);_,_,old_answers=old_kinds.current(sid)
    old_source=manual_source(p['pages'][0],old_answers[1])
    observations=reader.confirmed_legacy_observations(store,source,sid,p,numbers=[1])
    assert len(observations.pages)==14 and all(page._payload is None for page in observations.pages)
    assert observations.pages[0].classification==p['pages'][0]['classification']
    adapted=DurablePdfGrouping(g.store,lambda sid,previous:reader.confirmed_legacy_observations(store,source,sid,previous,numbers=[1]))
    _,proposal,answers=PageKindConfirmation(adapted).current(sid)
    assert manual_source(proposal['pages'][0],answers[1])==old_source
    assert answers[1]['human_classification']=='medical' and not answers[1]['gemini_allowed']
    assert t.payload==before and t.writes==writes


@pytest.mark.parametrize('change',['source','count','previous_page_hash','previous_kind','page_selection'])
def test_manual_source_or_intent_changes_never_become_confirmation(local_ocr,key,monkeypatch,change):
    store,source,p,g,t,value,transport=context(key,monkeypatch)
    p=deepcopy(p);numbers=[1];sid=p['source_file_id']
    if change=='source':source=lambda _:b'changed-source'
    if change=='count':source=lambda _:synthetic_pdf(('normal',)*15)
    if change=='previous_page_hash':p['pages'][0]['page_hash']='0'*64
    if change=='previous_kind':p['pages'][0]['classification']='normal'
    if change=='page_selection':numbers=[15]
    writes=t.writes
    with pytest.raises(StateError):reader.confirmed_legacy_observations(store,source,sid,p,numbers=numbers)
    assert t.writes==writes


def test_projection_or_local_cache_cannot_be_source_authority():
    with pytest.raises(StateError,match='durable_identity_required'):
        reader.confirmed_legacy_observations(SimpleNamespace(load=lambda:{}),lambda _:b'', 'synthetic',{})


def test_mid_confirmation_authority_change_is_held(local_ocr,key,monkeypatch):
    store,source,p,g,t,value,transport=context(key,monkeypatch)
    transport.read_versioned.side_effect=[(v2.encoded(value),'"first"'),(v2.encoded(value),'"changed"')]
    with pytest.raises(StateError,match='source_changed'):
        reader.confirmed_legacy_observations(store,source,p['source_file_id'],p,numbers=[1])
