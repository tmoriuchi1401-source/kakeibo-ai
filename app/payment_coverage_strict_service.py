from __future__ import annotations

from datetime import date
from typing import Iterable

from .payment_coverage_completion_bridge import (
    ManifestCompletionEvaluation,
    evaluate_manifest_completion,
)
from .payment_coverage_manifest import CoverageManifest
from .payment_coverage_overall import (
    OverallCoverageResult,
    ProviderCoverageResult,
    evaluate_overall_payment_coverage,
)


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid_{field}")
    return value.strip()


def provider_coverage_result_from_manifest_evaluation(
    evaluation: ManifestCompletionEvaluation,
) -> ProviderCoverageResult:
    """Adapt one completed manifest evaluation without re-reading evidence."""

    if not isinstance(evaluation, ManifestCompletionEvaluation):
        raise TypeError("invalid_manifest_completion_evaluation_type")
    if (
        not evaluation.evaluated
        or evaluation.required_start is None
        or evaluation.required_end is None
    ):
        raise ValueError("manifest_completion_evaluation_not_evaluated")
    return ProviderCoverageResult(
        provider=evaluation.provider,
        coverage_basis=evaluation.coverage_basis,
        required_start=evaluation.required_start,
        required_end=evaluation.required_end,
        # A required provider whose manifests were evaluated has a completion
        # result.  This does not claim an explicit applicability proof;
        # not_applicable remains a separate, future policy input.
        applicability_status="applicable",
        applicability_reason="manifest_completion_evaluated",
        completion=evaluation.completion,
        applicability_proven=False,
    )


def evaluate_strict_payment_coverage(
    manifests: Iterable[CoverageManifest],
    *,
    required_providers: Iterable[str],
    coverage_basis: str,
    required_start: date,
    required_end: date,
) -> OverallCoverageResult:
    """Assemble strict overall coverage for one basis and required window."""

    manifest_values = tuple(manifests)
    if any(not isinstance(item, CoverageManifest) for item in manifest_values):
        raise TypeError("invalid_coverage_manifest_type")
    if not isinstance(required_start, date) or not isinstance(required_end, date):
        raise ValueError("invalid_required_window")
    if required_start > required_end:
        raise ValueError("invalid_required_window")

    basis = _required_text(coverage_basis, "coverage_basis")
    required = tuple(sorted({
        _required_text(provider, "required_provider")
        for provider in required_providers
    }))
    grouped: dict[str, list[CoverageManifest]] = {
        provider: [] for provider in required
    }
    for manifest in manifest_values:
        if manifest.source in grouped and manifest.coverage_basis == basis:
            grouped[manifest.source].append(manifest)

    provider_results: list[ProviderCoverageResult] = []
    for provider in required:
        provider_manifests = grouped[provider]
        if not provider_manifests:
            # The overall aggregator owns missing-required-provider semantics.
            # Do not fabricate completion evidence or not_applicable here.
            continue
        evaluation = evaluate_manifest_completion(
            provider_manifests,
            provider=provider,
            coverage_basis=basis,
            required_start=required_start,
            required_end=required_end,
        )
        provider_results.append(
            provider_coverage_result_from_manifest_evaluation(evaluation),
        )

    return evaluate_overall_payment_coverage(
        provider_results,
        required_providers=required,
        coverage_basis=basis,
        required_start=required_start.isoformat(),
        required_end=required_end.isoformat(),
    )
