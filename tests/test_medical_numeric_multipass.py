from dataclasses import replace
import json
import pytest
from app.medical_layout_shadow import PageFrame
from app.medical_numeric_multipass import NumericOcrPass, compare_numeric_passes
from app.medical_receipt_privacy import _StructuredOcrToken as Token


def tok(text='1280',x=20,y=20,width=40,confidence=95,page=1):
    return Token(text,page,x,y,width,20,confidence,(1,1,1,1))


def view(*tokens,psm=6,scale=1,source=b'a'*32,page=1):
    return NumericOcrPass(source,psm,PageFrame(page,200*scale,300*scale),
        tuple(replace(t,x=t.x*scale,y=t.y*scale,width=t.width*scale,height=t.height*scale,page=page) for t in tokens))


def test_replay_not_a_second_vote_and_sources_never_join():
    a=view(tok())
    result=compare_numeric_passes((a,a,view(tok(),source=b'b'*32)))
    assert result.duplicate_passes==1 and result.source_groups==2
    assert len(result.observations)==2
    assert all(c.matching_configurations==1 for c in result.comparisons)


def test_identical_tokenization_in_different_settings_is_still_correlated():
    result=compare_numeric_passes((view(tok()),view(tok(),psm=11,scale=0.5)))
    assert all(c.matching_configurations==2 and c.tokenization_variants==1 for c in result.comparisons)
    assert 'correlated_ocr_views' in result.issues
    assert 'payment_role_unproven' in result.issues


@pytest.mark.parametrize('text,confidence',[('900',95),('900',69),('9.00',95),('???',20)])
def test_contained_small_fragment_cannot_be_bypassed(text,confidence):
    result=compare_numeric_passes((view(tok()),view(tok(),psm=11),
        view(tok(text,x=22,width=5,confidence=confidence),psm=3)))
    anchor=next(c for c in result.comparisons if c.anchor==(0,0))
    assert anchor.conflicting_interpretations or anchor.unresolved_quality or anchor.missing_configurations


def test_different_value_at_same_region_kept_even_with_many_matches():
    result=compare_numeric_passes((view(tok()),view(tok(),psm=11),view(tok('900'),psm=3)))
    assert all(c.conflicting_interpretations for c in result.comparisons)
    assert sum(len(o.atoms) for o in result.observations)==3


def test_same_value_elsewhere_is_not_agreement_and_missing_region_survives():
    result=compare_numeric_passes((view(tok()),view(tok(x=120),psm=11)))
    assert all(c.matching_configurations==1 and c.missing_configurations==1 for c in result.comparisons)


def test_segmentation_variation_retains_original_atoms_and_ambiguity():
    result=compare_numeric_passes((view(tok()),view(tok('1',width=10),tok('280',x=32,width=28),psm=11)))
    anchor=next(c for c in result.comparisons if c.anchor==(0,0))
    assert anchor.matching_configurations==2 and anchor.tokenization_variants==2
    assert anchor.conflicting_interpretations and anchor.unresolved_quality
    assert len(result.observations[1].atoms)==2


def test_same_configuration_with_changed_ocr_is_not_silently_overwritten():
    result=compare_numeric_passes((view(tok()),view(tok('900'))))
    assert result.duplicate_passes==0 and len(result.observations)==2
    assert 'configuration_reobserved' in result.issues
    assert all(c.matching_configurations==1 and c.conflicting_interpretations for c in result.comparisons)


def test_page_boundaries_remain_separate():
    result=compare_numeric_passes((view(tok(),page=1),view(tok(),page=2,psm=11)))
    assert result.source_groups==2
    assert all(c.matching_configurations==1 for c in result.comparisons)


def test_empty_failed_view_kept_and_no_sensitive_results(capsys):
    result=compare_numeric_passes((view(tok('PRIVATE_CANARY')),view(psm=11)))
    assert len(result.observations)==2
    assert 'observation_incomplete' in result.issues
    assert 'PRIVATE_CANARY' not in repr(result)+json.dumps(result.aggregate())
    assert capsys.readouterr()==('','')
    assert not hasattr(result,'amount') and not hasattr(result,'confirmed')


def test_unverified_coordinate_transform_prevents_comparison():
    a=view(tok()); b=replace(view(tok(),psm=11),frame=PageFrame(1,300,300))
    result=compare_numeric_passes((a,b))
    assert not result.comparisons and 'observation_incomplete' in result.issues
    assert len(result.observations)==2


def test_bounds_fail_closed():
    assert 'observation_incomplete' in compare_numeric_passes(tuple(view(tok()) for _ in range(13))).issues
    assert 'observation_incomplete' in compare_numeric_passes(()).issues
