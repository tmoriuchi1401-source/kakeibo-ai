"""Synthetic/adversarial tests for the hospital-lineage Level 2 shadow only."""
from dataclasses import replace
import ast
import json
from pathlib import Path

import pytest

from app.medical_ocr_observation_shadow import ReceiptImage, make_observation
from app.medical_payment_level2_shadow import (
    classify_structural_relation,
    evaluate_level2_payment_shadow,
    evaluate_materialization_stable_level2_shadow,
)
from app.medical_receipt_privacy import build_receipt_privacy_preview


def region(text, x, y, width=70, height=20, confidence=.96):
    return {"text": text, "confidence": confidence, "detection_confidence": .9,
            "polygon": [[x, y], [x + width, y], [x + width, y + height], [x, y + height]]}


def observation(*regions, issues=()):
    image = ReceiptImage("synthetic-unit", 1, b"synthetic")
    return make_observation(image, "synthetic", ("a" * 64,), 600, 800, list(regions), issues)


def valid(*extra):
    return observation(region("領収金額", 100, 500, 100), region("1234円", 110, 535, 80), *extra)


def count(source, **kwargs):
    return evaluate_level2_payment_shadow(source, **kwargs).aggregate()["shadow_candidate_count"]


def test_exact_label_and_unique_bounded_summary_pair_creates_shadow_only_handle():
    result = evaluate_level2_payment_shadow(valid())
    assert count(valid()) == 1
    candidate = result.candidates[0]
    assert candidate.evidence_level == 2 and candidate.status == "shadow_only"
    assert candidate.strong_label_evidence == "exact_allowlisted"
    assert candidate.spatial_relation == "column_below"
    assert not hasattr(candidate, "amount")
    assert result.payment_role_evidence_completeness == "complete"
    assert result.materialization_stability == "unverified"


@pytest.mark.parametrize("label", ["領収金額", "お支払い額", "お支払額", "支払額"])
def test_exact_allowlisted_strong_labels(label):
    assert count(observation(region(label, 100, 500), region("1234", 105, 535))) == 1


def test_nearer_wrong_amount_makes_pair_ambiguous_instead_of_nearest_selection():
    source = valid(region("999円", 105, 523, 70))
    result = evaluate_level2_payment_shadow(source)
    assert count(source) == 0
    assert result.blocked_competitor_count >= 1 or result.ambiguous_count == 1


@pytest.mark.parametrize("negative", ["小計", "保険負担額", "消費税", "預り金", "お釣り"])
def test_negative_context_in_summary_neighborhood_blocks(negative):
    source = valid(region(negative, 190, 535, 80))
    result = evaluate_level2_payment_shadow(source)
    assert count(source) == 0
    assert result.blocked_negative_context_count == 1


def test_duplicate_equal_observations_are_one_group_not_three_votes():
    source = valid(region("1234円", 110, 535, 80), region("1234円", 110, 535, 80))
    result = evaluate_level2_payment_shadow(source)
    assert len(result.candidates) == 1
    assert len(result.candidates[0].observation_reference) == 3


def test_two_equally_plausible_numeric_groups_abstain():
    source = observation(region("領収金額", 100, 500, 100),
                         region("1234円", 110, 535), region("5678円", 110, 570))
    assert count(source) == 0


def test_possible_payment_competitor_blocks():
    source = valid(region("自費 999円", 200, 535, 120))
    assert count(source) == 0


@pytest.mark.parametrize("gap,state", [(1.99, "STRONG"), (2.0, "STRONG"),
                                        (2.01, "UNCERTAIN"), (2.5, "UNCERTAIN"),
                                        (2.99, "UNCERTAIN"), (3.01, "UNRELATED")])
def test_column_geometry_has_conservative_uncertainty_band(gap, state):
    label = observation(region("支払額", 100, 100, 80, 20),
                        region("1234", 100, 120 + gap * 20, 80, 20)).regions
    assert classify_structural_relation(label[0], label[1]).state == state


@pytest.mark.parametrize("gap,state", [(2.99, "STRONG"), (3.0, "STRONG"),
                                        (3.01, "UNCERTAIN"), (4.0, "UNCERTAIN"),
                                        (4.99, "UNCERTAIN"), (5.01, "UNRELATED")])
def test_row_geometry_has_conservative_uncertainty_band(gap, state):
    label = observation(region("支払額", 100, 100, 80, 20),
                        region("1234", 180 + gap * 20, 100, 80, 20)).regions
    assert classify_structural_relation(label[0], label[1]).state == state


def test_uncertain_positive_relation_never_creates_candidate():
    source = observation(region("支払額", 100, 100, 80, 20),
                         region("1234", 100, 165, 80, 20))
    result = evaluate_level2_payment_shadow(source)
    assert count(source) == 0
    assert result.unresolved_competitor_count == 1
    assert result.payment_role_evidence_completeness == "unresolved"


def test_uncertain_equal_value_duplicate_blocks_strong_duplicate():
    source = observation(region("支払額", 100, 100, 80, 20),
                         region("1234", 100, 120 + 0.75 * 20, 80, 20),
                         region("1234", 100, 120 + 2.5 * 20, 80, 20))
    result = evaluate_level2_payment_shadow(source)
    assert not result.candidates
    assert result.blocked_competitor_count >= 1
    assert result.unresolved_competitor_count >= 1
    assert result.same_amount_competitor_count == 1
    assert result.payment_role_evidence_completeness == "unresolved"


@pytest.mark.parametrize("competitor_gap", [2.1892, 2.6364])
def test_materialization_boundary_variants_both_retain_uncertain_competitor(competitor_gap):
    source = observation(region("領収金額", 100, 100, 100, 20),
                         region("1234", 110, 135, 80, 20),
                         region("自費 999円", 110, 155 + competitor_gap * 20, 100, 20))
    result = evaluate_level2_payment_shadow(source)
    assert count(source) == 0
    assert result.blocked_competitor_count >= 1
    assert result.unresolved_competitor_count >= 1


def test_materialization_consensus_intersects_positive_evidence():
    first, second = valid(), observation(region("領収金額", 200, 200, 200, 40),
                                          region("1234円", 220, 270, 160, 40))
    result = evaluate_materialization_stable_level2_shadow(
        (first, second), same_source_confirmed=True)
    assert len(result.candidates) == 1
    assert result.candidates[0].materialization_stability == "confirmed"
    assert result.materialization_stability == "confirmed"


def test_materialization_consensus_unions_uncertain_blockers():
    clean = valid()
    blocked = observation(region("領収金額", 100, 100, 100, 20),
                          region("1234", 110, 135, 80, 20),
                          region("自費 999円", 110, 200, 100, 20))
    result = evaluate_materialization_stable_level2_shadow(
        (clean, blocked), same_source_confirmed=True)
    assert not result.candidates
    assert result.materialization_stability == "conflicting"


def test_materialization_consensus_requires_confirmed_lineage_and_two_inputs():
    assert not evaluate_materialization_stable_level2_shadow(
        (valid(),), same_source_confirmed=True).candidates
    assert not evaluate_materialization_stable_level2_shadow(
        (valid(), valid()), same_source_confirmed=False).candidates


def test_incomplete_observation_blocks():
    source = valid()
    assert count(replace(source, issues=("observation_incomplete",))) == 0
    assert evaluate_level2_payment_shadow(replace(source, issues=("x",))).incomplete_count == 1


@pytest.mark.parametrize("bad", ["1.234", "12O4", "-1234", "1,23,4"])
def test_malformed_numeric_evidence_blocks(bad):
    source = observation(region("支払額", 20, 500, 80), region(bad, 120, 500),
                         region("1234円", 220, 500))
    assert count(source) == 0


def test_unrelated_malformed_numeric_does_not_globally_veto_summary():
    assert count(valid(region("1.234", 400, 100))) == 1


def test_intervening_numeric_blocks_even_when_target_is_locally_aligned():
    source = observation(region("支払額", 20, 500, 80), region("999円", 110, 500, 30),
                         region("1234円", 150, 500, 50))
    assert count(source) == 0


def test_visually_near_but_different_structural_group_does_not_pair():
    source = observation(region("支払額", 20, 500, 80), region("1234円", 350, 555))
    assert count(source) == 0


def test_pharmacy_like_without_exact_label_remains_proposal_only():
    source = observation(region("お支 払い", 100, 500), region("1234円", 110, 535))
    result = evaluate_level2_payment_shadow(source)
    assert count(source) == 0 and result.proposal_only_count == 1


@pytest.mark.parametrize("classification", ["sensitive_unknown", "normal", "unrelated"])
def test_non_medical_classification_never_creates_shadow(classification):
    assert count(valid(), classification=classification) == 0


def test_low_confidence_label_or_amount_blocks():
    assert count(observation(region("支払額", 100, 500, confidence=.89),
                             region("1234", 105, 535))) == 0
    assert count(observation(region("支払額", 100, 500),
                             region("1234", 105, 535, confidence=.89))) == 0


def test_extra_exact_payment_label_blocks():
    assert count(valid(region("お支払額", 400, 100))) == 0


def test_output_and_repr_are_data_minimized():
    result = evaluate_level2_payment_shadow(valid())
    rendered = repr(result) + repr(result.candidates[0]) + json.dumps(result.aggregate())
    assert "1234" not in rendered and "領収金額" not in rendered
    assert set(result.aggregate()) == {"shadow_candidate_count", "blocked_competitor_count",
        "blocked_negative_context_count", "ambiguous_count", "proposal_only_count",
        "incomplete_count", "unresolved_competitor_count",
        "same_amount_competitor_count",
        "payment_role_evidence_complete", "materialization_stable", "evaluation_failed"}


def test_shadow_call_does_not_change_production_resolution():
    before = build_receipt_privacy_preview("病院 診療\n領収金額", ())
    evaluate_level2_payment_shadow(valid())
    assert build_receipt_privacy_preview("病院 診療\n領収金額", ()) == before
    assert before.status == "needs_review"


def test_production_modules_do_not_import_level2_shadow():
    root = Path(__file__).resolve().parents[1] / "app"
    production = ("receipt_pipeline.py", "receipt_privacy_gate.py", "receipt_text_extraction.py")
    for name in production:
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        assert not any(isinstance(node, ast.ImportFrom)
                       and node.module == "app.medical_payment_level2_shadow"
                       for node in ast.walk(tree))
