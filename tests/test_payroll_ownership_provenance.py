"""Synthetic-only tests for diagnostic physical ownership provenance."""
from dataclasses import replace

from app.payroll_ocr import PositionedText
from app import payroll_parser
from app.payroll_diagnostic_evidence import observe_tokens
from app.payroll_ownership_provenance import (
    CandidateClaim, assess_ownership, diagnose_incomplete_ownership,
    reconstruct_consumption, trace_incomplete_ownership,
)
from app.payroll_parser import parse_positioned_items

KEY = b"synthetic-local-evaluation-key-0000"


def token(text, x=20, y=20, width=30, height=10, page=1, confidence=100):
    return PositionedText(text, page, x, y, width, height, confidence)


def snapshot(tokens):
    return observe_tokens(tuple(tokens), local_key=KEY)


def test_unique_success_consumes_one_physical_token_and_excludes_another():
    observed = snapshot((token("基本給", -30, 40), token("1,234", 20, 40), token("2,345", 200, 200)))
    provenance = reconstruct_consumption(observed)
    assert provenance.complete
    claims = (CandidateClaim(observed.token_ids[0], observed.token_ids[1]),
              CandidateClaim(observed.token_ids[0], observed.token_ids[2]))
    result = assess_ownership(observed, provenance, claims)
    assert result[observed.token_ids[1]].state == "definitely_used"
    assert result[observed.token_ids[2]].state == "definitely_unused"


def test_duplicate_same_text_is_resolved_only_when_counterfactual_is_unique():
    observed = snapshot((token("基本給", -30, 40), token("1,234", 20, 40), token("1,234", 300, 300)))
    provenance = reconstruct_consumption(observed)
    # The distant occurrence cannot replace the parser result.
    assert provenance.complete
    result = assess_ownership(observed, provenance, (CandidateClaim(observed.token_ids[0], observed.token_ids[2]),))
    assert result[observed.token_ids[2]].state == "definitely_unused"


def test_duplicate_consumption_identity_stays_incomplete_not_unused():
    observed = snapshot((token("基本給", -30, 40), token("1,234", 20, 40), token("1,234", 25, 40)))
    # This represents a retained result whose same-text physical source was not
    # recorded.  A diagnostic observer must reject it as an incomplete ledger.
    observed = replace(observed, facts=(replace(observed.facts[0], needs_review=False,
                                                raw_value="1,234", reason=None),))
    provenance = reconstruct_consumption(observed)
    assert not provenance.complete
    result = assess_ownership(observed, provenance, (CandidateClaim(observed.token_ids[0], observed.token_ids[2]),))
    assert result[observed.token_ids[2]].state == "ledger_incomplete"


def test_indistinguishable_duplicate_and_stale_snapshot_remain_safe_unknowns():
    observed = snapshot((token("基本給", -30, 40), token("1,234", 20, 40), token("1,234", 20, 40)))
    provenance = reconstruct_consumption(observed, source_snapshot_id="stale")
    result = assess_ownership(observed, provenance, (CandidateClaim(observed.token_ids[0], observed.token_ids[1]),))
    assert result[observed.token_ids[1]].state == "ledger_incomplete"
    fresh = reconstruct_consumption(observed)
    assert assess_ownership(observed, fresh, (CandidateClaim(observed.token_ids[0], observed.token_ids[1]),))[observed.token_ids[1]].state == "ownership_ambiguous"


def test_same_normalized_value_different_original_text_and_confidence_do_not_merge():
    observed = snapshot((token("基本給", -30, 40), token("1,234", 20, 40, confidence=90), token("１，２３４", 300, 300, confidence=10)))
    assert observed.token_ids[1] != observed.token_ids[2]
    provenance = reconstruct_consumption(observed)
    result = assess_ownership(observed, provenance, (CandidateClaim(observed.token_ids[0], observed.token_ids[2]),))
    assert result[observed.token_ids[2]].state == "definitely_unused"


def test_missing_success_evidence_and_incomplete_scope_forbid_unused():
    observed = snapshot((token("基本給", -30, 40), token("1,234", 20, 40), token("2,345", 300, 300)))
    missing = replace(observed, facts=(replace(observed.facts[0], raw_value=None),))
    result = assess_ownership(missing, reconstruct_consumption(missing, page_scope_complete=False),
                              (CandidateClaim(missing.token_ids[0], missing.token_ids[2]),))
    assert result[missing.token_ids[2]].state == "ledger_incomplete"


def test_production_and_fallback_competition_are_distinguished_from_fallback_competition():
    observed = snapshot((token("基本給", -30, 40), token("1,234", 20, 40), token("2,345", 300, 300)))
    provenance = reconstruct_consumption(observed)
    used = assess_ownership(observed, provenance, (CandidateClaim(observed.token_ids[0], observed.token_ids[1]),))
    assert used[observed.token_ids[1]].state == "definitely_used"
    claims = (CandidateClaim(observed.token_ids[0], observed.token_ids[2]), CandidateClaim(observed.token_ids[1], observed.token_ids[2]))
    competed = assess_ownership(observed, provenance, claims)
    assert competed[observed.token_ids[2]].state == "candidate_competed"


def test_counterfactual_diagnostic_does_not_change_production_result():
    tokens = (token("基本給", -30, 40), token("1,234", 20, 40), token("2,345", 300, 300))
    before = parse_positioned_items(tokens)
    observed = snapshot(tokens)
    reconstruct_consumption(observed)
    assert parse_positioned_items(tokens) == before


def test_incomplete_reason_taxonomy_is_anonymous_and_does_not_promote_tokens():
    tokens = (
        token("基本", 0, 20, width=25, confidence=90),
        token("給", 28, 20, width=10, confidence=55),
        token("1,234", 45, 20, width=30, confidence=90),
        token("通勤手当", 0, 100, width=40, confidence=90),
        token("|", 200, 200, width=5, height=50, confidence=90),
    )
    observed = observe_tokens(tokens, local_key=KEY, parser_mode="ocr")
    before = parse_positioned_items(tokens, ocr=True)
    provenance = reconstruct_consumption(observed)
    claims = tuple(CandidateClaim("all", token_id) for token_id in observed.token_ids)
    original = assess_ownership(observed, provenance, claims)
    diagnostic = diagnose_incomplete_ownership(observed, provenance, claims)
    counts = {reason.reason_code: reason.count for reason in diagnostic.reasons}
    assert counts == {
        "no_decision_effect_observed_parser_observation_or_domain_exclusion_untraced": 1,
        "non_success_decision_dependency_outside_consumption_ledger": 1,
        "successful_decision_dependency_role_untyped": 2,
        "successful_value_counterfactual_present_but_success_ledger_unclosed": 1,
    }
    assert diagnostic.mapping_blockers == (("successful_label_identity_ambiguous", 1),)
    assert diagnostic.instrumentation_resolvable_count == 4
    assert diagnostic.semantic_ground_truth_required_count == 1
    assert all(value.state == "ledger_incomplete" for value in original.values())
    assert assess_ownership(observed, provenance, claims) == original
    assert parse_positioned_items(tokens, ocr=True) == before
    exported = str(diagnostic.safe_dict())
    assert "基本" not in exported and "通勤手当" not in exported and "1,234" not in exported


def test_incomplete_taxonomy_fails_closed_on_stale_snapshot():
    observed = snapshot((token("基本給", -30, 40), token("1,234", 20, 40)))
    provenance = reconstruct_consumption(observed, source_snapshot_id="stale")
    claims = tuple(CandidateClaim("all", token_id) for token_id in observed.token_ids)
    diagnostic = diagnose_incomplete_ownership(observed, provenance, claims)
    assert diagnostic.ledger_incomplete_count == 2
    assert [(reason.reason_code, reason.count) for reason in diagnostic.reasons] == [
        ("snapshot_provenance_unverified", 2),
    ]


def traced(tokens):
    observed = observe_tokens(tuple(tokens), local_key=KEY, parser_mode="ocr")
    provenance = reconstruct_consumption(observed)
    claims = tuple(CandidateClaim("all", token_id) for token_id in observed.token_ids)
    return observed, provenance, claims, trace_incomplete_ownership(observed, provenance, claims)


def test_trace_closes_multi_component_label_value_and_explicit_exclusion():
    observed, provenance, claims, diagnostic = traced((
        token("基本", 0, 20, width=25, confidence=90),
        token("給", 28, 20, width=10, confidence=55),
        token("1,234", 45, 20, width=30, confidence=90),
        token("123", 300, 300, width=30, confidence=90),
    ))
    assert not provenance.complete
    mapping = diagnostic.successful_mappings[0]
    assert mapping.complete
    assert mapping.logical_field == "basic_pay"
    assert mapping.label_component_ids == observed.token_ids[:2]
    assert mapping.value_token_id == observed.token_ids[2]
    assert set(mapping.counterfactual_dependency_ids) == set(observed.token_ids[:3])
    by_id = {trace.token_id: trace for trace in diagnostic.observations}
    assert [by_id[token_id].typed_role for token_id in observed.token_ids[:3]] == [
        "label_component", "label_component", "value",
    ]
    assert by_id[observed.token_ids[3]].disposition == "explicit_domain_exclusion"
    assert diagnostic.evidence_closed_count == 4
    exported = str(diagnostic.safe_dict())
    assert "基本" not in exported and "1,234" not in exported
    before = assess_ownership(observed, provenance, claims)
    trace_incomplete_ownership(observed, provenance, claims)
    assert assess_ownership(observed, provenance, claims) == before


def test_trace_closes_single_token_label_alongside_split_label():
    _observed, _provenance, _claims, diagnostic = traced((
        token("基本給", 0, 20, width=35), token("1,234", 45, 20),
        token("健康", 0, 100, width=25), token("保険", 28, 100, width=25),
        token("2,345", 60, 100),
    ))
    complete = [mapping for mapping in diagnostic.successful_mappings if mapping.complete]
    assert len(complete) == 2
    assert sorted(len(mapping.label_component_ids) for mapping in complete) == [1, 2]


def test_trace_types_successful_structural_decision_context():
    observed, _provenance, _claims, diagnostic = traced((
        token("基本給", 0, 20, width=30),
        token("通勤手当", 25, 26, width=10),
        token("|", 40, 15, width=3, height=25),
        token("1,234", 100, 23, width=30),
        token("健康", 0, 100, width=25), token("保険", 28, 100, width=25),
        token("2,345", 60, 100),
    ))
    context = next(trace for trace in diagnostic.observations
                   if trace.token_id == observed.token_ids[2])
    assert context.typed_role == "decision_context"
    assert context.evidence_closed


def test_trace_requires_observation_and_equivalent_peer_before_redundant():
    observed, _provenance, _claims, diagnostic = traced((
        token("氏名", 200, 200, width=40),
        token("氏名", 201, 210, width=40),
        token("氏名", 400, 400, width=40),
        token("基本", 0, 20, width=25, confidence=90),
        token("給", 28, 20, width=10, confidence=55),
        token("1,234", 45, 20, width=30, confidence=90),
    ))
    by_id = {trace.token_id: trace for trace in diagnostic.observations}
    assert by_id[observed.token_ids[0]].disposition == "redundant"
    assert by_id[observed.token_ids[1]].disposition == "redundant"
    assert by_id[observed.token_ids[2]].disposition == "observed"
    assert len(set(observed.token_ids[:3])) == 3


def test_trace_fails_closed_for_stale_snapshot_and_incomplete_provenance():
    observed = observe_tokens(
        (token("基本給", 0, 20), token("1,234", 40, 20)),
        local_key=KEY, parser_mode="ocr",
    )
    stale = reconstruct_consumption(observed, source_snapshot_id="stale")
    claims = tuple(CandidateClaim("all", token_id) for token_id in observed.token_ids)
    diagnostic = trace_incomplete_ownership(observed, stale, claims)
    assert diagnostic.evidence_closed_count == 0
    assert diagnostic.instrumentation_incomplete_count == 2
    incomplete = replace(reconstruct_consumption(observed), page_scope_complete=False)
    diagnostic = trace_incomplete_ownership(observed, incomplete, claims)
    assert diagnostic.successful_mapping_complete_count == 0
    assert diagnostic.evidence_closed_count == 0


def test_trace_keeps_review_dependencies_outside_adoption_relevance():
    observed, provenance, claims, diagnostic = traced((
        token("通勤手当", 0, 20, width=40),
        token("基本", 0, 100, width=25, confidence=90),
        token("給", 28, 100, width=10, confidence=55),
        token("1,234", 45, 100),
    ))
    trace = next(item for item in diagnostic.observations
                 if item.token_id == observed.token_ids[0])
    assert trace.disposition == "ground_truth_required"
    assert trace.requires_semantic_ground_truth
    assert not trace.evidence_closed
    before = assess_ownership(observed, provenance, claims)
    trace_incomplete_ownership(observed, provenance, claims)
    assert assess_ownership(observed, provenance, claims) == before


def test_instrumentation_does_not_change_production_parser_result():
    tokens = (
        token("基本", 0, 20, width=25, confidence=90),
        token("給", 28, 20, width=10, confidence=55),
        token("1,234", 45, 20, width=30, confidence=90),
    )
    before = parse_positioned_items(tokens, ocr=True)
    traced(tokens)
    assert parse_positioned_items(tokens, ocr=True) == before


def test_diagnostic_provenance_failure_is_isolated_from_production_parser(monkeypatch):
    tokens = (
        token("基本", 0, 20, width=25, confidence=90),
        token("給", 28, 20, width=10, confidence=55),
        token("1,234", 45, 20, width=30, confidence=90),
    )
    before = parse_positioned_items(tokens, ocr=True)

    def broken_provenance(_tokens):
        raise RuntimeError("diagnostic metadata unavailable")

    monkeypatch.setattr(
        payroll_parser, "_ocr_parser_labels_with_components", broken_provenance,
    )
    assert parse_positioned_items(tokens, ocr=True) == before
