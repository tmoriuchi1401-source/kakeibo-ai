"""Synthetic geometry only; no original images, OCR records, or transports."""
from dataclasses import replace
import ast
import json
from pathlib import Path

import pytest

from app.medical_layout_shadow import PageFrame, observe_layout
from app.medical_receipt_privacy import _StructuredOcrToken as Token, build_receipt_privacy_preview


def token(text, x=20, y=20, width=80, height=20, confidence=96, page=1, line=1):
    return Token(text, page, x, y, width, height, confidence, (1, 1, line, 5))


def observe(*tokens, frames=(PageFrame(1, 600, 800),), pages=1, complete=True):
    return observe_layout(tokens, frames, expected_pages=pages, observation_complete=complete)


def test_garbled_label_horizontal_and_vertical_hypotheses_without_confirmation():
    for amount in (token("1200", x=120), token("1200", y=65)):
        result = observe(token("支□総□"), amount)
        assert len(result.hypotheses) == 1
        h = result.hypotheses[0]
        assert h.status == "unresolved"
        assert h.issues == ("payment_role_unproven",)
        assert h.axis == ("row" if amount.x == 120 else "column")
        assert not hasattr(result, "amount")
        assert not hasattr(result, "confirmed")


@pytest.mark.parametrize("factor", [0.125, 0.5, 2, 7, 100])
def test_scale_invariance_preserves_every_relationship_and_issue(factor):
    original = (token("支□額"), token("1200", x=120), token("3.400", x=200, confidence=69))
    base = observe(*original)
    scaled = tuple(replace(t, x=t.x*factor, y=t.y*factor, width=t.width*factor,
                           height=t.height*factor) for t in original)
    result = observe(*scaled, frames=(PageFrame(1, 600*factor, 800*factor),))
    assert result.aggregate() == base.aggregate()
    assert result.hypotheses == base.hypotheses
    for a, b in zip(result.regions, base.regions, strict=True):
        assert a.box == pytest.approx(b.box)
    for a, b in zip(result.relations, base.relations, strict=True):
        assert (a.left, a.right, a.axis) == (b.left, b.right, b.axis)
        assert a.relative_gap == pytest.approx(b.relative_gap)
        assert a.alignment == pytest.approx(b.alignment)


def test_page_margins_do_not_change_relative_gap_or_pairings():
    tokens = (token("□額"), token("1200", x=120), token("税額", y=65))
    base = observe(*tokens)
    result = observe(*(replace(t, x=t.x+300, y=t.y+100) for t in tokens),
                     frames=(PageFrame(1, 1400, 1200),))
    assert result.hypotheses == base.hypotheses
    for a, b in zip(result.relations, base.relations, strict=True):
        assert a.relative_gap == pytest.approx(b.relative_gap)


@pytest.mark.parametrize("gap,linked", [(159, True), (161, False)])
def test_horizontal_search_window_boundary_never_authorizes_payment(gap, linked):
    result = observe(token("□額"), token("1200", x=100+gap))
    assert bool(result.hypotheses) is linked
    assert all(h.status == "unresolved" for h in result.hypotheses)
    if not linked:
        assert "numeric_region_unassigned" in result.issues


def test_token_order_and_numeric_magnitude_do_not_select_a_winner():
    tokens = (token("□額"), token("100", x=120), token("999999", x=220))
    base = observe(*tokens)
    swapped = observe(tokens[2], tokens[0], tokens[1])
    changed = observe(tokens[0], replace(tokens[1], text="999999"), replace(tokens[2], text="100"))
    assert base.aggregate() == swapped.aggregate() == changed.aggregate()
    assert base.hypotheses == changed.hypotheses
    assert len(base.hypotheses) == 2


@pytest.mark.parametrize("text,confidence,state", [
    ("3400", 69, "low_confidence_numeric"), ("3.400", 96, "malformed_numeric"),
    ("???円", 96, "malformed_numeric"), ("3O00", 96, "malformed_numeric"),
    ("3400", -1, "low_confidence_numeric"), ("3400", float("nan"), "low_confidence_numeric"),
])
def test_unresolved_competitor_retained(text, confidence, state):
    result = observe(token("支□額"), token("1200", x=120), token(text, x=220, confidence=confidence))
    assert len(result.regions) == 3
    assert result.regions[2].numeric_state == state
    assert {h.numeric for h in result.hypotheses} == {1, 2}
    assert all("competing_relationships" in h.issues for h in result.hypotheses)


@pytest.mark.parametrize("label", ["点数", "総医療費", "保険負担", "公費負担", "小計", "税額", "預り金", "お釣り", "自己負担額", "一部負担", "自費"])
def test_negative_context_is_retained_and_never_erases_amount(label):
    result = observe(token(label), token("1200", x=120))
    assert result.regions[0].negative_context
    assert len(result.hypotheses) == 1
    assert "negative_context_observed" in result.hypotheses[0].issues
    assert result.aggregate()["numeric_regions"] == 1


def test_partial_payment_and_unknown_total_are_both_unresolved():
    result = observe(token("自己負担額"), token("1200", x=120),
                     token("□額", y=140), token("2400", x=120, y=140))
    assert {h.numeric for h in result.hypotheses} == {1, 3}
    assert all(h.status == "unresolved" for h in result.hypotheses)
    assert all("payment_role_unproven" in h.issues for h in result.hypotheses)


def test_two_separated_columns_keep_vertical_cell_hypotheses_separate():
    result = observe(token("□額", x=20, width=60), token("1200", x=20, y=60, width=60),
                     token("税額", x=400, width=60), token("300", x=400, y=60, width=60))
    assert {(h.label, h.numeric, h.axis) for h in result.hypotheses} == {(0, 1, "column"), (2, 3, "column")}


def test_equal_line_key_cannot_bridge_distant_regions_and_different_keys_can_align():
    assert not observe(token("□額"), token("1200", x=400, y=600)).hypotheses
    assert len(observe(token("□額", line=4), token("1200", x=120, line=9)).hypotheses) == 1


def test_no_transitive_row_merging_and_no_cross_page_edges():
    result = observe(token("□額", y=20), token("1200", x=120, y=30), token("300", x=220, y=40))
    assert {(r.left, r.right) for r in result.relations} == {(0, 1), (1, 2)}
    result = observe(token("□額"), token("1200", x=120, page=2),
                     frames=(PageFrame(1, 600, 800), PageFrame(2, 600, 800)), pages=2)
    assert not result.relations


def test_overlapping_regions_and_duplicate_ocr_are_not_independent_signals():
    result = observe(token("□額"), token("1200", x=60), token("1200", x=60))
    assert result.observation_group == 0
    assert len(result.hypotheses) == 2
    assert all("overlapping_regions" in h.issues and "competing_relationships" in h.issues
               for h in result.hypotheses)


@pytest.mark.parametrize("changes", [{"x": -1}, {"width": 0}, {"x": float("inf")}, {"x": 590}])
def test_invalid_geometry_is_kept_as_unlocated_region(changes):
    result = observe(token("□額"), replace(token("1200", x=120), **changes))
    assert len(result.regions) == 2
    assert result.regions[1].box is None
    assert "observation_incomplete" in result.issues
    assert not result.hypotheses


@pytest.mark.parametrize("frames", [(), (PageFrame(1, 0, 800),), (PageFrame(1, 600, float("nan")),),
                                    (PageFrame(1, 600, 800), PageFrame(1, 600, 800))])
def test_missing_or_invalid_page_frames_cannot_look_complete(frames):
    assert "observation_incomplete" in observe(token("1200"), frames=frames).issues


def test_coverage_empty_ocr_and_explicit_failure_are_not_success():
    assert "observation_incomplete" in observe().issues
    assert "observation_incomplete" in observe(token("1200"), complete=False).issues
    result = observe(token("1200"), pages=2, frames=(PageFrame(1, 600, 800), PageFrame(2, 600, 800)))
    assert "observation_incomplete" in result.issues
    assert "observation_incomplete" in observe(token("1200"), pages=True).issues


def test_unreadable_regions_and_excess_input_are_explicit():
    result = observe(token("???"), token("1200", x=120))
    assert "unreadable_region" in result.hypotheses[0].issues
    assert observe(*(token("1200") for _ in range(513))).issues == (
        "observation_incomplete", "region_limit_exceeded")


def test_aggregate_and_repr_do_not_retain_or_disclose_raw_text_or_values(capsys):
    result = observe(token("SYNTHETIC_PRIVATE_MARKER"), token("987654", x=120))
    exported = json.dumps(result.aggregate()) + repr(result) + repr(result.regions)
    assert "SYNTHETIC_PRIVATE_MARKER" not in exported
    assert "987654" not in exported
    assert all(type(v) is int for v in result.aggregate().values())
    assert capsys.readouterr() == ("", "")


def test_shadow_has_no_production_callers_and_cannot_change_resolution():
    tokens = (token("支払額"), token("1200", x=120), token("3.400", x=220))
    before = build_receipt_privacy_preview("病院 診療", tokens)
    observe(*tokens)
    assert build_receipt_privacy_preview("病院 診療", tokens) == before
    assert before.status == "needs_review"
    for path in (Path(__file__).resolve().parents[1] / "app").glob("*.py"):
        if path.name in {"medical_layout_shadow.py", "medical_layout_evaluation.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not any(isinstance(n, ast.ImportFrom) and
                       any(s in (n.module or "") for s in ("medical_layout_shadow", "medical_layout_evaluation"))
                       for n in ast.walk(tree))
