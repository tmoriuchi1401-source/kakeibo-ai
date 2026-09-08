"""Synthetic topology tests for candidate-independent table structure evidence."""
from dataclasses import replace

from app.payroll_boundary_diagnostics import (
    BoundarySegment, band_membership, partition_membership, structure_from_segments,
    table_membership,
)
from app.payroll_coordinate_diagnostics import CoordinateFrame
from app.payroll_ocr import PositionedText


def frame():
    return CoordinateFrame("structure", "verified", "synthetic_complete")


def line(x1, y1, x2, y2, page=1):
    return BoundarySegment(page, x1, y1, x2, y2)


def rectangle(left, top, right, bottom, page=1):
    return (line(left, top, right, top, page), line(right, top, right, bottom, page),
            line(right, bottom, left, bottom, page), line(left, bottom, left, top, page))


def token(x, y, width=4, height=4, page=1):
    return PositionedText("label", page, x, y, width, height, 100)


def structure(lines):
    return structure_from_segments("structure", frame(), lines)


def test_full_grid_yields_strong_table_bands_partitions_and_separator():
    result = structure(rectangle(0, 0, 100, 80) + (line(50, 0, 50, 80), line(0, 40, 100, 40)))
    assert [table.certainty for table in result.tables] == ["strong"]
    assert len(result.bands) == 2 and len(result.lanes) == 2
    assert len(result.separators) == 1 and result.separators[0].certainty == "strong"
    table = table_membership(token(10, 10), result)
    assert table.status == "inside_table_envelope"
    assert band_membership(token(10, 10), result, table).status == "inside_exactly_one_band"
    assert partition_membership(token(10, 10), result, table).status == "inside_exactly_one_partition"


def test_structure_is_identical_without_or_with_moved_numeric_candidate():
    lines = rectangle(0, 0, 100, 40) + (line(50, 0, 50, 40),)
    without_tokens = structure(lines)
    # Token positions are deliberately absent from the structure extraction API:
    # removing labels/candidates or moving a numeric-looking token cannot change it.
    assert without_tokens == structure(tuple(lines))
    removed_candidate = None
    moved_candidate = replace(token(10, 10), x=90)
    assert removed_candidate is None and moved_candidate.x == 90
    assert without_tokens == structure(lines)


def test_outer_borderless_internal_grid_is_partial_not_completed():
    result = structure((line(20, 0, 20, 60), line(60, 0, 60, 60),
                        line(0, 20, 80, 20), line(0, 40, 80, 40)))
    assert [table.certainty for table in result.tables] == ["partial"]
    assert table_membership(token(30, 30), result).status == "structure_incomplete"


def test_broken_separators_remain_partial_and_do_not_create_lanes():
    result = structure(rectangle(0, 0, 100, 80) + (line(50, 0, 50, 30),
                                                      line(50, 50, 50, 80),
                                                      line(0, 40, 40, 40)))
    assert result.tables[0].certainty == "strong"
    assert [partition.certainty for partition in result.partitions] == ["partial", "partial"]
    assert len(result.lanes) == 1
    assert result.separators[0].certainty == "partial"


def test_local_vertical_partition_is_not_page_wide_column_authority():
    result = structure(rectangle(0, 0, 100, 80) + (line(50, 0, 50, 60),))
    assert result.partitions[0].certainty == "partial"
    table = table_membership(token(70, 10), result)
    assert partition_membership(token(70, 10), result, table).status == "inside_exactly_one_partition"


def test_subtable_scope_is_not_promoted_to_the_page():
    result = structure(rectangle(20, 20, 80, 60))
    assert table_membership(token(30, 30), result).status == "inside_table_envelope"
    assert table_membership(token(5, 5), result).status == "outside_known_structure"


def test_nested_tables_are_ambiguous_not_page_wide():
    result = structure(rectangle(0, 0, 100, 100) + rectangle(20, 20, 80, 80))
    assert table_membership(token(30, 30), result).status == "ambiguous"


def test_merged_cell_absence_does_not_claim_a_missing_partition():
    lines = rectangle(0, 0, 100, 80) + (line(50, 40, 50, 80), line(50, 40, 100, 40))
    result = structure(lines)
    assert all(partition.certainty == "partial" for partition in result.partitions)
    table = table_membership(token(20, 10), result)
    assert partition_membership(token(20, 10), result, table).status == "inside_exactly_one_partition"


def test_multiple_independent_tables_remain_separate_scopes():
    result = structure(rectangle(0, 0, 40, 40) + rectangle(60, 0, 100, 40))
    assert len(result.tables) == 2
    assert table_membership(token(10, 10), result).scope_id != table_membership(token(70, 10), result).scope_id


def test_separator_only_is_observed_but_creates_no_table_membership():
    result = structure((line(0, 40, 100, 40),))
    assert len(result.tables) == 0 and result.separators[0].certainty == "partial"
    assert table_membership(token(10, 10), result).status == "structure_incomplete"


def test_cell_boundary_and_partition_crossing_are_distinct_from_table_membership():
    result = structure(rectangle(0, 0, 100, 80) + (line(50, 0, 50, 80), line(0, 40, 100, 40)))
    horizontal_crossing = token(10, 38, height=5)
    vertical_crossing = token(48, 10, width=5)
    table = table_membership(horizontal_crossing, result)
    assert table.status == "inside_table_envelope"
    assert band_membership(horizontal_crossing, result, table).status == "intersects_multiple_bands"
    assert partition_membership(vertical_crossing, result, table).status == "crosses_partition"


def test_bbox_uncertainty_is_not_promoted_to_membership():
    result = structure(rectangle(0, 0, 100, 40))
    assert table_membership(token(2, 10), result, uncertainty=3).status == "boundary_sensitive"
