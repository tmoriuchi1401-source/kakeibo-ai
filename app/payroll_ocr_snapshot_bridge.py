"""Diagnostic-only bridge from a PDF or PNG to OCR ownership evidence.

All snapshots are private, in-memory objects.  The public report contains only
opaque IDs and aggregate statuses.  This module deliberately has no production
caller and never changes parser, extraction, fallback, writer, or adoption
behavior.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
import hashlib
import hmac
import json
import math
from pathlib import Path
import secrets
import unicodedata

from .payroll_diagnostic_evidence import Snapshot, observe_tokens
from .payroll_ocr import PositionedText, extract_payroll_text
from .payroll_parser import parse_positioned_items


EXTRACTION_VERSION = "diagnostic-ocr-snapshot-bridge-v2"
RENDER_SCALE = 3.0
OCR_LANGUAGE = "jpn+eng"
OCR_CONFIG = "--psm 6"
CONTRAST = 1.7
MINIMUM_PDF_TEXT = 80


@dataclass(frozen=True)
class SourceBinding:
    source_id: str
    page_count: int
    byte_count: int
    extraction_version: str
    renderer: str
    renderer_version: str
    engine: str
    engine_version: str
    language: str
    config: str
    complete: bool
    reason: str


@dataclass(frozen=True)
class RasterPage:
    page: int
    raster_width: int
    raster_height: int
    ocr_width: int
    ocr_height: int
    render_scale: float
    resize_factor: int
    page_rotation: int
    crop_matches_media: bool
    user_unit: float


@dataclass(frozen=True)
class OcrTokenProvenance:
    page: int
    block: int
    paragraph: int
    line: int
    word: int


@dataclass(frozen=True)
class OcrCoordinateFrame:
    snapshot_id: str
    status: str  # verified / unknown / unsupported
    reason: str
    pixel_origin: str = "top_left"
    bbox_convention: str = "left_top_width_height_half_open"
    uncertainty: float | None = None
    normalized_tokens: tuple[PositionedText, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class ParserModeCompatibility:
    status: str  # established / ineligible
    reason: str


@dataclass(frozen=True)
class OcrOwnershipSnapshot:
    source: SourceBinding
    snapshot: Snapshot = field(repr=False)
    raster_pages: tuple[RasterPage, ...] = field(repr=False)
    token_provenance: tuple[OcrTokenProvenance, ...] = field(repr=False)
    coordinate_frame: OcrCoordinateFrame = field(repr=False)
    rerun_id: str = ""
    rerun_stable: bool = False
    page_scope_complete: bool = False
    physical_identity_status: str = "incomplete"
    parser_mode: ParserModeCompatibility = field(
        default_factory=lambda: ParserModeCompatibility("ineligible", "not_evaluated")
    )
    ownership_ready: bool = False
    reason: str = "not_evaluated"


@dataclass(frozen=True)
class OcrOwnershipReport:
    """Safe-to-report aggregate: no locator, text, amount, or geometry."""
    source_id: str
    page_count: int
    token_count: int
    rerun_stable: bool
    source_binding: str
    physical_identity: str
    coordinate_provenance: str
    parser_mode_compatibility: str
    ownership_ready: bool
    reason: str


def _digest(key: bytes, payload) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
    return hmac.new(key, encoded, hashlib.sha256).hexdigest()


def _source_id(key: bytes, source_bytes: bytes) -> str:
    """Bind exact bytes without encoding or retaining them in a report payload."""
    return hmac.new(key, b"payroll-ocr-source-v1\x00" + source_bytes, hashlib.sha256).hexdigest()


def _input_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".png":
        return "png"
    raise ValueError("unsupported_diagnostic_input")


def _page_metadata(path: Path):
    """Return source-frame metadata without rendering or changing source bytes."""
    if _input_kind(path) == "png":
        from PIL import Image
        with Image.open(path) as image:
            if image.format != "PNG":
                raise ValueError("png_format_mismatch")
            width, height = image.size
        if width <= 0 or height <= 0:
            raise ValueError("invalid_image_dimensions")
        # PNG has one native, top-left pixel frame.  The tuple shape deliberately
        # matches the PDF metadata shape but is interpreted only by the PNG path.
        return ((1, (0.0, 0.0, float(width), float(height)),
                 (0.0, 0.0, float(width), float(height)), 0, 1.0),)
    from pypdf import PdfReader
    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ValueError("encrypted_pdf")
    pages = []
    for number, page in enumerate(reader.pages, 1):
        media = tuple(float(value) for value in (page.mediabox.left, page.mediabox.bottom,
                                                  page.mediabox.right, page.mediabox.top))
        crop = media if page.get("/CropBox") is None else tuple(float(value) for value in page.get("/CropBox"))
        pages.append((number, media, crop, int(page.get("/Rotate", 0)) % 360,
                      float(page.get("/UserUnit", 1))))
    return tuple(pages)


def _engine_details():
    import pypdfium2
    import pytesseract
    return (str(getattr(pypdfium2, "__version__", "unknown")),
            str(pytesseract.get_tesseract_version()).splitlines()[0])


def _run_local_ocr(path: Path, metadata, *, timeout_seconds: int):
    """Reproduce only the token-producing portion of the production OCR path."""
    import pytesseract
    from PIL import Image, ImageEnhance, ImageOps

    input_kind = _input_kind(path)
    tokens, provenance, pages = [], [], []
    if input_kind == "pdf":
        import pypdfium2 as pdfium
        document = pdfium.PdfDocument(str(path))
        if len(document) != len(metadata):
            raise ValueError("renderer_page_count_mismatch")
        rendered = ((number, page.render(scale=RENDER_SCALE).to_pil(), RENDER_SCALE)
                    for number, page in enumerate(document, 1))
    else:
        if len(metadata) != 1:
            raise ValueError("image_page_count_mismatch")
        with Image.open(path) as source:
            if source.format != "PNG":
                raise ValueError("png_format_mismatch")
            # copy() loads exactly the supplied PNG raster; no EXIF transpose,
            # crop, or rotation is applied before the tracked OCR preprocessing.
            raster = source.copy()
        rendered = ((1, raster, 1.0),)
    for number, raster, render_scale in rendered:
        raster_width, raster_height = raster.width, raster.height
        image = ImageEnhance.Contrast(ImageOps.grayscale(raster)).enhance(CONTRAST)
        resize = 2 if image.width < 1800 else 1
        if resize == 2:
            image = image.resize((image.width*2, image.height*2))
        _, media, crop, rotation, user_unit = metadata[number-1]
        pages.append(RasterPage(number, raster_width, raster_height, image.width, image.height,
                                render_scale, resize, rotation, crop == media, user_unit))
        data = pytesseract.image_to_data(image, lang=OCR_LANGUAGE, config=OCR_CONFIG,
                                         timeout=timeout_seconds,
                                         output_type=pytesseract.Output.DICT)
        for index, raw in enumerate(data["text"]):
            value = str(raw).strip()
            if not value:
                continue
            tokens.append(PositionedText(value, number, float(data["left"][index]),
                                         float(data["top"][index]), float(data["width"][index]),
                                         float(data["height"][index]), float(data["conf"][index])))
            provenance.append(OcrTokenProvenance(number, int(data["block_num"][index]),
                                                  int(data["par_num"][index]), int(data["line_num"][index]),
                                                  int(data["word_num"][index])))
    return tuple(tokens), tuple(provenance), tuple(pages)


def _coordinate_frame(snapshot_id: str, tokens, raster_pages, metadata, *, input_kind="pdf"):
    """Verify the declared renderer mapping, otherwise preserve uncertainty.

    PDFium documents scale in PDF canvas units.  The bridge accepts only the
    unrotated, MediaBox-equal CropBox, UserUnit=1 subset whose expected raster
    dimensions independently agree with the actual render.  Other valid PDF
    cases are deliberately not guessed from OCR pixel positions.
    """
    unknown = lambda reason: OcrCoordinateFrame(snapshot_id, "unknown", reason)
    by_page = {page.page: page for page in raster_pages}
    if len(by_page) != len(raster_pages) or len(metadata) != len(raster_pages):
        return unknown("raster_page_provenance_incomplete")
    normalized = []
    for number, media, crop, rotation, user_unit in metadata:
        page = by_page.get(number)
        if page is None or page.page_rotation != rotation or page.user_unit != user_unit:
            return unknown("raster_page_metadata_mismatch")
        if rotation != 0:
            return OcrCoordinateFrame(snapshot_id, "unsupported", "page_rotation_requires_renderer_calibration")
        if not page.crop_matches_media:
            return OcrCoordinateFrame(snapshot_id, "unsupported", "cropbox_requires_renderer_calibration")
        if user_unit != 1:
            return unknown("user_unit_requires_renderer_calibration")
        width, height = media[2]-media[0], media[3]-media[1]
        if width <= 0 or height <= 0 or not all(math.isfinite(v) for v in media):
            return unknown("invalid_page_box")
        if input_kind == "png":
            # Native image pixels are the source coordinate authority.  OCR sees
            # only grayscale/contrast (geometry preserving) plus this recorded
            # integer resize; any crop/rotation would fail the metadata checks.
            if page.render_scale != 1 or page.raster_width != width or page.raster_height != height:
                return unknown("native_image_dimensions_do_not_bind_source_frame")
            if page.ocr_width != page.raster_width*page.resize_factor or page.ocr_height != page.raster_height*page.resize_factor:
                return unknown("ocr_preprocessing_dimensions_unbound")
            continue
        # PDFium's scale is pixels per PDF canvas unit.  A one-pixel rounding
        # allowance avoids upgrading a materially different raster.
        if abs(page.raster_width-width*page.render_scale) > 1 or abs(page.raster_height-height*page.render_scale) > 1:
            return unknown("raster_dimensions_do_not_bind_render_scale")
    for token in tokens:
        page = by_page.get(token.page)
        if page is None:
            return unknown("token_page_missing_from_raster_scope")
        factor = page.render_scale*page.resize_factor
        if factor <= 0 or not math.isfinite(factor):
            return unknown("invalid_effective_raster_scale")
        # OCR's left/top/width/height pixels are converted as an envelope from
        # the raster's top-left frame to the canonical PDF page top-left frame.
        values = (token.x/factor, token.y/factor, token.width/factor, token.height/factor)
        if not all(math.isfinite(value) for value in values) or values[2] <= 0 or values[3] <= 0:
            return unknown("invalid_ocr_bbox")
        if (token.x < 0 or token.y < 0 or token.x+token.width > page.ocr_width
                or token.y+token.height > page.ocr_height):
            return unknown("ocr_bbox_outside_preprocessed_image")
        normalized.append(PositionedText(token.text, token.page, *values, token.confidence))
    uncertainty = max(1/(page.render_scale*page.resize_factor) for page in raster_pages) if raster_pages else None
    reason = ("native_image_pixel_frame_bound" if input_kind == "png"
              else "pdfium_scale_bound_unrotated_media_frame")
    return OcrCoordinateFrame(snapshot_id, "verified", reason,
                              uncertainty=uncertainty, normalized_tokens=tuple(normalized))


def _physical_ambiguity(snapshot: Snapshot, provenance):
    """Use OCR line provenance as a discriminator, never data-list position."""
    keys = []
    for token, detail in zip(snapshot.tokens, provenance):
        keys.append((token.page, token.x, token.y, token.width, token.height, token.text,
                     unicodedata.normalize("NFC", token.text), token.confidence,
                     detail.block, detail.paragraph, detail.line, detail.word))
    counts = Counter(keys)
    return frozenset(identifier for identifier, key in zip(snapshot.token_ids, keys) if counts[key] > 1)


def _snapshot_context(source: SourceBinding, pages):
    """Opaque source binding plus all rendering choices that affect bboxes."""
    return ("ocr-token-snapshot-context-v1", source.source_id, source.extraction_version,
            source.renderer, source.renderer_version, source.engine, source.engine_version,
            source.language, source.config,
            tuple((page.page, page.raster_width, page.raster_height, page.ocr_width,
                   page.ocr_height, page.render_scale, page.resize_factor, page.page_rotation,
                   page.crop_matches_media, page.user_unit) for page in pages))


def _rerun_id(key: bytes, source: SourceBinding, snapshot: Snapshot, pages, provenance):
    return _digest(key, ("ocr-rerun-identity-v1", source.source_id, source.extraction_version,
                         source.renderer, source.renderer_version, source.engine,
                         source.engine_version, source.language, source.config, snapshot.snapshot_id,
                         tuple((page.page, page.raster_width, page.raster_height, page.ocr_width,
                                page.ocr_height, page.render_scale, page.resize_factor,
                                page.page_rotation, page.crop_matches_media, page.user_unit) for page in pages),
                         tuple((item.page, item.block, item.paragraph, item.line, item.word)
                               for item in provenance)))


def _compatibility(path: Path, tokens, *, source_bytes: bytes):
    """Compare the diagnostic token universe with the unchanged production path."""
    if path.read_bytes() != source_bytes:
        return ParserModeCompatibility("ineligible", "source_bytes_changed_during_capture")
    extracted = extract_payroll_text(path, minimum_pdf_text=MINIMUM_PDF_TEXT)
    if extracted.extraction_method != "ocr":
        return ParserModeCompatibility("ineligible", "production_did_not_select_ocr")
    if tuple(extracted.tokens) != tuple(tokens):
        return ParserModeCompatibility("ineligible", "production_ocr_token_universe_mismatch")
    # This is intentionally the production parser function, in its actual OCR
    # mode.  It proves equality of facts, not semantic correctness of OCR text.
    observed = parse_positioned_items(tuple(tokens), ocr=True)
    replay = parse_positioned_items(tuple(extracted.tokens), ocr=True)
    if observed != replay:
        return ParserModeCompatibility("ineligible", "production_ocr_fact_replay_mismatch")
    return ParserModeCompatibility("established", "same_tokens_same_ocr_parser_mode")


def capture_ocr_ownership_snapshot(path, *, local_key=None, timeout_seconds=30):
    """Create a private OCR snapshot and fail closed when any contract breaks."""
    path = Path(path)
    key = secrets.token_bytes(32) if local_key is None else local_key
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("local_key_too_short")
    source_bytes = path.read_bytes()
    source_id = _source_id(key, source_bytes)
    try:
        input_kind = _input_kind(path)
        metadata = _page_metadata(path)
        renderer_version, engine_version = _engine_details()
        renderer = ("pypdfium2.render(scale=3)" if input_kind == "pdf"
                    else "PIL.Image.open(native_png_pixels)")
        if input_kind == "png":
            from PIL import __version__ as pillow_version
            renderer_version = str(pillow_version)
        source = SourceBinding(source_id, len(metadata), len(source_bytes), EXTRACTION_VERSION,
                               renderer, renderer_version,
                               "tesseract", engine_version, OCR_LANGUAGE, OCR_CONFIG,
                               True, "same_source_bytes_bound")
        tokens, provenance, pages = _run_local_ocr(path, metadata, timeout_seconds=timeout_seconds)
    except Exception:
        empty = SourceBinding(source_id, 0, len(source_bytes), EXTRACTION_VERSION, "unavailable", "unavailable",
                              "unavailable", "unavailable", OCR_LANGUAGE, OCR_CONFIG, False,
                              "local_ocr_capture_failed")
        blank = observe_tokens((), local_key=key, parser_mode="ocr")
        return OcrOwnershipSnapshot(empty, blank, (), (), OcrCoordinateFrame(blank.snapshot_id, "unknown", "capture_failed"),
                                    reason="local_ocr_capture_failed")
    context = _snapshot_context(source, pages)
    snapshot = observe_tokens(tokens, local_key=key, parser_mode="ocr", snapshot_context=context)
    physical_complete = (len(tokens) == len(provenance)
                         and all(token.page == detail.page for token, detail in zip(tokens, provenance)))
    ambiguity = _physical_ambiguity(snapshot, provenance) if physical_complete else frozenset(snapshot.token_ids)
    snapshot = replace(snapshot, identity_ambiguous=ambiguity)
    frame = _coordinate_frame(snapshot.snapshot_id, tokens, pages, metadata, input_kind=input_kind)
    scope = len(pages) == source.page_count and {page.page for page in pages} == set(range(1, source.page_count+1))
    try:
        second_tokens, second_provenance, second_pages = _run_local_ocr(path, metadata, timeout_seconds=timeout_seconds)
        second = observe_tokens(second_tokens, local_key=key, parser_mode="ocr",
                                snapshot_context=_snapshot_context(source, second_pages))
        rerun_stable = (tokens == second_tokens and provenance == second_provenance and pages == second_pages
                        and snapshot.snapshot_id == second.snapshot_id)
    except Exception:
        rerun_stable = False
    compatibility = _compatibility(path, tokens, source_bytes=source_bytes)
    bytes_current = path.read_bytes() == source_bytes
    identity_status = ("incomplete" if not physical_complete
                       else "ambiguous_explicit" if ambiguity else "unique")
    complete = (source.complete and bytes_current and scope and rerun_stable
                and identity_status in {"unique", "ambiguous_explicit"}
                and frame.status == "verified" and compatibility.status == "established")
    reasons = []
    if not bytes_current: reasons.append("source_bytes_changed_during_capture")
    if not scope: reasons.append("incomplete_page_scope")
    if not physical_complete: reasons.append("physical_token_provenance_incomplete")
    if not rerun_stable: reasons.append("ocr_rerun_unstable")
    if frame.status != "verified": reasons.append(frame.reason)
    if compatibility.status != "established": reasons.append(compatibility.reason)
    return OcrOwnershipSnapshot(source, snapshot, pages, provenance, frame,
                                _rerun_id(key, source, snapshot, pages, provenance), rerun_stable,
                                scope, identity_status, compatibility, complete,
                                "ownership_ready" if complete else reasons[0] if reasons else "incomplete_evidence")


def safe_ocr_ownership_report(snapshot: OcrOwnershipSnapshot) -> OcrOwnershipReport:
    return OcrOwnershipReport(snapshot.source.source_id, snapshot.source.page_count,
                              len(snapshot.snapshot.token_ids), snapshot.rerun_stable,
                              "complete" if snapshot.source.complete else "incomplete",
                              snapshot.physical_identity_status, snapshot.coordinate_frame.status,
                              snapshot.parser_mode.status, snapshot.ownership_ready, snapshot.reason)
