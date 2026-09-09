"""Synthetic-only contracts for anonymous pre-candidate diagnostics."""
from dataclasses import replace
import ast
import json
from pathlib import Path

import pytest

from app.medical_ocr_observation_shadow import ReceiptImage, make_observation
from app.medical_payment_level2_shadow import evaluate_level2_payment_shadow
from app.medical_payment_pre_candidate_diagnostics import (
    SCHEMA_VERSION,
    observe_pre_candidate_diagnostics,
)
from app.medical_receipt_privacy import build_receipt_privacy_preview


def region(text, x, y, width=80, height=20, confidence=.96, **changes):
    value = {
        "text": text,
        "confidence": confidence,
        "detection_confidence": .9,
        "polygon": [[x, y], [x + width, y], [x + width, y + height], [x, y + height]],
    }
    value.update(changes)
    return value


def observation(*regions, issues=()):
    image = ReceiptImage("synthetic-private-unit", 1, b"synthetic-only")
    return make_observation(image, "synthetic", ("a" * 64,), 800, 1000, list(regions), issues)


def test_exact_label_is_observed_without_candidate_authority():
    source = observation(region("領収金額", 100, 100), region("1234円", 100, 135))
    result = observe_pre_candidate_diagnostics(source)
    assert result.label.exact_match_count == 1
    assert not hasattr(result, "candidates") and not hasattr(result, "amount")


def test_fragmented_label_regions_are_anonymous_partial_observations():
    source = observation(region("領収", 100, 100), region("金額", 185, 100),
                         region("1234円", 100, 135))
    result = observe_pre_candidate_diagnostics(source)
    assert result.label.exact_match_count == 0
    assert result.label.partial_or_fragment_match_count == 2


def test_partial_normalized_label_shape_is_counted_without_becoming_exact():
    source = observation(region("領 収 金", 100, 100), region("1234円", 100, 135))
    result = observe_pre_candidate_diagnostics(source)
    assert result.label.partial_or_fragment_match_count == 1
    assert result.label.normalization_near_match_count == 1
    assert evaluate_level2_payment_shadow(source).candidates == ()


def test_low_confidence_exact_label_is_observed_but_not_authorized():
    source = observation(region("支払額", 100, 100, confidence=.89),
                         region("1234円", 100, 135))
    result = observe_pre_candidate_diagnostics(source)
    assert result.label.exact_match_count == 0
    assert result.label.low_confidence_match_count == 1
    assert evaluate_level2_payment_shadow(source).candidates == ()


def test_unsupported_payment_label_shape_is_only_diagnostic():
    source = observation(region("領収請求", 100, 100), region("1234円", 100, 135))
    result = observe_pre_candidate_diagnostics(source)
    assert result.label.unsupported_label_shape_count == 1
    assert result.label.exact_match_count == 0
    assert evaluate_level2_payment_shadow(source).candidates == ()


def test_contiguous_unsupported_shape_has_single_token_and_fragment():
    result = observe_pre_candidate_diagnostics(
        observation(region("領収請求", 100, 100))
    ).unsupported_shape
    assert result.observation_count == 1
    assert result.contiguous_count == 1
    assert result.token_count_one == 1
    assert result.fragment_count_one == 1


def test_fragmented_unsupported_shape_records_explicit_whitespace_split():
    result = observe_pre_candidate_diagnostics(
        observation(region("領収 請求", 100, 100))
    ).unsupported_shape
    assert result.fragmented_count == 1
    assert result.token_count_two == 1
    assert result.fragment_count_two == 1
    assert result.separator_or_whitespace_split_count == 1


@pytest.mark.parametrize(("text", "field"), [
    ("領収金額控", "prefix_only_count"),
    ("控領収金額", "suffix_only_count"),
    ("控領収金額再", "interior_fragment_count"),
])
def test_allowlist_fragment_position_is_anonymous(text, field):
    result = observe_pre_candidate_diagnostics(
        observation(region(text, 100, 100))
    ).unsupported_shape
    assert getattr(result, field) == 1


def test_separator_split_is_observed_without_normalizing_to_an_exact_label():
    source = observation(region("領収/請求", 100, 100))
    result = observe_pre_candidate_diagnostics(source)
    assert result.unsupported_shape.separator_or_whitespace_split_count == 1
    assert result.unsupported_shape.fragment_count_two == 1
    assert result.label.exact_match_count == 0
    assert evaluate_level2_payment_shadow(source).candidates == ()


def test_geometry_split_bucket_counts_adjacent_label_like_regions_only():
    source = observation(
        region("領収請求", 100, 100),
        region("支払請求", 190, 100),
    )
    result = observe_pre_candidate_diagnostics(source).unsupported_shape
    assert result.observation_count == 2
    assert result.geometry_pair_count == 2
    assert result.geometry_isolated_count == 0


def test_multiple_explicit_fragments_use_bounded_many_bucket():
    result = observe_pre_candidate_diagnostics(
        observation(region("領収 / 請求 / 支払", 100, 100))
    ).unsupported_shape
    assert result.fragment_count_many == 1
    assert result.token_count_many == 1
    assert result.fragmented_count == 1


def test_mixed_character_class_is_shape_only_and_never_authorizes():
    source = observation(region("支払A1", 100, 100), region("1234円", 100, 135))
    result = observe_pre_candidate_diagnostics(source)
    assert result.unsupported_shape.mixed_character_class_count == 1
    assert evaluate_level2_payment_shadow(source).candidates == ()


def test_label_and_strong_numeric_relation_are_counted():
    source = observation(region("支払額", 100, 100), region("1234円", 100, 135))
    result = observe_pre_candidate_diagnostics(source)
    assert result.structure.numeric_observation_count == 1
    assert result.structure.strong_relation_count == 1
    assert result.structure.uncertain_relation_count == 0
    assert result.structure.unrelated_relation_count == 0
    assert result.competitors.competitor_count == 0
    assert result.structure.buckets[0].axis == "COLUMN_BELOW"


def test_label_and_uncertain_numeric_relation_are_counted_without_candidate():
    source = observation(region("支払額", 100, 100, height=20),
                         region("1234円", 100, 165, height=20))
    result = observe_pre_candidate_diagnostics(source)
    assert result.structure.strong_relation_count == 0
    assert result.structure.uncertain_relation_count == 1
    assert result.competitors.payment_connected_count == 1
    assert result.structure.buckets[0].distance == "UNCERTAIN_BAND"
    assert evaluate_level2_payment_shadow(source).candidates == ()


def test_multiple_numeric_competitors_have_anonymous_role_counts():
    source = observation(
        region("支払額", 100, 100),
        region("1234円", 100, 135),
        region("5678円", 100, 170),
        region("点数", 300, 200),
        region("999", 300, 225),
    )
    result = observe_pre_candidate_diagnostics(source)
    assert result.structure.numeric_observation_count == 3
    # The sole STRONG observation is the provisional target for diagnostic
    # partitioning only; the remaining two observations are competitors.
    assert result.competitors.competitor_count == 2
    assert result.competitors.payment_connected_count == 1
    assert result.competitors.label_connected_count == 1
    assert result.competitors.exclusion_role_count == 1
    assert result.competitors.ambiguous_role_count == 0
    assert evaluate_level2_payment_shadow(source).candidates == ()


def test_target_connected_numeric_shape_is_visible_when_not_label_connected():
    source = observation(
        region("支払額", 20, 100),
        region("1234円", 140, 100, width=60),
        region("自費999円", 145, 165, width=80),
    )
    level2 = evaluate_level2_payment_shadow(source)
    result = observe_pre_candidate_diagnostics(source)
    assert level2.candidates == () and level2.unresolved_competitor_count == 1
    assert result.competitors.label_connected_count == 0
    assert result.competitors.target_connected_count == 1
    assert result.competitors.payment_connected_count == 1


@pytest.mark.parametrize(("raw", "field"), [
    ({"text": "", "confidence": .9, "detection_confidence": .9,
      "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]]}, "blank_region_count"),
    ({"text": None, "confidence": .9, "detection_confidence": .9,
      "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]]}, "recognition_missing_count"),
    ({"text": "支払額", "confidence": .9, "detection_confidence": .9,
      "polygon": []}, "invalid_geometry_count"),
    ({"text": "支払額", "confidence": None, "detection_confidence": .9,
      "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]]}, "invalid_confidence_count"),
])
def test_ocr_incomplete_breakdown_representative_categories(raw, field):
    result = observe_pre_candidate_diagnostics(observation(raw))
    assert getattr(result.incomplete, field) == 1
    assert result.observation_complete is False


def test_observation_level_incomplete_is_counted_as_other():
    source = observation(region("支払額", 100, 100), issues=("observation_incomplete",))
    result = observe_pre_candidate_diagnostics(source)
    assert result.incomplete.other_incomplete_count == 1
    assert result.observation_complete is False


def test_fixed_schema_and_repr_do_not_leak_text_amount_geometry_or_source_identity():
    private_values = ("患者秘密", "私設医療機関", "987654", "source-private-name.png")
    source = observation(
        region("患者秘密領収金額", 123, 456, 91, 17),
        region("987654円", 130, 490, 88, 19),
    )
    result = observe_pre_candidate_diagnostics(source)
    rendered = repr(result) + json.dumps(result.aggregate(), ensure_ascii=False)
    assert result.schema_version == SCHEMA_VERSION
    assert set(result.aggregate()) == {
        "schema_version", "exact_match_count", "partial_or_fragment_match_count",
        "low_confidence_match_count", "normalization_near_match_count",
        "unsupported_label_shape_count", "numeric_observation_count",
        "unsupported_shape_observation_count", "unsupported_token_count_one",
        "unsupported_token_count_two", "unsupported_token_count_many",
        "unsupported_contiguous_count", "unsupported_fragmented_count",
        "unsupported_fragment_count_one", "unsupported_fragment_count_two",
        "unsupported_fragment_count_many", "unsupported_allowlist_length_delta_zero",
        "unsupported_allowlist_length_delta_one", "unsupported_allowlist_length_delta_two",
        "unsupported_allowlist_length_delta_large", "unsupported_prefix_only_count",
        "unsupported_suffix_only_count", "unsupported_interior_fragment_count",
        "unsupported_no_allowlist_fragment_count",
        "unsupported_separator_or_whitespace_split_count",
        "unsupported_mixed_character_class_count", "unsupported_geometry_isolated_count",
        "unsupported_geometry_pair_count", "unsupported_geometry_multi_fragment_count",
        "unsupported_geometry_unknown_count", "unsupported_neighboring_token_count_zero",
        "unsupported_neighboring_token_count_one", "unsupported_neighboring_token_count_many",
        "strong_relation_count", "uncertain_relation_count", "unrelated_relation_count",
        "competitor_count", "payment_connected_count", "label_connected_count",
        "target_connected_count", "structurally_near_count",
        "exclusion_role_count", "ambiguous_role_count", "blank_region_count",
        "recognition_missing_count", "invalid_geometry_count", "invalid_confidence_count",
        "other_incomplete_count", "observation_complete", "evaluation_failed",
    }
    assert all(value not in rendered for value in private_values)
    assert "123" not in rendered and "456" not in rendered and "490" not in rendered


@pytest.mark.parametrize("source", [
    observation(region("領収金額", 100, 100), region("1234円", 100, 135)),
    observation(region("領収", 100, 100), region("金額", 185, 100),
                region("1234円", 100, 135)),
    observation(region("支払額", 100, 100), region("1234円", 100, 165)),
])
def test_level2_final_result_is_invariant_before_and_after_observation(source):
    before = evaluate_level2_payment_shadow(source)
    observe_pre_candidate_diagnostics(source)
    after = evaluate_level2_payment_shadow(source)
    # Candidate IDs are intentionally random handles, so compare every stable
    # decision field and the candidate contract rather than the opaque ID.
    assert before.aggregate() == after.aggregate()
    assert [replace(item, candidate_id="opaque") for item in before.candidates] == [
        replace(item, candidate_id="opaque") for item in after.candidates
    ]


def test_production_privacy_result_is_invariant():
    before = build_receipt_privacy_preview("病院 診療\n領収金額", ())
    observe_pre_candidate_diagnostics(
        observation(region("領収金額", 100, 100), region("1234円", 100, 135))
    )
    assert build_receipt_privacy_preview("病院 診療\n領収金額", ()) == before
    assert before.status == "needs_review"


def test_production_modules_do_not_import_pre_candidate_observer():
    root = Path(__file__).resolve().parents[1] / "app"
    production = ("receipt_pipeline.py", "receipt_privacy_gate.py", "receipt_text_extraction.py")
    for name in production:
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        assert not any(
            isinstance(node, ast.ImportFrom)
            and node.module == "app.medical_payment_pre_candidate_diagnostics"
            for node in ast.walk(tree)
        )
