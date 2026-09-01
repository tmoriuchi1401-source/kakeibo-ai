from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Generic, Iterable, Literal, TypeAlias, TypeVar

from .payment_coverage_manifest import CoverageManifest
from .payment_coverage_overall import OverallCoverageResult, OverallCoverageStatus
from .payment_coverage_strict_service import evaluate_strict_payment_coverage


StrictEvaluationStatus: TypeAlias = Literal[
    "not_evaluated", "evaluated", "failed",
]
LegacyResult = TypeVar("LegacyResult")


@dataclass(frozen=True)
class StrictPaymentCoverageTrack:
    """Optional strict runtime result kept separate from legacy coverage."""

    evaluation_status: StrictEvaluationStatus
    reason: str
    overall_result: OverallCoverageResult | None = None
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        if self.evaluation_status == "evaluated":
            if not isinstance(self.overall_result, OverallCoverageResult):
                raise ValueError("evaluated_strict_track_requires_overall_result")
        elif self.overall_result is not None:
            raise ValueError("unevaluated_strict_track_cannot_have_overall_result")
        if self.evaluation_status == "failed" and not self.diagnostic:
            raise ValueError("failed_strict_track_requires_diagnostic")

    @property
    def coverage_status(self) -> OverallCoverageStatus | None:
        if self.evaluation_status == "evaluated" and self.overall_result is not None:
            return self.overall_result.status
        if self.evaluation_status == "failed":
            return "unknown"
        return None


@dataclass(frozen=True)
class PaymentCoverageDualTrackResult(Generic[LegacyResult]):
    """Legacy output plus an additive strict track; neither replaces the other."""

    legacy_result: LegacyResult
    strict_result: StrictPaymentCoverageTrack


def evaluate_payment_coverage_dual_track(
    legacy_result: LegacyResult,
    *,
    manifests: Iterable[CoverageManifest] = (),
    required_providers: Iterable[str] | None = None,
    coverage_basis: str | None = None,
    required_start: date | None = None,
    required_end: date | None = None,
) -> PaymentCoverageDualTrackResult[LegacyResult]:
    """Optionally add strict evaluation without changing a legacy result.

    ``required_providers=None`` is the explicit legacy-only mode.  Once strict
    evaluation is requested, basis and both window endpoints must also be
    supplied by the caller; this boundary never infers them from evidence.
    """

    if required_providers is None:
        strict = StrictPaymentCoverageTrack(
            evaluation_status="not_evaluated",
            reason="required_providers_not_supplied",
        )
        return PaymentCoverageDualTrackResult(legacy_result, strict)

    missing = tuple(
        name for name, value in (
            ("coverage_basis", coverage_basis),
            ("required_start", required_start),
            ("required_end", required_end),
        )
        if value is None
    )
    if missing:
        strict = StrictPaymentCoverageTrack(
            evaluation_status="failed",
            reason="strict_inputs_incomplete",
            diagnostic=f"missing_strict_inputs={','.join(missing)}",
        )
        return PaymentCoverageDualTrackResult(legacy_result, strict)

    try:
        overall = evaluate_strict_payment_coverage(
            manifests,
            required_providers=required_providers,
            coverage_basis=coverage_basis,
            required_start=required_start,
            required_end=required_end,
        )
    except Exception as exc:
        # Strict validation/runtime failures are additive diagnostics.  They
        # must not interrupt or reinterpret an already-produced legacy result.
        strict = StrictPaymentCoverageTrack(
            evaluation_status="failed",
            reason="strict_evaluation_failed",
            diagnostic=f"{type(exc).__name__}: {exc}",
        )
    else:
        strict = StrictPaymentCoverageTrack(
            evaluation_status="evaluated",
            reason="strict_evaluation_completed",
            overall_result=overall,
        )
    return PaymentCoverageDualTrackResult(legacy_result, strict)
