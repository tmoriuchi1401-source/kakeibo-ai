from __future__ import annotations

from dataclasses import asdict
from datetime import date

import pytest

import app.payment_coverage_strict_service as strict_service
from app.coverage_confirmation import COVERAGE_REASON_OPERATIONAL_ONLY
from app.payment_coverage_completion_bridge import evaluate_manifest_completion
from app.payment_coverage_manifest import CoverageManifest
from app.payment_coverage_status_preview import CoverageEvidence
from app.payment_coverage_strict_service import (
    evaluate_strict_payment_coverage,
    provider_coverage_result_from_manifest_evaluation,
)


START = date(2025, 1, 1)
END = date(2025, 1, 31)
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _manifest(
    start: str | None = "2025-01-01",
    end: str | None = "2025-01-31",
    *,
    source: str = "paypay",
    basis: str = "transaction_date",
    sha: str | None = SHA_A,
    provider_proven: bool = True,
    evidence_id: str | None = None,
) -> CoverageManifest:
    return CoverageManifest(
        source=source,
        coverage_start=start,
        coverage_end=end,
        coverage_basis=basis,
        evidence_type=("provider_verified" if provider_proven else "csv_file"),
        evidence_id=evidence_id,
        content_hash=sha,
        completeness_reason=(
            "provider_completeness_verified"
            if provider_proven
            else COVERAGE_REASON_OPERATIONAL_ONLY
        ),
        completeness_proven=provider_proven,
        operational_coverage="usable",
        operational_reason="operational_checks_passed",
    )


def _evaluate(
    manifests,
    *,
    required=("paypay",),
    basis="transaction_date",
    start=START,
    end=END,
):
    return evaluate_strict_payment_coverage(
        manifests,
        required_providers=required,
        coverage_basis=basis,
        required_start=start,
        required_end=end,
    )


def _provider_result(result, provider="paypay"):
    return next(item for item in result.provider_results if item.provider == provider)


def test_adapter_maps_manifest_evaluation_without_reinterpreting_completion() -> None:
    evaluation = evaluate_manifest_completion(
        [_manifest()],
        provider="paypay",
        coverage_basis="transaction_date",
        required_start=START,
        required_end=END,
    )

    result = provider_coverage_result_from_manifest_evaluation(evaluation)

    assert result.provider == evaluation.provider
    assert result.coverage_basis == evaluation.coverage_basis
    assert result.required_start == evaluation.required_start
    assert result.required_end == evaluation.required_end
    assert result.completion is evaluation.completion
    assert result.applicability_status == "applicable"
    assert result.applicability_reason == "manifest_completion_evaluated"
    assert result.applicability_proven is False


def test_adapter_never_derives_not_applicable() -> None:
    evaluation = evaluate_manifest_completion(
        [_manifest(provider_proven=False)],
        provider="paypay",
        coverage_basis="transaction_date",
        required_start=START,
        required_end=END,
    )

    result = provider_coverage_result_from_manifest_evaluation(evaluation)

    assert result.normalized_status == "unknown"
    assert result.applicability_status != "not_applicable"


def test_adapter_rejects_unevaluated_completion() -> None:
    evaluation = evaluate_manifest_completion(
        [_manifest()],
        provider="paypay",
        coverage_basis="transaction_date",
        required_start=None,
        required_end=END,
    )

    with pytest.raises(ValueError, match="manifest_completion_evaluation_not_evaluated"):
        provider_coverage_result_from_manifest_evaluation(evaluation)


def test_paypay_user_attested_export_scope_is_provider_and_overall_unknown() -> None:
    result = _evaluate([_manifest(provider_proven=False)])
    provider = _provider_result(result)

    assert provider.completion is not None
    assert provider.completion.completion_status == "unknown"
    assert provider.completion.effective_proof_level == "user_attested"
    assert provider.normalized_status == "unknown"
    assert result.status == "unknown"


def test_provider_proven_full_window_is_provider_and_overall_complete() -> None:
    result = _evaluate([_manifest()])

    assert _provider_result(result).normalized_status == "complete"
    assert result.status == "complete"


@pytest.mark.parametrize(
    "manifests",
    [
        [_manifest("2025-01-05", "2025-01-31")],
        [_manifest("2025-01-01", "2025-01-27")],
        [
            _manifest("2025-01-01", "2025-01-10", sha=SHA_A),
            _manifest("2025-01-15", "2025-01-31", sha=SHA_B),
        ],
    ],
    ids=("left_gap", "right_gap", "internal_gap"),
)
def test_provider_proof_gap_is_provider_and_overall_incomplete(manifests) -> None:
    result = _evaluate(manifests)

    assert _provider_result(result).normalized_status == "incomplete"
    assert result.status == "incomplete"


def test_conflicting_manifests_are_provider_and_overall_unknown() -> None:
    result = _evaluate([
        _manifest(sha=SHA_A),
        _manifest(sha=SHA_B),
    ])
    provider = _provider_result(result)

    assert provider.normalized_status == "unknown"
    assert provider.completion is not None
    assert provider.completion.reason == "conflicting_evidence"
    assert result.status == "unknown"


def test_missing_required_provider_is_aggregator_unknown_not_not_applicable() -> None:
    result = _evaluate([], required=("paypay",))
    provider = _provider_result(result)

    assert result.status == "unknown"
    assert result.reason == "required_provider_result_missing"
    assert provider.applicability_status == "unknown"
    assert provider.normalized_status == "unknown"


def test_empty_required_provider_set_is_not_applicable() -> None:
    result = _evaluate([_manifest()], required=())

    assert result.status == "not_applicable"
    assert result.reason == "no_required_providers"
    assert result.provider_results == ()


def test_non_required_provider_manifest_does_not_affect_overall() -> None:
    result = _evaluate(
        [_manifest(source="other")],
        required=("paypay",),
    )

    assert result.status == "unknown"
    assert tuple(item.provider for item in result.provider_results) == ("paypay",)


def test_mismatched_basis_is_not_evaluated_and_required_provider_is_missing() -> None:
    result = _evaluate(
        [_manifest(basis="message_date")],
        required=("paypay",),
        basis="transaction_date",
    )

    assert result.status == "unknown"
    assert result.reason == "required_provider_result_missing"
    assert _provider_result(result).applicability_status == "unknown"


def test_multiple_same_provider_manifests_are_unioned_by_existing_evaluator() -> None:
    result = _evaluate([
        _manifest("2025-01-01", "2025-01-15", sha=SHA_A),
        _manifest("2025-01-16", "2025-01-31", sha=SHA_B),
    ])

    assert _provider_result(result).normalized_status == "complete"
    assert result.status == "complete"


def test_same_sha_duplicate_is_collapsed_by_existing_evaluator() -> None:
    result = _evaluate([_manifest(), _manifest()])
    completion = _provider_result(result).completion

    assert completion is not None
    assert completion.duplicate_collapse_count == 1
    assert result.status == "complete"


def test_user_attested_and_provider_proof_mix_remains_unknown() -> None:
    result = _evaluate([
        _manifest("2025-01-01", "2025-01-10", sha=SHA_A),
        _manifest(
            "2025-01-11", "2025-01-20",
            sha=SHA_B, provider_proven=False,
        ),
        _manifest("2025-01-21", "2025-01-31", sha=SHA_C),
    ])

    assert _provider_result(result).normalized_status == "unknown"
    assert result.status == "unknown"


@pytest.mark.parametrize(
    ("paypay", "amazon", "expected"),
    [
        (_manifest(), _manifest(source="amazon", provider_proven=False), "unknown"),
        (_manifest(), _manifest("2025-01-01", "2025-01-10", source="amazon"), "incomplete"),
        (_manifest(provider_proven=False), _manifest("2025-01-01", "2025-01-10", source="amazon"), "incomplete"),
        (_manifest(), _manifest(source="amazon"), "complete"),
    ],
    ids=("complete_unknown", "complete_incomplete", "unknown_incomplete", "all_complete"),
)
def test_multiple_provider_aggregation(paypay, amazon, expected) -> None:
    result = _evaluate(
        [paypay, amazon],
        required=("paypay", "amazon"),
    )

    assert result.status == expected


def test_manifest_without_range_is_safely_unknown() -> None:
    result = _evaluate([_manifest(None, None)])
    completion = _provider_result(result).completion

    assert completion is not None
    assert completion.completion_status == "unknown"
    assert completion.reason == "matching_coverage_evidence_missing"


def test_missing_sha_is_not_fabricated() -> None:
    result = _evaluate([_manifest(sha=None, evidence_id="provider-record-1")])
    completion = _provider_result(result).completion

    assert completion is not None
    assert completion.completion_status == "complete"
    assert completion.duplicate_collapse_count == 0


def test_legacy_coverage_evidence_is_not_an_accepted_manifest_input() -> None:
    with pytest.raises(TypeError, match="invalid_coverage_manifest_type"):
        _evaluate([CoverageEvidence(source="paypay")])


def test_imported_data_is_not_added_to_required_providers() -> None:
    result = _evaluate(
        [_manifest(source="imported_data")],
        required=("paypay",),
    )

    assert result.required_providers == ("paypay",)
    assert result.status == "unknown"


def test_amazon_gmail_is_not_renamed_to_amazon() -> None:
    result = _evaluate(
        [_manifest(source="amazon_gmail")],
        required=("amazon",),
    )

    assert result.required_providers == ("amazon",)
    assert _provider_result(result, "amazon").applicability_status == "unknown"
    assert result.status == "unknown"


def test_service_does_not_mutate_manifests() -> None:
    manifests = [_manifest(), _manifest(source="other")]
    before = [asdict(item) for item in manifests]

    _evaluate(manifests)

    assert [asdict(item) for item in manifests] == before


def test_adapter_does_not_mutate_evaluation_or_completion() -> None:
    evaluation = evaluate_manifest_completion(
        [_manifest()],
        provider="paypay",
        coverage_basis="transaction_date",
        required_start=START,
        required_end=END,
    )
    before = asdict(evaluation)

    result = provider_coverage_result_from_manifest_evaluation(evaluation)

    assert asdict(evaluation) == before
    assert result.completion is evaluation.completion


def test_same_required_window_is_passed_to_every_provider(monkeypatch) -> None:
    calls = []
    original = strict_service.evaluate_manifest_completion

    def spy(manifests, **kwargs):
        calls.append((
            kwargs["provider"],
            kwargs["required_start"],
            kwargs["required_end"],
            tuple(item.source for item in manifests),
        ))
        return original(manifests, **kwargs)

    monkeypatch.setattr(strict_service, "evaluate_manifest_completion", spy)

    _evaluate(
        [_manifest(), _manifest(source="amazon")],
        required=("paypay", "amazon"),
    )

    assert sorted(calls) == [
        ("amazon", START, END, ("amazon",)),
        ("paypay", START, END, ("paypay",)),
    ]


def test_service_delegates_provider_and_overall_logic(monkeypatch) -> None:
    provider_calls = 0
    overall_calls = 0
    original_provider = strict_service.evaluate_manifest_completion
    original_overall = strict_service.evaluate_overall_payment_coverage

    def provider_spy(*args, **kwargs):
        nonlocal provider_calls
        provider_calls += 1
        return original_provider(*args, **kwargs)

    def overall_spy(*args, **kwargs):
        nonlocal overall_calls
        overall_calls += 1
        return original_overall(*args, **kwargs)

    monkeypatch.setattr(strict_service, "evaluate_manifest_completion", provider_spy)
    monkeypatch.setattr(strict_service, "evaluate_overall_payment_coverage", overall_spy)

    result = _evaluate([_manifest()])

    assert result.status == "complete"
    assert provider_calls == 1
    assert overall_calls == 1


def test_invalid_required_window_fails_closed() -> None:
    with pytest.raises(ValueError, match="invalid_required_window"):
        _evaluate(
            [_manifest()],
            start=date(2025, 2, 1),
            end=date(2025, 1, 31),
        )
