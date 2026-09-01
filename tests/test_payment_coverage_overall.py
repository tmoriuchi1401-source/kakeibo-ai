from __future__ import annotations

from dataclasses import fields

import pytest

from app.payment_coverage_completion import (
    CoverageCompletionResult,
    NormalizedCoverageInterval,
    evaluate_payment_coverage_completion,
)
from app.payment_coverage_manifest import CoverageManifest
from app.payment_coverage_overall import (
    ProviderCoverageResult,
    evaluate_overall_payment_coverage,
)
from app.payment_coverage_status_preview import CoverageEvidence


START = "2025-01-01"
END = "2025-01-31"
SHA_A = "a" * 64
SHA_B = "b" * 64


def _completion(status: str) -> CoverageCompletionResult:
    if status == "complete":
        return CoverageCompletionResult(
            "complete",
            "provider_proven_coverage_complete",
            effective_proof_level="provider_proven",
            completeness_proven=True,
        )
    if status == "incomplete":
        return CoverageCompletionResult(
            "incomplete",
            "provider_proven_coverage_gap",
        )
    return CoverageCompletionResult("unknown", "provider_completeness_not_proven")


def _provider(
    provider: str,
    status: str = "complete",
    *,
    basis: str = "transaction_date",
    start: str = START,
    end: str = END,
) -> ProviderCoverageResult:
    return ProviderCoverageResult(
        provider=provider,
        coverage_basis=basis,
        required_start=start,
        required_end=end,
        applicability_status="applicable",
        applicability_reason="required_provider",
        completion=_completion(status),
    )


def _not_applicable(provider: str) -> ProviderCoverageResult:
    return ProviderCoverageResult(
        provider=provider,
        coverage_basis="transaction_date",
        required_start=START,
        required_end=END,
        applicability_status="not_applicable",
        applicability_reason="explicit_required_provider_configuration",
        completion=None,
        applicability_proven=True,
    )


def _unknown_applicability(provider: str) -> ProviderCoverageResult:
    return ProviderCoverageResult(
        provider=provider,
        coverage_basis="transaction_date",
        required_start=START,
        required_end=END,
        applicability_status="unknown",
        applicability_reason="applicability_not_established",
        completion=None,
    )


def _overall(
    results,
    *,
    required=("amazon", "paypay"),
    basis="transaction_date",
    start=START,
    end=END,
):
    return evaluate_overall_payment_coverage(
        results,
        required_providers=required,
        coverage_basis=basis,
        required_start=start,
        required_end=end,
    )


def _strict_completion(
    *,
    kind: str,
    claim: str,
    proven: bool,
    intervals: tuple[tuple[str, str, str], ...] = ((START, END, SHA_A),),
) -> CoverageCompletionResult:
    normalized = [
        NormalizedCoverageInterval(
            provider="paypay",
            coverage_basis="transaction_date",
            coverage_start=start,
            coverage_end=end,
            content_sha256=sha,
            operational_coverage="usable",
            attestation_kind=kind,
            attestation_claim=claim,
            provider_completeness_proven=proven,
        )
        for start, end, sha in intervals
    ]
    return evaluate_payment_coverage_completion(
        normalized,
        provider="paypay",
        coverage_basis="transaction_date",
        required_start=START,
        required_end=END,
    )


def _paypay_result(completion: CoverageCompletionResult) -> ProviderCoverageResult:
    return ProviderCoverageResult(
        provider="paypay",
        coverage_basis="transaction_date",
        required_start=START,
        required_end=END,
        applicability_status="applicable",
        applicability_reason="required_provider",
        completion=completion,
    )


def test_case_01_all_complete_is_complete() -> None:
    result = _overall([_provider("amazon"), _provider("paypay")])

    assert result.status == "complete"
    assert result.reason == "all_required_providers_complete"


def test_case_02_complete_and_not_applicable_is_complete() -> None:
    result = _overall([_provider("amazon"), _not_applicable("paypay")])

    assert result.status == "complete"
    assert result.reason == "required_provider_coverage_satisfied"


def test_case_03_complete_and_unknown_is_unknown() -> None:
    result = _overall([_provider("amazon"), _provider("paypay", "unknown")])

    assert result.status == "unknown"


def test_case_04_complete_and_incomplete_is_incomplete() -> None:
    result = _overall([_provider("amazon"), _provider("paypay", "incomplete")])

    assert result.status == "incomplete"


def test_case_05_unknown_and_incomplete_is_incomplete() -> None:
    result = _overall([
        _provider("amazon", "unknown"),
        _provider("paypay", "incomplete"),
    ])

    assert result.status == "incomplete"


def test_case_06_unknown_and_not_applicable_is_unknown() -> None:
    result = _overall([
        _provider("amazon", "unknown"),
        _not_applicable("paypay"),
    ])

    assert result.status == "unknown"


def test_case_07_incomplete_and_not_applicable_is_incomplete() -> None:
    result = _overall([
        _provider("amazon", "incomplete"),
        _not_applicable("paypay"),
    ])

    assert result.status == "incomplete"


def test_case_08_all_unknown_is_unknown() -> None:
    result = _overall([
        _provider("amazon", "unknown"),
        _provider("paypay", "unknown"),
    ])

    assert result.status == "unknown"


def test_case_09_all_not_applicable_is_not_applicable() -> None:
    result = _overall([_not_applicable("amazon"), _not_applicable("paypay")])

    assert result.status == "not_applicable"
    assert result.reason == "no_applicable_required_providers"


def test_case_10_empty_results_with_required_providers_is_unknown() -> None:
    result = _overall([])

    assert result.status == "unknown"
    assert result.status_counts.unknown == 2


def test_case_11_explicit_empty_required_set_is_not_applicable() -> None:
    result = _overall([], required=())

    assert result.status == "not_applicable"
    assert result.reason == "no_required_providers"


def test_case_12_missing_required_provider_is_unknown() -> None:
    result = _overall([_provider("amazon")])

    assert result.status == "unknown"
    assert result.reason == "required_provider_result_missing"
    assert "missing_required_providers=paypay" in (result.diagnostic or "")


def test_case_13_duplicate_provider_result_fails_closed() -> None:
    result = _overall([
        _provider("amazon"),
        _provider("paypay"),
        _provider("paypay"),
    ])

    assert result.status == "unknown"
    assert result.reason == "provider_result_validation_failed"
    assert "duplicate_provider_result=paypay" in (result.diagnostic or "")


def test_case_14_required_window_mismatch_fails_closed() -> None:
    result = _overall([
        _provider("amazon"),
        _provider("paypay", start="2025-01-02"),
    ])

    assert result.status == "unknown"
    assert "required_window_mismatch=paypay" in (result.diagnostic or "")


def test_case_15_coverage_basis_mismatch_fails_closed() -> None:
    result = _overall([
        _provider("amazon"),
        _provider("paypay", basis="message_date"),
    ])

    assert result.status == "unknown"
    assert "coverage_basis_mismatch=paypay" in (result.diagnostic or "")


def test_case_16_invalid_provider_status_is_rejected() -> None:
    invalid = CoverageCompletionResult("undefined", "invalid")

    with pytest.raises(ValueError, match="invalid_completion_status"):
        _paypay_result(invalid)


def test_case_17_non_required_provider_is_ignored_with_diagnostic() -> None:
    result = _overall(
        [_provider("amazon"), _provider("paypay", "incomplete")],
        required=("amazon",),
    )

    assert result.status == "complete"
    assert tuple(item.provider for item in result.provider_results) == ("amazon",)
    assert result.diagnostic == "ignored_non_required_providers=paypay"


def test_case_18_paypay_user_export_scope_keeps_overall_unknown() -> None:
    paypay = _strict_completion(
        kind="user_attested",
        claim="export_scope",
        proven=False,
    )
    result = _overall([_provider("amazon"), _paypay_result(paypay)])

    assert paypay.completion_status == "unknown"
    assert result.status == "unknown"


def test_case_19_paypay_provider_proof_and_amazon_complete_is_complete() -> None:
    paypay = _strict_completion(
        kind="provider_proven",
        claim="complete_for_range",
        proven=True,
    )
    result = _overall([_provider("amazon"), _paypay_result(paypay)])

    assert paypay.completion_status == "complete"
    assert result.status == "complete"


def test_case_20_paypay_provider_gap_and_amazon_unknown_is_incomplete() -> None:
    paypay = _strict_completion(
        kind="provider_proven",
        claim="complete_for_range",
        proven=True,
        intervals=(("2025-01-01", "2025-01-10", SHA_A),),
    )
    result = _overall([_provider("amazon", "unknown"), _paypay_result(paypay)])

    assert paypay.completion_status == "incomplete"
    assert result.status == "incomplete"


def test_case_21_provider_proven_no_activity_contributes_complete() -> None:
    paypay = _strict_completion(
        kind="provider_proven",
        claim="no_activity",
        proven=True,
        intervals=((START, END, SHA_A),),
    )
    result = _overall([_provider("amazon"), _paypay_result(paypay)])

    assert paypay.completion_status == "complete"
    assert result.status == "complete"


def test_case_22_user_attested_no_activity_contributes_unknown() -> None:
    paypay = _strict_completion(
        kind="user_attested",
        claim="no_activity",
        proven=False,
    )
    result = _overall([_provider("amazon"), _paypay_result(paypay)])

    assert paypay.completion_status == "unknown"
    assert result.status == "unknown"


def test_case_23_not_applicable_requires_explicit_evidence() -> None:
    with pytest.raises(ValueError, match="not_applicable_requires_explicit_evidence"):
        ProviderCoverageResult(
            provider="paypay",
            coverage_basis="transaction_date",
            required_start=START,
            required_end=END,
            applicability_status="not_applicable",
            applicability_reason="no_csv_found",
            completion=None,
            applicability_proven=False,
        )


def test_case_24_missing_provider_is_unknown_not_not_applicable() -> None:
    result = _overall([_provider("amazon")])
    paypay = next(item for item in result.provider_results if item.provider == "paypay")

    assert paypay.applicability_status == "unknown"
    assert paypay.normalized_status == "unknown"


def test_case_25_aggregator_model_has_no_sha_field() -> None:
    names = {item.name for item in fields(ProviderCoverageResult)}

    assert "content_sha256" not in names
    assert "content_hash" not in names


def test_case_26_aggregator_model_has_no_observed_range_field() -> None:
    names = {item.name for item in fields(ProviderCoverageResult)}

    assert "observed_start" not in names
    assert "observed_end" not in names


def test_case_27_aggregator_does_not_accept_attestation_details() -> None:
    names = {item.name for item in fields(ProviderCoverageResult)}

    assert "attestation_kind" not in names
    assert "provider_completeness_proven" not in names


def test_case_28_coverage_manifest_is_not_a_provider_result() -> None:
    result = _overall([
        CoverageManifest(source="paypay"),
        _provider("amazon"),
    ])

    assert result.status == "unknown"
    assert "invalid_provider_result_type_count=1" in (result.diagnostic or "")


def test_case_29_legacy_coverage_evidence_is_not_a_provider_result() -> None:
    result = _overall([
        CoverageEvidence(source="paypay", explicitly_incomplete=True),
        _provider("amazon"),
    ])

    assert result.status == "unknown"
    assert "invalid_provider_result_type_count=1" in (result.diagnostic or "")


def test_case_30_provider_results_are_not_mutated() -> None:
    amazon = _provider("amazon")
    paypay = _provider("paypay", "unknown")
    before = (amazon, paypay)

    _overall(before)

    assert before == (amazon, paypay)


def test_case_31_input_order_does_not_change_overall_result() -> None:
    first = _overall([_provider("amazon"), _provider("paypay", "unknown")])
    second = _overall([_provider("paypay", "unknown"), _provider("amazon")])

    assert first == second


def test_case_32_status_counts_include_required_providers_only() -> None:
    result = _overall([
        _provider("amazon"),
        _not_applicable("paypay"),
        _provider("other", "incomplete"),
    ])

    assert result.status_counts.complete == 1
    assert result.status_counts.incomplete == 0
    assert result.status_counts.unknown == 0
    assert result.status_counts.not_applicable == 1


def test_case_33_strict_incomplete_has_precedence_over_unknown() -> None:
    result = _overall([
        _provider("amazon", "unknown"),
        _provider("paypay", "incomplete"),
    ])

    assert result.status == "incomplete"
    assert result.reason == "strict_provider_coverage_incomplete"


def test_case_34_undefined_input_type_never_falls_through_to_complete() -> None:
    result = _overall([{"provider": "amazon"}, {"provider": "paypay"}])

    assert result.status == "unknown"
    assert result.reason == "provider_result_validation_failed"


def test_not_applicable_cannot_carry_complete_or_incomplete_completion() -> None:
    with pytest.raises(
        ValueError,
        match="non_applicable_provider_cannot_have_completion_result",
    ):
        ProviderCoverageResult(
            provider="paypay",
            coverage_basis="transaction_date",
            required_start=START,
            required_end=END,
            applicability_status="not_applicable",
            applicability_reason="explicit_exclusion",
            completion=_completion("complete"),
            applicability_proven=True,
        )


def test_unknown_applicability_cannot_carry_complete_completion() -> None:
    with pytest.raises(
        ValueError,
        match="non_applicable_provider_cannot_have_completion_result",
    ):
        ProviderCoverageResult(
            provider="paypay",
            coverage_basis="transaction_date",
            required_start=START,
            required_end=END,
            applicability_status="unknown",
            applicability_reason="not_established",
            completion=_completion("complete"),
        )


def test_invalid_required_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid_required_window"):
        _provider("paypay", start="2025-02-01", end="2025-01-31")
    with pytest.raises(ValueError, match="invalid_required_window"):
        _overall([], required=(), start="2025-02-01", end="2025-01-31")


@pytest.mark.parametrize(
    ("provider", "basis", "reason"),
    [
        ("", "transaction_date", "invalid_provider"),
        ("paypay", "", "invalid_coverage_basis"),
    ],
)
def test_empty_provider_or_basis_is_rejected(
    provider: str,
    basis: str,
    reason: str,
) -> None:
    with pytest.raises(ValueError, match=reason):
        _provider(provider, basis=basis)


def test_unknown_applicability_is_deterministically_unknown() -> None:
    result = _overall([_provider("amazon"), _unknown_applicability("paypay")])

    assert result.status == "unknown"
    assert result.provider_results[1].normalized_status == "unknown"
