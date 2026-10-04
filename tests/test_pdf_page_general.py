from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock
import pytest
from app.drive_run_state import StateError
from app.pdf_page_general import manual_values
from app.pdf_page_review import cards,process_page_request
from test_pdf_general_grouping import setup
from test_pdf_grouping_authority import request
from test_pdf_page_review import Sheet,snapshot,CATEGORIES
from test_receipt_pdf_units import local_ocr

MASTER=CATEGORIES+[('食費','外食')]
FIELDS={'date':'2026/09/24','amount':'500','category':'食費｜外食',
        'merchant':'','payment':'','memo':'','manual_action':'一般手入力を確定'}

def context():
    g,live,t,kinds,evidence=setup();view=g.regenerate_general('drive-source-id',evidence)
    view=g.review(request(view));_,p,answers=kinds.current('drive-source-id')
    card=next(c for c in cards(view,answers,MASTER) if c['identity']['kind']=='general_manual')
    sheet=Sheet(snapshot(card,FIELDS));sheet.categories=MASTER
    return g,live,t,kinds,view,answers,sheet

def run(args,factory=None):
    g,live,t,kinds,view,answers,sheet=args
    return process_page_request(kinds,sheet,'87654321-1234-1234-1234-123456789abc',
        general_factory=factory,medical_factory=Mock(side_effect=AssertionError('Medical forbidden')))

def test_three_unknown_human_normal_pages_get_blank_manual_cards_only_after_group_confirmation(local_ocr):
    args=context();g,live,t,kinds,view,answers,sheet=args
    cs=cards(view,answers,MASTER)
    manual=[c for c in cs if c['identity']['kind']=='general_manual']
    assert [c['identity']['page_numbers'] for c in manual]==[[4],[10],[14]]
    assert all(all(v=='' for f,_,v in c['rows'] if f in FIELDS) for c in manual)
    unconfirmed=deepcopy(view);unconfirmed.update(status='grouping_required',confirmation=None)
    assert not any(c['identity']['kind']=='general_manual' for c in cards(unconfirmed,answers,MASTER))
    assert next(c for c in cs if c['identity']['kind']=='medical')['identity']['page_numbers']==[1]

def test_explicit_confirm_checks_owner_snapshot_and_never_creates_grouping_authority(local_ocr):
    args=context();before=args[2].payload;called=[]
    def factory(identity,uid,payload,owner):
        assert owner()==payload==manual_values(FIELDS,MASTER);called.append(identity)
        return '一般手入力済み'
    assert run(args,factory)=='一般手入力済み'
    assert len(called)==1 and called[0]['page_numbers']==[4] and args[2].payload==before
    card=next(c for c in args[-1].published if c['identity']['kind']=='general_manual')
    assert dict((f,v) for f,_,v in card['rows'])['state']=='一般手入力済み'

@pytest.mark.parametrize('field,value',[('date',''),('date','2026/02/30'),('amount','0'),
    ('amount','0.5'),('category',''),('category','unknown｜unknown')])
def test_incomplete_or_invalid_input_cannot_reach_host(field,value,local_ocr):
    args=context();args[-1].capture=snapshot(next(c for c in cards(args[4],args[5],MASTER)
        if c['identity']['kind']=='general_manual'),{**FIELDS,field:value})
    factory=Mock();before=args[2].payload
    with pytest.raises(StateError,match='complete_manual_input_required'):run(args,factory)
    factory.assert_not_called();assert args[2].payload==before

@pytest.mark.parametrize('changed',['inputs','queue','hash','grouping'])
def test_current_source_owner_or_partition_change_blocks_callback_read(changed,local_ocr):
    args=context()
    def factory(identity,uid,payload,owner):
        if changed=='inputs':args[-1].live['rows'][5][2]='2026/09/25'
        elif changed=='queue':args[-1].capture['rows'][5][2]='2026/09/25'
        elif changed=='hash':
            old=args[1].observations
            args[1].observations=replace(old,source_content_hash='a'*64,
                pages=tuple(replace(p,source_content_hash='a'*64) for p in old.pages))
        else:
            args[0].review(request(args[4],operation='edit',partition=[[n] for n in range(2,15)],number=2))
        owner()
        return '一般手入力済み'
    with pytest.raises(StateError):run(args,factory)

def test_without_existing_backend_no_success_label_or_authority(local_ocr):
    args=context();before=args[2].payload
    with pytest.raises(StateError,match='manual_backend_required'):run(args)
    assert args[2].payload==before and not args[-1].published

def test_hold_and_page_kind_dont_call_general_backend(local_ocr):
    args=context();args[-1].capture=snapshot(next(c for c in cards(args[4],args[5],MASTER)
        if c['identity']['kind']=='general_manual'),{'manual_action':'保留'})
    factory=Mock();assert run(args,factory)=='一般手入力待ち';factory.assert_not_called()
