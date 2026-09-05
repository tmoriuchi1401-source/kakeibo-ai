from dataclasses import FrozenInstanceError

import pytest

from app.medical_payment_evidence import collect_payment_evidence, resolve_payment_evidence
from app.medical_receipt_privacy import (
    _StructuredOcrToken as Token, ReceiptPrivacyPreview, SafeModelValidationError,
    build_receipt_privacy_preview,
)


def token(text, x=10, *, confidence=96, y=20, line=1, page=1):
    return Token(text, page, x, y, 50, 12, confidence, (1, 1, line, 5))


def preview(text="病院 診療", tokens=(), **kwargs):
    return build_receipt_privacy_preview(text, tokens, **kwargs)


@pytest.mark.parametrize(
    "competitor, code",
    [
        (token("3400", 180, confidence=69), "amount_observation_low_confidence"),
        (token("3.400", 180), "ambiguous_numeric_observations"),
        (token("1,23,456", 180), "ambiguous_numeric_observations"),
        (token("-3400", 180), "ambiguous_numeric_observations"),
        (token("3O00", 180), "ambiguous_numeric_observations"),
        (token("???円", 180), "ambiguous_numeric_observations"),
        (token("3400", 180), "ambiguous_numeric_observations"),
    ],
)
@pytest.mark.parametrize("text", ["病院 診療", "病院 診療\n支払額 1200円"])
def test_unresolved_competitor_cannot_disappear_or_be_bypassed_by_text(competitor, code, text):
    result = preview(text, (token("支払額"), token("1200円", 100), competitor))
    assert result.status == "needs_review"
    assert result.payment_amount is None
    assert code in result.diagnostic_codes
    assert result.gemini_allowed is False


def test_text_currency_amount_does_not_hide_structured_bare_numeric_ambiguity():
    result = preview("病院 診療\n支払額 1200円 3400",
                     (token("支払額"), token("1200円", 100), token("3400", 180)))
    assert result.status == "needs_review"
    assert "ambiguous_numeric_observations" in result.diagnostic_codes


@pytest.mark.parametrize("competitor", ["3400", "3.400", "3O00", "-3400", "???円"])
def test_text_only_keeps_bare_or_malformed_numeric_observations(competitor):
    result = preview("病院 診療\n支払額 1200円 " + competitor)
    assert result.status == "needs_review"
    assert "ambiguous_numeric_observations" in result.diagnostic_codes


def test_low_confidence_structured_region_cannot_be_bypassed_by_text():
    result = preview("病院 診療\n支払額 1200円",
                     (token("支払額", confidence=20), token("1200円", 100, confidence=20)))
    assert result.status == "needs_review"
    assert "amount_observation_low_confidence" in result.diagnostic_codes


@pytest.mark.parametrize("text", ["病院 診療", "病院 診療\n支払額 1200円"])
def test_line_key_does_not_override_incompatible_vertical_geometry(text):
    result = preview(text, (token("支払額"), token("1200円", 100, y=500)))
    assert result.status == "needs_review"
    assert "structural_relationship_unresolved" in result.diagnostic_codes


@pytest.mark.parametrize("extra", ["自費 500円", "ご請求額 1700円", "領収金額 1700円", "合計 1700円"])
def test_partial_responsibility_cannot_ignore_other_payment_hypothesis(extra):
    result = preview("病院 診療\n自己負担額 1200円\n" + extra)
    assert result.status == "needs_review"
    assert result.payment_amount is None


def test_partial_responsibility_competition_in_structured_only_channel():
    result = preview(tokens=(
        token("自己負担額"), token("1200", 100),
        token("自費", line=2, y=40), token("500", 100, line=2, y=40),
    ))
    assert result.status == "needs_review"


@pytest.mark.parametrize("unrelated", ["患者番号 A12.34", "診療点数 3.400", "保険者請求額 3.400円"])
def test_unrelated_negative_observation_is_retained_without_global_veto(unrelated):
    tokens = (token("支払額"), token("1200円", 100),
              token(unrelated, confidence=20, line=2, y=80))
    text = "病院 診療\n支払額 1200円\n" + unrelated
    evidence = collect_payment_evidence(text, tokens)
    assert any(r.observations and not r.payment_relevant for r in evidence.regions)
    result = resolve_payment_evidence(evidence)
    assert result.status == "confirmed"
    assert result.amount == 1200
    assert result.candidate_count == 1
    assert not result.diagnostic_codes


def test_same_observation_across_channels_is_not_two_independent_votes():
    evidence = collect_payment_evidence(
        "病院 診療\n支払額 1200円", (token("支払額"), token("1200円", 100)))
    assert {r.observation_group for r in evidence.regions} == {0}
    result = resolve_payment_evidence(evidence)
    assert result.status == "confirmed"
    assert result.candidate_count == 1
    assert result.reason_code == "unique_strong_candidate"


def test_unmatched_text_proposal_is_not_assumed_to_share_structured_region():
    result = preview("病院 診療\n支払額 1200円", (token("患者番号"), token("1234", 100)))
    assert result.status == "needs_review"
    assert "observation_incomplete" in result.diagnostic_codes


def test_equal_amount_and_role_do_not_establish_cross_channel_correspondence():
    result = preview("病院 診療\n今回 支払額 1200円",
                     (token("支払額"), token("1200円", 100)))
    assert result.status == "needs_review"
    assert "observation_incomplete" in result.diagnostic_codes


def test_repeated_identical_lines_are_not_uniquely_aligned():
    result = preview("病院 診療\n支払額 1200円\n支払額 1200円",
                     (token("支払額"), token("1200円", 100)))
    assert result.status == "needs_review"
    assert "observation_incomplete" in result.diagnostic_codes


@pytest.mark.parametrize(
    "text,tokens,complete,code",
    [
        ("病院 診療", (), True, "payment_label_not_observed"),
        ("病院 診療\n支払額", (), True, "amount_not_observed"),
        ("病院 診療", (token("支払額"), token("1200", 100, confidence=69)),
         True, "amount_observation_low_confidence"),
        ("病院 診療\n支払額 1200円 3400", (), True, "ambiguous_numeric_observations"),
        ("病院 診療", (token("支払額"), token("1200", 100, y=500)),
         True, "structural_relationship_unresolved"),
        ("病院 診療\n支払額 1200円\n領収金額 3400円", (), True, "conflicting_payment_candidates"),
        ("病院 診療\n支払額 1200円", (), False, "observation_incomplete"),
    ],
)
def test_fixed_internal_taxonomy_preserves_public_shape(text, tokens, complete, code):
    result = preview(text, tokens, observation_complete=complete)
    assert result.status == "needs_review"
    assert code in result.diagnostic_codes
    assert "diagnostic_codes" not in result.model_dump()
    assert "diagnostic_codes" not in repr(result)


def test_rejected_observations_do_not_store_raw_strings_or_numeric_values():
    marker = "SYNTHETIC_PRIVATE_MARKER"
    evidence = collect_payment_evidence(
        "病院 診療\n支払額 1200円 " + marker,
        (token("支払額"), token("1200円", 100), token(marker + "3.400", 180)),
    )
    observed = [o for r in evidence.regions for o in r.observations]
    assert {o.state for o in observed} >= {"valid_numeric", "malformed_numeric"}
    assert all(not hasattr(o, "text") and not hasattr(o, "amount") for o in observed)
    result = resolve_payment_evidence(evidence)
    assert marker not in repr(evidence)
    assert marker not in repr(evidence.regions)
    assert marker not in result.model_dump_json()
    with pytest.raises(FrozenInstanceError):
        evidence.observation_complete = True


def test_diagnostics_reject_free_text_without_exposure():
    with pytest.raises(SafeModelValidationError) as captured:
        ReceiptPrivacyPreview.safe_validate({
            "classification": "medical", "status": "needs_review", "reason_code": "no_candidate",
            "category": "医療費", "diagnostic_codes": ["SYNTHETIC_PRIVATE_MARKER"],
        })
    assert "SYNTHETIC_PRIVATE_MARKER" not in str(captured.value)


def test_exclusion_cannot_remove_legacy_conflict_and_increase_confirmation():
    result = preview("病院 診療\n支払額 1200円\n小計 支払額 3400円")
    assert result.status == "needs_review"
    assert "conflicting_payment_candidates" in result.diagnostic_codes


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
def test_malformed_payment_geometry_fails_closed(bad):
    result = preview("病院 診療\n支払額 1200円",
                     (token("支払額"), token("1200", 100, y=bad)))
    assert result.status == "needs_review"
    assert "observation_incomplete" in result.diagnostic_codes
