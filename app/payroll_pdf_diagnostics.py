"""Value-free PDF pairing observations; never an eligibility or repair API.

Relations are geometric possibilities, not evidence of semantic ownership.
Missing tokens cannot distinguish a genuinely blank cell from extraction loss.
No caller in the production parser/writer imports this module.
"""
from collections import Counter
from dataclasses import dataclass
import math
import re

from .payroll_ocr import PositionedText
from .payroll_parser import amounts


@dataclass(frozen=True)
class Relation:
    """In-memory geometry only. Do not export coordinates from private inputs."""

    dx: float
    dy: float
    horizontal_overlap: bool
    vertical_window: bool
    adjacent_column: bool  # Legacy name: off-column y-neighbor, NOT membership.
    label_bbox: tuple[float, float, float, float]
    candidate_bbox: tuple[float, float, float, float]
    same_column_window: bool
    vertical_edge_distance: float

    @property
    def horizontal_edge_distance(self):
        """Gap in input units, not a calibrated closeness or ownership claim."""
        return max(0, self.candidate_bbox[0] - self.label_bbox[2],
                   self.label_bbox[0] - self.candidate_bbox[2])


@dataclass(frozen=True)
class ColumnBand:
    """Externally established page-space boundaries; never inferred from proximity."""

    page: int
    left: float
    right: float


def column_relationship(label, value, bands=()):
    """Full bbox containment, not a label-width window or semantic ownership."""
    if label.page != value.page:
        return "different_page"
    page_bands = sorted((b for b in bands if b.page == label.page), key=lambda b: b.left)
    if not page_bands:
        return "unknown"
    if (any(not math.isfinite(b.left) or not math.isfinite(b.right)
            or b.left >= b.right for b in page_bands)
            or any(a.right > b.left for a, b in zip(page_bands, page_bands[1:]))):
        raise ValueError("invalid_column_bands")
    if not _valid(label) or not _valid(value):
        return "boundary_ambiguous"
    def membership(t):
        return [i for i, b in enumerate(page_bands)
                if b.left <= t.x and t.x + t.width <= b.right]
    left, right = membership(label), membership(value)
    if len(left) != 1 or len(right) != 1:
        return "boundary_ambiguous"
    distance = abs(left[0] - right[0])
    return "same_column" if distance == 0 else "adjacent_column" if distance == 1 else "remote_column"


def relation(label: PositionedText, value: PositionedText) -> Relation:
    overlap = max(label.x, value.x) < min(label.x + label.width, value.x + value.width)
    below = value.y - (label.y + label.height)
    above = label.y - (value.y + value.height)
    vertical_distance_ok = (0 <= below <= label.height * 2.2
                            or 0 <= above <= label.height * 1.2)
    column = label.x - label.width * .35 <= value.x <= label.x + label.width * 1.35
    return Relation(value.x - label.x, value.y - label.y, overlap,
                    vertical_distance_ok and column,
                    vertical_distance_ok and not overlap and not column,
                    (label.x, label.y, label.x + label.width, label.y + label.height),
                    (value.x, value.y, value.x + value.width, value.y + value.height),
                    column, max(0, below, above))


def _valid(token):
    return (all(math.isfinite(v) for v in (token.x, token.y, token.width, token.height))
            and token.width > 0 and token.height > 0)


def diagnose_pdf_pairing(tokens, items, *, extraction_method, column_bands=()):
    """Return counts only. Primary counts partition failures; secondary overlap.

    Pass final, unmodified parser items and the exact extraction tokens together.
    Numeric-looking is an observation, not expanded production money grammar.
    No raw text, IDs, values, or coordinates leave this function.
    Used-token evidence is reconstructed only from confirmed raw_value and exact
    source tokens on the same page. Repeated text is possible use, not identity.
    Column bands must describe the same coordinate frame as the tokens. Without
    them off-column proximity cannot establish adjacent-column membership.
    """
    if extraction_method != "pdf_text":
        raise ValueError("pdf_text_required")
    tokens, items = tuple(tokens), tuple(items)
    column_bands = tuple(column_bands)
    primary, secondary = Counter(), Counter()
    used, possibly_used = set(), set()
    for item in items:
        if item.needs_review or item.value is None or item.raw_value is None:
            continue
        matches = [id(t) for t in tokens if t.page == item.page
                   and t.text == item.raw_value and _valid(t)]
        if len(matches) == 1:
            used.update(matches)
        else:
            possibly_used.update(matches)
    labels = []
    for item in items:
        matches = [t for t in tokens if t.page == item.page and t.x == item.x
                   and t.y == item.y and t.text.strip() == item.raw_item_name]
        labels.append(matches[0] if len(matches) == 1 and _valid(matches[0]) else None)
    for item, label in zip(items, labels):
        if item.review_reason_code != "pairing_not_found":
            continue
        if label is None:
            primary["unresolved"] += 1
            continue
        numeric = [t for t in tokens if t.page == label.page and _valid(t) and amounts(t.text)]
        vertical = [t for t in numeric if relation(label, t).vertical_window]
        adjacent = [t for t in numeric if relation(label, t).adjacent_column]
        adjacent_members = [t for t in adjacent if column_relationship(label, t, column_bands) == "adjacent_column"]
        remote_members = [t for t in adjacent if column_relationship(label, t, column_bands) == "remote_column"]
        already_used = any(id(t) in used for t in vertical)
        possible_use = any(id(t) in possibly_used for t in vertical)
        cross_column = any(column_relationship(label, t, column_bands)
                           in ("adjacent_column", "remote_column") for t in vertical)
        unknown_column = any(column_relationship(label, t, column_bands) == "unknown"
                             for t in vertical + adjacent)
        ambiguous_boundary = any(column_relationship(label, t, column_bands) == "boundary_ambiguous"
                                 for t in vertical + adjacent)
        rejected = [t for t in tokens if t.page == label.page and _valid(t)
                    and not amounts(t.text)
                    and re.fullmatch(r"\s*[+-]?\d+(?:[,.]\d+)*(?:円|日|時間)?\s*", t.text)
                    and (relation(label, t).vertical_window or (
                        abs(t.y-label.y) <= max(t.height,label.height)*.65
                        and t.x >= label.x+label.width-3
                        and t.x <= label.x+label.width*2))]
        shared = any(sum(other is not None and other.page == t.page
                         and relation(other, t).vertical_window for other in labels) > 1
                     for t in vertical)
        for key, flag in (("vertical_single_candidate", len(vertical) == 1),
                          ("multiple_vertical_candidates", len(vertical) > 1),
                          ("candidate_shared_with_other_label", shared),
                          ("candidate_already_used", already_used),
                          ("candidate_possibly_used", possible_use),
                          ("vertical_candidate_crosses_column", cross_column),
                          ("candidate_column_unknown", unknown_column),
                          ("candidate_column_boundary_ambiguous", ambiguous_boundary),
                          ("off_column_y_neighbor", bool(adjacent)),
                          ("adjacent_column_candidate", bool(adjacent_members)),
                          ("remote_column_candidate", bool(remote_members)),
                          ("numeric_format_rejected", bool(rejected))):
            secondary[key] += int(flag)
        if already_used:
            reason = "candidate_already_used"
        elif possible_use:
            reason = "candidate_possibly_used"
        elif cross_column:
            reason = "vertical_candidate_crosses_column"
        elif shared:
            reason = "candidate_shared_with_other_label"
        elif len(vertical) > 1:
            reason = "multiple_vertical_candidates"
        elif vertical:
            reason = "vertical_candidate_exists"
        elif rejected:
            reason = "numeric_format_rejected"
        elif adjacent_members:
            reason = "adjacent_column_candidate"
        elif remote_members:
            reason = "remote_column_candidate"
        elif adjacent:
            reason = "off_column_y_neighbor"
        elif numeric:
            reason = "candidate_outside_allowed_geometry"
        else:
            reason = "no_numeric_candidate"
        primary[reason] += 1
    return {"pairing_not_found_total": sum(primary.values()),
            "primary": dict(sorted(primary.items())),
            "secondary": dict(sorted(secondary.items())),
            "blank_region_confirmed": 0}
