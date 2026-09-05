"""Offline comparison only: no acquisition, persistence, transport or promotion."""
from __future__ import annotations

from typing import get_args

from .medical_layout_shadow import LayoutShadow, PageFrame, observe_layout
from .medical_payment_evidence import collect_payment_evidence, resolve_payment_evidence
from .medical_receipt_privacy import PaymentDiagnosticCode, _StructuredOcrToken


def _empty_summary() -> dict[str, int]:
    return {
        "evaluation_failed": 0,
        "ocr_observation_groups": 1,
        "production_confirmed": 0,
        "production_needs_review": 0,
        "production_unresolved_regions": 0,
        # Channel views, not independent votes or unique document regions.
        **{f"production_{scope}_views": 0 for scope in
           ("payment_region", "possible_payment_region", "excluded", "unassigned")},
        **{f"production_{code}": 0 for code in get_args(PaymentDiagnosticCode)},
        **{f"shadow_{key}": 0 for key in LayoutShadow((), (), (), ()).aggregate()},
    }


def evaluate_medical_layout(
    text: str, tokens: tuple[_StructuredOcrToken, ...], frames: tuple[PageFrame, ...],
    *, expected_pages: int, observation_complete: bool,
) -> dict[str, int]:
    """Compare one source OCR group without joining regions or returning amounts.

    Text and structured tokens must describe the same source observation. The
    shadow is never passed to the resolver. Output is a fixed integer allowlist;
    consumers must check evaluation_failed before interpreting baseline counts.
    """
    result = _empty_summary()
    try:
        if type(text) is not str:
            raise ValueError()
        shadow = observe_layout(tokens, frames, expected_pages=expected_pages,
                                observation_complete=observation_complete)
        result.update({f"shadow_{key}": value for key, value in shadow.aggregate().items()})
        if "observation_incomplete" in shadow.issues:
            result["evaluation_failed"] = 1
            return result
        evidence = collect_payment_evidence(text, tokens, observation_complete=observation_complete)
        resolution = resolve_payment_evidence(evidence)
        result["production_confirmed"] = int(resolution.status == "confirmed")
        result["production_needs_review"] = int(resolution.status == "needs_review")
        for code in get_args(PaymentDiagnosticCode):
            result[f"production_{code}"] = int(code in resolution.diagnostic_codes)
        for scope in ("payment_region", "possible_payment_region", "excluded", "unassigned"):
            result[f"production_{scope}_views"] = sum(r.scope == scope for r in evidence.regions)
        result["production_unresolved_regions"] = sum(bool(r.diagnostic_codes) for r in evidence.regions)
        return result
    except Exception:
        # Never publish partial baseline counters or exception details.
        result = _empty_summary()
        result["evaluation_failed"] = 1
        return result
