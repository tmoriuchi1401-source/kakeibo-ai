"""Synthetic evidence only. Production parser remains the baseline oracle."""
import copy
from dataclasses import replace
import json

import pytest

from app.payroll_ocr import PositionedText, extract_payroll_text
from app.payroll_parser import parse_positioned_items
from app.payroll_diagnostic_evidence import (
    observe_tokens, consumption_ledger, frame_evidence, inspect_pdf_frame,
    Region, BoundaryEvidence, OperatorConfirmation, evaluate_candidate,
    column_section_gate,
)


KEY = b"synthetic-local-evaluation-key-0000"
IDENTITY = (1, 0, 0, 1, 0, 0)


def token(text, x=20, y=20, width=30, height=10, page=1):
    return PositionedText(text, page, x, y, width, height, 100)


def basic():
    return observe_tokens((token("基本給"), token("1,234", y=40)), local_key=KEY)


def verified(snapshot, matrices=None, **kwargs):
    count = len(snapshot.tokens)
    return frame_evidence(snapshot, matrices or (IDENTITY,)*count,
                          text_axes=((1, 0, 0, 1),)*count, page_rotations=(0,)*count,
                          user_units=(1,)*count, extraction_matches=True, **kwargs)


def boundaries(snapshot, regions=None, error=0):
    return BoundaryEvidence(snapshot.snapshot_id,
                            (Region(1, 0, 0, 100, 100),) if regions is None else regions,
                            "synthetic_structure", error)


def record(snapshot, **kwargs):
    return evaluate_candidate(snapshot, snapshot.token_ids[0], snapshot.token_ids[1], **kwargs)


def test_snapshot_identity_distinguishes_same_text_and_repeats_stably():
    tokens = (token("1,234"), token("1,234", x=80))
    a = observe_tokens(tokens, local_key=KEY)
    b = observe_tokens(tokens, local_key=KEY)
    assert a == b and a.token_ids[0] != a.token_ids[1]
    assert observe_tokens(tokens).snapshot_id != a.snapshot_id
    assert observe_tokens(tuple(reversed(tokens)), local_key=KEY).snapshot_id != a.snapshot_id
    assert not a.identity_ambiguous


def test_coincident_identical_occurrences_are_unique_locators_but_not_physical_proof():
    snapshot = observe_tokens((token("1,234"), token("1,234")), local_key=KEY)
    assert len(set(snapshot.token_ids)) == 2
    assert snapshot.identity_ambiguous == frozenset(snapshot.token_ids)


def test_normalized_text_does_not_collapse_distinct_physical_occurrences():
    snapshot = observe_tokens((token("e\u0301"), token("\u00e9")), local_key=KEY)
    assert len(set(snapshot.token_ids)) == 2
    assert len(snapshot.identity_ambiguous) == 2


def test_ledger_definite_used_and_distinct_unused_without_id_or_value_matching():
    snapshot = observe_tokens((token("基本給", x=-30, y=40), token("1,234", y=40),
                               token("2,345", x=200, y=200)), local_key=KEY)
    ledger = consumption_ledger(snapshot)
    assert ledger.complete
    assert ledger.ownership(snapshot, snapshot.token_ids[1]).reason_code == "token_already_used"
    assert ledger.ownership(snapshot, snapshot.token_ids[2]).reason_code == "definitely_distinct_unused"


def test_same_text_ambiguity_cannot_establish_unused_even_for_distinct_other_token():
    snapshot = observe_tokens((token("基本給", x=-30, y=40), token("1,234", y=40),
                               token("1,234", x=200, y=200), token("2,345", x=300, y=300)), local_key=KEY)
    ledger = consumption_ledger(snapshot)
    assert not ledger.complete
    assert ledger.ownership(snapshot, snapshot.token_ids[1]).reason_code == "same_text_ownership_ambiguous"
    assert ledger.ownership(snapshot, snapshot.token_ids[3]).reason_code == "consumption_ledger_incomplete"


def test_missing_consumption_provenance_stays_unknown():
    snapshot = observe_tokens((token("基本給", x=-30, y=40), token("1,234", y=40)), local_key=KEY)
    snapshot = replace(snapshot, facts=(replace(snapshot.facts[0], raw_value=None),))
    assert not consumption_ledger(snapshot).complete
    assert consumption_ledger(snapshot).ownership(snapshot, snapshot.token_ids[1]).status == "unknown"


def test_two_pending_labels_compete_for_one_numeric_token():
    snapshot = observe_tokens((token("基本給"), token("通勤手当", x=25), token("1,234", y=40)), local_key=KEY)
    result = evaluate_candidate(snapshot, snapshot.token_ids[0], snapshot.token_ids[2], frame=verified(snapshot))
    assert dict(result.gates)["ownership"].reason_code == "fallback_ownership_collision"
    assert result.outcome == "reject"


def test_used_token_overrides_apparent_vertical_uniqueness():
    snapshot = observe_tokens((token("通勤手当"), token("1,234", y=40), token("基本給", x=-30, y=40)), local_key=KEY)
    result = record(snapshot, frame=verified(snapshot))
    assert dict(result.gates)["ownership"].reason_code == "token_already_used"
    assert result.outcome == "reject"


@pytest.mark.parametrize("regions,reason,status", [
    ((Region(1, 0, 0, 100, 100),), "known_column_section", "pass"),
    ((Region(1, 0, 0, 100, 100), Region(1, 10, 0, 110, 100)), "column_section_ambiguous", "unknown"),
    ((), "column_section_unknown", "unknown"),
    ((Region(1, 0, 0, 100, 45),), "column_boundary_crossed", "fail"),
    ((Region(1, 0, 0, 100, 35), Region(1, 0, 35, 100, 100)), "unexpected_column_or_section", "fail"),
])
def test_independent_column_and_section_evidence(regions, reason, status):
    snapshot = basic()
    gate = column_section_gate(snapshot, *snapshot.tokens, boundaries(snapshot, regions))
    assert (gate.reason_code, gate.status) == (reason, status)


def test_candidate_cannot_select_its_own_expected_region():
    snapshot = basic()
    regions = (Region(1, 0, 0, 60, 100), Region(1, 60, 0, 120, 100))
    candidate = replace(snapshot.tokens[1], x=70)
    gate = column_section_gate(snapshot, snapshot.tokens[0], candidate, boundaries(snapshot, regions))
    assert gate.reason_code == "unexpected_column_or_section"


def test_width_uncertainty_can_flip_membership_and_is_boundary_sensitive():
    snapshot = basic()
    region = (Region(1, 0, 0, 51, 100),)
    assert column_section_gate(snapshot, *snapshot.tokens, boundaries(snapshot, region, error=0)).status == "pass"
    assert column_section_gate(snapshot, *snapshot.tokens, boundaries(snapshot, region, error=2)).reason_code == "boundary_sensitive"
    assert column_section_gate(snapshot, *snapshot.tokens, boundaries(snapshot, region, error=None)).reason_code == "bbox_uncertainty_unknown"


@pytest.mark.parametrize("matrices,status,reason", [
    ((IDENTITY, IDENTITY), "verified", "common_raw_identity_or_translation"),
    (((1, 0, 0, 1, 40, 50),)*2, "verified", "common_raw_identity_or_translation"),
    (((2, 0, 0, 2, 0, 0),)*2, "unknown", "positive_scaling_requires_calibration"),
    (((0, 1, -1, 0, 0, 0),)*2, "unsupported", "rotation_skew_or_reflection"),
    (((1, 0, 1, 1, 0, 0),)*2, "unsupported", "rotation_skew_or_reflection"),
    ((IDENTITY, (1, 0, 0, 1, 10, 0)), "unsupported", "inconsistent_transform"),
])
def test_transform_classification(matrices, status, reason):
    evidence = verified(basic(), matrices)
    assert (evidence.status, evidence.reason) == (status, reason)


def test_cm_alone_is_not_frame_proof_and_stale_evidence_cannot_pass():
    snapshot = basic()
    frame = frame_evidence(snapshot, (IDENTITY,)*2, text_axes=((1, 0, 0, 1),)*2,
                           page_rotations=(0,)*2, user_units=(1,)*2)
    assert frame.status == "unknown"
    stale = replace(verified(snapshot), snapshot_id="stale")
    assert dict(record(snapshot, frame=stale).gates)["coordinates"].status == "unknown"


def test_semantic_gate_requires_explicit_confirmation_even_with_all_other_gates_pass():
    snapshot = basic()
    result = record(snapshot, frame=verified(snapshot), boundaries=boundaries(snapshot))
    gates = dict(result.gates)
    assert all(g.status == "pass" for name, g in gates.items() if name != "semantic")
    assert gates["semantic"].reason_code == "ground_truth_required"
    assert result.outcome == "unresolved"
    confirmation = OperatorConfirmation(snapshot.snapshot_id, *snapshot.token_ids, True)
    assert record(snapshot, frame=verified(snapshot), boundaries=boundaries(snapshot),
                  confirmation=confirmation).outcome == "shadow_eligible"
    assert record(snapshot, frame=verified(snapshot), boundaries=boundaries(snapshot),
                  confirmation=replace(confirmation, snapshot_id="stale")).outcome == "unresolved"


def test_unmapped_item_stays_unresolved_despite_operator_confirmation():
    snapshot = observe_tokens((token("合成手当"), token("1,234", y=40)), local_key=KEY)
    confirmation = OperatorConfirmation(snapshot.snapshot_id, *snapshot.token_ids, True)
    result = record(snapshot, frame=verified(snapshot), boundaries=boundaries(snapshot), confirmation=confirmation)
    assert dict(result.gates)["semantic"].reason_code == "standard_item_unresolved"
    assert result.outcome == "unresolved"


@pytest.mark.parametrize("text", ["123", "1,234 2,345", "prefix1,234", "-1,234", "1,234時間"])
def test_numeric_policy_is_not_expanded(text):
    snapshot = observe_tokens((token("基本給"), token(text, y=40)), local_key=KEY)
    assert dict(record(snapshot).gates)["numeric"].status == "fail"


def test_observation_preserves_production_and_export_contains_no_text_geometry_or_values():
    tokens = (token("基本給"), token("1,234", y=40))
    before = copy.deepcopy(parse_positioned_items(tokens))
    snapshot = observe_tokens(tokens, local_key=KEY)
    output = record(snapshot, frame=verified(snapshot), boundaries=boundaries(snapshot)).safe_dict()
    assert parse_positioned_items(tokens) == before
    assert snapshot == observe_tokens(tokens, local_key=KEY)
    serialized = json.dumps(output, ensure_ascii=False)
    assert all(secret not in serialized for secret in ("基本給", "1,234", '"bbox":', '"raw_value":'))
    assert "基本給" not in repr(snapshot)


def test_actual_pdf_visitor_must_match_snapshot(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
        DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"q 1 0 0 1 10 10 cm BT /F1 10 Tf 1 0 0 1 20 100 Tm (Label) Tj ET Q")
    page[NameObject("/Contents")] = writer._add_object(stream)
    path = tmp_path / "synthetic.pdf"
    try:
        with path.open("wb") as handle:
            writer.write(handle)
        extracted = extract_payroll_text(path, minimum_pdf_text=0)
        snapshot = observe_tokens(extracted.tokens, local_key=KEY)
        assert inspect_pdf_frame(path, snapshot).status == "verified"
        assert inspect_pdf_frame(path, basic()).status == "unknown"
    finally:
        path.unlink(missing_ok=True)


def test_unrecognized_locator_is_not_echoed_into_safe_export():
    snapshot = basic()
    result = evaluate_candidate(snapshot, snapshot.token_ids[0], "synthetic-secret-input")
    assert result.outcome == "unresolved"
    assert "synthetic-secret-input" not in json.dumps(result.safe_dict())


def test_invalid_numeric_neighbor_is_not_removed_to_create_uniqueness():
    snapshot = observe_tokens((token("基本給"), token("1,234", y=40),
                               token("123", x=25, y=42)), local_key=KEY)
    result = record(snapshot, frame=verified(snapshot), boundaries=boundaries(snapshot))
    assert dict(result.gates)["geometry"].reason_code == "multiple_candidates"
    assert result.outcome == "reject"
