from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, cast

from .payment_coverage_dual_track import PaymentCoverageDualTrackResult
from .paypay_evidence_bundle import SignatureVerifier
from .payment_coverage_manifest import (
    CoverageConfirmationResolver,
    CoverageManifest,
    _prepare_payment_coverage_manifests,
)
from .payment_coverage_status_preview import preview_payment_coverage_status


@dataclass(frozen=True)
class PaymentCoverageStrictRequest:
    """Explicit application inputs for one raw-to-strict evaluation."""

    required_providers: Iterable[str]
    coverage_basis: str
    required_start: date
    required_end: date
    paypay_csvs: list[str] | None = None
    paypay_export_evidence_files: list[str] | None = None
    paypay_status_image_files: list[str] | None = None
    paypay_confirmed_ranges: list[str] | None = None
    au_pay_card_csvs: list[str] | None = None
    signature_verifier: SignatureVerifier | None = None
    confirmation_resolver: CoverageConfirmationResolver | None = None
    as_of: date | None = None


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


def preview_payment_coverage_status_with_strict_request(
    db,
    request: PaymentCoverageStrictRequest,
) -> PaymentCoverageDualTrackResult[dict]:
    """Run one explicit application request through the existing raw boundary."""

    if not isinstance(request, PaymentCoverageStrictRequest):
        raise TypeError("invalid_payment_coverage_strict_request_type")
    return _preview_payment_coverage_status_with_strict_from_raw_inputs(
        db,
        paypay_csvs=request.paypay_csvs,
        paypay_export_evidence_files=request.paypay_export_evidence_files,
        paypay_status_image_files=request.paypay_status_image_files,
        paypay_confirmed_ranges=request.paypay_confirmed_ranges,
        au_pay_card_csvs=request.au_pay_card_csvs,
        signature_verifier=request.signature_verifier,
        confirmation_resolver=request.confirmation_resolver,
        required_providers=request.required_providers,
        coverage_basis=request.coverage_basis,
        required_start=request.required_start,
        required_end=request.required_end,
        as_of=request.as_of,
    )


def _preview_payment_coverage_status_with_strict_from_raw_inputs(
    db,
    *,
    paypay_csvs: list[str] | None = None,
    paypay_export_evidence_files: list[str] | None = None,
    paypay_status_image_files: list[str] | None = None,
    paypay_confirmed_ranges: list[str] | None = None,
    au_pay_card_csvs: list[str] | None = None,
    signature_verifier: SignatureVerifier | None = None,
    confirmation_resolver: CoverageConfirmationResolver | None = None,
    required_providers: Iterable[str],
    coverage_basis: str,
    required_start: date,
    required_end: date,
    as_of: date | None = None,
) -> PaymentCoverageDualTrackResult[dict]:
    """Connect one raw preparation result to the object-only strict boundary."""

    preparation = _prepare_payment_coverage_manifests(
        paypay_csvs=paypay_csvs,
        paypay_export_evidence_files=paypay_export_evidence_files,
        paypay_status_image_files=paypay_status_image_files,
        paypay_confirmed_ranges=paypay_confirmed_ranges,
        au_pay_card_csvs=au_pay_card_csvs,
        signature_verifier=signature_verifier,
        confirmation_resolver=confirmation_resolver,
    )
    return preview_payment_coverage_status_with_strict(
        db,
        manifests=preparation.manifests,
        required_providers=required_providers,
        coverage_basis=coverage_basis,
        required_start=required_start,
        required_end=required_end,
        as_of=as_of,
    )
