from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable, cast

from .coverage_confirmation import COVERAGE_REASON_OPERATIONAL_ONLY
from .payment_coverage_completion import (
    AttestationClaim,
    AttestationKind,
    CoverageCompletionResult,
    NormalizedCoverageInterval,
    OperationalCoverage,
    evaluate_payment_coverage_completion,
)
from .payment_coverage_manifest import CoverageManifest


_SHA256 = re.compile(r"[0-9a-f]{64}")
_OPERATIONAL_STATUSES = {"usable", "needs_confirmation", "rejected"}

# This is deliberately an explicit allow-list.  completeness_proven alone does
# not identify the provenance of a proof, and completion_status is an output,
# not proof evidence.
PROVIDER_PROOF_EVIDENCE_TYPES = frozenset({"provider_verified"})


@dataclass(frozen=True)
class ManifestCompletionEvaluation:
    provider: str
    coverage_basis: str
    required_start: str | None
    required_end: str | None
    evaluated: bool
    normalized_intervals: tuple[NormalizedCoverageInterval, ...]
    skipped_manifest_ids: tuple[str, ...]
    completion: CoverageCompletionResult


def _content_sha256(manifest: CoverageManifest) -> str | None:
    if not manifest.content_hash:
        return None
    candidate = manifest.content_hash.lower()
    return candidate if _SHA256.fullmatch(candidate) else None


def _operational_coverage(manifest: CoverageManifest) -> OperationalCoverage:
    if manifest.parse_error:
        return "rejected"
    if manifest.operational_coverage in _OPERATIONAL_STATUSES:
        return cast(OperationalCoverage, manifest.operational_coverage)
    # A provider without explicit operational evidence must not become usable
    # completion evidence merely because it has an observed range.
    return "needs_confirmation"


def _attestation(
    manifest: CoverageManifest,
) -> tuple[AttestationKind, AttestationClaim | None]:
    if (
        manifest.completeness_proven
        and manifest.evidence_type in PROVIDER_PROOF_EVIDENCE_TYPES
    ):
        return "provider_proven", "complete_for_range"
    if (
        manifest.source == "paypay"
        and not manifest.completeness_proven
        and manifest.completeness_reason == COVERAGE_REASON_OPERATIONAL_ONLY
    ):
        return "user_attested", "export_scope"
    return "none", None


def _diagnostic(manifest: CoverageManifest) -> str | None:
    return (
        manifest.parse_error
        or manifest.operational_reason
        or manifest.completeness_reason
        or None
    )


def normalized_interval_from_manifest(
    manifest: CoverageManifest,
) -> NormalizedCoverageInterval | None:
    """Adapt one manifest without inferring unavailable coverage evidence."""

    if not manifest.coverage_start or not manifest.coverage_end:
        return None
    kind, claim = _attestation(manifest)
    try:
        return NormalizedCoverageInterval(
            provider=manifest.source,
            coverage_basis=manifest.coverage_basis,
            coverage_start=manifest.coverage_start,
            coverage_end=manifest.coverage_end,
            content_sha256=_content_sha256(manifest),
            operational_coverage=_operational_coverage(manifest),
            attestation_kind=kind,
            attestation_claim=claim,
            provider_completeness_proven=(kind == "provider_proven"),
            evidence_id=manifest.evidence_id or manifest.manifest_id,
            diagnostic=_diagnostic(manifest),
            # CoverageManifest has no explicit-incomplete evidence field.
            # Do not reinterpret its evaluated completion_status as evidence.
            explicitly_incomplete=False,
        )
    except ValueError:
        return None


def evaluate_manifest_completion(
    manifests: Iterable[CoverageManifest],
    *,
    provider: str,
    coverage_basis: str,
    required_start: date | None,
    required_end: date | None,
) -> ManifestCompletionEvaluation:
    """Evaluate one provider/basis without mutating source manifests."""

    normalized: list[NormalizedCoverageInterval] = []
    skipped: list[str] = []
    for manifest in manifests:
        if manifest.source != provider or manifest.coverage_basis != coverage_basis:
            continue
        interval = normalized_interval_from_manifest(manifest)
        if interval is None:
            skipped.append(manifest.manifest_id)
        else:
            normalized.append(interval)

    start = required_start.isoformat() if required_start is not None else None
    end = required_end.isoformat() if required_end is not None else None
    completion = evaluate_payment_coverage_completion(
        normalized,
        provider=provider,
        coverage_basis=coverage_basis,
        required_start=start,
        required_end=end,
    )
    return ManifestCompletionEvaluation(
        provider=provider,
        coverage_basis=coverage_basis,
        required_start=start,
        required_end=end,
        evaluated=required_start is not None and required_end is not None,
        normalized_intervals=tuple(normalized),
        skipped_manifest_ids=tuple(skipped),
        completion=completion,
    )
