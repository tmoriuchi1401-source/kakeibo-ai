from __future__ import annotations

from datetime import date

import app.payment_coverage_dual_track as dual_track
import app.payment_coverage_status_preview as status_preview
from app.coverage_confirmation import COVERAGE_REASON_OPERATIONAL_ONLY
from app.payment_coverage_dual_track import PaymentCoverageDualTrackResult
from app.payment_coverage_manifest import CoverageManifest
from app.payment_coverage_status_preview import (
    build_payment_coverage_context,
    preview_payment_coverage_status,
)


START = date(2025, 1, 1)
END = date(2025, 1, 31)
AS_OF = date(2025, 1, 31)
SHA_A = "a" * 64
ORDER_ID = "249-4045234-9353402"


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


def _event(event_type: str) -> list:
    row = [""] * 24
    row[5] = event_type
    row[6] = ORDER_ID
    row[7] = "2025-01-01"
    return row


def _header() -> list:
    row = [""] * 15
    row[0] = ORDER_ID
    row[1] = "2025-01-01"
    return row


def _build(
    *,
    strict_evaluation: bool = True,
    manifests=(),
    required=("paypay",),
    basis="transaction_date",
    start=START,
    end=END,
):
    return build_payment_coverage_context(
        [],
        [_event("order"), _event("cancellation")],
        [_header()],
        as_of=AS_OF,
        strict_evaluation=strict_evaluation,
        strict_manifests=manifests,
        strict_required_providers=required,
        strict_coverage_basis=basis,
        strict_required_start=start,
        strict_required_end=end,
    )


def test_default_caller_path_does_not_call_dual_track(monkeypatch) -> None:
    def unexpected(*args, **kwargs):
        raise AssertionError("dual-track bridge called")

    monkeypatch.setattr(
        status_preview,
        "evaluate_payment_coverage_dual_track",
        unexpected,
    )

    result = _build(strict_evaluation=False, manifests=[_manifest()])

    assert isinstance(result, dict)
    assert set(result) == {"source_coverage", "orders"}
    assert result["orders"][0]["overall_payment_coverage_status"] == "incomplete"


def test_opt_in_preserves_legacy_result_and_adds_strict_track() -> None:
    legacy = _build(strict_evaluation=False)
    result = _build(manifests=[_manifest()])

    assert isinstance(result, PaymentCoverageDualTrackResult)
    assert result.legacy_result == legacy
    assert result.legacy_result["orders"][0][
        "overall_payment_coverage_status"
    ] == "incomplete"
    assert result.strict_result.coverage_status == "complete"


def test_paypay_user_attested_is_strict_unknown() -> None:
    result = _build(manifests=[_manifest(provider_proven=False)])

    assert isinstance(result, PaymentCoverageDualTrackResult)
    assert result.strict_result.evaluation_status == "evaluated"
    assert result.strict_result.coverage_status == "unknown"


def test_provider_proven_full_window_is_strict_complete() -> None:
    result = _build(manifests=[_manifest()])

    assert isinstance(result, PaymentCoverageDualTrackResult)
    assert result.strict_result.coverage_status == "complete"


def test_provider_proof_gap_is_strict_incomplete() -> None:
    result = _build(manifests=[_manifest(end="2025-01-10")])

    assert isinstance(result, PaymentCoverageDualTrackResult)
    assert result.strict_result.coverage_status == "incomplete"


def test_required_providers_omitted_is_explicitly_not_evaluated() -> None:
    result = _build(manifests=[_manifest()], required=None)

    assert isinstance(result, PaymentCoverageDualTrackResult)
    assert result.strict_result.evaluation_status == "not_evaluated"
    assert result.strict_result.coverage_status is None


def test_missing_basis_fails_closed_without_inference() -> None:
    result = _build(manifests=[_manifest()], basis=None)

    assert isinstance(result, PaymentCoverageDualTrackResult)
    assert result.strict_result.evaluation_status == "failed"
    assert result.strict_result.coverage_status == "unknown"
    assert result.strict_result.diagnostic == "missing_strict_inputs=coverage_basis"


def test_missing_window_fails_closed_without_inference() -> None:
    result = _build(manifests=[_manifest()], start=None)

    assert isinstance(result, PaymentCoverageDualTrackResult)
    assert result.strict_result.evaluation_status == "failed"
    assert result.strict_result.coverage_status == "unknown"
    assert result.strict_result.diagnostic == "missing_strict_inputs=required_start"


def test_strict_validation_failure_does_not_change_legacy_result() -> None:
    legacy = _build(strict_evaluation=False)
    result = _build(
        manifests=[_manifest()],
        start=date(2025, 2, 1),
        end=date(2025, 1, 31),
    )

    assert isinstance(result, PaymentCoverageDualTrackResult)
    assert result.legacy_result == legacy
    assert result.strict_result.evaluation_status == "failed"
    assert result.strict_result.coverage_status == "unknown"
    assert "invalid_required_window" in (result.strict_result.diagnostic or "")


def test_strict_runtime_failure_does_not_change_legacy_result(monkeypatch) -> None:
    legacy = _build(strict_evaluation=False)

    def fail(*args, **kwargs):
        raise RuntimeError("strict service unavailable")

    monkeypatch.setattr(dual_track, "evaluate_strict_payment_coverage", fail)

    result = _build(manifests=[_manifest()])

    assert isinstance(result, PaymentCoverageDualTrackResult)
    assert result.legacy_result == legacy
    assert result.strict_result.evaluation_status == "failed"
    assert result.strict_result.coverage_status == "unknown"
    assert result.strict_result.diagnostic == "RuntimeError: strict service unavailable"


def test_basis_mismatch_is_unknown_not_cross_basis_evaluation() -> None:
    result = _build(
        manifests=[_manifest(basis="message_date")],
        basis="transaction_date",
    )

    assert isinstance(result, PaymentCoverageDualTrackResult)
    assert result.strict_result.coverage_status == "unknown"
    assert result.strict_result.overall_result is not None
    assert result.strict_result.overall_result.reason == "required_provider_result_missing"


def test_imported_data_and_amazon_gmail_are_not_normalized_to_amazon() -> None:
    result = _build(
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


def test_internal_caller_delegates_to_existing_dual_track_bridge(monkeypatch) -> None:
    calls = []
    original = status_preview.evaluate_payment_coverage_dual_track

    def spy(legacy_result, **kwargs):
        calls.append((legacy_result, kwargs))
        return original(legacy_result, **kwargs)

    monkeypatch.setattr(
        status_preview,
        "evaluate_payment_coverage_dual_track",
        spy,
    )

    result = _build(manifests=[_manifest()])

    assert isinstance(result, PaymentCoverageDualTrackResult)
    assert len(calls) == 1
    assert calls[0][0] is result.legacy_result
    assert calls[0][1]["required_providers"] == ("paypay",)
    assert calls[0][1]["coverage_basis"] == "transaction_date"
    assert calls[0][1]["required_start"] == START
    assert calls[0][1]["required_end"] == END


class _ReadOnlyDB:
    def __init__(self) -> None:
        self.values = {
            "取込データ!A2:L": [],
            "Amazonイベント!A2:X": [],
            "Amazon注文ヘッダ!A2:O": [],
        }

    def get(self, rng):
        return self.values[rng]


def test_existing_preview_output_contract_remains_plain_dict() -> None:
    result = preview_payment_coverage_status(_ReadOnlyDB(), as_of=AS_OF)

    assert isinstance(result, dict)
    assert "legacy_result" not in result
    assert "strict_result" not in result
    assert result["source_count"] == 5
