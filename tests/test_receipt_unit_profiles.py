"""Reuse the existing Google OIDC transport; each private Unit is pinned."""
from copy import deepcopy
from types import SimpleNamespace
import pytest
from app.drive_run_state import StateError
from app.page_receipt_model import digest
from services.human_general.page_router import validate_profiles,SCHEMA,PageRouter
from services.human_general.receipt_review_readers import validate_config_review
from services.human_general.receipt_review_runtime import ReceiptReviewRuntime
from test_human_general_real_page import fixture,page_config
from test_human_general_auth_transport import keys
from test_human_general_http import HttpRig

def review(cfg,index=1):
    value=dict(drive=cfg,candidate_file='synthetic-candidate-'+str(cfg['page']['page_number'])+'-'+str(index),
        candidate_digest=digest(['synthetic',cfg['binding'],index]),journal_file='synthetic-journal-'+str(cfg['page']['page_number'])+'-'+str(index))
    value['scope']=digest(['receipt-item-confirmation-live-v1',cfg['binding'],value['candidate_file'],value['journal_file'],value['candidate_digest']])
    return value

def profiles(monkeypatch,keys):
    _,_,cfg,info=fixture(monkeypatch,keys)
    bundle=dict(schema=SCHEMA,profiles={'p4':page_config(cfg,4),'p10':page_config(cfg,10),'p14':cfg},receipt_review=review(cfg))
    bundle['receipt_reviews']={}
    for n in (4,10):
        for i in (1,2):
            r=review(bundle['profiles']['p'+str(n)],i);bundle['receipt_reviews'][f'p{n}_items_'+r['scope']]=r
    return bundle,info

def test_multiple_units_each_page_keep_p14_profile(monkeypatch,keys):
    bundle,_=profiles(monkeypatch,keys);assert validate_profiles(bundle)==bundle
    assert len({r['scope'] for r in bundle['receipt_reviews'].values()})==4
    for value in bundle['receipt_reviews'].values():assert validate_config_review(value)==value

@pytest.mark.parametrize('change',['name','source_profile','page','candidate_collision','journal_collision','hga_collision','scope','digest'])
def test_profile_tamper_rejected(monkeypatch,keys,change):
    b,_=profiles(monkeypatch,keys);name=next(iter(b['receipt_reviews']));r=b['receipt_reviews'][name]
    if change=='name':b['receipt_reviews']['p4_items_arbitrary']=b['receipt_reviews'].pop(name)
    elif change=='source_profile':r['drive']=b['profiles']['p10']
    elif change=='page':r['drive']['page']['page_number']=1
    elif change in {'candidate_collision','journal_collision'}:
        old=b['receipt_review'];r['candidate_file' if change=='candidate_collision' else 'journal_file']=old['candidate_file' if change=='candidate_collision' else 'journal_file']
        r['scope']=digest(['receipt-item-confirmation-live-v1',r['drive']['binding'],r['candidate_file'],r['journal_file'],r['candidate_digest']])
        b['receipt_reviews'][f'p4_items_'+r['scope']]=b['receipt_reviews'].pop(name)
    elif change=='hga_collision':r['candidate_file']=r['drive']['authority_file']
    else:r['scope' if change=='scope' else 'candidate_digest']='0'*64
    with pytest.raises(StateError):validate_profiles(b)

def test_runtime_labels_use_selected_page_without_changing_p14():
    for n in (4,10,14):
        rt=object.__new__(ReceiptReviewRuntime);rt.drive=SimpleNamespace(page=SimpleNamespace(page_number=n))
        assert rt.label('ignored').startswith(f'p{n} ')
        assert rt.success_label('ignored').startswith(f'p{n}の')

def test_unknown_profile_never_creates_auth_request(monkeypatch,keys):
    bundle,info=profiles(monkeypatch,keys);rig=HttpRig(keys)
    router=PageRouter(rig.db,rig.settings,rig.key,bundle,info,clock=lambda:rig.now,identity=rig.runtime.identity,exchange=rig.exchange)
    before=deepcopy(rig.db.data);writes=rig.db.writes
    for name in ('p4','p1','p14','p4_items_unknown'):
        with pytest.raises(StateError):router.seed_item_review(name)
    assert rig.db.data==before and rig.db.writes==writes
