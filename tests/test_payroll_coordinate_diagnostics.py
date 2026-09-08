"""Synthetic coordinate provenance only; no private document text or geometry."""
from pathlib import Path

import pytest

from app.payroll_coordinate_diagnostics import (
    PageProvenance, TokenProvenance, affine_point, compose_affine,
    inspect_pdf_coordinate_frame, normalize_coordinate_frame,
)
from app.payroll_ocr import PositionedText, extract_payroll_text


IDENTITY = (1, 0, 0, 1, 0, 0)
PAGE = PageProvenance(1, (0, 0, 400, 400), (0, 0, 400, 400), 0, 1)


def tokens():
    return (PositionedText("Label", 1, 40, 100, 30, 10, 100),
            PositionedText("1,234", 1, 40, 120, 30, 10, 100))


def frame(matrix=IDENTITY, *, rotation=0, axes=(1, 0, 0, 1), user_unit=1,
          match=True, per_token=False):
    page = PageProvenance(1, (0, 0, 400, 400), (0, 0, 400, 400), rotation, user_unit)
    values = (matrix, (1, 0, 0, 1, 20, 10)) if per_token else (matrix, matrix)
    return normalize_coordinate_frame("synthetic", tokens(), (page,),
                                      tuple(TokenProvenance(1, value, axes) for value in values),
                                      extraction_matches=match)


@pytest.mark.parametrize("matrix,rotation", [
    (IDENTITY, 0),
    ((1, 0, 0, 1, 30, 40), 0),
    ((2, 0, 0, 2, 0, 0), 0),
    ((2, 0, 0, 3, 0, 0), 0),
    ((0, 1, -1, 0, 400, 0), 90),
    ((-1, 0, 0, -1, 400, 400), 180),
    ((0, -1, 1, 0, 0, 400), 270),
    ((1, 0, .25, 1, 0, 0), 0),
])
def test_supported_affine_transform_has_complete_canonical_frame(matrix, rotation):
    result = frame(matrix, rotation=rotation)
    assert result.status == "verified"
    assert result.reason == "canonical_crop_local_frame"
    assert result.transform_scope == "page_common"
    assert len(result.normalized_tokens) == 2
    assert all(token.width > 0 and token.height > 0 for token in result.normalized_tokens)


def test_rotation_axis_aligned_envelope_swaps_bbox_orientation():
    result = frame((0, 1, -1, 0, 400, 0), rotation=90)
    original, normalized = tokens()[0], result.normalized_tokens[0]
    assert normalized.width == original.height
    assert normalized.height == original.width


def test_scaling_propagates_width_estimate_and_needs_separate_boundary_uncertainty():
    result = frame((2, 0, 0, 3, 0, 0))
    original, normalized = tokens()[0], result.normalized_tokens[0]
    assert normalized.width == original.width*2
    assert normalized.height == original.height*3
    # Coordinate verification does not claim the estimated original width was exact.


def test_reflection_and_nonidentity_text_matrix_are_unsupported():
    assert frame((-1, 0, 0, 1, 400, 0)).reason == "ctm_noninvertible_or_reflective"
    assert frame(IDENTITY, axes=(1, 0, .2, 1)).reason == "text_matrix_unsupported"


@pytest.mark.parametrize("kwargs,reason", [
    ({"match": False}, "frame_provenance_incomplete"),
    ({"user_unit": 2}, "user_unit_requires_calibration"),
    ({"rotation": 45}, "page_rotation_unsupported"),
])
def test_incomplete_or_uncalibrated_provenance_stays_nonverified(kwargs, reason):
    result = frame(**kwargs)
    assert result.status in {"unknown", "unsupported"}
    assert result.reason == reason


def test_nested_composition_is_equivalent_to_its_composed_ctm():
    outer, inner = (1, 0, 0, 1, 7, 11), (2, 0, 0, 2, 20, 30)
    composed = compose_affine(outer, inner)
    assert affine_point(composed, 40, 300) == affine_point(outer, *affine_point(inner, 40, 300))
    assert frame(composed).status == "verified"


def test_complete_per_token_ctms_have_a_unique_page_frame_but_are_marked_as_such():
    result = frame(per_token=True)
    assert result.status == "verified"
    assert result.transform_scope == "per_token"


def write_pdf(path: Path, matrix, *, rotation=0, nested=False):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=400)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
        DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    if rotation:
        page[NameObject("/Rotate")] = NumberObject(rotation)
    commands = []
    for text, y in (("Label", 300), ("1,234", 280)):
        nested_prefix = "1 0 0 1 7 11 cm " if nested else ""
        commands.append("q " + nested_prefix + " ".join(str(value) for value in matrix)
                        + f" cm BT /F1 10 Tf 1 0 0 1 40 {y} Tm ({text}) Tj ET Q")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(commands).encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


@pytest.mark.parametrize("matrix,rotation,nested,status", [
    (IDENTITY, 0, False, "verified"),
    ((1, 0, 0, 1, 20, 30), 0, False, "verified"),
    ((2, 0, 0, 2, 0, 0), 0, False, "verified"),
    ((2, 0, 0, 3, 0, 0), 0, False, "verified"),
    ((0, 1, -1, 0, 400, 0), 90, False, "verified"),
    ((-1, 0, 0, -1, 400, 400), 180, False, "verified"),
    ((0, -1, 1, 0, 0, 400), 270, False, "verified"),
    ((1, 0, .25, 1, 0, 0), 0, False, "verified"),
    ((-1, 0, 0, 1, 400, 0), 0, False, "unsupported"),
    ((1, 0, 0, 1, 20, 30), 0, True, "verified"),
])
def test_actual_pdf_visitor_provenance_normalizes_synthetic_pdf(
        tmp_path, matrix, rotation, nested, status):
    path = tmp_path / "synthetic.pdf"
    try:
        write_pdf(path, matrix, rotation=rotation, nested=nested)
        extracted = extract_payroll_text(path, minimum_pdf_text=0)
        result = inspect_pdf_coordinate_frame(path, "synthetic", extracted.tokens)
        assert result.status == status
        if status == "verified":
            assert len(result.normalized_tokens) == len(extracted.tokens)
    finally:
        path.unlink(missing_ok=True)
