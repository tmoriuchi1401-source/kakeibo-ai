"""Synthetic positive/negative contracts for Level 2 rescue comparison."""
from dataclasses import replace
import ast
import json
from pathlib import Path

from app.medical_ocr_observation_shadow import ReceiptImage, make_observation
from app.medical_payment_level2_shadow import evaluate_level2_payment_shadow
from app.medical_payment_rescue_shadow import (
    SCHEMA_VERSION,
    evaluate_materialization_stable_boundary_shadow,
    evaluate_payment_rescue_shadow,
    evaluate_stable_coherent_form,
)
from app.medical_receipt_privacy import build_receipt_privacy_preview


def region(text, x, y, width=40, height=20, confidence=.96):
    return {
        "text": text,
        "confidence": confidence,
        "detection_confidence": .9,
        "polygon": [[x, y], [x + width, y], [x + width, y + height], [x, y + height]],
    }


def observation(*regions, unit="synthetic-unit"):
    image = ReceiptImage(unit, 1, b"synthetic-only-" + unit.encode())
    return make_observation(image, "synthetic", ("a" * 64,), 800, 1000, list(regions))


def test_boundary_reconstruction_is_exact_and_shadow_only():
    source = observation(
        region("領収", 100, 100), region("金額", 145, 100),
        region("1234円", 100, 130, width=70),
    )
    result = evaluate_payment_rescue_shadow(source)
    assert result.boundary_observation_count == 1
    assert result.boundary_exact_reconstruction_count == 1
    assert result.boundary_high_confidence_count == 1
    assert result.boundary_unique_numeric_count == 1
    assert result.boundary_strong_relation_count == 1
    assert result.boundary_competitor_free_count == 1
    assert result.boundary_pre_stability_positive_count == 1
    assert result.boundary_safe_positive_count == 0
    assert result.boundary_veto_materialization_unverified_count == 1
    assert result.existing_level2_candidate_count == 0
    assert evaluate_level2_payment_shadow(source).candidates == ()


def test_single_region_separator_boundary_reconstructs_only_to_exact_allowlist():
    source = observation(
        region("領収/金額", 100, 100, width=90),
        region("1234円", 100, 135, width=70),
    )
    result = evaluate_payment_rescue_shadow(source)
    assert result.boundary_exact_reconstruction_count == 1
    assert result.boundary_separator_observation_count == 1
    assert result.boundary_pre_stability_positive_count == 1
    assert result.boundary_safe_positive_count == 0
    assert result.existing_level2_candidate_count == 0


def test_boundary_reconstruction_rejects_nonexact_and_reversed_fragments():
    nonexact = observation(
        region("領収", 100, 100), region("支払", 145, 100),
        region("1234円", 100, 130, width=70),
    )
    reversed_source = observation(
        region("金額", 100, 100), region("領収", 145, 100),
        region("1234円", 100, 130, width=70),
    )
    assert evaluate_payment_rescue_shadow(nonexact).boundary_exact_reconstruction_count == 0
    assert evaluate_payment_rescue_shadow(reversed_source).boundary_exact_reconstruction_count == 0
    assert evaluate_payment_rescue_shadow(nonexact).boundary_veto_non_exact_count == 1
    assert evaluate_payment_rescue_shadow(reversed_source).boundary_veto_non_exact_count == 1


def test_boundary_multiple_numeric_observations_never_become_safe_positive():
    source = observation(
        region("領収", 100, 100), region("金額", 145, 100),
        region("1234円", 100, 130, width=70),
        region("5678円", 100, 160, width=70),
    )
    result = evaluate_payment_rescue_shadow(source)
    assert result.boundary_exact_reconstruction_count == 1
    assert result.boundary_numeric_blocker_count == 1
    assert result.boundary_safe_positive_count == 0


def test_boundary_uncertain_relation_is_a_veto_not_positive_evidence():
    source = observation(
        region("領収", 100, 100), region("金額", 145, 100),
        region("1234円", 100, 165, width=70),
    )
    result = evaluate_payment_rescue_shadow(source)
    assert result.boundary_exact_reconstruction_count == 1
    assert result.boundary_unique_numeric_count == 1
    assert result.boundary_strong_relation_count == 0
    assert result.boundary_veto_uncertain_relation_count == 1
    assert result.boundary_pre_stability_positive_count == 0


def test_multiple_boundary_pairings_are_ambiguous_and_fail_closed():
    source = observation(
        region("領収", 100, 100), region("金額", 145, 100),
        region("領収", 100, 160), region("金額", 145, 160),
        region("1234円", 100, 190, width=70),
    )
    result = evaluate_payment_rescue_shadow(source)
    assert result.boundary_exact_reconstruction_count == 2
    assert result.boundary_ambiguous_reconstruction_count == 1
    assert result.boundary_competitor_free_count == 0
    assert result.boundary_pre_stability_positive_count == 0


def test_intervening_fragment_prevents_boundary_merge():
    source = observation(
        region("領収", 100, 100), region("商品", 145, 100), region("金額", 190, 100),
        region("1234円", 100, 130, width=70),
    )
    result = evaluate_payment_rescue_shadow(source)
    assert result.boundary_exact_reconstruction_count == 0
    assert result.geometry_exact_reconstruction_count == 0


def test_unrelated_fragment_is_not_a_boundary_observation():
    source = observation(
        region("領収", 100, 100), region("商品", 145, 100),
        region("1234円", 100, 130, width=70),
    )
    result = evaluate_payment_rescue_shadow(source)
    assert result.boundary_observation_count == 0
    assert result.boundary_exact_reconstruction_count == 0
    assert result.boundary_safe_positive_count == 0


def test_local_negative_context_blocks_otherwise_strong_boundary_pair():
    source = observation(
        region("領収", 100, 100), region("金額", 145, 100),
        region("点数", 190, 100), region("1234円", 100, 130, width=70),
    )
    result = evaluate_payment_rescue_shadow(source)
    assert result.boundary_strong_relation_count == 1
    assert result.boundary_competitor_free_count == 0
    assert result.boundary_veto_competitor_count == 1
    assert result.boundary_safe_positive_count == 0


def test_broader_geometry_reconstruction_is_observed_but_not_authorized():
    source = observation(
        region("領収", 100, 100), region("金額", 180, 100),
        region("1234円", 100, 145, width=70),
    )
    result = evaluate_payment_rescue_shadow(source)
    assert result.boundary_exact_reconstruction_count == 0
    assert result.geometry_exact_reconstruction_count == 1
    assert result.geometry_strong_relation_count == 1
    assert result.boundary_safe_positive_count == 0
    assert result.existing_level2_candidate_count == 0


def test_vertical_fragmentation_uses_existing_geometry_bucket_only():
    source = observation(
        region("領収", 100, 100, width=60), region("金額", 100, 135, width=60),
        region("1234円", 100, 170, width=70),
    )
    result = evaluate_payment_rescue_shadow(source)
    assert result.boundary_exact_reconstruction_count == 0
    assert result.geometry_exact_reconstruction_count == 1
    assert result.geometry_strong_relation_count == 1
    assert result.boundary_safe_positive_count == 0


def test_geometry_uncertain_numeric_relation_is_never_strong():
    source = observation(
        region("領収", 100, 100), region("金額", 180, 100),
        region("1234円", 100, 165, width=70),
    )
    result = evaluate_payment_rescue_shadow(source)
    assert result.geometry_exact_reconstruction_count == 1
    assert result.geometry_strong_relation_count == 0
    assert result.geometry_uncertain_relation_count == 1


def test_multiple_geometry_pairings_record_overmerge_risk():
    source = observation(
        region("領収", 100, 100), region("金額", 180, 100),
        region("領収", 100, 160), region("金額", 180, 160),
        region("1234円", 100, 205, width=70),
    )
    result = evaluate_payment_rescue_shadow(source)
    assert result.geometry_exact_reconstruction_count == 2
    assert result.geometry_overmerge_risk_count >= 1
    assert result.boundary_safe_positive_count == 0


def test_coherent_unsupported_form_is_diagnostic_only():
    source = observation(region("領収請求", 100, 100, width=80),
                         region("1234円", 100, 135, width=70))
    result = evaluate_payment_rescue_shadow(source)
    assert result.coherent_unsupported_count == 1
    assert result.coherent_strong_relation_count == 1
    assert result.existing_level2_candidate_count == 0
    assert not hasattr(result, "amount") and not hasattr(result, "candidate")


def test_coherent_form_with_multiple_numbers_is_blocked():
    source = observation(
        region("領収請求", 100, 100, width=80),
        region("1234円", 100, 135, width=70),
        region("5678円", 100, 170, width=70),
    )
    result = evaluate_payment_rescue_shadow(source)
    assert result.coherent_numeric_blocker_count == 1
    assert result.coherent_strong_relation_count == 1


def test_coherent_form_requires_exact_materialization_stability():
    first = observation(region("領収請求", 100, 100, width=80),
                        region("1234円", 100, 135, width=70), unit="first")
    second = observation(region("領収請求", 102, 101, width=80),
                         region("1234円", 102, 136, width=70), unit="second")
    stable = evaluate_stable_coherent_form((first, second))
    assert stable.stable_exact_form_count == 1
    assert stable.stable_strong_relation_count == 1
    assert stable.materialization_conflict_count == 0


def test_coherent_form_mismatch_is_conflict_not_fuzzy_accept():
    first = observation(region("領収請求", 100, 100, width=80),
                        region("1234円", 100, 135, width=70), unit="first")
    second = observation(region("領収請求控", 100, 100, width=90),
                         region("1234円", 100, 135, width=70), unit="second")
    result = evaluate_stable_coherent_form((first, second))
    assert result.stable_exact_form_count == 0
    assert result.materialization_conflict_count == 2


def test_low_confidence_fragment_fails_closed_without_reconstruction():
    source = observation(region("領収", 100, 100, confidence=.96),
                         region("金額", 145, 100, confidence=.50))
    result = evaluate_payment_rescue_shadow(source)
    assert result.boundary_exact_reconstruction_count == 1
    assert result.boundary_high_confidence_count == 0
    assert result.boundary_veto_low_confidence_count == 1
    assert result.boundary_safe_positive_count == 0


def test_boundary_safe_positive_requires_stable_materializations():
    first = observation(
        region("領収", 100, 100), region("金額", 145, 100),
        region("1234円", 100, 130, width=70), unit="first-boundary",
    )
    second = observation(
        region("領収", 102, 101), region("金額", 147, 101),
        region("1234円", 102, 131, width=70), unit="second-boundary",
    )
    result = evaluate_materialization_stable_boundary_shadow(
        (first, second), same_source_confirmed=True
    )
    assert result.pre_stability_positive_count == 2
    assert result.safe_positive_count == 1
    assert result.materialization_conflict_count == 0


def test_boundary_materialization_mismatch_fails_closed():
    first = observation(
        region("領収", 100, 100), region("金額", 145, 100),
        region("1234円", 100, 130, width=70), unit="first-mismatch",
    )
    second = observation(
        region("領収", 100, 100), region("金額", 145, 100),
        region("1235円", 100, 130, width=70), unit="second-mismatch",
    )
    result = evaluate_materialization_stable_boundary_shadow(
        (first, second), same_source_confirmed=True
    )
    assert result.safe_positive_count == 0
    assert result.materialization_conflict_count == 1


def test_fixed_aggregate_does_not_expose_text_amount_geometry_or_identity():
    marker = "SYNTHETIC_PRIVATE_MARKER"
    source = observation(
        region(marker + "領収請求", 123, 456, width=91),
        region("987654円", 130, 490, width=88), unit="private-source-name.png",
    )
    result = evaluate_payment_rescue_shadow(source)
    rendered = repr(result) + json.dumps(result.aggregate(), ensure_ascii=False)
    assert result.schema_version == SCHEMA_VERSION
    assert marker not in rendered and "987654" not in rendered
    assert "123" not in rendered and "456" not in rendered and "private-source-name" not in rendered
    assert set(result.aggregate()) == {
        "schema_version", "boundary_observation_count",
        "boundary_separator_observation_count", "boundary_adjacent_pair_observation_count",
        "boundary_exact_reconstruction_count", "boundary_high_confidence_count",
        "boundary_unique_numeric_count", "boundary_strong_relation_count",
        "boundary_competitor_free_count", "boundary_pre_stability_positive_count",
        "boundary_safe_positive_count", "boundary_ambiguous_reconstruction_count",
        "boundary_numeric_blocker_count", "boundary_veto_incomplete_count",
        "boundary_veto_non_exact_count", "boundary_veto_low_confidence_count",
        "boundary_veto_numeric_not_unique_count", "boundary_veto_uncertain_relation_count",
        "boundary_veto_unrelated_relation_count", "boundary_veto_competitor_count",
        "boundary_veto_ambiguous_reconstruction_count",
        "boundary_veto_existing_level2_candidate_count",
        "boundary_veto_materialization_unverified_count",
        "geometry_exact_reconstruction_count",
        "geometry_strong_relation_count", "geometry_overmerge_risk_count",
        "geometry_uncertain_relation_count", "coherent_unsupported_count",
        "coherent_strong_relation_count", "coherent_numeric_blocker_count",
        "observation_complete", "existing_level2_candidate_count", "evaluation_failed",
    }

    boundary = evaluate_payment_rescue_shadow(observation(
        region("領収", 100, 100), region("金額", 145, 100),
        region("987654円", 100, 130, width=88), unit="private-boundary-source.png",
    ))
    boundary_rendered = repr(boundary) + json.dumps(boundary.aggregate(), ensure_ascii=False)
    assert "領収" not in boundary_rendered and "金額" not in boundary_rendered
    assert "987654" not in boundary_rendered and "private-boundary-source" not in boundary_rendered


def test_level2_result_is_invariant_before_and_after_rescue_observation():
    source = observation(
        region("領収", 100, 100), region("金額", 145, 100),
        region("1234円", 100, 130, width=70),
    )
    before = evaluate_level2_payment_shadow(source)
    evaluate_payment_rescue_shadow(source)
    after = evaluate_level2_payment_shadow(source)
    assert before.aggregate() == after.aggregate()
    assert [replace(item, candidate_id="opaque") for item in before.candidates] == [
        replace(item, candidate_id="opaque") for item in after.candidates
    ]


def test_production_privacy_preview_is_invariant_after_boundary_observation():
    source = observation(
        region("領収", 100, 100), region("金額", 145, 100),
        region("1234円", 100, 130, width=70),
    )
    before = build_receipt_privacy_preview("病院 診療\n領収金額", ())
    evaluate_payment_rescue_shadow(source)
    after = build_receipt_privacy_preview("病院 診療\n領収金額", ())
    assert after == before
    assert after.status == "needs_review"


def test_production_modules_do_not_import_rescue_shadow():
    root = Path(__file__).resolve().parents[1] / "app"
    production = ("receipt_pipeline.py", "receipt_privacy_gate.py", "receipt_text_extraction.py",
                  "medical_receipt_privacy.py", "medical_payment_evidence.py")
    for name in production:
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        assert not any(
            isinstance(node, ast.ImportFrom)
            and node.module == "app.medical_payment_rescue_shadow"
            for node in ast.walk(tree)
        )
