from __future__ import annotations

from datetime import date
from typing import Iterable, cast

from .payment_coverage_dual_track import PaymentCoverageDualTrackResult
from .payment_coverage_manifest import CoverageManifest
from .payment_coverage_status_preview import preview_payment_coverage_status


def preview_payment_coverage_status_with_strict(
    db,
    *,
    manifests: Iterable[CoverageManifest],
    required_providers: Iterable[str],
    coverage_basis: str,
    required_start: date,
    required_end: date,
    as_of: date | None = None,
) -> PaymentCoverageDualTrackResult[dict]:
    """Opt into the existing dual-track preview with explicit strict inputs."""

    return cast(
        PaymentCoverageDualTrackResult[dict],
        preview_payment_coverage_status(
            db,
            as_of=as_of,
            strict_evaluation=True,
            strict_manifests=manifests,
            strict_required_providers=required_providers,
            strict_coverage_basis=coverage_basis,
            strict_required_start=required_start,
            strict_required_end=required_end,
        ),
    )
