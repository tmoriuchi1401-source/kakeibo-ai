from __future__ import annotations

from datetime import date

import app.payment_coverage_dual_track as dual_track
import app.payment_coverage_status_preview as status_preview
from app.coverage_confirmation import COVERAGE_REASON_OPERATIONAL_ONLY
from app.payment_coverage_dual_track import PaymentCoverageDualTrackResult
from app.payment_coverage_manifest import CoverageManifest
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
        self.reads = []

    def get(self, rng):
        self.reads.append(rng)
        return self.values[rng]

    def __getattr__(self, name):
        if name.startswith(("append", "update", "clear", "ensure", "configure")):
            raise AssertionError(f"write method used: {name}")
        raise AttributeError(name)


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
    strict_evaluation: bool = True,
    manifests=(),
    required=("paypay",),
    basis="transaction_date",
    start=START,
    end=END,
):
    db = _ReadOnlyDB()
    result = preview_payment_coverage_status(
        db,
        as_of=AS_OF,
        strict_evaluation=strict_evaluation,
        strict_manifests=manifests,
        strict_required_providers=required,
        strict_coverage_basis=basis,
        strict_required_start=start,
        strict_required_end=end,
    )
    return result, db


def test_default_runtime_path_returns_legacy_dict_only() -> None:
    result, db = _preview(strict_evaluation=False, manifests=[_manifest()])

    assert isinstance(result, dict)
    assert "legacy_result" not in result
    assert "strict_result" not in result
    assert result["source_count"] == 5
    assert db.reads == [
        "取込データ!A2:L",
        "Amazonイベント!A2:X",
        "Amazon注文ヘッダ!A2:O",
    ]


def test_opt_in_returns_final_legacy_preview_and_strict_track() -> None:
    legacy, _ = _preview(strict_evaluation=False)
    result, _ = _preview(manifests=[_manifest()])

    assert isinstance(result, PaymentCoverageDualTrackResult)
    assert result.legacy_result == legacy
    assert result.legacy_result["source_count"] == 5
    assert result.strict_result.coverage_status == "complete"


def test_paypay_user_attested_is_strict_unknown() -> None:
    result, _ = _preview(manifests=[_manifest(provider_proven=False)])

    assert isinstance(result, PaymentCoverageDualTrackResult)
    assert result.strict_result.evaluation_status == "evaluated"
    assert result.strict_result.coverage_status == "unknown"


def test_provider_proven_full_window_is_strict_complete() -> None:
    result, _ = _preview(manifests=[_manifest()])

    assert isinstance(result, PaymentCoverageDualTrackResult)
    assert result.strict_result.coverage_status == "complete"


def test_provider_proof_gap_is_strict_incomplete() -> None:
    result, _ = _preview(manifests=[_manifest(end="2025-01-10")])

    assert isinstance(result, PaymentCoverageDualTrackResult)
    assert result.strict_result.coverage_status == "incomplete"


def test_required_providers_omitted_is_not_evaluated() -> None:
    result, _ = _preview(manifests=[_manifest()], required=None)

    assert isinstance(result, PaymentCoverageDualTrackResult)
    assert result.strict_result.evaluation_status == "not_evaluated"
    assert result.strict_result.coverage_status is None


def test_missing_basis_fails_closed_without_inference() -> None:
    result, _ = _preview(manifests=[_manifest()], basis=None)

    assert isinstance(result, PaymentCoverageDualTrackResult)
    assert result.strict_result.evaluation_status == "failed"
    assert result.strict_result.coverage_status == "unknown"
    assert result.strict_result.diagnostic == "missing_strict_inputs=coverage_basis"


def test_missing_window_fails_closed_without_inference() -> None:
    result, _ = _preview(manifests=[_manifest()], start=None)

    assert isinstance(result, PaymentCoverageDualTrackResult)
    assert result.strict_result.evaluation_status == "failed"
    assert result.strict_result.coverage_status == "unknown"
    assert result.strict_result.diagnostic == "missing_strict_inputs=required_start"


def test_strict_validation_failure_preserves_final_legacy_preview() -> None:
    legacy, _ = _preview(strict_evaluation=False)
    result, _ = _preview(
        manifests=[_manifest()],
        start=date(2025, 2, 1),
        end=date(2025, 1, 31),
    )

    assert isinstance(result, PaymentCoverageDualTrackResult)
    assert result.legacy_result == legacy
    assert result.strict_result.evaluation_status == "failed"
    assert result.strict_result.coverage_status == "unknown"
    assert "invalid_required_window" in (result.strict_result.diagnostic or "")


def test_strict_runtime_failure_preserves_final_legacy_preview(monkeypatch) -> None:
    legacy, _ = _preview(strict_evaluation=False)

    def fail(*args, **kwargs):
        raise RuntimeError("strict service unavailable")

    monkeypatch.setattr(dual_track, "evaluate_strict_payment_coverage", fail)

    result, _ = _preview(manifests=[_manifest()])

    assert isinstance(result, PaymentCoverageDualTrackResult)
    assert result.legacy_result == legacy
    assert result.strict_result.evaluation_status == "failed"
    assert result.strict_result.coverage_status == "unknown"
    assert result.strict_result.diagnostic == "RuntimeError: strict service unavailable"


def test_basis_mismatch_is_unknown_not_cross_basis_evaluation() -> None:
    result, _ = _preview(
        manifests=[_manifest(basis="message_date")],
        basis="transaction_date",
    )

    assert isinstance(result, PaymentCoverageDualTrackResult)
    assert result.strict_result.coverage_status == "unknown"
    assert result.strict_result.overall_result is not None
    assert result.strict_result.overall_result.reason == "required_provider_result_missing"


def test_imported_data_and_amazon_gmail_are_not_normalized_to_amazon() -> None:
    result, _ = _preview(
        manifests=[
            _manifest(source="imported_data"),
            _manifest(source="amazon_gmail"),
        ],
        required=("amazon",),
    )

    assert isinstance(result, PaymentCoverageDualTrackResult)
    assert result.strict_result.coverage_status == "unknown"
    assert result.strict_result.overall_result is not None
    assert result.strict_result.overall_result.required_providers == ("amazon",)


def test_runtime_passes_explicit_strict_inputs_to_context(monkeypatch) -> None:
    calls = []
    original = status_preview.build_payment_coverage_context

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(status_preview, "build_payment_coverage_context", spy)

    result, _ = _preview(manifests=[_manifest()])

    assert isinstance(result, PaymentCoverageDualTrackResult)
    assert len(calls) == 1
    assert calls[0]["strict_evaluation"] is True
    assert calls[0]["strict_required_providers"] == ("paypay",)
    assert calls[0]["strict_coverage_basis"] == "transaction_date"
    assert calls[0]["strict_required_start"] == START
    assert calls[0]["strict_required_end"] == END
