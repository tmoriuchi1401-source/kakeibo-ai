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
    adjacent_column: bool
    label_bbox: tuple[float, float, float, float]
    candidate_bbox: tuple[float, float, float, float]
    same_column_window: bool
    vertical_edge_distance: float


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


def diagnose_pdf_pairing(tokens, items, *, extraction_method):
    """Return counts only. Primary counts partition failures; secondary overlap.

    Pass final, unmodified parser items and the exact extraction tokens together.
    Numeric-looking is an observation, not expanded production money grammar.
    No raw text, IDs, values, or coordinates leave this function.
    """
    if extraction_method != "pdf_text":
        raise ValueError("pdf_text_required")
    tokens, items = tuple(tokens), tuple(items)
    primary, secondary = Counter(), Counter()
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
                          ("adjacent_column_candidate", bool(adjacent)),
                          ("numeric_format_rejected", bool(rejected))):
            secondary[key] += int(flag)
        if shared:
            reason = "candidate_shared_with_other_label"
        elif len(vertical) > 1:
            reason = "multiple_vertical_candidates"
        elif vertical:
            reason = "vertical_candidate_exists"
        elif rejected:
            reason = "numeric_format_rejected"
        elif adjacent:
            reason = "adjacent_column_candidate"
        elif numeric:
            reason = "candidate_outside_allowed_geometry"
        else:
            reason = "no_numeric_candidate"
        primary[reason] += 1
    return {"pairing_not_found_total": sum(primary.values()),
            "primary": dict(sorted(primary.items())),
            "secondary": dict(sorted(secondary.items())),
            "blank_region_confirmed": 0}
