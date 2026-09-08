"""Synthetic tokens only; no production document geometry or values."""
import copy
import json

import pytest

from app.payroll_ocr import PositionedText
from app.payroll_parser import parse_positioned_items
from app.payroll_pdf_diagnostics import diagnose_pdf_pairing, relation


def token(text, x=0, y=0, width=40, height=10):
    return PositionedText(text, 1, x, y, width, height, 100)


@pytest.mark.parametrize("tokens,reason", [
    ((token("基本給"), token("1,234", 45)), None),
    ((token("基本給"), token("1,234", 0, 20)), "vertical_candidate_exists"),
    ((token("基本給"), token("1,234", 100, 20)), "adjacent_column_candidate"),
    ((token("基本給"), token("時間外手当", 15), token("1,234", 20, 20)),
     "candidate_shared_with_other_label"),
    ((token("基本給"), token("1,234", 0, 20), token("2,345", 20, 25)),
     "multiple_vertical_candidates"),
    ((token("基本給"),), "no_numeric_candidate"),
    ((token("基本給"), token("123", 0, 20)), "numeric_format_rejected"),
    ((token("基本給"), token("1,234", 0, 200)), "candidate_outside_allowed_geometry"),
])
def test_synthetic_relations_are_observations_only(tokens, reason):
    before = parse_positioned_items(tokens)
    frozen = copy.deepcopy(before)
    report = diagnose_pdf_pairing(tokens, before, extraction_method="pdf_text")
    assert before == frozen == parse_positioned_items(tokens)
    assert report["pairing_not_found_total"] == sum(
        item.review_reason_code == "pairing_not_found" for item in before)
    if reason is None:
        assert report["primary"] == {}
        assert before[0].value == 1234
    else:
        assert report["primary"][reason] > 0
        assert all(item.value is None and item.needs_review for item in before)
    serialized = json.dumps(report)
    assert "1,234" not in serialized and "基本給" not in serialized
    assert report["blank_region_confirmed"] == 0


def test_repeated_legacy_table_still_pairs():
    tokens = tuple(t for y in (0, 50) for x, name in enumerate(
        ("基本給", "通勤手当", "健康保険", "住民税"))
        for t in (token(name, x*50, y), token("1,234", x*50, y+12)))
    before = parse_positioned_items(tokens)
    assert len(before) == 8
    assert all(item.value == 1234 for item in before)
    assert diagnose_pdf_pairing(tokens, before, extraction_method="pdf_text")["primary"] == {}
    assert before == parse_positioned_items(tokens)


def test_unresolved_and_non_pdf_fail_closed():
    tokens = (token("基本給"),)
    items = parse_positioned_items(tokens)
    assert diagnose_pdf_pairing((), items, extraction_method="pdf_text")["primary"] == {"unresolved": 1}
    with pytest.raises(ValueError, match="pdf_text_required"):
        diagnose_pdf_pairing(tokens, items, extraction_method="ocr")


def test_width_and_transform_can_change_geometric_observation():
    label, value = token("基本給"), token("1,234", 45, 20)
    assert relation(label, value).vertical_window
    assert not relation(token("基本給", width=10), value).vertical_window
    # Synthetic equivalent of a translated token in a separate PDF text frame.
    # This demonstrates sensitivity, not evidence that a real page needs repair.
    assert not relation(label, token("1,234", 145, 20)).vertical_window


def test_observer_does_not_import_or_call_writer():
    import inspect
    import app.payroll_pdf_diagnostics as module
    source = inspect.getsource(module)
    assert "payroll_writer" not in source
    assert "apply_payroll" not in source
