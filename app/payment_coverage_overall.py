from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Literal, TypeAlias

from .payment_coverage_completion import CoverageCompletionResult


ApplicabilityStatus: TypeAlias = Literal[
    "applicable", "not_applicable", "unknown",
]
NormalizedProviderStatus: TypeAlias = Literal[
    "complete", "incomplete", "unknown", "not_applicable",
]
OverallCoverageStatus: TypeAlias = NormalizedProviderStatus

_APPLICABILITY_STATUSES = {"applicable", "not_applicable", "unknown"}
_COMPLETION_STATUSES = {"complete", "incomplete", "unknown"}
_NORMALIZED_STATUSES = _COMPLETION_STATUSES | {"not_applicable"}


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid_{field}")
    return value.strip()


def _iso_date(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid_{field}")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid_{field}") from exc


@dataclass(frozen=True)
class ProviderCoverageResult:
    """One already-evaluated provider result for a single strict window."""

    provider: str
    coverage_basis: str
    required_start: str
    required_end: str
    applicability_status: ApplicabilityStatus
    applicability_reason: str
    completion: CoverageCompletionResult | None
    applicability_proven: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _required_text(self.provider, "provider"))
        object.__setattr__(
            self,
            "coverage_basis",
            _required_text(self.coverage_basis, "coverage_basis"),
        )
        start = _iso_date(self.required_start, "required_start")
        end = _iso_date(self.required_end, "required_end")
        if start > end:
            raise ValueError("invalid_required_window")
        object.__setattr__(self, "required_start", start)
        object.__setattr__(self, "required_end", end)
        object.__setattr__(
            self,
            "applicability_reason",
            _required_text(self.applicability_reason, "applicability_reason"),
        )
        if self.applicability_status not in _APPLICABILITY_STATUSES:
            raise ValueError("invalid_applicability_status")

        if self.applicability_status == "applicable":
            if not isinstance(self.completion, CoverageCompletionResult):
                raise ValueError("applicable_provider_requires_completion_result")
            if self.completion.completion_status not in _COMPLETION_STATUSES:
                raise ValueError("invalid_completion_status")
        elif self.completion is not None:
            raise ValueError("non_applicable_provider_cannot_have_completion_result")

        if self.applicability_status == "not_applicable":
            if not self.applicability_proven:
                raise ValueError("not_applicable_requires_explicit_evidence")
        elif self.applicability_proven:
            raise ValueError("applicability_proven_requires_not_applicable")

    @property
    def normalized_status(self) -> NormalizedProviderStatus:
        if self.applicability_status == "not_applicable":
            return "not_applicable"
        if self.applicability_status == "unknown":
            return "unknown"
        if not isinstance(self.completion, CoverageCompletionResult):
            return "unknown"
        status = self.completion.completion_status
        return status if status in _COMPLETION_STATUSES else "unknown"


@dataclass(frozen=True)
class CoverageStatusCounts:
    complete: int = 0
    incomplete: int = 0
    unknown: int = 0
    not_applicable: int = 0

    def __post_init__(self) -> None:
        if any(value < 0 for value in (
            self.complete,
            self.incomplete,
            self.unknown,
            self.not_applicable,
        )):
            raise ValueError("status counts cannot be negative")


@dataclass(frozen=True)
class OverallCoverageResult:
    status: OverallCoverageStatus
    reason: str
    provider_results: tuple[ProviderCoverageResult, ...]
    status_counts: CoverageStatusCounts
    required_providers: tuple[str, ...]
    coverage_basis: str
    required_start: str
    required_end: str
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _NORMALIZED_STATUSES:
            raise ValueError("invalid_overall_coverage_status")
        _required_text(self.reason, "overall_reason")


def _unknown_provider_result(
    provider: str,
    *,
    coverage_basis: str,
    required_start: str,
    required_end: str,
    reason: str,
) -> ProviderCoverageResult:
    return ProviderCoverageResult(
        provider=provider,
        coverage_basis=coverage_basis,
        required_start=required_start,
        required_end=required_end,
        applicability_status="unknown",
        applicability_reason=reason,
        completion=None,
    )


def _counts(
    provider_results: Iterable[ProviderCoverageResult],
) -> CoverageStatusCounts:
    values = [item.normalized_status for item in provider_results]
    return CoverageStatusCounts(
        complete=values.count("complete"),
        incomplete=values.count("incomplete"),
        unknown=values.count("unknown"),
        not_applicable=values.count("not_applicable"),
    )


def _diagnostic(parts: list[str]) -> str | None:
    return ";".join(sorted(set(parts))) if parts else None


def evaluate_overall_payment_coverage(
    provider_results: Iterable[ProviderCoverageResult],
    *,
    required_providers: Iterable[str],
    coverage_basis: str,
    required_start: str,
    required_end: str,
) -> OverallCoverageResult:
    """Aggregate normalized strict results without reinterpreting evidence."""

    basis = _required_text(coverage_basis, "coverage_basis")
    start = _iso_date(required_start, "required_start")
    end = _iso_date(required_end, "required_end")
    if start > end:
        raise ValueError("invalid_required_window")
    required = tuple(sorted({
        _required_text(provider, "required_provider")
        for provider in required_providers
    }))
    raw_results = list(provider_results)

    if not required:
        ignored = sorted({
            item.provider
            for item in raw_results
            if isinstance(item, ProviderCoverageResult)
        })
        diagnostic = (
            f"ignored_non_required_providers={','.join(ignored)}"
            if ignored else None
        )
        return OverallCoverageResult(
            status="not_applicable",
            reason="no_required_providers",
            provider_results=(),
            status_counts=CoverageStatusCounts(),
            required_providers=(),
            coverage_basis=basis,
            required_start=start,
            required_end=end,
            diagnostic=diagnostic,
        )

    diagnostics: list[str] = []
    invalid_type_count = sum(
        not isinstance(item, ProviderCoverageResult) for item in raw_results
    )
    if invalid_type_count:
        diagnostics.append(f"invalid_provider_result_type_count={invalid_type_count}")
    valid_results = [
        item for item in raw_results if isinstance(item, ProviderCoverageResult)
    ]
    ignored = sorted({
        item.provider for item in valid_results if item.provider not in required
    })
    if ignored:
        diagnostics.append(f"ignored_non_required_providers={','.join(ignored)}")

    by_provider: dict[str, list[ProviderCoverageResult]] = {
        provider: [] for provider in required
    }
    for item in valid_results:
        if item.provider in by_provider:
            by_provider[item.provider].append(item)

    selected: list[ProviderCoverageResult] = []
    structural_error = invalid_type_count > 0
    missing: list[str] = []
    for provider in required:
        candidates = by_provider[provider]
        if not candidates:
            missing.append(provider)
            selected.append(_unknown_provider_result(
                provider,
                coverage_basis=basis,
                required_start=start,
                required_end=end,
                reason="required_provider_result_missing",
            ))
            continue
        if len(candidates) > 1:
            structural_error = True
            diagnostics.append(f"duplicate_provider_result={provider}")
            selected.append(_unknown_provider_result(
                provider,
                coverage_basis=basis,
                required_start=start,
                required_end=end,
                reason="duplicate_provider_result",
            ))
            continue
        candidate = candidates[0]
        if candidate.coverage_basis != basis:
            structural_error = True
            diagnostics.append(f"coverage_basis_mismatch={provider}")
            selected.append(_unknown_provider_result(
                provider,
                coverage_basis=basis,
                required_start=start,
                required_end=end,
                reason="coverage_basis_mismatch",
            ))
            continue
        if candidate.required_start != start or candidate.required_end != end:
            structural_error = True
            diagnostics.append(f"required_window_mismatch={provider}")
            selected.append(_unknown_provider_result(
                provider,
                coverage_basis=basis,
                required_start=start,
                required_end=end,
                reason="required_window_mismatch",
            ))
            continue
        selected.append(candidate)

    if missing:
        diagnostics.append(f"missing_required_providers={','.join(missing)}")
    selected.sort(key=lambda item: item.provider)
    counts = _counts(selected)
    statuses = {item.normalized_status for item in selected}

    if structural_error:
        status: OverallCoverageStatus = "unknown"
        reason = "provider_result_validation_failed"
    elif "incomplete" in statuses:
        status = "incomplete"
        reason = "strict_provider_coverage_incomplete"
    elif "unknown" in statuses:
        status = "unknown"
        reason = (
            "required_provider_result_missing"
            if missing else "strict_provider_coverage_unknown"
        )
    elif statuses == {"not_applicable"}:
        status = "not_applicable"
        reason = "no_applicable_required_providers"
    else:
        status = "complete"
        reason = (
            "required_provider_coverage_satisfied"
            if "not_applicable" in statuses
            else "all_required_providers_complete"
        )

    return OverallCoverageResult(
        status=status,
        reason=reason,
        provider_results=tuple(selected),
        status_counts=counts,
        required_providers=required,
        coverage_basis=basis,
        required_start=start,
        required_end=end,
        diagnostic=_diagnostic(diagnostics),
    )
