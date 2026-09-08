"""Read-only PDF coordinate normalization for diagnostic observers only.

The canonical frame is CropBox-local with a top-left origin, before page-display
rotation. It is never passed to the production parser or writer. Token width is
still an extraction estimate, so a verified frame is not boundary evidence.
"""
from dataclasses import dataclass, field
import math

from .payroll_ocr import PositionedText


Affine = tuple[float, float, float, float, float, float]
Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class PageProvenance:
    page: int
    media_box: Box
    crop_box: Box
    rotation: int
    user_unit: float


@dataclass(frozen=True)
class TokenProvenance:
    page: int
    ctm: Affine
    text_axes: tuple[float, float, float, float]


@dataclass(frozen=True)
class CoordinateFrame:
    snapshot_id: str
    status: str  # verified / unknown / unsupported
    reason: str
    normalized_tokens: tuple[PositionedText, ...] = field(default=(), repr=False)
    transform_scope: str = "none"  # page_common / per_token / none


def affine_point(matrix: Affine, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return a*x+c*y+e, b*x+d*y+f


def compose_affine(outer: Affine, inner: Affine) -> Affine:
    """Return the transform that applies inner and then outer."""
    a, b, c, d, e, f = outer
    g, h, i, j, k, l = inner
    return (a*g+c*h, b*g+d*h, a*i+c*j, b*i+d*j,
            a*k+c*l+e, b*k+d*l+f)


def inverse_affine(matrix: Affine) -> Affine | None:
    a, b, c, d, e, f = matrix
    determinant = a*d-b*c
    # Negative determinant mirrors text advance. Estimated width has no direction,
    # so observer bbox construction cannot safely normalize reflected text.
    if not math.isfinite(determinant) or determinant <= 1e-10:
        return None
    return (d/determinant, -b/determinant, -c/determinant, a/determinant,
            (c*f-d*e)/determinant, (b*e-a*f)/determinant)


def _valid_box(box: Box) -> bool:
    return (all(math.isfinite(value) for value in box)
            and box[0] < box[2] and box[1] < box[3])


def _normalized_token(token: PositionedText, page: PageProvenance,
                      matrix: Affine) -> PositionedText | None:
    """Map the extractor's estimated raw bbox envelope into canonical page space."""
    page_height = page.media_box[3] - page.media_box[1]
    top = page_height-token.y
    bottom = top-token.height
    corners = [affine_point(matrix, x, y)
               for x in (token.x, token.x+token.width)
               for y in (bottom, top)]
    left, _, _, crop_top = page.crop_box
    normalized = [((x-left)*page.user_unit, (crop_top-y)*page.user_unit)
                  for x, y in corners]
    xs, ys = zip(*normalized)
    width, height = max(xs)-min(xs), max(ys)-min(ys)
    if not all(math.isfinite(value) for value in (*xs, *ys, width, height)):
        return None
    if width <= 0 or height <= 0:
        return None
    return PositionedText(token.text, token.page, min(xs), min(ys), width, height,
                          token.confidence)


def normalize_coordinate_frame(snapshot_id: str, tokens, pages, token_provenance,
                               *, extraction_matches: bool) -> CoordinateFrame:
    """Return a complete affine-normalized observer frame or a conservative state.

    A verified result requires exact raw snapshot reconstruction, complete token
    provenance, a supported page frame, identity text matrices, positive-orientation
    invertible CTMs, finite transformed bboxes, and an affine round-trip within
    numerical tolerance. It does not prove glyph metrics or column membership.
    """
    tokens, pages, token_provenance = tuple(tokens), tuple(pages), tuple(token_provenance)
    unknown = lambda reason: CoordinateFrame(snapshot_id, "unknown", reason)
    unsupported = lambda reason: CoordinateFrame(snapshot_id, "unsupported", reason)
    if not extraction_matches or not tokens or len(tokens) != len(token_provenance):
        return unknown("frame_provenance_incomplete")
    by_page = {page.page: page for page in pages}
    if len(by_page) != len(pages):
        return unknown("page_provenance_ambiguous")
    for page in pages:
        if not _valid_box(page.media_box) or not _valid_box(page.crop_box):
            return unknown("invalid_page_box")
        if page.rotation not in (0, 90, 180, 270):
            return unsupported("page_rotation_unsupported")
        if not math.isfinite(page.user_unit) or page.user_unit <= 0:
            return unknown("invalid_user_unit")
        if page.user_unit != 1:
            return unknown("user_unit_requires_calibration")
    normalized = []
    scope = []
    for page in pages:
        matrices = {entry.ctm for entry in token_provenance if entry.page == page.page}
        scope.append("page_common" if len(matrices) == 1 else "per_token")
    for token, provenance in zip(tokens, token_provenance):
        if token.page != provenance.page or token.page not in by_page:
            return unknown("page_token_provenance_mismatch")
        if len(provenance.ctm) != 6 or len(provenance.text_axes) != 4:
            return unknown("invalid_transform_metadata")
        if not all(math.isfinite(value) for value in (*provenance.ctm, *provenance.text_axes)):
            return unknown("invalid_transform_metadata")
        if provenance.text_axes != (1, 0, 0, 1):
            return unsupported("text_matrix_unsupported")
        inverse = inverse_affine(provenance.ctm)
        if inverse is None:
            return unsupported("ctm_noninvertible_or_reflective")
        page = by_page[token.page]
        raw_y = page.media_box[3]-page.media_box[1]-token.y
        restored = affine_point(inverse, *affine_point(provenance.ctm, token.x, raw_y))
        if max(abs(restored[0]-token.x), abs(restored[1]-raw_y)) > 1e-7:
            return unknown("affine_round_trip_calibration_failed")
        value = _normalized_token(token, page, provenance.ctm)
        if value is None:
            return unknown("normalized_bbox_invalid")
        normalized.append(value)
    return CoordinateFrame(snapshot_id, "verified", "canonical_crop_local_frame",
                           tuple(normalized), "page_common" if all(value == "page_common" for value in scope)
                           else "per_token")


def inspect_pdf_coordinate_frame(path, snapshot_id: str, tokens) -> CoordinateFrame:
    """Read local PDF provenance and verify that it reconstructs the given snapshot."""
    from pypdf import PdfReader
    reader = PdfReader(path)
    if reader.is_encrypted:
        return CoordinateFrame(snapshot_id, "unknown", "encrypted_pdf")
    observed, pages, provenance = [], [], []
    for number, page in enumerate(reader.pages, 1):
        media = tuple(float(value) for value in (page.mediabox.left, page.mediabox.bottom,
                                                  page.mediabox.right, page.mediabox.top))
        crop = media if page.get("/CropBox") is None else tuple(float(value) for value in page.get("/CropBox"))
        pages.append(PageProvenance(number, media, crop, int(page.get("/Rotate", 0)) % 360,
                                    float(page.get("/UserUnit", 1))))
        height = media[3]-media[1]
        def visitor(value, cm, tm, font, font_size):
            value = value.strip()
            if not value:
                return
            size = float(font_size or 10)
            observed.append(PositionedText(value, number, float(tm[4]), height-float(tm[5]),
                                           max(size, len(value)*size*.55), size, 100.0))
            provenance.append(TokenProvenance(number, tuple(float(entry) for entry in cm),
                                               tuple(float(entry) for entry in tm[:4])))
        page.extract_text(visitor_text=visitor)
    return normalize_coordinate_frame(snapshot_id, tokens, pages, provenance,
                                      extraction_matches=tuple(observed) == tuple(tokens))
