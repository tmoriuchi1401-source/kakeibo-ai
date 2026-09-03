from __future__ import annotations

from datetime import date

import app.payment_coverage_manifest as manifest_module
import app.payment_coverage_orchestration as orchestration
from app.coverage_confirmation import COVERAGE_REASON_OPERATIONAL_ONLY
from app.payment_coverage_dual_track import PaymentCoverageDualTrackResult
from app.payment_coverage_manifest import (
    CoverageManifest,
    _PaymentCoverageManifestPreparation,
    assemble_payment_coverage_manifests,
)
from app.payment_coverage_status_preview import preview_payment_coverage_status


START = date(2025, 1, 1)
END = date(2025, 1, 31)
AS_OF = date(2025, 1, 31)
SHA_A = "a" * 64


class _ReadOnlyDB:
    def __init__(self) -> None:
        self.values = {
            "取込データ!A2:L": [],
            "Amazonイベント!A2:X": [],
            "Amazon注文ヘッダ!A2:O": [],
        }

    def get(self, rng):
        return self.values[rng]


def _manifest(
    start: str = "2025-01-01",
    end: str = "2025-01-31",
    *,
    source: str = "paypay",
    basis: str = "transaction_date",
    provider_proven: bool = True,
) -> CoverageManifest:
    return CoverageManifest(
        source=source,
        coverage_start=start,
        coverage_end=end,
        coverage_basis=basis,
        evidence_type=("provider_verified" if provider_proven else "csv_file"),
        content_hash=SHA_A,
        completeness_reason=(
            "provider_completeness_verified"
            if provider_proven
            else COVERAGE_REASON_OPERATIONAL_ONLY
        ),
        completeness_proven=provider_proven,
        operational_coverage="usable",
        operational_reason="operational_checks_passed",
    )


def _preview(
    *,
    manifests=(),
    required_providers=("paypay",),
    coverage_basis="transaction_date",
    required_start=START,
    required_end=END,
):
    return orchestration.preview_payment_coverage_status_with_strict(
        _ReadOnlyDB(),
        manifests=manifests,
        required_providers=required_providers,
        coverage_basis=coverage_basis,
        required_start=required_start,
        required_end=required_end,
        as_of=AS_OF,
    )


def test_boundary_passes_explicit_inputs_and_manifest_objects_unchanged(
    monkeypatch,
) -> None:
    manifests = [_manifest()]
    required_providers = ["paypay"]
    calls = []
    original = orchestration.preview_payment_coverage_status

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(orchestration, "preview_payment_coverage_status", spy)

    result = orchestration.preview_payment_coverage_status_with_strict(
        _ReadOnlyDB(),
        manifests=manifests,
        required_providers=required_providers,
        coverage_basis="transaction_date",
        required_start=START,
        required_end=END,
        as_of=AS_OF,
    )

    assert isinstance(result, PaymentCoverageDualTrackResult)
    assert len(calls) == 1
    assert calls[0][1]["strict_evaluation"] is True
    assert calls[0][1]["strict_manifests"] is manifests
    assert calls[0][1]["strict_required_providers"] is required_providers
    assert calls[0][1]["strict_coverage_basis"] == "transaction_date"
    assert calls[0][1]["strict_required_start"] is START
    assert calls[0][1]["strict_required_end"] is END


def test_paypay_user_attested_is_strict_unknown() -> None:
    result = _preview(manifests=[_manifest(provider_proven=False)])

    assert result.strict_result.evaluation_status == "evaluated"
    assert result.strict_result.coverage_status == "unknown"


def test_assembled_manifest_objects_flow_directly_to_strict_runtime() -> None:
    manifests = assemble_payment_coverage_manifests([_manifest()])
    original = tuple(manifests)

    result = _preview(manifests=manifests)

    assert result.strict_result.coverage_status == "complete"
    assert tuple(manifests) == original


def test_provider_proven_full_window_is_strict_complete() -> None:
    result = _preview(manifests=[_manifest()])

    assert result.strict_result.coverage_status == "complete"


def test_provider_proof_gap_is_strict_incomplete() -> None:
    result = _preview(manifests=[_manifest(end="2025-01-10")])

    assert result.strict_result.coverage_status == "incomplete"


def test_missing_required_provider_is_strict_unknown() -> None:
    result = _preview(manifests=[])

    assert result.strict_result.coverage_status == "unknown"
    assert result.strict_result.overall_result is not None
    assert result.strict_result.overall_result.reason == "required_provider_result_missing"


def test_invalid_basis_fails_closed_without_changing_legacy_result() -> None:
    legacy = preview_payment_coverage_status(_ReadOnlyDB(), as_of=AS_OF)
    result = _preview(manifests=[_manifest()], coverage_basis="")

    assert result.legacy_result == legacy
    assert result.strict_result.evaluation_status == "failed"
    assert result.strict_result.coverage_status == "unknown"
    assert "invalid_coverage_basis" in (result.strict_result.diagnostic or "")


def test_invalid_window_fails_closed_without_changing_legacy_result() -> None:
    legacy = preview_payment_coverage_status(_ReadOnlyDB(), as_of=AS_OF)
    result = _preview(
        manifests=[_manifest()],
        required_start=date(2025, 2, 1),
        required_end=END,
    )

    assert result.legacy_result == legacy
    assert result.strict_result.evaluation_status == "failed"
    assert result.strict_result.coverage_status == "unknown"
    assert "invalid_required_window" in (result.strict_result.diagnostic or "")


def test_boundary_does_not_run_raw_manifest_preparation(monkeypatch) -> None:
    def unexpected(*args, **kwargs):
        raise AssertionError("raw manifest preparation called")

    monkeypatch.setattr(
        manifest_module,
        "assemble_payment_coverage_manifests",
        unexpected,
    )
    monkeypatch.setattr(manifest_module, "csv_manifest", unexpected)

    result = _preview(manifests=[_manifest()])

    assert result.strict_result.coverage_status == "complete"


def test_boundary_does_not_normalize_provider_identities() -> None:
    result = _preview(
        manifests=[
            _manifest(source="imported_data"),
            _manifest(source="amazon_gmail"),
        ],
        required_providers=("amazon",),
    )

    assert result.strict_result.coverage_status == "unknown"
    assert result.strict_result.overall_result is not None
    assert result.strict_result.overall_result.required_providers == ("amazon",)


def test_raw_boundary_prepares_once_and_passes_manifest_objects_unchanged(
    monkeypatch,
) -> None:
    manifests = [_manifest()]
    preparation = _PaymentCoverageManifestPreparation(
        manifests=manifests,
        paypay_operational_evidences=[],
        paypay_evidence_verifications=[],
        duplicate_evidence_count=0,
        conflicting_evidence_count=0,
        operational_duplicate_count=0,
        operational_conflict_count=0,
    )
    preparation_calls = []
    strict_calls = []

    def prepare(**kwargs):
        preparation_calls.append(kwargs)
        return preparation

    original = orchestration.preview_payment_coverage_status_with_strict

    def strict(*args, **kwargs):
        strict_calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(orchestration, "_prepare_payment_coverage_manifests", prepare)
    monkeypatch.setattr(
        orchestration, "preview_payment_coverage_status_with_strict", strict,
    )

    result = orchestration._preview_payment_coverage_status_with_strict_from_raw_inputs(
        _ReadOnlyDB(),
        paypay_csvs=["raw.csv"],
        required_providers=["paypay"],
        coverage_basis="transaction_date",
        required_start=START,
        required_end=END,
        as_of=AS_OF,
    )

    assert result.strict_result.coverage_status == "complete"
    assert len(preparation_calls) == 1
    assert strict_calls[0][1]["manifests"] is manifests
    assert strict_calls[0][1]["required_providers"] == ["paypay"]
    assert strict_calls[0][1]["coverage_basis"] == "transaction_date"
    assert strict_calls[0][1]["required_start"] is START
    assert strict_calls[0][1]["required_end"] is END
