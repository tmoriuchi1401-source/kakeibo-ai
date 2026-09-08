"""Candidate-independent PDF table-boundary diagnostics.

Only stroked drawing paths build regions. Text tokens, candidate values, parser
rows and whitespace never participate in extraction. Regions are diagnostic
geometry, not a semantic label/value claim or a production parsing input.
"""
from collections import defaultdict
from dataclasses import dataclass, field
import math

from .payroll_coordinate_diagnostics import CoordinateFrame, affine_point
from .payroll_ocr import PositionedText


@dataclass(frozen=True)
class BoundarySegment:
    page: int
    x1: float
    y1: float
    x2: float
    y2: float
    source: str = "stroked_path"


@dataclass(frozen=True)
class StructuralRegion:
    page: int
    left: float
    top: float
    right: float
    bottom: float
    evidence_source: str = "closed_stroked_cell"
    certainty: str = "strong"


@dataclass(frozen=True)
class PageBoundaryEvidence:
    page: int
    status: str  # constructed / partial / unavailable / coordinate_unavailable
    reason: str
    primitive_count: int
    region_count: int
    provenance_complete: bool


@dataclass(frozen=True)
class StructuralBoundaryEvidence:
    snapshot_id: str
    coordinate_snapshot_id: str
    coordinate_status: str
    pages: tuple[PageBoundaryEvidence, ...]
    regions: tuple[StructuralRegion, ...] = field(repr=False)
    source: str = "pdf_stroked_drawing_primitives"


@dataclass(frozen=True)
class RegionMembership:
    status: str  # exactly_one / multiple / outside / boundary_crossing / boundary_sensitive / unknown
    region: StructuralRegion | None = field(default=None, repr=False)
    reason: str = ""


def _axis(segment, tolerance=1e-6):
    if abs(segment.x1-segment.x2) <= tolerance:
        return "vertical"
    if abs(segment.y1-segment.y2) <= tolerance:
        return "horizontal"
    return None


def _merged_spans(segments, axis, tolerance=1e-6):
    groups = defaultdict(list)
    for segment in segments:
        if _axis(segment, tolerance) != axis:
            continue
        coordinate = (segment.x1+segment.x2)/2 if axis == "vertical" else (segment.y1+segment.y2)/2
        start, end = sorted((segment.y1, segment.y2) if axis == "vertical" else (segment.x1, segment.x2))
        groups[round(coordinate/tolerance)*tolerance].append((start, end))
    result = {}
    for coordinate, spans in groups.items():
        merged = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1]+tolerance:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        result[coordinate] = tuple(merged)
    return result


def _covers(spans, start, end, tolerance=1e-6):
    return any(left <= start+tolerance and right >= end-tolerance for left, right in spans)


def regions_from_segments(snapshot_id: str, coordinate_frame: CoordinateFrame, segments,
                          *, tolerance=1e-6) -> StructuralBoundaryEvidence:
    """Build only adjacent closed grid cells from candidate-independent strokes."""
    if coordinate_frame.status != "verified":
        page_ids = sorted({segment.page for segment in segments})
        return StructuralBoundaryEvidence(snapshot_id, coordinate_frame.snapshot_id,
                                          coordinate_frame.status,
                                          tuple(PageBoundaryEvidence(page, "coordinate_unavailable",
                                                                     "coordinate_frame_unverified", 0, 0, False)
                                                for page in page_ids), ())
    segments = tuple(segment for segment in segments if all(math.isfinite(value) for value in
                     (segment.x1, segment.y1, segment.x2, segment.y2)))
    pages, regions = [], []
    for page in sorted({segment.page for segment in segments}):
        page_segments = tuple(segment for segment in segments if segment.page == page)
        vertical = _merged_spans(page_segments, "vertical", tolerance)
        horizontal = _merged_spans(page_segments, "horizontal", tolerance)
        xs, ys = sorted(vertical), sorted(horizontal)
        page_regions = []
        for left, right in zip(xs, xs[1:]):
            for top, bottom in zip(ys, ys[1:]):
                if (_covers(vertical[left], top, bottom, tolerance)
                        and _covers(vertical[right], top, bottom, tolerance)
                        and _covers(horizontal[top], left, right, tolerance)
                        and _covers(horizontal[bottom], left, right, tolerance)):
                    page_regions.append(StructuralRegion(page, left, top, right, bottom))
        # Identical primitive paths are duplicate evidence, not distinct regions.
        unique = tuple(dict.fromkeys(page_regions))
        regions.extend(unique)
        if unique:
            status, reason = "constructed", "closed_stroked_cells"
        elif page_segments:
            status, reason = "partial", "no_closed_stroked_cells"
        else:
            status, reason = "unavailable", "no_axis_aligned_stroked_primitives"
        pages.append(PageBoundaryEvidence(page, status, reason, len(page_segments), len(unique), True))
    return StructuralBoundaryEvidence(snapshot_id, coordinate_frame.snapshot_id,
                                      coordinate_frame.status, tuple(pages), tuple(regions))


def _canonical_point(matrix, crop_box, x, y):
    transformed_x, transformed_y = affine_point(matrix, x, y)
    return transformed_x-crop_box[0], crop_box[3]-transformed_y


def inspect_pdf_boundaries(path, snapshot_id: str, coordinate_frame: CoordinateFrame):
    """Read-only stroked-path extraction in the coordinate frame's canonical space."""
    from pypdf import PdfReader
    from pypdf.generic import ContentStream
    if coordinate_frame.snapshot_id != snapshot_id:
        return StructuralBoundaryEvidence(snapshot_id, coordinate_frame.snapshot_id, "unknown", ())
    reader = PdfReader(path)
    if reader.is_encrypted:
        return StructuralBoundaryEvidence(snapshot_id, coordinate_frame.snapshot_id, "unknown", ())
    segments = []
    for number, page in enumerate(reader.pages, 1):
        media = (float(page.mediabox.left), float(page.mediabox.bottom),
                 float(page.mediabox.right), float(page.mediabox.top))
        crop = media if page.get("/CropBox") is None else tuple(float(value) for value in page.get("/CropBox"))
        path_parts, current, start = [], None, None
        ctm, stack = (1, 0, 0, 1, 0, 0), []
        def append_segment(first, second):
            if first is not None and second is not None:
                segments.append(BoundarySegment(number, first[0], first[1], second[0], second[1]))
        # ContentStream is used rather than extract_text's visitor so a page with
        # only table strokes still yields structural evidence.  It is read-only.
        content = ContentStream(page.get_contents(), reader)
        for operands, operator in content.operations:
            op = operator.decode("latin1") if isinstance(operator, bytes) else str(operator)
            if op == "q":
                stack.append(ctm)
                continue
            if op == "Q":
                if stack:
                    ctm = stack.pop()
                continue
            if op == "cm" and len(operands) >= 6:
                supplied = tuple(float(value) for value in operands[:6])
                from .payroll_coordinate_diagnostics import compose_affine
                ctm = compose_affine(ctm, supplied)
                continue
            matrix = ctm
            if op == "m" and len(operands) >= 2:
                current = _canonical_point(matrix, crop, float(operands[0]), float(operands[1]))
                start = current
            elif op == "l" and len(operands) >= 2:
                next_point = _canonical_point(matrix, crop, float(operands[0]), float(operands[1]))
                if current is not None:
                    path_parts.append((current, next_point))
                current = next_point
            elif op == "re" and len(operands) >= 4:
                x, y, width, height = (float(value) for value in operands[:4])
                points = [_canonical_point(matrix, crop, x, y), _canonical_point(matrix, crop, x+width, y),
                          _canonical_point(matrix, crop, x+width, y+height), _canonical_point(matrix, crop, x, y+height)]
                path_parts.extend(zip(points, points[1:]+points[:1]))
            elif op == "h" and current is not None and start is not None:
                path_parts.append((current, start))
                current = start
            elif op in {"S", "s", "B", "B*", "b", "b*"}:
                for first, second in path_parts:
                    append_segment(first, second)
                path_parts, current, start = [], None, None
            elif op in {"n", "f", "F", "f*"}:
                path_parts, current, start = [], None, None
            # All remaining graphics/text operators intentionally leave the
            # current stroked path untouched unless listed above.
    return regions_from_segments(snapshot_id, coordinate_frame, segments)


def membership(token: PositionedText, evidence: StructuralBoundaryEvidence, *, uncertainty=0.0):
    """Classify full-bbox membership without inferring a region from the candidate."""
    if evidence.coordinate_status != "verified":
        return RegionMembership("unknown", reason="coordinate_frame_unverified")
    if not math.isfinite(uncertainty) or uncertainty < 0:
        return RegionMembership("unknown", reason="bbox_uncertainty_unknown")
    candidates = [region for region in evidence.regions if region.page == token.page]
    if not candidates:
        return RegionMembership("unknown", reason="boundary_unavailable")
    inside = [region for region in candidates if region.left <= token.x
              and token.x+token.width <= region.right and region.top <= token.y
              and token.y+token.height <= region.bottom]
    if len(inside) > 1:
        return RegionMembership("multiple", reason="multiple_possible_regions")
    if len(inside) == 1:
        region = inside[0]
        robust = (region.left <= token.x-uncertainty
                  and token.x+token.width+uncertainty <= region.right
                  and region.top <= token.y-uncertainty
                  and token.y+token.height+uncertainty <= region.bottom)
        return RegionMembership("exactly_one" if robust else "boundary_sensitive", region,
                                "closed_stroked_region" if robust else "bbox_uncertainty_changes_membership")
    overlap = [region for region in candidates if not (token.x+token.width <= region.left
               or region.right <= token.x or token.y+token.height <= region.top
               or region.bottom <= token.y)]
    return RegionMembership("boundary_crossing" if overlap else "outside",
                            reason="bbox_crosses_region_boundary" if overlap else "outside_known_regions")
