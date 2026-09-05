"""Private, transient evidence for medical payment resolution.

Candidate generation is unchanged. This layer can only veto or deduplicate
existing proposals; it never repairs digits, labels, or document layout.
Neither raw text nor rejected numeric values are retained.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Literal

from .medical_receipt_privacy import (
    PaymentAmountCandidate, PaymentAmountResolution, PaymentDiagnosticCode,
    _StructuredOcrToken, _EXCLUDED_AMOUNT_CONTEXT, _EXCLUDED_COMPOUND_PAYMENT_RE,
    _MIN_STRUCTURED_AMOUNT_CONFIDENCE, _compact_ocr_token,
    _payment_labels_on_line, _structured_amount,
    _structured_label_matches, _structured_line_key,
    extract_medical_payment_candidates, extract_structured_medical_payment_candidates,
    resolve_medical_payment_candidates,
)

Channel = Literal["text", "structured"]
Scope = Literal["payment_region", "possible_payment_region", "excluded", "unassigned"]
NumericState = Literal["valid_numeric", "low_confidence_numeric", "malformed_numeric"]
# These terms only mark unresolved payment context; they NEVER authorize a value.
_POSSIBLE_PAYMENT_CONTEXT = ("自費", "負担金", "請求額", "合計", "領収", "支払")
_NUMERIC_RUN = re.compile(
    r"[-−+]?\s*¥?\s*[A-Za-z]*[0-9][0-9A-Za-z,.'’_/+−-]*(?:\s*円)?"
    r"|[?？□�OIl]+\s*円|¥\s*[?？□�OIl]+"
)
_NON_PAYMENT_CONTEXT = ("小計", "税額", "消費税")


@dataclass(frozen=True)
class NumericObservation:
    state: NumericState
    # Ordinals and geometry are private, transient provenance, not business data.
    ordinal: int
    bbox: tuple[float, float, float, float] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class PaymentRegionEvidence:
    channel: Channel
    region_index: int
    scope: Scope
    page: int | None = None
    line_key: tuple[int, int, int, int] | None = field(default=None, repr=False)
    observations: tuple[NumericObservation, ...] = field(default=(), repr=False)
    candidates: tuple[PaymentAmountCandidate, ...] = field(default=(), repr=False)
    diagnostic_codes: tuple[PaymentDiagnosticCode, ...] = ()
    # Both OCR APIs describe ONE source observation, never independent votes.
    observation_group: int = 0
    correspondence_id: int | None = field(default=None, repr=False)

    @property
    def payment_relevant(self) -> bool:
        return self.scope in {"payment_region", "possible_payment_region"}


@dataclass(frozen=True)
class PaymentEvidence:
    regions: tuple[PaymentRegionEvidence, ...] = field(repr=False)
    structured_observed: bool
    observation_complete: bool
    legacy_resolution: PaymentAmountResolution = field(repr=False)


def _scope(compact: str, has_label: bool) -> Scope:
    if (any(s in compact for s in _EXCLUDED_AMOUNT_CONTEXT + _NON_PAYMENT_CONTEXT)
            or _EXCLUDED_COMPOUND_PAYMENT_RE.search(compact)):
        return "excluded"
    if has_label:
        return "payment_region"
    if any(s in compact for s in _POSSIBLE_PAYMENT_CONTEXT):
        return "possible_payment_region"
    return "unassigned"


def _numeric_state(value: str, confidence: float | None = None) -> NumericState:
    if confidence is not None and (
        not math.isfinite(confidence) or confidence < _MIN_STRUCTURED_AMOUNT_CONFIDENCE
    ):
        return "low_confidence_numeric"
    return "valid_numeric" if _structured_amount(value.strip()) is not None else "malformed_numeric"


def _looks_numeric(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value).strip()
    return normalized not in {"", "¥", "円"} and bool(
        re.search(r"[0-9¥]", normalized) or normalized.endswith("円")
    )


def _numeric_diagnostics(observations: tuple[NumericObservation, ...]) -> set[PaymentDiagnosticCode]:
    reasons: set[PaymentDiagnosticCode] = set()
    if not observations:
        reasons.add("amount_not_observed")
    if any(o.state == "low_confidence_numeric" for o in observations):
        reasons.add("amount_observation_low_confidence")
    if len(observations) > 1 or any(o.state == "malformed_numeric" for o in observations):
        reasons.add("ambiguous_numeric_observations")
    return reasons


def _valid_geometry(token: _StructuredOcrToken) -> bool:
    return (
        all(math.isfinite(v) for v in (
            token.x, token.y, token.width, token.height, token.confidence
        ))
        and token.page >= 1 and token.x >= 0 and token.y >= 0
        and token.width > 0 and token.height > 0 and 0 <= token.confidence <= 100
    )


def _aligned(left: _StructuredOcrToken, right: _StructuredOcrToken) -> bool:
    # This is a veto on the existing same-line pairing, not a new detector.
    overlap = min(left.y + left.height, right.y + right.height) - max(left.y, right.y)
    return overlap >= 0.5 * min(left.height, right.height)


def collect_payment_evidence(
    text: str,
    tokens: tuple[_StructuredOcrToken, ...] = (),
    *,
    observation_complete: bool = True,
) -> PaymentEvidence:
    regions: list[PaymentRegionEvidence] = []
    # Raw normalized lines are used only here for exact, unique correspondence;
    # they are not retained in the returned evidence or any diagnostic output.
    alignment: dict[str, dict[Channel, list[int]]] = {}
    for index, raw in enumerate(text.splitlines()):
        line = unicodedata.normalize("NFKC", raw)
        labels = _payment_labels_on_line(line)
        scope = _scope(_compact_ocr_token(line), bool(labels))
        alignment.setdefault(_compact_ocr_token(line), {"text": [], "structured": []})["text"].append(len(regions))
        observations = tuple(
            NumericObservation(_numeric_state(m.group()), ordinal)
            for ordinal, m in enumerate(_NUMERIC_RUN.finditer(line))
        )
        candidates = tuple(
            PaymentAmountCandidate(**{**c.model_dump(), "source_line_index": index})
            for c in extract_medical_payment_candidates(line)
        )
        reasons: set[PaymentDiagnosticCode] = set()
        if scope in {"payment_region", "possible_payment_region"}:
            reasons.update(_numeric_diagnostics(observations))
            if scope == "possible_payment_region" or len(labels) != 1:
                reasons.add("structural_relationship_unresolved")
            elif observations and not candidates:
                reasons.add("structural_relationship_unresolved")
        # Excluded regions remain available for shadow diagnostics, never proposals.
        regions.append(PaymentRegionEvidence(
            "text", index, scope, observations=observations,
            candidates=candidates if scope == "payment_region" else (),
            diagnostic_codes=tuple(sorted(reasons)),
        ))

    by_line: dict[tuple[int, tuple[int, int, int, int]], list[_StructuredOcrToken]] = {}
    for token in tokens:
        by_line.setdefault(_structured_line_key(token), []).append(token)
    for index, ((page, line_key), line_tokens) in enumerate(sorted(by_line.items())):
        ordered = tuple(sorted(line_tokens, key=lambda t: t.x))
        compact = "".join(_compact_ocr_token(t.text) for t in ordered)
        alignment.setdefault(compact, {"text": [], "structured": []})["structured"].append(len(regions))
        valid_geometry = all(_valid_geometry(t) for t in ordered)
        labels = _structured_label_matches(ordered) if valid_geometry else []
        # Observe low-confidence labels too, without accepting them.
        raw_labels = _payment_labels_on_line(compact)
        scope = _scope(compact, bool(labels or raw_labels))
        observations = tuple(
            NumericObservation(_numeric_state(t.text, t.confidence), ordinal,
                               (t.x, t.y, t.width, t.height))
            for ordinal, t in enumerate(ordered)
            if _looks_numeric(t.text)
        )
        candidates = tuple(
            PaymentAmountCandidate(**{**c.model_dump(), "source_line_index": index})
            for c in extract_structured_medical_payment_candidates(ordered)
        ) if valid_geometry else ()
        reasons = set()
        if scope in {"payment_region", "possible_payment_region"}:
            reasons.update(_numeric_diagnostics(observations))
            if not valid_geometry:
                reasons.add("observation_incomplete")
            if scope == "possible_payment_region" or len(labels) != 1:
                reasons.add("structural_relationship_unresolved")
            if candidates and len(labels) == 1:
                numeric_tokens = [t for t in ordered if _structured_amount(t.text) is not None]
                if any(not _aligned(labels[0].token, t) for t in numeric_tokens):
                    reasons.add("structural_relationship_unresolved")
            elif observations:
                reasons.add("structural_relationship_unresolved")
        regions.append(PaymentRegionEvidence(
            "structured", index, scope, page, line_key, observations,
            candidates if scope == "payment_region" else (), tuple(sorted(reasons)),
        ))
    for compact, channels in alignment.items():
        if compact and len(channels["text"]) == len(channels["structured"]) == 1:
            text_index, structured_index = channels["text"][0], channels["structured"][0]
            regions[text_index] = replace(regions[text_index], correspondence_id=text_index)
            regions[structured_index] = replace(regions[structured_index], correspondence_id=text_index)
    legacy = resolve_medical_payment_candidates(
        extract_medical_payment_candidates(text) + extract_structured_medical_payment_candidates(tokens)
    )
    return PaymentEvidence(tuple(regions), bool(tokens), observation_complete, legacy)


def resolve_payment_evidence(evidence: PaymentEvidence) -> PaymentAmountResolution:
    relevant = [r for r in evidence.regions if r.payment_relevant]
    candidates = [c for r in relevant for c in r.candidates]
    reasons: set[PaymentDiagnosticCode] = {
        code for r in relevant for code in r.diagnostic_codes
    }
    if not relevant:
        reasons.add("payment_label_not_observed")
    if not evidence.observation_complete:
        reasons.add("observation_incomplete")

    # All payment hypotheses concern the same document total. Unknown account/
    # date/point rows are unassigned or explicitly excluded, not global vetoes.
    # Without a layout detector we cannot assert a relationship between a
    # text-only proposal and a missing/contradictory structured payment region.
    # Correspondence requires unique, exact normalized line content in the
    # collector. Matching amounts or labels alone cannot establish a region.
    text_pairs = [(r, c) for r in relevant if r.channel == "text" for c in r.candidates]
    structured_pairs = [(r, c) for r in relevant if r.channel == "structured" for c in r.candidates]

    def counterparts(region: PaymentRegionEvidence, candidate: PaymentAmountCandidate):
        return [c for r, c in structured_pairs
                if region.correspondence_id is not None
                and r.correspondence_id == region.correspondence_id
                and (c.amount, c.label_type, c.strength) ==
                    (candidate.amount, candidate.label_type, candidate.strength)]

    if evidence.structured_observed:
        for region, candidate in text_pairs:
            if len(counterparts(region, candidate)) != 1:
                reasons.add("observation_incomplete")

    # Deduplicate one-to-one channel views. Never combine OCR confidence or count
    # the two calls to the same engine as separate votes.
    deduplicated = [c for _, c in structured_pairs]
    for region, candidate in text_pairs:
        if len(counterparts(region, candidate)) != 1:
            deduplicated.append(candidate)

    baseline = resolve_medical_payment_candidates(deduplicated)
    if len({c.amount for c in candidates}) > 1:
        reasons.add("conflicting_payment_candidates")
    # This phase must not turn any legacy review into a confirmation, including
    # by dropping an excluded proposal which formerly conflicted with another.
    if evidence.legacy_resolution.status != "confirmed":
        if evidence.legacy_resolution.reason_code == "conflicting_candidates":
            reasons.add("conflicting_payment_candidates")
        elif not reasons:
            return evidence.legacy_resolution
    if reasons:
        return PaymentAmountResolution(
            status="needs_review", amount=None, candidate_count=len(deduplicated),
            reason_code=("conflicting_candidates" if "conflicting_payment_candidates" in reasons
                         else "weak_candidate_only" if baseline.reason_code == "weak_candidate_only"
                         else "no_candidate"),
            diagnostic_codes=tuple(sorted(reasons)),
        )
    return baseline
