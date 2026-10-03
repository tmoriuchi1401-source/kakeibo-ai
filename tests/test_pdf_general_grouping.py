"""Scoped human transaction partition, independent of privacy and Medical."""
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

import pytest

from app.drive_run_state import StateError
from app.pdf_page_kind import PageKindConfirmation, kind_key
from app.pdf_page_review import cards, process_page_request
from app.receipt_pdf_grouping import AdjacentPageGrouping, PageEvidence, _valid_proposal, _snapshot
from app.receipt_pdf_units import _digest
from app import pdf_grouping_review as reader_module
from test_pdf_grouping_authority import context, request
from test_pdf_page_review import CATEGORIES, Sheet, snapshot
from test_receipt_pdf_units import local_ocr, synthetic_pdf


def setup():
    g, live, transport = context(('unknown', 'normal'))
    base = live.observations.pages[1]
    live.observations = replace(live.observations, pages=tuple(replace(base,
        page_number=n, page_hash=_digest(['synthetic-page',n]), grouping_hints=None,
        classification='sensitive_unknown' if n in {1,4,10,14} else 'normal') for n in range(1,15)))
    g.proposer=AdjacentPageGrouping(lambda obs: tuple(PageEvidence() for _ in obs.pages))
    view = g.display('drive-source-id')
    kinds = PageKindConfirmation(g)
    kinds.confirm(view['proposal'], {n:'医療' if n==1 else '一般' for n in range(1,15)})
    evidence = {n:PageEvidence() for n in range(2,15)}
    evidence[2] = PageEvidence('same-store','same-date','same-receipt',1,2,False,True)
    evidence[3] = PageEvidence('same-store','same-date','same-receipt',2,2,True,False)
    provider = Mock(return_value=evidence)
    return g,live,transport,kinds,provider


def test_scoped_proposal_excludes_medical_and_preserves_all_privacy(local_ocr):
    g,live,t,kinds,evidence=setup()
    original=deepcopy(g.store.load());old=original['records'][_digest('drive-source-id')]['proposal']
    view=g.regenerate_general('drive-source-id',evidence);p=view['proposal']
    assert p['grouping_page_numbers']==list(range(2,15)) and p['page_count']==14
    assert p['pages']==old['pages'] and p['grouping_version']==2
    assert p['groups'][0]['page_numbers']==[2,3]
    assert [n for group in p['groups'] for n in group['page_numbers']]==list(range(2,15))
    assert p['pages'][3]['classification']=='sensitive_unknown'
    assert view['confirmation'] is None and not g.confirmed_units('drive-source-id')
    assert g.store.load()['page_kinds']==original['page_kinds']
    assert _valid_proposal(p,_snapshot(live.observations))
    evidence.assert_called_once_with(old,list(range(2,15)))
    assert all(x not in str(g.store.load()) for x in ('same-store','same-date','same-receipt'))


def test_candidate_regeneration_replay_has_no_ocr_or_write(local_ocr):
    g,live,t,kinds,evidence=setup();view=g.regenerate_general('drive-source-id',evidence)
    before=t.payload;writes=t.writes
    assert g.regenerate_general('drive-source-id',Mock(side_effect=AssertionError))==view
    assert t.payload==before and t.writes==writes


def test_confirm_readback_and_replay_preserve_scope_units_digest_time(local_ocr):
    g,live,t,kinds,evidence=setup();view=g.regenerate_general('drive-source-id',evidence)
    r=request(view);confirmed=g.review(r);proof=confirmed['confirmation']
    assert proof['confirmed_partition']==[[2,3]]+[[n] for n in range(4,15)]
    assert all(proof[k] is False for k in ('gemini_allowed','accounting_allowed','medical_handoff_allowed','archive_allowed'))
    assert proof['page_kind_digests']==view['proposal']['page_kind_digests']
    units=g.confirmed_units('drive-source-id')
    assert all(1 not in u.page_numbers for u in units)
    assert next(u for u in units if 4 in u.page_numbers).classification=='sensitive_unknown'
    before=t.payload;writes=t.writes
    assert g.review(r)['confirmation']==proof
    assert g.display('drive-source-id')['confirmation']==proof
    assert g.confirmed_units('drive-source-id')==units
    assert t.payload==before and t.writes==writes


def test_shared_one_action_confirm_cannot_invoke_medical(local_ocr):
    g,live,t,kinds,evidence=setup();view=g.regenerate_general('drive-source-id',evidence)
    _,p,answers=kinds.current('drive-source-id')
    card=next(c for c in cards(view,answers,CATEGORIES) if c['identity']['kind']=='grouping')
    factory=Mock(side_effect=AssertionError('Medical forbidden'))
    sheet=Sheet(snapshot(card,{'group_action':'確定'}))
    result=process_page_request(kinds,sheet,request(view)['request_id'],medical_factory=factory)
    assert result=='confirmed';factory.assert_not_called()
    medical=next(c for c in sheet.published if c['identity']['kind']=='medical')
    assert dict((f,v) for f,_,v in medical['rows'])['state']=='医療入力待ち'


@pytest.mark.parametrize('partition',[[[1,2,3]]+[[n] for n in range(4,15)],[[n] for n in range(1,15)],[[2,4]]+[[n] for n in range(5,15)]])
def test_excluded_medical_or_invalid_coverage_cannot_be_merged(local_ocr,partition):
    g,live,t,kinds,evidence=setup();view=g.regenerate_general('drive-source-id',evidence);before=t.payload
    with pytest.raises(ValueError):g.review(request(view,operation='edit',partition=partition))
    assert t.payload==before and not g.confirmed_units('drive-source-id')


def test_edit_redisplays_unconfirmed_then_confirm_new_revision(local_ocr):
    g,live,t,kinds,evidence=setup();view=g.regenerate_general('drive-source-id',evidence)
    confirmed=g.review(request(view));oldids=[u.unit_id for u in g.confirmed_units('drive-source-id')]
    edited=g.review(request(confirmed,operation='edit',partition=[[n] for n in range(2,15)],number=2))
    assert edited['confirmation'] is None and edited['proposal']['grouping_version']==3
    assert not g.confirmed_units('drive-source-id')
    assert g.review(request(view,number=3))['result']=='stale_proposal'
    after=g.review(request(edited,number=4))
    assert after['confirmation']['confirmed_partition']==[[n] for n in range(2,15)]
    assert set(oldids).isdisjoint(u.unit_id for u in g.confirmed_units('drive-source-id'))


@pytest.mark.parametrize('changed',['source','page','count'])
def test_source_page_or_count_change_revokes_scoped_authority(local_ocr,changed):
    g,live,t,kinds,evidence=setup();view=g.regenerate_general('drive-source-id',evidence);g.review(request(view))
    if changed=='source':
        h=_digest('changed-source');live.observations=replace(live.observations,source_content_hash=h,
            pages=tuple(replace(p,source_content_hash=h) for p in live.observations.pages))
    elif changed=='page':live.observations=replace(live.observations,pages=tuple(replace(p,page_hash=_digest('changed-page')) if p.page_number==2 else p for p in live.observations.pages))
    else:live.observations=replace(live.observations,pages=live.observations.pages[:-1])
    assert not g.confirmed_units('drive-source-id')
    assert g.review(request(view,number=2))['result']=='stale_proposal'


def test_kind_change_revokes_authority_and_stales_old_request(local_ocr):
    g,live,t,kinds,evidence=setup();view=g.regenerate_general('drive-source-id',evidence);g.review(request(view))
    kinds.confirm(view['proposal'],{4:'医療'})
    assert not g.confirmed_units('drive-source-id')
    assert g.review(request(view,number=2))['result']=='stale_proposal'
    assert g.store.load()['page_kinds'][kind_key('drive-source-id',1)]['human_classification']=='medical'


def test_proposal_regeneration_keeps_medical_card_identity_and_blank_inputs(local_ocr):
    g,live,t,kinds,evidence=setup();_,old,answers=kinds.current('drive-source-id')
    medical=lambda view:next(c for c in cards(view,answers,CATEGORIES) if c['identity']['kind']=='medical')
    before=medical(g.view(g.store.load()['records'][_digest('drive-source-id')]))
    after=medical(g.regenerate_general('drive-source-id',evidence))
    assert before==after


def test_concurrent_etag_change_rejects_proposal_write_without_retry(local_ocr):
    g,live,t,kinds,evidence=setup();before=t.writes
    def concurrent(p,n):
        value=g.store.load();value['generation']+=1
        # Another worker changes the same object, not this store's cached tag.
        payload=t.payload;tag=str(t.tag)
        from app.pdf_grouping_authority import encoded
        t.replace_versioned(payload,tag,encoded(value))
        return evidence.return_value
    with pytest.raises(StateError,match='stale_proposal'):g.regenerate_general('drive-source-id',concurrent)
    assert t.writes==before+1


def test_nonadjacent_normal_pages_never_automatically_join(local_ocr):
    g,live,t,kinds,evidence=setup()
    kinds.confirm(kinds.current('drive-source-id')[1],{3:'医療'})
    hints=evidence.return_value
    hints[2]=PageEvidence('a','b','c',1,2,False,True)
    hints[4]=PageEvidence('a','b','c',2,2,False,True)
    view=g.regenerate_general('drive-source-id',lambda p,n:{i:hints[i] for i in n})
    assert all(not {2,4}<=set(group['page_numbers']) for group in view['proposal']['groups'])


def test_scoped_live_evidence_adapter_reads_only_general_pages_and_retains_no_pixels(local_ocr,monkeypatch):
    content=synthetic_pdf(('unknown','normal','normal'))
    from app.receipt_pdf_units import observe_pdf
    from app.receipt_pdf_grouping import _proposal
    observed=observe_pdf(content,'drive-source-id')
    p=_proposal(_snapshot(observed),[{'page_numbers':[n],'confidence':0.,'reason':'insufficient_continuation_evidence'} for n in range(1,4)],1)
    files=Mock();files.get.return_value.execute.return_value={'id':'drive-source-id','mimeType':'application/pdf','parents':['inbox'],'version':'1'}
    files.get_media.return_value.execute.return_value=content
    service=Mock();service.files.return_value=files
    reader=reader_module.DrivePdfReader(service,'inbox')
    extracted=Mock(return_value=type('Extraction',(),{'status':'extracted','observation_complete':True,'text':'店舗名: synthetic\n2026/10/01\nレシート番号: 123'})())
    monkeypatch.setattr('app.receipt_text_extraction._extract_receipt_text',extracted)
    evidence=reader.grouping_evidence('drive-source-id',p,[2,3])
    assert set(evidence)=={2,3} and extracted.call_count==2
    assert all(isinstance(e,PageEvidence) for e in evidence.values())
    assert all(len(e.issuer)==64 for e in evidence.values())
    assert all(not hasattr(e,'_payload') and not hasattr(e,'text') for e in evidence.values())


def test_old_confirm_request_replay_cannot_report_confirmed_after_hold(local_ocr):
    g,live,t,kinds,evidence=setup();view=g.regenerate_general('drive-source-id',evidence)
    _,p,answers=kinds.current('drive-source-id')
    card=next(c for c in cards(view,answers,CATEGORIES) if c['identity']['kind']=='grouping')
    capture=snapshot(card,{'group_action':'確定'});sheet=Sheet(capture);r=request(view)
    assert process_page_request(kinds,sheet,r['request_id'])=='confirmed'
    g.review(request(view,operation='hold',number=2))
    with pytest.raises(StateError,match='stale_proposal'):process_page_request(kinds,sheet,r['request_id'])
    assert not g.confirmed_units('drive-source-id')


@pytest.mark.parametrize('flag',[True,0,None])
def test_scoped_gemini_flag_requires_boolean_false(local_ocr,flag):
    g,live,t,kinds,evidence=setup();view=g.regenerate_general('drive-source-id',evidence);g.review(request(view))
    value=g.store.load();value['records'][_digest('drive-source-id')]['confirmation']['gemini_allowed']=flag
    with pytest.raises(StateError,match='grouping_state_invalid'):g.store.save(value)
