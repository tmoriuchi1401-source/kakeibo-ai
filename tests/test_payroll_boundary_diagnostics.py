"""Synthetic, candidate-independent structural-boundary tests only."""
from pathlib import Path

from app.payroll_boundary_diagnostics import (
    BoundarySegment, StructuralBoundaryEvidence, StructuralRegion, inspect_pdf_boundaries, membership, regions_from_segments,
)
from app.payroll_coordinate_diagnostics import CoordinateFrame
from app.payroll_diagnostic_evidence import (
    observe_tokens, structural_column_section_gate,
)
from app.payroll_ocr import PositionedText


def frame():
    return CoordinateFrame("synthetic", "verified", "synthetic_complete")


def segment(x1, y1, x2, y2, page=1):
    return BoundarySegment(page, x1, y1, x2, y2)


def rectangle(left, top, right, bottom, page=1):
    return (segment(left, top, right, top, page), segment(right, top, right, bottom, page),
            segment(right, bottom, left, bottom, page), segment(left, bottom, left, top, page))


def token(x, y, width=5, height=5, text="T"):
    return PositionedText(text, 1, x, y, width, height, 100)


def evidence(segments):
    return regions_from_segments("synthetic", frame(), segments)


def test_two_explicit_columns_construct_without_any_candidate_token():
    lines = rectangle(0, 0, 100, 40) + (segment(50, 0, 50, 40),)
    first = evidence(lines)
    second = evidence(tuple(lines))  # No tokens enter either extraction call.
    assert first.pages[0].status == "constructed"
    assert len(first.regions) == 2
    assert first == second


def test_multiple_columns_and_section_separator_build_closed_cells():
    lines = rectangle(0, 0, 120, 80) + (segment(40, 0, 40, 80), segment(80, 0, 80, 80),
                                         segment(0, 40, 120, 40))
    result = evidence(lines)
    assert result.pages[0].region_count == 6
    assert membership(token(10, 10), result).status == "exactly_one"
    assert membership(token(90, 50), result).status == "exactly_one"


def test_missing_or_broken_border_is_partial_not_inferred():
    missing = rectangle(0, 0, 100, 40)[:-1]
    broken = (segment(0, 0, 100, 0), segment(100, 0, 100, 40),
              segment(100, 40, 0, 40), segment(0, 0, 0, 15), segment(0, 25, 0, 40))
    assert evidence(missing).pages[0].status == "partial"
    assert evidence(broken).pages[0].status == "partial"


def test_nested_and_merged_cell_layouts_preserve_ambiguity_not_semantics():
    nested = rectangle(0, 0, 100, 100) + rectangle(20, 20, 80, 80)
    # Adjacent-grid construction selects the smallest closed cell, avoiding an
    # invented outer-table membership.
    assert membership(token(30, 30), evidence(nested)).status == "exactly_one"
    merged = (rectangle(0, 0, 100, 100) + (segment(50, 50, 50, 100),
              segment(50, 50, 100, 50)))
    result = evidence(merged)
    assert membership(token(20, 20), result).status == "outside"
    assert result.pages[0].status == "constructed"  # Only independently closed cells count.


def test_boundary_crossing_and_uncertainty_are_fail_closed():
    result = evidence(rectangle(0, 0, 100, 40) + (segment(50, 0, 50, 40),))
    assert membership(token(48, 10, 5), result).status == "boundary_crossing"
    assert membership(token(3, 10, 5), result, uncertainty=4).status == "boundary_sensitive"


def test_different_and_same_column_memberships_are_distinct():
    result = evidence(rectangle(0, 0, 100, 40) + (segment(50, 0, 50, 40),))
    left, right = membership(token(10, 10), result), membership(token(70, 10), result)
    same = membership(token(20, 20), result)
    assert left.region != right.region
    assert left.region == same.region


def test_duplicate_paths_are_not_distinct_regions_and_overlaps_are_ambiguous():
    duplicated = rectangle(0, 0, 100, 40) * 2
    assert len(evidence(duplicated).regions) == 1
    ambiguous = StructuralBoundaryEvidence("synthetic", "synthetic", "verified", (),
        (StructuralRegion(1, 0, 0, 100, 40), StructuralRegion(1, 20, 0, 120, 40)))
    assert membership(token(30, 10), ambiguous).status == "multiple"


def test_coordinate_unverified_is_not_boundary_evidence():
    bad = CoordinateFrame("synthetic", "unknown", "incomplete")
    result = regions_from_segments("synthetic", bad, rectangle(0, 0, 100, 40))
    assert result.pages[0].status == "coordinate_unavailable"
    assert membership(token(10, 10), result).status == "unknown"


def test_gate_uses_normalized_snapshot_tokens_and_never_promotes_other_gates():
    tokens = (PositionedText("label", 1, 10, 10, 5, 5, 100),
              PositionedText("1,234", 1, 20, 20, 5, 5, 100))
    snapshot = observe_tokens(tokens)
    normalized = CoordinateFrame(snapshot.snapshot_id, "verified", "synthetic", tokens)
    structure = regions_from_segments(snapshot.snapshot_id, normalized, rectangle(0, 0, 50, 50))
    gate = structural_column_section_gate(snapshot, snapshot.token_ids[0], snapshot.token_ids[1],
                                          normalized, structure)
    assert gate.status == "pass"
    # The column gate does not assert ownership, numeric grammar, or semantics.
    assert gate.reason_code == "same_closed_structural_region"


def test_real_pdf_stroked_primitives_are_read_without_text_or_candidate_input(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=40)
    stream = DecodedStreamObject()
    stream.set_data(b"0 0 m 100 0 l 100 40 l 0 40 l h S 50 0 m 50 40 l S")
    page[NameObject("/Contents")] = writer._add_object(stream)
    path = Path(tmp_path) / "table.pdf"
    with path.open("wb") as handle:
        writer.write(handle)
    result = inspect_pdf_boundaries(path, "synthetic", frame())
    assert result.pages[0].status == "constructed"
    assert result.pages[0].region_count == 2
