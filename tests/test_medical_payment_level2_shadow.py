"""Synthetic/adversarial tests for the hospital-lineage Level 2 shadow only."""
from dataclasses import replace
import ast
import json
from pathlib import Path

import pytest

import app.medical_payment_level2_shadow as level2
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


def test_malformed_blocker_diagnostic_records_multiple_geometry_flags_and_categories():
    source = observation(region("支払額", 20, 500, 80),
                         region("自費 1.234", 105, 500, 20),
                         region("1234円", 140, 500, 60))
    result = evaluate_level2_payment_shadow(source)
    assert not result.candidates and result.malformed_blocked_group_count == 1
    assert result.malformed_blockers == (
        level2.MalformedNumericBlockerDiagnostic(
            geometry_flags=("BETWEEN_TARGET", "LABEL_TO_BLOCKER_STRONG"),
            context_category="PAYMENT_CONTEXT",
            confidence_band="HIGH",
            document_role_signal="EXPLICIT_PAYMENT_CONTEXT",
            target_relation_flags=("BLOCKER_TO_TARGET_STRONG", "BLOCKER_TARGET_SAME_ROW"),
            parser_rejection_class="DECIMAL_OR_DOT_LIKE",
            text_affix_structure_flags=(
                "KANA_KANJI_PREFIX", "SEPARATOR_BOUNDARY",
                "MULTI_TOKEN_TEXT_NUMERIC_STRUCTURE"),
            neighborhood_flags=(
                "LABEL_LOCAL_BLOCK_PROXY", "DIRECT_PAYMENT_LABEL_CONNECTION",
                "TARGET_LOCAL_BLOCK_PROXY"),
            payment_connectivity="PAYMENT_LABEL_AND_VALID_TARGET",
        ),
    )


def test_reverse_only_non_payment_context_is_observable_without_changing_block():
    source = observation(region("12O4", 100, 40, 80),
                         region("支払額", 100, 100, 80),
                         region("1234円", 220, 100, 80))
    result = evaluate_level2_payment_shadow(source)
    diagnostic = result.malformed_blockers[0]
    assert not result.candidates and result.blocked_competitor_count == 1
    assert diagnostic.geometry_flags == ("BLOCKER_TO_LABEL_STRONG",)
    assert diagnostic.context_category == "NO_PAYMENT_CONTEXT"
    assert diagnostic.document_role_signal == "UNRESOLVED"
    assert diagnostic.target_relation_flags == ("BLOCKER_TARGET_UNRELATED",)
    assert diagnostic.parser_rejection_class == "OCR_ALPHA_CONTAMINATION"


@pytest.mark.parametrize(("bad", "expected"), [
    ("1,23,4", "MALFORMED_GROUPING"),
    ("-1234", "SIGN_OR_PREFIX"),
    ("12O4", "OCR_ALPHA_CONTAMINATION"),
    ("1.234", "DECIMAL_OR_DOT_LIKE"),
    ("12/34", "SLASH_SEPARATED"),
    ("12-34", "INTERNAL_HYPHENATED"),
    ("番号12_34", "TEXT_AFFIXED_NUMERIC"),
    ("12:34", "OTHER_UNKNOWN"),
])
def test_parser_rejection_class_is_data_minimized(bad, expected):
    item = observation(region(bad, 100, 100)).regions[0]
    assert level2._malformed_parser_rejection_class(item) == expected


@pytest.mark.parametrize(("confidence", "expected"), [
    (.96, "HIGH"), (.70, "MID"), (.69, "LOW"), (None, "UNKNOWN"),
])
def test_diagnostic_confidence_bands_do_not_affect_thresholds(confidence, expected):
    item = observation(region("1.234", 100, 100)).regions[0]
    item = replace(item, confidence=confidence)
    assert level2._malformed_confidence_band(item) == expected


@pytest.mark.parametrize(("text", "expected"), [
    ("自費 1.234", "EXPLICIT_PAYMENT_CONTEXT"),
    ("2026/09/09", "DATE_LIKE_NUMERIC"),
    ("12-34", "IDENTIFIER_LIKE_NUMERIC"),
    ("12:34", "UNRESOLVED"),
])
def test_document_role_signal_uses_only_existing_context_and_numeric_shape(text, expected):
    item = observation(region(text, 100, 100)).regions[0]
    assert level2._malformed_document_role_signal(item) == expected


def test_true_competitor_and_unrelated_identifier_have_distinct_diagnostic_signals():
    competitor = observation(region("支払額", 20, 500, 80),
                             region("自費 1.234", 105, 500, 20),
                             region("1234円", 140, 500, 60))
    identifier = observation(region("12-34", 100, 40, 80),
                             region("支払額", 100, 100, 80),
                             region("1234円", 220, 100, 80))
    true_signal = evaluate_level2_payment_shadow(competitor).malformed_blockers[0]
    unrelated_signal = evaluate_level2_payment_shadow(identifier).malformed_blockers[0]
    assert true_signal.document_role_signal == "EXPLICIT_PAYMENT_CONTEXT"
    assert "BLOCKER_TO_TARGET_STRONG" in true_signal.target_relation_flags
    assert unrelated_signal.document_role_signal == "IDENTIFIER_LIKE_NUMERIC"
    assert unrelated_signal.target_relation_flags == ("BLOCKER_TARGET_UNRELATED",)
    assert unrelated_signal.parser_rejection_class == "INTERNAL_HYPHENATED"


def test_date_like_reverse_only_blocker_is_observable_but_still_fails_closed():
    source = observation(region("2026/09/09", 100, 40, 80),
                         region("支払額", 100, 100, 80),
                         region("1234円", 220, 100, 80))
    result = evaluate_level2_payment_shadow(source)
    diagnostic = result.malformed_blockers[0]
    assert not result.candidates and result.blocked_competitor_count == 1
    assert diagnostic.document_role_signal == "DATE_LIKE_NUMERIC"
    assert diagnostic.parser_rejection_class == "SLASH_SEPARATED"


def test_unresolved_role_signal_preserves_fail_closed_block():
    source = observation(region("12:34", 100, 40, 80),
                         region("支払額", 100, 100, 80),
                         region("1234円", 220, 100, 80))
    result = evaluate_level2_payment_shadow(source)
    assert not result.candidates and result.blocked_competitor_count == 1
    assert result.malformed_blockers[0].document_role_signal == "UNRESOLVED"


@pytest.mark.parametrize(("text", "expected"), [
    ("ABC123", {"ALPHABETIC_PREFIX", "SINGLE_TOKEN_MIXED_TEXT_NUMERIC"}),
    ("123XYZ", {"ALPHABETIC_SUFFIX", "SINGLE_TOKEN_MIXED_TEXT_NUMERIC"}),
    ("番号123項", {"KANA_KANJI_PREFIX", "KANA_KANJI_SUFFIX",
                 "NUMERIC_SURROUNDED_BY_TEXT", "SINGLE_TOKEN_MIXED_TEXT_NUMERIC"}),
    ("ID-123", {"ALPHABETIC_PREFIX", "SEPARATOR_BOUNDARY",
                "SINGLE_TOKEN_MIXED_TEXT_NUMERIC"}),
    ("番号 123", {"KANA_KANJI_PREFIX", "MULTI_TOKEN_TEXT_NUMERIC_STRUCTURE"}),
])
def test_text_affix_structure_is_anonymous_and_shape_only(text, expected):
    item = observation(region(text, 100, 100)).regions[0]
    flags = level2._text_affix_structure_flags(item)
    assert set(flags) == expected
    assert text not in repr(flags)


def _synthetic_malformed_contrast_cases():
    return {
        "true_malformed_payment_amount_with_affix": observation(
            region("自費123円", 105, 100, 20),
            region("支払額", 20, 100, 80),
            region("1234円", 140, 100, 80)),
        "identifier_like_text_affixed_numeric": observation(
            region("ID-123", 100, 40, 80),
            region("支払額", 100, 100, 80),
            region("1234円", 220, 100, 80)),
        "date_reference_like_text_affixed_numeric": observation(
            region("REF-2026/09/09", 100, 40, 80),
            region("支払額", 100, 100, 80),
            region("1234円", 220, 100, 80)),
        "payment_label_nearby_different_role_numeric": observation(
            region("受付123", 105, 100, 20),
            region("支払額", 20, 100, 80),
            region("1234円", 140, 100, 80)),
        "reverse_only_target_unrelated": observation(
            region("番号123", 100, 40, 80),
            region("支払額", 100, 100, 80),
            region("1234円", 220, 100, 80)),
        "target_local_true_competitor": observation(
            region("患者123円", 105, 100, 20),
            region("支払額", 20, 100, 80),
            region("1234円", 140, 100, 80)),
        "ambiguous_text_affixed_numeric": observation(
            region("A123B", 105, 100, 20),
            region("支払額", 20, 100, 80),
            region("1234円", 140, 100, 80)),
    }


def test_synthetic_malformed_contrast_set_retains_fail_closed_semantics():
    results = {name: evaluate_level2_payment_shadow(source)
               for name, source in _synthetic_malformed_contrast_cases().items()}
    assert set(results) == {
        "true_malformed_payment_amount_with_affix",
        "identifier_like_text_affixed_numeric",
        "date_reference_like_text_affixed_numeric",
        "payment_label_nearby_different_role_numeric",
        "reverse_only_target_unrelated",
        "target_local_true_competitor",
        "ambiguous_text_affixed_numeric",
    }
    assert all(not result.candidates for result in results.values())
    assert all(result.payment_role_evidence_completeness == "unresolved"
               for result in results.values())
    assert all(result.blocked_competitor_count == 1 for result in results.values())

    diagnostics = {name: result.malformed_blockers[0]
                   for name, result in results.items()}
    assert diagnostics[
        "true_malformed_payment_amount_with_affix"].document_role_signal == (
            "EXPLICIT_PAYMENT_CONTEXT")
    assert diagnostics["target_local_true_competitor"].payment_connectivity == (
        "PAYMENT_LABEL_AND_VALID_TARGET")
    assert "TARGET_LOCAL_BLOCK_PROXY" in diagnostics[
        "target_local_true_competitor"].neighborhood_flags

    for name in ("identifier_like_text_affixed_numeric",
                 "date_reference_like_text_affixed_numeric",
                 "reverse_only_target_unrelated"):
        diagnostic = diagnostics[name]
        assert diagnostic.payment_connectivity == "PAYMENT_LABEL_ONLY"
        assert "TARGET_LOCAL_INDEPENDENT" in diagnostic.neighborhood_flags
        assert diagnostic.target_relation_flags == ("BLOCKER_TARGET_UNRELATED",)

    assert diagnostics[
        "ambiguous_text_affixed_numeric"].document_role_signal == "UNRESOLVED"
    assert diagnostics[
        "payment_label_nearby_different_role_numeric"].document_role_signal == "UNRESOLVED"


def test_same_row_non_payment_neighborhood_is_anonymous():
    source = observation(region("受付123", 105, 100, 20),
                         region("支払額", 20, 100, 80),
                         region("1234円", 140, 100, 80),
                         region("明細", 330, 100, 60))
    diagnostic = evaluate_level2_payment_shadow(source).malformed_blockers[0]
    assert "SAME_ROW_NON_PAYMENT_TEXT" in diagnostic.neighborhood_flags
    assert "明細" not in repr(diagnostic)


def test_diagnostic_collection_cannot_change_candidate_or_block_semantics(monkeypatch):
    source = observation(region("支払額", 20, 500, 80), region("1.234", 105, 500, 20),
                         region("1234円", 140, 500, 60))
    before = evaluate_level2_payment_shadow(source)
    monkeypatch.setattr(level2, "_build_malformed_blocker_diagnostics", lambda *args: ())
    after = evaluate_level2_payment_shadow(source)
    without_diagnostics = lambda result: replace(
        result, malformed_blockers=(), malformed_blocked_group_count=0)
    assert without_diagnostics(before) == without_diagnostics(after)
    assert len(before.malformed_blockers) == 1 and not after.malformed_blockers


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
        "malformed_blocked_group_count", "malformed_blocker_count",
        "malformed_geometry_between_target",
        "malformed_geometry_label_to_blocker_strong",
        "malformed_geometry_label_to_blocker_uncertain",
        "malformed_geometry_blocker_to_label_strong",
        "malformed_geometry_blocker_to_label_uncertain",
        "malformed_context_payment_context", "malformed_context_no_payment_context",
        "malformed_context_context_unknown",
        "malformed_confidence_high", "malformed_confidence_mid",
        "malformed_confidence_low", "malformed_confidence_unknown",
        "malformed_rejection_malformed_grouping", "malformed_rejection_sign_or_prefix",
        "malformed_rejection_ocr_alpha_contamination",
        "malformed_rejection_decimal_or_dot_like", "malformed_rejection_slash_separated",
        "malformed_rejection_internal_hyphenated", "malformed_rejection_text_affixed_numeric",
        "malformed_rejection_other_unknown",
        "malformed_role_explicit_payment_context", "malformed_role_date_like_numeric",
        "malformed_role_identifier_like_numeric", "malformed_role_unresolved",
        "malformed_target_blocker_to_target_strong",
        "malformed_target_blocker_to_target_uncertain",
        "malformed_target_target_to_blocker_strong",
        "malformed_target_target_to_blocker_uncertain",
        "malformed_target_blocker_target_same_row",
        "malformed_target_blocker_target_same_column",
        "malformed_target_blocker_target_unrelated",
        "malformed_affix_alphabetic_prefix", "malformed_affix_alphabetic_suffix",
        "malformed_affix_kana_kanji_prefix", "malformed_affix_kana_kanji_suffix",
        "malformed_affix_numeric_surrounded_by_text",
        "malformed_affix_separator_boundary",
        "malformed_affix_single_token_mixed_text_numeric",
        "malformed_affix_multi_token_text_numeric_structure",
        "malformed_neighborhood_same_row_non_payment_text",
        "malformed_neighborhood_label_local_block_proxy",
        "malformed_neighborhood_target_local_block_proxy",
        "malformed_neighborhood_direct_payment_label_connection",
        "malformed_neighborhood_target_local_independent",
        "malformed_connectivity_payment_label_and_valid_target",
        "malformed_connectivity_payment_label_only",
        "malformed_connectivity_valid_target_only",
        "malformed_connectivity_no_local_payment_connection",
        "payment_role_evidence_complete", "materialization_stable", "evaluation_failed"}


def test_shadow_call_does_not_change_production_resolution():
    before = build_receipt_privacy_preview("病院 診療\n領収金額", ())
    evaluate_level2_payment_shadow(valid())
    assert build_receipt_privacy_preview("病院 診療\n領収金額", ()) == before
    assert before.status == "needs_review"


def test_synthetic_diagnostic_matrix_does_not_change_production_resolution():
    before = build_receipt_privacy_preview("病院 診療\n領収金額", ())
    for source in _synthetic_malformed_contrast_cases().values():
        evaluate_level2_payment_shadow(source)
    after = build_receipt_privacy_preview("病院 診療\n領収金額", ())
    assert after == before
    assert after.status == "needs_review"


def test_production_modules_do_not_import_level2_shadow():
    root = Path(__file__).resolve().parents[1] / "app"
    production = ("receipt_pipeline.py", "receipt_privacy_gate.py", "receipt_text_extraction.py")
    for name in production:
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        assert not any(isinstance(node, ast.ImportFrom)
                       and node.module == "app.medical_payment_level2_shadow"
                       for node in ast.walk(tree))
