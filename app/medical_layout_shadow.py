"""Local, transient layout observations. Never an amount or AI authorization API.

Page frames must describe the exact coordinate space of the supplied OCR pass.
There is no image reader, transport, logger, persistence, or production caller.
Relationships are hypotheses, not inferred table cells or independent votes.
"""
from __future__ import annotations

import math
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from .medical_payment_evidence import _looks_numeric, _numeric_state
from .medical_receipt_privacy import _StructuredOcrToken


@dataclass(frozen=True)
class PageFrame:
    page: int
    width: float
    height: float


@dataclass(frozen=True)
class LayoutRegion:
    ordinal: int
    page: int
    kind: Literal["numeric_like", "label_like", "unreadable"]
    box: tuple[float, float, float, float] | None = field(repr=False)
    numeric_state: str | None = None
    negative_context: bool = False
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class LayoutRelation:
    left: int
    right: int
    axis: Literal["row", "column", "overlap"]
    # Gap is relative to the smaller token height (horizontal) or width
    # (vertical). Alignment is overlap / smaller extent. No absolute pixels.
    relative_gap: float
    alignment: float


@dataclass(frozen=True)
class CellHypothesis:
    label: int
    numeric: int
    axis: str
    issues: tuple[str, ...]
    # Even a unique, high-quality geometric pairing does not prove payment role.
    status: Literal["unresolved"] = "unresolved"


@dataclass(frozen=True)
class LayoutShadow:
    regions: tuple[LayoutRegion, ...] = field(repr=False)
    relations: tuple[LayoutRelation, ...] = field(repr=False)
    hypotheses: tuple[CellHypothesis, ...] = field(repr=False)
    issues: tuple[str, ...]
    observation_group: int = 0

    def aggregate(self) -> dict[str, int]:
        """Explicit data-free allowlist; never serialize internal dataclasses."""
        return {
            "regions": len(self.regions),
            "numeric_regions": sum(r.kind == "numeric_like" for r in self.regions),
            "low_confidence_regions": sum("low_confidence" in r.issues for r in self.regions),
            "malformed_numeric_regions": sum(r.numeric_state == "malformed_numeric" for r in self.regions),
            "invalid_geometry_regions": sum(r.box is None for r in self.regions),
            "negative_context_regions": sum(r.negative_context for r in self.regions),
            "row_relations": sum(r.axis == "row" for r in self.relations),
            "column_relations": sum(r.axis == "column" for r in self.relations),
            "overlap_relations": sum(r.axis == "overlap" for r in self.relations),
            "unresolved_hypotheses": len(self.hypotheses),
            "competing_hypotheses": sum("competing_relationships" in h.issues for h in self.hypotheses),
            "observation_incomplete": int("observation_incomplete" in self.issues),
        }


# Negative hints only. They never erase a number or establish payment semantics.
# Split/garbled labels remain unproven; there is deliberately no fuzzy repair.
_NEGATIVE = ("点数", "医療費", "保険", "公費", "小計", "税", "預り", "預かり",
             "お釣", "釣銭", "自己負担", "一部負担", "自費")
_MAX_REGIONS = 512


def _finite_positive(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _box(token: _StructuredOcrToken, frame: PageFrame | None):
    if frame is None or not all(type(v) in (int, float) and math.isfinite(v)
                               for v in (token.x, token.y, token.width, token.height)):
        return None
    if (token.x < 0 or token.y < 0 or token.width <= 0 or token.height <= 0
            or token.x + token.width > frame.width or token.y + token.height > frame.height):
        return None
    return (token.x / frame.width, token.y / frame.height,
            (token.x + token.width) / frame.width, (token.y + token.height) / frame.height)


def _relation(a: LayoutRegion, b: LayoutRegion) -> LayoutRelation | None:
    if a.page != b.page or a.box is None or b.box is None:
        return None
    ax, ay, ar, ab = a.box
    bx, by, br, bb = b.box
    xo, yo = min(ar, br) - max(ax, bx), min(ab, bb) - max(ay, by)
    w, h = min(ar - ax, br - bx), min(ab - ay, bb - by)
    if xo > 0 and yo > 0:
        return LayoutRelation(a.ordinal, b.ordinal, "overlap", 0, min(xo / w, yo / h))
    # Horizontal gap divided by height must use one coordinate scale. Both
    # axes here are page-normalized; the caller supplies aspect-correct boxes
    # below by converting x into units of page height for this calculation.
    if yo / h >= 0.5 and -xo / h <= 8:
        return LayoutRelation(a.ordinal, b.ordinal, "row", max(0, -xo / h), yo / h)
    if xo / w >= 0.5 and -yo / w <= 3:
        return LayoutRelation(a.ordinal, b.ordinal, "column", max(0, -yo / w), xo / w)
    return None


def observe_layout(
    tokens: tuple[_StructuredOcrToken, ...],
    frames: tuple[PageFrame, ...],
    *,
    expected_pages: int,
    observation_complete: bool,
) -> LayoutShadow:
    """Observe one OCR pass only. No amounts, ranking, or confirmation are returned.

    Incomplete observations are retained where possible, including bad boxes.
    Malformed API objects or excessive input yield a fixed incomplete result;
    they must not produce a misleading successful subset or echo input errors.
    """
    try:
        return _observe(tokens, frames, expected_pages, observation_complete)
    except Exception:
        return LayoutShadow((), (), (), ("observation_incomplete", "invalid_observation_input"))


def _observe(tokens, frames, expected_pages, observation_complete):
    issues: set[str] = set()
    if (type(expected_pages) is not int or not 1 <= expected_pages <= 32
            or type(observation_complete) is not bool
            or type(tokens) is not tuple or type(frames) is not tuple):
        return LayoutShadow((), (), (), ("observation_incomplete", "invalid_observation_input"))
    if len(tokens) > _MAX_REGIONS:
        return LayoutShadow((), (), (), ("observation_incomplete", "region_limit_exceeded"))
    page_frames = {}
    for f in frames:
        if (type(f) is not PageFrame or type(f.page) is not int
                or not 1 <= f.page <= expected_pages or f.page in page_frames
                or not _finite_positive(f.width) or not _finite_positive(f.height)):
            return LayoutShadow((), (), (), ("observation_incomplete", "invalid_page_frames"))
        page_frames[f.page] = f
    regions = []
    for ordinal, t in enumerate(tokens):
        if type(t) is not _StructuredOcrToken or type(t.text) is not str or type(t.page) is not int:
            return LayoutShadow((), (), (), ("observation_incomplete", "invalid_observation_input"))
        text = unicodedata.normalize("NFKC", t.text).strip()
        box = _box(t, page_frames.get(t.page))
        numeric = _looks_numeric(text)
        confidence_ok = type(t.confidence) in (int, float) and math.isfinite(t.confidence)
        quality = []
        if not confidence_ok or not 0 <= t.confidence <= 100:
            quality.append("invalid_confidence")
        if not confidence_ok or t.confidence < 70:
            quality.append("low_confidence")
        if box is None:
            quality.append("invalid_geometry")
        if not text or all(c in "?？□�" for c in text):
            quality.append("unreadable_region")
        state = _numeric_state(text, t.confidence if confidence_ok else float("nan")) if numeric else None
        if state == "malformed_numeric":
            quality.append("malformed_numeric")
        if quality:
            issues.add("quality_unresolved")
        regions.append(LayoutRegion(ordinal, t.page,
            "numeric_like" if numeric else "unreadable" if "unreadable_region" in quality else "label_like",
            box, state, any(n in text for n in _NEGATIVE), tuple(sorted(quality))))
    if (not observation_complete or set(page_frames) != set(range(1, expected_pages + 1))
            or {r.page for r in regions} != set(page_frames)
            or any(r.box is None or "invalid_confidence" in r.issues for r in regions)):
        issues.add("observation_incomplete")
    # Pairwise graph, never transitive row/column merging or nearest-neighbor
    # selection. Aspect correction avoids dependence on paper aspect ratio.
    from dataclasses import replace
    corrected = [replace(r, box=(r.box[0] * page_frames[r.page].width / page_frames[r.page].height,
                                r.box[1], r.box[2] * page_frames[r.page].width / page_frames[r.page].height,
                                r.box[3])) if r.box else r for r in regions]
    relations = tuple(rel for i, a in enumerate(corrected) for b in corrected[i + 1:]
                      if (rel := _relation(a, b)) is not None)
    pairs = []
    for rel in relations:
        a, b = regions[rel.left], regions[rel.right]
        if (a.kind == "numeric_like") == (b.kind == "numeric_like"):
            continue
        label, number = (b, a) if a.kind == "numeric_like" else (a, b)
        pairs.append((label, number, rel))
    label_degree = Counter(l.ordinal for l, _, _ in pairs)
    numeric_degree = Counter(n.ordinal for _, n, _ in pairs)
    hypotheses = []
    for label, number, rel in pairs:
        reasons = {"payment_role_unproven", *label.issues, *number.issues}
        if label.negative_context or number.negative_context:
            reasons.add("negative_context_observed")
        if rel.axis == "overlap":
            reasons.add("overlapping_regions")
        if label_degree[label.ordinal] > 1 or numeric_degree[number.ordinal] > 1:
            reasons.add("competing_relationships")
        hypotheses.append(CellHypothesis(label.ordinal, number.ordinal, rel.axis, tuple(sorted(reasons))))
    linked = {h.numeric for h in hypotheses}
    if any(r.kind == "numeric_like" and r.ordinal not in linked for r in regions):
        issues.add("numeric_region_unassigned")
    if not hypotheses:
        issues.add("layout_relationship_not_observed")
    return LayoutShadow(tuple(regions), relations, tuple(hypotheses), tuple(sorted(issues)))
