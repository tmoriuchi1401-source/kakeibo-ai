"""Synthetic contracts for observer-only Level 2 root-cause diagnostics."""
from dataclasses import replace
import ast
import json
from pathlib import Path

import pytest

from app.medical_level2_failure_diagnostics import (
    SCHEMA_VERSION,
    observe_level2_failure_diagnostics,
)
from app.medical_ocr_observation_shadow import ReceiptImage, make_observation
from app.medical_payment_level2_shadow import evaluate_level2_payment_shadow
from app.medical_receipt_privacy import build_receipt_privacy_preview


def region(text, x, y, width=80, height=20, confidence=.96):
    return {"text": text, "confidence": confidence, "detection_confidence": .9,
            "polygon": [[x, y], [x + width, y], [x + width, y + height], [x, y + height]]}


def observation(*regions, issues=()):
    image = ReceiptImage("synthetic-private-unit", 1, b"synthetic-only")
    return make_observation(image, "synthetic", ("a" * 64,), 800, 1000,
                            list(regions), issues)


def categories(source):
    return [item.category for item in observe_level2_failure_diagnostics(source).label_failures]


def test_exact_fragment_component_is_identified_without_reconstruction_authority():
    source = observation(region("領収金", 100, 100))
    result = observe_level2_failure_diagnostics(source)
    assert categories(source) == ["EXACT_FRAGMENT_COMPONENT"]
    assert result.label_failures[0].matched_allowlist_label_id.startswith("strong-label-")
    assert evaluate_level2_payment_shadow(source).candidates == ()


def test_ambiguous_fragment_remains_unresolved():
    assert categories(observation(region("支払", 100, 100))) == ["UNRESOLVED"]


def test_adjacent_tokens_reconstruct_only_inside_observer():
    source = observation(
        region("領収", 100, 100, 70),
        region("金額", 180, 100, 70),
        region("1234円", 105, 135),
    )
    result = observe_level2_failure_diagnostics(source)
    assert categories(source) == ["MULTI_TOKEN_RECONSTRUCTABLE"] * 2
    assert {item.token_count for item in result.label_failures} == {2}
    assert {item.geometry_relation for item in result.label_failures} == {
        "SAME_ROW_ADJACENT"
    }
    assert result.relations[0].anchor_kind == "OBSERVER_RECONSTRUCTED"
    assert result.relations[0].category == "SAME_COLUMN_STRONG"
    assert evaluate_level2_payment_shadow(source).candidates == ()


def test_one_character_substitution_is_likely_ocr_artifact():
    source = observation(region("領収金顕", 100, 100), region("1234円", 100, 135))
    result = observe_level2_failure_diagnostics(source)
    assert categories(source) == ["OCR_SUBSTITUTION_LIKELY"]
    assert result.label_failures[0].edit_distance == 1
    assert result.label_failures[0].normalized_edit_distance == .25


def test_decoration_affix_is_removed_only_in_observer():
    source = observation(region("【領収金額】", 100, 100), region("1234円", 100, 135))
    assert categories(source) == ["DECORATION_AFFIX"]
    assert evaluate_level2_payment_shadow(source).candidates == ()


def test_distinct_payment_vocabulary_is_true_unsupported():
    source = observation(region("合計請求額", 100, 100), region("1234円", 100, 135))
    assert categories(source) == ["TRUE_UNSUPPORTED_LABEL"]


@pytest.mark.parametrize("text", ["合計", "領収に関する長い説明文は金額ラベルではありません"])
def test_generic_context_or_prose_is_unresolved_not_a_coverage_gap(text):
    assert categories(observation(region(text, 100, 100))) == ["UNRESOLVED"]


@pytest.mark.parametrize(("number", "expected"), [
    (region("1234円", 200, 100), "SAME_ROW_STRONG"),
    (region("1234円", 100, 135), "SAME_COLUMN_STRONG"),
    (region("1234円", 100, 165), "NEAREST_NUMERIC_BUT_WEAK"),
    (region("1234円", 500, 500), "GEOMETRY_SCOPE_MISMATCH"),
])
def test_relation_geometry_categories(number, expected):
    source = observation(region("領収金額", 100, 100), number)
    diagnostic = observe_level2_failure_diagnostics(source).relations[0]
    assert diagnostic.category == expected
    assert diagnostic.normalized_gap_ratio is not None
    assert diagnostic.row_alignment is not None
    assert diagnostic.column_alignment is not None


def test_equal_and_unequal_competitors_are_separate():
    equal = observation(
        region("領収金額", 100, 100),
        region("1234円", 100, 135),
        region("1234円", 100, 170),
    )
    unequal = observation(
        region("領収金額", 100, 100),
        region("1234円", 100, 135),
        region("5678円", 100, 170),
    )
    equal_result = observe_level2_failure_diagnostics(equal).relations[0]
    unequal_result = observe_level2_failure_diagnostics(unequal).relations[0]
    assert equal_result.category == "MULTIPLE_EQUAL_COMPETITORS"
    assert equal_result.same_amount_count == 2
    assert equal_result.competing_amount_count == 0
    assert unequal_result.category == "MULTIPLE_UNEQUAL_COMPETITORS"
    assert unequal_result.competing_amount_count == 1


def test_negative_context_is_reported_after_strong_relation():
    source = observation(
        region("領収金額", 100, 100),
        region("1234円", 100, 135),
        region("保険負担額", 190, 135),
    )
    diagnostic = observe_level2_failure_diagnostics(source).relations[0]
    assert diagnostic.category == "NEGATIVE_CONTEXT_INTERFERENCE"
    assert diagnostic.negative_context_proximity == "TARGET_LOCAL"
    assert diagnostic.negative_context_count == 1
    assert evaluate_level2_payment_shadow(source).blocked_negative_context_count == 1


def test_missing_and_malformed_numeric_are_distinguished():
    absent = observe_level2_failure_diagnostics(
        observation(region("領収金額", 100, 100))
    ).relations[0]
    malformed = observe_level2_failure_diagnostics(
        observation(region("領収金額", 100, 100), region("12A4円", 100, 135))
    ).relations[0]
    assert absent.category == "NUMERIC_NOT_OBSERVED"
    assert malformed.category == "UNRESOLVED"


def test_local_unparsed_numeric_blocks_apparent_strong_success():
    source = observation(
        region("領収金額", 100, 100),
        region("1234円", 100, 135),
        region("自費 12A4円", 190, 135, 110),
    )
    diagnostic = observe_level2_failure_diagnostics(source).relations[0]
    assert diagnostic.category == "UNRESOLVED"
    assert diagnostic.unparsed_numeric_count == 1
    assert evaluate_level2_payment_shadow(source).candidates == ()


def test_result_schema_and_repr_do_not_retain_private_values():
    private = ("患者秘密", "私設医療機関", "987654", "source-private.png")
    source = observation(
        region("患者秘密領収金顕", 123, 456, 91, 17),
        region("987654円", 130, 490, 88, 19),
    )
    result = observe_level2_failure_diagnostics(source)
    rendered = repr(result) + json.dumps(result.aggregate(), ensure_ascii=False)
    assert result.schema_version == SCHEMA_VERSION
    assert all(value not in rendered for value in private)
    assert "123" not in rendered and "456" not in rendered and "490" not in rendered
    assert not hasattr(result, "candidates") and not hasattr(result, "amount")


@pytest.mark.parametrize("source", [
    observation(region("領収金額", 100, 100), region("1234円", 100, 135)),
    observation(region("領収", 100, 100, 70), region("金額", 180, 100, 70),
                region("1234円", 100, 135)),
    observation(region("領収金額", 100, 100), region("1234円", 100, 135),
                region("保険負担額", 190, 135)),
])
def test_level2_output_is_invariant_before_and_after_observation(source):
    before = evaluate_level2_payment_shadow(source)
    observe_level2_failure_diagnostics(source)
    after = evaluate_level2_payment_shadow(source)
    assert before.aggregate() == after.aggregate()
    assert [replace(item, candidate_id="opaque") for item in before.candidates] == [
        replace(item, candidate_id="opaque") for item in after.candidates
    ]


def test_production_privacy_output_is_invariant():
    before = build_receipt_privacy_preview("病院 診療\n領収金額", ())
    observe_level2_failure_diagnostics(
        observation(region("領収金額", 100, 100), region("1234円", 100, 135))
    )
    assert build_receipt_privacy_preview("病院 診療\n領収金額", ()) == before


def test_production_modules_do_not_import_failure_observer():
    root = Path(__file__).resolve().parents[1] / "app"
    observer = "app.medical_level2_failure_diagnostics"
    for path in root.glob("*.py"):
        if path.name == "medical_level2_failure_diagnostics.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not any(
            isinstance(node, ast.ImportFrom) and node.module == observer
            for node in ast.walk(tree)
        )
