"""Entirely invented layouts and PDF content; no private source coordinates."""
import copy
from dataclasses import replace

import pytest

from app.payroll_ocr import PositionedText, extract_payroll_text
from app.payroll_parser import parse_positioned_items
from app.payroll_pdf_diagnostics import (
    ColumnBand, column_relationship, diagnose_pdf_pairing, relation,
)


def token(text, x=0, y=0, width=40, height=10, page=1):
    return PositionedText(text, page, x, y, width, height, 100)


def observe(tokens, **kwargs):
    items = parse_positioned_items(tokens)
    frozen = copy.deepcopy(items)
    report = diagnose_pdf_pairing(tokens, items, extraction_method="pdf_text", **kwargs)
    assert items == frozen == parse_positioned_items(tokens)
    assert report == diagnose_pdf_pairing(tokens, items, extraction_method="pdf_text", **kwargs)
    return items, report


def test_horizontal_owner_blocks_apparently_unique_vertical_candidate():
    tokens = (token("基本給", -50, 20), token("通勤手当"), token("1,234", 0, 20))
    items, report = observe(tokens)
    assert items[0].value == 1234 and not items[0].needs_review
    assert items[1].review_reason_code == "pairing_not_found"
    assert report["secondary"]["vertical_single_candidate"] == 1
    assert report["secondary"]["candidate_shared_with_other_label"] == 0
    assert report["primary"] == {"candidate_already_used": 1}


def test_duplicate_numeric_text_is_possible_use_not_proven_token_identity():
    tokens = (token("基本給", -50, 20), token("通勤手当"),
              token("1,234", 0, 20), token("1,234", 400, 400))
    _, report = observe(tokens)
    assert report["primary"] == {"candidate_possibly_used": 1}
    assert report["secondary"]["candidate_already_used"] == 0


def test_same_numeric_text_on_another_page_is_not_a_competitor():
    tokens = (token("基本給", -50, 20), token("通勤手当"),
              token("1,234", 0, 20), token("1,234", 400, 400, page=2))
    _, report = observe(tokens)
    assert report["primary"] == {"candidate_already_used": 1}


def test_value_without_confirmed_raw_token_provenance_is_not_used_evidence():
    tokens = (token("基本給", -50, 20), token("通勤手当"), token("1,234", 0, 20))
    items = parse_positioned_items(tokens)
    items[0] = items[0].model_copy(update={"raw_value": None})
    report = diagnose_pdf_pairing(tokens, items, extraction_method="pdf_text")
    assert report["secondary"]["candidate_already_used"] == 0
    # Absence of evidence is deliberately not a claim that the token is unused.


BANDS = (ColumnBand(1, 0, 60), ColumnBand(1, 60, 120), ColumnBand(1, 120, 180))


@pytest.mark.parametrize("x,expected", [(70, "adjacent_column"), (130, "remote_column")])
def test_explicit_columns_separate_nearby_and_remote_tokens(x, expected):
    tokens = (token("基本給", 10, width=30), token("1,234", x, 20, width=20))
    assert column_relationship(*tokens) == "unknown"
    assert column_relationship(*tokens, BANDS) == expected
    _, without = observe(tokens)
    _, with_bands = observe(tokens, column_bands=BANDS)
    assert without["primary"] == {"off_column_y_neighbor": 1}
    assert with_bands["primary"] == {expected + "_candidate": 1}


def test_same_y_does_not_establish_same_column():
    label, value = token("基本給", 10, width=30), token("1,234", 130, width=20)
    assert relation(label, value).dy == 0
    assert column_relationship(label, value, BANDS) == "remote_column"
    # Existing horizontal production behavior is observed, not repaired here.
    items, report = observe((label, value), column_bands=BANDS)
    assert items[0].value == 1234
    assert report["pairing_not_found_total"] == 0


def test_wide_window_is_not_column_membership():
    label, value = token("基本給", 0, width=60), token("1,234", 70, 20, width=20)
    assert relation(label, value).vertical_window
    _, report = observe((label, value), column_bands=BANDS)
    assert report["primary"] == {"vertical_candidate_crosses_column": 1}


def test_boundary_crossing_and_missing_column_evidence_are_unknown():
    label = token("基本給", 10, width=30)
    assert column_relationship(label, token("1,234", 50, 20, width=20), BANDS) == "boundary_ambiguous"
    assert column_relationship(label, token("1,234", 10000, 20), BANDS) == "boundary_ambiguous"
    assert column_relationship(label, token("1,234", 10000, 20)) == "unknown"
    _, report = observe((label, token("1,234", 10000, 20)))
    assert report["primary"] == {"off_column_y_neighbor": 1}
    assert report["secondary"]["adjacent_column_candidate"] == 0
    assert report["secondary"]["candidate_column_unknown"] == 1
    assert relation(label, token("1,234", 10000, 20)).horizontal_edge_distance == 9960


def test_touching_bboxes_on_a_boundary_do_not_prove_one_column():
    tokens = (token("基本給", 10, width=30), token("1,234", 50, 20, width=20))
    _, report = observe(tokens, column_bands=BANDS)
    assert report["secondary"]["candidate_column_boundary_ambiguous"] == 1


def test_invalid_column_bands_are_rejected():
    with pytest.raises(ValueError, match="invalid_column_bands"):
        column_relationship(token("label"), token("value"),
                            (ColumnBand(1, 0, 60), ColumnBand(1, 50, 100)))


def write_pdf(path, matrices, value_x=40):
    """Real PDF graphics/text operators, no mocked extractor or real document."""
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=400)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
        DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    commands = []
    for text, x, y, matrix in zip(("Label", "1,234"), (40, value_x), (300, 280), matrices):
        commands.append("q " + " ".join(map(str, matrix)) +
                        f" cm BT /F1 10 Tf 1 0 0 1 {x} {y} Tm ({text}) Tj ET Q")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(commands).encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


def projected_bbox(t, matrix):
    """Synthetic affine envelope of the extractor's estimated bbox, not glyph truth."""
    a, b, c, d, e, f = matrix
    points = [(a*x+c*y+e, 400-(b*x+d*y+f))
              for x in (t.x, t.x+t.width)
              for y in (400-t.y, 400-t.y-t.height)]
    xs, ys = zip(*points)
    return replace(t, x=min(xs), y=min(ys), width=max(xs)-min(xs), height=max(ys)-min(ys))


IDENTITY = (1, 0, 0, 1, 0, 0)


@pytest.mark.parametrize("matrices,expected", [
    ((IDENTITY, IDENTITY), True),
    (((1, 0, 0, 1, 100, 50), (1, 0, 0, 1, 100, 50)), True),
    ((IDENTITY, (1, 0, 0, 1, 200, 0)), False),
    (((2, 0, 0, 2, 0, 0), (2, 0, 0, 2, 0, 0)), True),
    (((2, 0, 0, 3, 0, 0), (2, 0, 0, 3, 0, 0)), True),
    ((IDENTITY, (2, 0, 0, 2, 0, 0)), False),
    (((0, 1, -1, 0, 400, 0), (0, 1, -1, 0, 400, 0)), False),
    (((1, 0, 3, 1, 0, 0), (1, 0, 3, 1, 0, 0)), False),
])
def test_actual_pdf_cm_raw_coordinates_and_projected_geometry(tmp_path, matrices, expected):
    path = tmp_path / "synthetic.pdf"
    try:
        write_pdf(path, matrices)
        first = extract_payroll_text(path, minimum_pdf_text=0)
        assert first == extract_payroll_text(path, minimum_pdf_text=0)
        assert first.extraction_method == "pdf_text" and len(first.tokens) == 2
        label, value = first.tokens
        assert (label.x, label.y, value.x, value.y) == (40, 100, 40, 120)
        assert relation(label, value).vertical_window
        projected = tuple(projected_bbox(t, m) for t, m in zip(first.tokens, matrices))
        assert relation(*projected).vertical_window is expected
    finally:
        path.unlink(missing_ok=True)


def test_different_text_frames_can_hide_a_geometric_candidate(tmp_path):
    path = tmp_path / "synthetic.pdf"
    matrices = (IDENTITY, (1, 0, 0, 1, -200, 0))
    try:
        write_pdf(path, matrices, value_x=240)
        extracted = extract_payroll_text(path, minimum_pdf_text=0)
        assert not relation(*extracted.tokens).vertical_window
        projected = tuple(projected_bbox(t, m) for t, m in zip(extracted.tokens, matrices))
        assert relation(*projected).vertical_window
    finally:
        path.unlink(missing_ok=True)


def test_common_scale_preserves_observer_window_but_not_fixed_production_tolerance():
    original = (token("基本給"), token("1,234", 38))
    scaled = tuple(replace(t, x=t.x*2, y=t.y*2, width=t.width*2, height=t.height*2) for t in original)
    assert parse_positioned_items(original)[0].value == 1234
    assert parse_positioned_items(scaled)[0].review_reason_code == "pairing_not_found"
    # The existing horizontal -3 tolerance is absolute, not scale-normalized.


def test_production_modules_do_not_depend_on_observer():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "app"
    for path in root.glob("*.py"):
        if path.name in {"payroll_pdf_diagnostics.py", "payroll_diagnostic_evidence.py",
                         "payroll_coordinate_diagnostics.py", "payroll_boundary_diagnostics.py",
                         "payroll_ownership_provenance.py", "payroll_extraction_path_diagnostics.py",
                         "payroll_ocr_snapshot_bridge.py",
                         "payroll_ownership_integration.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert "payroll_pdf_diagnostics" not in (node.module or "")
                assert "payroll_diagnostic_evidence" not in (node.module or "")
                assert "payroll_coordinate_diagnostics" not in (node.module or "")
                assert "payroll_boundary_diagnostics" not in (node.module or "")
                assert "payroll_ownership_provenance" not in (node.module or "")
                assert "payroll_extraction_path_diagnostics" not in (node.module or "")
            elif isinstance(node, ast.Import):
                assert all("payroll_pdf_diagnostics" not in alias.name for alias in node.names)
                assert all("payroll_diagnostic_evidence" not in alias.name for alias in node.names)
                assert all("payroll_coordinate_diagnostics" not in alias.name for alias in node.names)
                assert all("payroll_boundary_diagnostics" not in alias.name for alias in node.names)
                assert all("payroll_ownership_provenance" not in alias.name for alias in node.names)
                assert all("payroll_extraction_path_diagnostics" not in alias.name for alias in node.names)


def test_ownership_integration_is_the_only_narrow_production_bridge():
    import ast
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "app" / "payroll_ownership_integration.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    provenance_imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "payroll_ownership_provenance"
        for alias in node.names
    }
    assert provenance_imports == {
        "PayrollOwnershipAdoptionCandidate",
        "PayrollOwnershipAttestationEvaluation",
        "PayrollOwnershipPlanBinding",
        "attest_payroll_write_plan_ownership",
    }
    source = path.read_text(encoding="utf-8-sig")
    assert "apply_payroll_write_plans" not in source
    assert "append_header_rows" not in source
    assert "append_item_rows" not in source
