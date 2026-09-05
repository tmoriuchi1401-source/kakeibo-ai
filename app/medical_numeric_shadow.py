"""Transient numeric observations, independent of ground truth and payment role.

No OCR engine, transport, logging, amounts in results, or production callers.
Atoms are never removed when a possible joined span is observed. All spans are
alternative views of one OCR pass, never independent confirmation signals.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field

from .medical_layout_shadow import PageFrame, observe_layout
from .medical_receipt_privacy import _StructuredOcrToken, _structured_amount

_FRAGMENT = re.compile(r"[0-9,\.¥円+\-]+")
_MAX_MEMBERS = 6
_MAX_SPANS = 1024


def _surface(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip()


def _numeric_like(text: str) -> bool:
    return bool(re.search(r"[0-9¥円]", text) or text in {",", ".", "+", "-", "?", "？", "□", "�"})


@dataclass(frozen=True)
class NumericAtom:
    ordinal: int
    parse_valid: bool
    issues: tuple[str, ...]


@dataclass(frozen=True)
class NumericSpan:
    members: tuple[int, ...] = field(repr=False)
    parse_valid: bool
    issues: tuple[str, ...]


@dataclass(frozen=True)
class NumericShadow:
    atoms: tuple[NumericAtom, ...] = field(repr=False)
    spans: tuple[NumericSpan, ...] = field(repr=False)
    issues: tuple[str, ...]
    observation_group: int = 0

    def aggregate(self) -> dict[str, int]:
        return {
            "numeric_atoms": len(self.atoms),
            "parse_valid_atoms": sum(a.parse_valid for a in self.atoms),
            "low_confidence_atoms": sum("low_confidence" in a.issues for a in self.atoms),
            "malformed_atoms": sum("malformed_numeric" in a.issues for a in self.atoms),
            "fragment_atoms": sum("fragment_syntax" in a.issues for a in self.atoms),
            "spans": len(self.spans),
            "parse_valid_spans": sum(s.parse_valid for s in self.spans),
            "low_confidence_spans": sum("low_confidence" in s.issues for s in self.spans),
            "malformed_spans": sum(not s.parse_valid for s in self.spans),
            "ambiguous_spans": sum(bool(set(s.issues) & {
                "branching_adjacency", "digit_concatenation_unproven", "overlapping_observations"
            }) for s in self.spans),
            "observation_incomplete": int("observation_incomplete" in self.issues),
        }


def _adjacent(a, b):
    if a.page != b.page:
        return False
    gap = b.x - a.x - a.width
    if gap < 0 or gap > 0.75 * max(a.height, b.height):
        return False
    overlap = min(a.y + a.height, b.y + b.height) - max(a.y, b.y)
    # A small comma may sit near the baseline with little vertical overlap.
    return (overlap >= 0.5 * min(a.height, b.height)
            or abs(a.y + a.height - b.y - b.height) <= 0.25 * max(a.height, b.height))


def _intervenes(a, b, other):
    if other.page != a.page or other.x < a.x + a.width or other.x + other.width > b.x:
        return False
    return min(max(a.y + a.height, b.y + b.height), other.y + other.height) > max(min(a.y, b.y), other.y)


def observe_numeric_fragments(tokens: tuple[_StructuredOcrToken, ...], frames: tuple[PageFrame, ...],
                              *, expected_pages: int, observation_complete: bool) -> NumericShadow:
    """Observe bounded spans without knowing an expected value.

    Same OCR line keys do not override geometry. Geometry does not prove a shared
    table cell. Plain digit concatenation is always ambiguous. Currency/comma
    joins retain their original atoms and never become payment candidates.
    """
    try:
        layout = observe_layout(tokens, frames, expected_pages=expected_pages,
                                observation_complete=observation_complete)
        if not layout.regions and tokens:
            return NumericShadow((), (), ("observation_incomplete",))
        normalized = tuple(_surface(t.text) for t in tokens)
        atoms = []
        valid_geometry = set()
        for r in layout.regions:
            if r.box is not None:
                valid_geometry.add(r.ordinal)
            text = normalized[r.ordinal]
            if not _numeric_like(text):
                continue
            token = tokens[r.ordinal]
            issues = set()
            parsed = _structured_amount(text) is not None
            if not parsed:
                issues.add("fragment_syntax" if re.fullmatch(r"[¥円,.]+", text) else "malformed_numeric")
            if (type(token.confidence) not in (int, float) or not math.isfinite(token.confidence)
                    or token.confidence < 70):
                issues.add("low_confidence")
            if r.box is None:
                issues.add("invalid_geometry")
            if "invalid_confidence" in r.issues:
                issues.add("invalid_confidence")
            atoms.append(NumericAtom(r.ordinal, parsed, tuple(sorted(issues))))
        by_ordinal = {a.ordinal: a for a in atoms}
        fragments = [a.ordinal for a in atoms if a.ordinal in valid_geometry and _FRAGMENT.fullmatch(normalized[a.ordinal])]
        edges = {i: [] for i in fragments}
        incoming = {i: 0 for i in fragments}
        for i in fragments:
            for j in fragments:
                if i == j or not _adjacent(tokens[i], tokens[j]):
                    continue
                if any(_intervenes(tokens[i], tokens[j], t) for k, t in enumerate(tokens) if k not in {i, j} and k in valid_geometry):
                    continue
                edges[i].append(j)
                incoming[j] += 1
        spans = []
        truncated = False
        pending = [(i,) for i in fragments]
        while pending:
            members = pending.pop()
            if len(members) >= 2:
                surface = "".join(normalized[i] for i in members)
                parsed = _structured_amount(surface) is not None
                issues = {"segmentation_alternative", "payment_role_unproven"}
                for i in members:
                    issues.update(set(by_ordinal[i].issues) - {"fragment_syntax", "malformed_numeric"})
                if not parsed:
                    issues.add("malformed_numeric")
                if any(len(edges[i]) > 1 or incoming[i] > 1 for i in members):
                    issues.add("branching_adjacency")
                if any(normalized[left][-1:].isdigit() and normalized[right][:1].isdigit()
                       for left, right in zip(members, members[1:])):
                    issues.add("digit_concatenation_unproven")
                # Duplicate/overlapping OCR regions cannot silently lend weight.
                if any(rel.axis == "overlap" and (rel.left in members or rel.right in members)
                       for rel in layout.relations):
                    issues.add("overlapping_observations")
                spans.append(NumericSpan(members, parsed, tuple(sorted(issues))))
                if len(spans) >= _MAX_SPANS:
                    truncated = truncated or bool(pending or edges[members[-1]])
                    break
            if len(members) < _MAX_MEMBERS:
                pending.extend(members + (j,) for j in edges[members[-1]])
            elif edges[members[-1]]:
                truncated = True
        issues = set()
        if "observation_incomplete" in layout.issues or truncated:
            issues.add("observation_incomplete")
        if truncated:
            issues.add("span_limit_exceeded")
        return NumericShadow(tuple(atoms), tuple(spans), tuple(sorted(issues)))
    except Exception:
        return NumericShadow((), (), ("observation_incomplete", "invalid_observation_input"))
