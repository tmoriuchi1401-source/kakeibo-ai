"""Conservative, local-only Level 2 payment-role hypotheses.

This module is deliberately outside the production resolver.  It observes one
complete OCR materialization and emits only shadow hypotheses; it never returns
an amount, changes ``needs_review``, persists data, or calls a transport.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import math
import secrets
import unicodedata
from typing import Literal

from .medical_ocr_observation_shadow import OcrObservation, TextRegion
from .medical_payment_evidence import _NUMERIC_RUN, _POSSIBLE_PAYMENT_CONTEXT
from .medical_receipt_privacy import (
    _EXCLUDED_AMOUNT_CONTEXT,
    _EXCLUDED_COMPOUND_PAYMENT_RE,
    _compact_ocr_token,
    _exact_strong_structured_label_match,
    _structured_amount,
)

_MIN_HIGH_CONFIDENCE = 0.90
# A positive relationship must sit inside the conservative inner boundary.
# The old row=4/column=2.5 cutoffs are not widened: the new STRONG boundary is
# smaller, while a surrounding uncertainty band exists only to retain blockers.
_STRONG_ROW_GAP_HEIGHT = 3.0
_UNCERTAIN_ROW_GAP_HEIGHT = 5.0
_STRONG_COLUMN_GAP_HEIGHT = 2.0
_UNCERTAIN_COLUMN_GAP_HEIGHT = 3.0
_STRONG_ALIGNMENT = 0.60
_UNCERTAIN_ALIGNMENT = 0.40
_ALLOWED_CLASSIFICATIONS = {"medical"}
# Shadow-only conservative additions.  These are blockers, never labels or
# candidate evidence, so extending them cannot authorize a value.
_LOCAL_NEGATIVE = ("小計", "税", "預り", "預かり", "お釣", "釣銭", "保険", "公費",
                   "点数", "現金", "単価", "数量")


@dataclass(frozen=True)
class MedicalPaymentShadowCandidate:
    """Data-minimized diagnostic handle; the numeric value stays in the source."""

    candidate_id: str
    evidence_level: Literal[2] = 2
    strong_label_evidence: Literal["exact_allowlisted"] = "exact_allowlisted"
    spatial_relation: Literal["row_right", "column_below"] = "row_right"
    confidence_category: Literal["high"] = "high"
    competitor_state: Literal["none"] = "none"
    exclusion_state: Literal["none_local"] = "none_local"
    completeness_state: Literal["complete"] = "complete"
    materialization_stability: Literal["unverified", "confirmed"] = "unverified"
    status: Literal["shadow_only"] = "shadow_only"
    # Receipt-local provenance is deliberately hidden and is not serializable
    # through aggregate().  It identifies a canonical equal-value group, not
    # independent votes and not a production PaymentAmountCandidate.
    observation_reference: tuple[int, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class Level2ShadowEvaluation:
    candidates: tuple[MedicalPaymentShadowCandidate, ...] = field(default=(), repr=False)
    blocked_competitor_count: int = 0
    blocked_negative_context_count: int = 0
    ambiguous_count: int = 0
    proposal_only_count: int = 0
    incomplete_count: int = 0
    unresolved_competitor_count: int = 0
    same_amount_competitor_count: int = 0
    payment_role_evidence_completeness: Literal["complete", "unresolved", "incomplete"] = "unresolved"
    materialization_stability: Literal["unverified", "confirmed", "conflicting"] = "unverified"
    evaluation_failed: int = 0

    def aggregate(self) -> dict[str, int]:
        return {
            "shadow_candidate_count": len(self.candidates),
            "blocked_competitor_count": self.blocked_competitor_count,
            "blocked_negative_context_count": self.blocked_negative_context_count,
            "ambiguous_count": self.ambiguous_count,
            "proposal_only_count": self.proposal_only_count,
            "incomplete_count": self.incomplete_count,
            "unresolved_competitor_count": self.unresolved_competitor_count,
            "same_amount_competitor_count": self.same_amount_competitor_count,
            "payment_role_evidence_complete": int(
                self.payment_role_evidence_completeness == "complete"),
            "materialization_stable": int(self.materialization_stability == "confirmed"),
            "evaluation_failed": self.evaluation_failed,
        }


@dataclass(frozen=True)
class _Numeric:
    ordinal: int
    value: int = field(repr=False)
    region: TextRegion = field(repr=False)


RelationState = Literal["STRONG", "UNCERTAIN", "UNRELATED"]


@dataclass(frozen=True)
class StructuralRelation:
    state: RelationState
    axis: Literal["row_right", "column_below"] | None = None
    gap_ratio: float | None = field(default=None, repr=False)
    alignment: float | None = field(default=None, repr=False)


def _whole_numeric(region: TextRegion) -> int | None:
    text = unicodedata.normalize("NFKC", region.text).strip()
    return _structured_amount(text)


def _safe_box(region: TextRegion) -> tuple[float, float, float, float] | None:
    box = region.bbox
    if box is None or not all(math.isfinite(v) for v in box) or min(box[2:]) <= 0:
        return None
    return box


def classify_structural_relation(label: TextRegion, number: TextRegion) -> StructuralRelation:
    """Classify an ordered pair without turning a boundary miss into absence.

    STRONG is the only positive evidence.  UNCERTAIN is retained solely as a
    blocker.  It can never create a shadow candidate.
    """
    lb, nb = _safe_box(label), _safe_box(number)
    if lb is None or nb is None:
        return StructuralRelation("UNRELATED")
    lx, ly, lw, lh = lb
    nx, ny, nw, nh = nb
    vertical_overlap = min(ly + lh, ny + nh) - max(ly, ny)
    horizontal_overlap = min(lx + lw, nx + nw) - max(lx, nx)
    row_alignment = vertical_overlap / min(lh, nh)
    row_gap = (nx - (lx + lw)) / max(lh, nh)
    if row_alignment >= _UNCERTAIN_ALIGNMENT and 0 <= row_gap <= _UNCERTAIN_ROW_GAP_HEIGHT:
        state: RelationState = ("STRONG" if row_alignment >= _STRONG_ALIGNMENT
                                and row_gap <= _STRONG_ROW_GAP_HEIGHT else "UNCERTAIN")
        return StructuralRelation(state, "row_right", row_gap, row_alignment)
    column_alignment = horizontal_overlap / min(lw, nw)
    column_gap = (ny - (ly + lh)) / max(lh, nh)
    if (column_alignment >= _UNCERTAIN_ALIGNMENT
            and 0 <= column_gap <= _UNCERTAIN_COLUMN_GAP_HEIGHT):
        state = ("STRONG" if column_alignment >= _STRONG_ALIGNMENT
                 and column_gap <= _STRONG_COLUMN_GAP_HEIGHT else "UNCERTAIN")
        return StructuralRelation(state, "column_below", column_gap, column_alignment)
    return StructuralRelation("UNRELATED")


def _relation(label: TextRegion, number: TextRegion) -> str | None:
    """Compatibility helper: expose only positive STRONG axes."""
    relation = classify_structural_relation(label, number)
    return relation.axis if relation.state == "STRONG" else None


def _between(label: TextRegion, number: TextRegion, other: TextRegion, relation: str) -> bool:
    lb, nb, ob = _safe_box(label), _safe_box(number), _safe_box(other)
    if lb is None or nb is None or ob is None:
        return False
    lx, ly, lw, lh = lb; nx, ny, nw, nh = nb; ox, oy, ow, oh = ob
    if relation == "row_right":
        overlap = min(ly + lh, oy + oh) - max(ly, oy)
        return overlap >= _UNCERTAIN_ALIGNMENT * min(lh, oh) and lx + lw <= ox and ox + ow <= nx
    overlap = min(lx + lw, ox + ow) - max(lx, ox)
    return overlap >= _UNCERTAIN_ALIGNMENT * min(lw, ow) and ly + lh <= oy and oy + oh <= ny


def _locally_related(left: TextRegion, right: TextRegion) -> bool:
    return (classify_structural_relation(left, right).state != "UNRELATED"
            or classify_structural_relation(right, left).state != "UNRELATED")


def _strong_relation(left: TextRegion, right: TextRegion) -> StructuralRelation | None:
    relation = classify_structural_relation(left, right)
    return relation if relation.state == "STRONG" else None


def _blocking_relation(left: TextRegion, right: TextRegion) -> StructuralRelation | None:
    direct = classify_structural_relation(left, right)
    if direct.state != "UNRELATED":
        return direct
    reverse = classify_structural_relation(right, left)
    return reverse if reverse.state != "UNRELATED" else None


def _is_negative(region: TextRegion) -> bool:
    compact = _compact_ocr_token(region.text)
    return any(value in compact for value in _EXCLUDED_AMOUNT_CONTEXT + _LOCAL_NEGATIVE) or bool(
        _EXCLUDED_COMPOUND_PAYMENT_RE.search(compact)
    )


def _is_possible_payment(region: TextRegion) -> bool:
    compact = _compact_ocr_token(region.text)
    return any(value in compact for value in _POSSIBLE_PAYMENT_CONTEXT)


def evaluate_level2_payment_shadow(
    observation: OcrObservation,
    *,
    classification: str = "medical",
) -> Level2ShadowEvaluation:
    """Return at most one Level 2 hypothesis, otherwise fail closed.

    A geometric relationship is a bounded local grouping, never a claimed
    table cell.  Equal numeric values are grouped before ambiguity checks and
    therefore cannot become multiple votes.
    """
    try:
        return _evaluate(observation, classification)
    except Exception:
        return Level2ShadowEvaluation(evaluation_failed=1)


def _evaluate(observation: OcrObservation, classification: str) -> Level2ShadowEvaluation:
    if type(observation) is not OcrObservation or classification not in _ALLOWED_CLASSIFICATIONS:
        return Level2ShadowEvaluation(proposal_only_count=1)
    if not observation.complete:
        return Level2ShadowEvaluation(incomplete_count=1,
            payment_role_evidence_completeness="incomplete")

    labels = [r for r in observation.regions
              if r.confidence is not None and r.confidence >= _MIN_HIGH_CONFIDENCE
              and _exact_strong_structured_label_match(r.text) is not None]
    numerics = [_Numeric(r.ordinal, amount, r) for r in observation.regions
                if r.confidence is not None and r.confidence >= _MIN_HIGH_CONFIDENCE
                if (amount := _whole_numeric(r)) is not None]
    malformed = [r for r in observation.regions if _NUMERIC_RUN.search(
        unicodedata.normalize("NFKC", r.text)) and _whole_numeric(r) is None]
    if len(labels) != 1 or not numerics:
        return Level2ShadowEvaluation(proposal_only_count=int(bool(numerics)),
                                      ambiguous_count=int(len(labels) > 1))

    label = labels[0]
    groups: dict[int, list[_Numeric]] = defaultdict(list)
    for numeric in numerics:
        groups[numeric.value].append(numeric)

    eligible: list[tuple[list[_Numeric], str]] = []
    negative_blocks = competitor_blocks = unresolved_competitors = same_amount_competitors = 0
    for group in groups.values():
        uncertain_positive = [item for item in group
                              if classify_structural_relation(label, item.region).state == "UNCERTAIN"]
        unresolved_competitors += int(bool(uncertain_positive))
        same_amount_competitors += int(bool(uncertain_positive))
        related = [(item, relation.axis) for item in group
                   if (relation := _strong_relation(label, item.region)) is not None]
        if not related:
            continue
        # A same-value observation with only an uncertain relation is still
        # an unresolved competitor.  It must block the group; otherwise a
        # strong duplicate could incorrectly turn incomplete geometry into a
        # complete positive.
        if uncertain_positive:
            competitor_blocks += 1
            continue
        relations = {relation for _, relation in related}
        if len(relations) != 1:
            competitor_blocks += 1
            continue
        relation = next(iter(relations))
        # The bounded corridor and its immediate pairwise neighborhood define
        # the summary grouping.  This is observable geometry, not a cell claim.
        other_numbers = [item for item in numerics if item.value != group[0].value]
        numeric_blockers = [(other, classify_structural_relation(label, other.region))
                            for other in other_numbers]
        if any(_between(label, item.region, other.region, relation)
               or other_relation.state in {"STRONG", "UNCERTAIN"}
               for item, _ in related for other, other_relation in numeric_blockers):
            unresolved_competitors += int(any(r.state == "UNCERTAIN" for _, r in numeric_blockers))
            competitor_blocks += 1
            continue
        malformed_blockers = [(other, _blocking_relation(label, other)) for other in malformed]
        if any(_between(label, item.region, other, relation)
               or other_relation is not None
               for item, _ in related for other, other_relation in malformed_blockers):
            unresolved_competitors += int(any(r is not None and r.state == "UNCERTAIN"
                                              for _, r in malformed_blockers))
            competitor_blocks += 1
            continue
        local_regions = [r for r in observation.regions
                         if r.ordinal not in {label.ordinal, *(i.ordinal for i in group)}
                         and (_locally_related(label, r)
                              or any(_locally_related(item.region, r) for item, _ in related))]
        if any(_is_negative(r) for r in (label, *(i.region for i in group), *local_regions)):
            negative_blocks += 1
            continue
        # A possible-payment text fragment is not itself a competing numeric.
        # It blocks only when that same observed region contains numeric
        # evidence; proximity alone must neither authorize nor veto a value.
        possible_numeric = [r for r in local_regions if _is_possible_payment(r)
                            and _NUMERIC_RUN.search(unicodedata.normalize("NFKC", r.text))]
        if possible_numeric:
            unresolved_competitors += int(any(
                (rel := _blocking_relation(item.region, r)) is not None
                and rel.state == "UNCERTAIN"
                for item, _ in related for r in possible_numeric))
            competitor_blocks += 1
            continue
        eligible.append((group, relation))

    if len(eligible) != 1:
        return Level2ShadowEvaluation(
            blocked_competitor_count=competitor_blocks,
            blocked_negative_context_count=negative_blocks,
            ambiguous_count=int(len(eligible) > 1),
            proposal_only_count=int(bool(numerics)),
            unresolved_competitor_count=unresolved_competitors,
            same_amount_competitor_count=same_amount_competitors,
            payment_role_evidence_completeness="unresolved",
        )
    group, relation = eligible[0]
    candidate = MedicalPaymentShadowCandidate(
        candidate_id=secrets.token_urlsafe(16),
        spatial_relation=relation,  # type: ignore[arg-type]
        observation_reference=tuple(sorted(item.ordinal for item in group)),
    )
    return Level2ShadowEvaluation(
        candidates=(candidate,),
        blocked_competitor_count=competitor_blocks,
        blocked_negative_context_count=negative_blocks,
        unresolved_competitor_count=unresolved_competitors,
        same_amount_competitor_count=same_amount_competitors,
        payment_role_evidence_completeness="complete",
    )


def evaluate_materialization_stable_level2_shadow(
    observations: tuple[OcrObservation, ...],
    *,
    same_source_confirmed: bool,
) -> Level2ShadowEvaluation:
    """Intersect positive evidence and union blockers across materializations.

    This diagnostic API requires an explicit, pre-established same-source fact.
    Any incomplete/unresolved member or any blocker prevents consensus.  It is
    not imported by production and cannot promote a production resolution.
    """
    if (type(observations) is not tuple or len(observations) < 2
            or same_source_confirmed is not True):
        return Level2ShadowEvaluation(payment_role_evidence_completeness="incomplete",
                                      materialization_stability="conflicting")
    results = tuple(evaluate_level2_payment_shadow(item) for item in observations)
    blocked = sum(item.blocked_competitor_count for item in results)
    negative = sum(item.blocked_negative_context_count for item in results)
    unresolved = sum(item.unresolved_competitor_count for item in results)
    same_amount = sum(item.same_amount_competitor_count for item in results)
    if (blocked or negative or unresolved or any(len(item.candidates) != 1 for item in results)):
        return Level2ShadowEvaluation(
            blocked_competitor_count=blocked,
            blocked_negative_context_count=negative,
            unresolved_competitor_count=unresolved,
            same_amount_competitor_count=same_amount,
            payment_role_evidence_completeness="unresolved",
            materialization_stability="conflicting",
        )
    values = []
    axes = []
    for observation, result in zip(observations, results, strict=True):
        candidate = result.candidates[0]
        selected = [r for r in observation.regions
                    if r.ordinal in candidate.observation_reference]
        value_set = {_whole_numeric(r) for r in selected}
        if len(value_set) != 1 or None in value_set:
            return Level2ShadowEvaluation(payment_role_evidence_completeness="unresolved",
                                          materialization_stability="conflicting")
        values.append(next(iter(value_set)))
        axes.append(candidate.spatial_relation)
    if len(set(values)) != 1 or len(set(axes)) != 1:
        return Level2ShadowEvaluation(payment_role_evidence_completeness="unresolved",
                                      materialization_stability="conflicting")
    first = results[0].candidates[0]
    stable = MedicalPaymentShadowCandidate(
        candidate_id=secrets.token_urlsafe(16),
        spatial_relation=first.spatial_relation,
        materialization_stability="confirmed",
        observation_reference=first.observation_reference,
    )
    return Level2ShadowEvaluation(candidates=(stable,),
        payment_role_evidence_completeness="complete",
        materialization_stability="confirmed")
