from dataclasses import replace
import json
import pytest
from app.medical_layout_shadow import PageFrame
from app.medical_numeric_multipass import NumericOcrPass
from app.medical_region_shadow import fingerprint_region_passes, compare_region_views
from app.medical_receipt_privacy import _StructuredOcrToken as Token


def token(text,x=20,y=20,width=40,confidence=96):
    return Token(text,1,x,y,width,20,confidence,(1,1,1,1))


def view(*tokens,psm=6,scale=1,source=b'a'*32,page=1):
    return NumericOcrPass(source,psm,PageFrame(page,400*scale,600*scale),
        tuple(replace(t,x=t.x*scale,y=t.y*scale,width=t.width*scale,height=t.height*scale,page=page) for t in tokens))


def compare(a,b,**kwargs):
    output=fingerprint_region_passes((a,b))
    return compare_region_views(*output.views,**kwargs)


def test_value_change_does_not_change_fingerprint_or_link():
    a=view(token('1280')); b=view(token('9050'),psm=11)
    result=fingerprint_region_passes((a,b))
    assert result.views[0].regions==result.views[1].regions
    assert compare_region_views(*result.views)[0].classification=='stable_region'


def test_split_merge_and_scale_match_without_numeric_key():
    a=view(token('1280'))
    b=view(token('1',width=10),token('280',x=32,width=28),scale=0.5,psm=11)
    links=compare(a,b)
    assert len(links)==1 and links[0].classification=='tokenization_variant'


def test_different_line_keys_do_not_override_baseline():
    a=view(token('1280'))
    assert compare(a,view(token('1280',y=120),psm=11))[0].classification=='region_missing'
    assert compare(a,view(token('1280',x=24),psm=11))[0].classification=='bbox_variant'


def test_competing_duplicate_regions_are_not_chosen_by_value():
    links=compare(view(token('1280')),view(token('1280'),token('900'),psm=11))
    assert links[0].classification=='ambiguous_correspondence'
    assert len(links[0].right)==2


def test_low_confidence_malformed_and_negative_context_survive():
    result=fingerprint_region_passes((view(token('1.280',confidence=25),token('税',x=70)),))
    r=result.views[0].regions[0]
    assert {'low_confidence','malformed_numeric'}<=set(r.issues)
    assert r.negative_context


def test_deduplication_and_no_sensitive_fingerprint(capsys):
    a=view(token('PRIVATE_CANARY'),token('987654',x=100))
    result=fingerprint_region_passes((a,a))
    assert result.duplicate_passes==1 and len(result.views)==1
    exposed=repr(result)+repr(result.views[0].regions)
    assert 'PRIVATE_CANARY' not in exposed and '987654' not in exposed
    assert capsys.readouterr()==('','')


def test_pages_never_join_and_unregistered_sources_remain_ambiguous():
    a=view(token('1280'))
    assert compare(a,view(token('1280'),psm=11,page=2))[0].classification=='ambiguous_correspondence'
    assert compare(a,view(token('1280'),source=b'b'*32),cross_source=True)[0].classification=='ambiguous_correspondence'


def test_empty_observation_is_incomplete_not_verified_missing():
    assert compare(view(token('1280')),view(psm=11))[0].classification=='ambiguous_correspondence'


def test_large_cluster_keeps_tokens_with_explicit_uncertainty():
    result=fingerprint_region_passes((view(*(token('1',x=20+12*i,width=10) for i in range(8))),))
    assert len(result.views[0].regions)==8
    assert all('cluster_extent_unresolved' in r.issues for r in result.views[0].regions)


def test_nearby_private_text_is_only_a_coarse_pattern():
    a=view(token('1280'),token('PRIVATE_A',x=90))
    b=view(token('9050'),token('PRIVATE_B',x=90),psm=11)
    r=fingerprint_region_passes((a,b))
    assert r.views[0].regions==r.views[1].regions


def test_ambiguous_numeric_spans_remain_in_fingerprint_quality():
    r=fingerprint_region_passes((view(token('12',width=10),token('80',x=32,width=10)),))
    assert 'digit_concatenation_unproven' in r.views[0].regions[0].issues


def test_inconsistent_page_aspect_is_not_silently_normalized():
    a=view(token('1280'))
    b=replace(view(token('1280'),psm=11),frame=PageFrame(1,800,600))
    assert compare(a,b)[0].classification=='ambiguous_correspondence'


def test_partial_fragment_coverage_is_unresolved_not_region_absence():
    links=compare(view(token('1280',width=80)),
        view(token('90',width=10),token('50',x=50,width=10),psm=11))
    assert links[0].classification=='ambiguous_correspondence'
    assert links[0].right==(0,1)
    assert 'partial_region_coverage' in links[0].issues
