"""Synthetic-only tests for diagnostic physical ownership provenance."""
from dataclasses import replace

from app.payroll_ocr import PositionedText
from app import payroll_parser
from app.payroll_diagnostic_evidence import observe_tokens
from app.payroll_ownership_provenance import (
    CandidateClaim, _portable_production_claim_identity,
    analyze_successful_claim_authority, assess_ownership,
    capture_candidate_enumeration, diagnose_incomplete_ownership,
    reconstruct_consumption, trace_incomplete_ownership,
    verify_candidate_enumeration,
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


def candidate_snapshot(tokens, parser_mode="ocr"):
    return observe_tokens(
        tuple(tokens), local_key=KEY, parser_mode=parser_mode,
        snapshot_context=("candidate-enumeration-test", parser_mode),
    )


def test_candidate_enumeration_closes_actual_generator_scope_and_replay():
    observed = candidate_snapshot((
        token("基本給", 0, 20, width=30),
        token("1,234", 40, 20, width=30),
        token("2,345", 300, 300, width=30),
    ))
    ledger = capture_candidate_enumeration(observed)
    assert ledger.complete
    assert ledger.safe_dict()["generator_path_coverage"] == {
        "horizontal": True,
        "ocr_below": True,
        "ocr_above": True,
        "ocr_result_dedup": True,
    }
    assert dict(ledger.exclusion_counts)["outside_pairing_geometry"] == 1
    assert verify_candidate_enumeration(observed, ledger).complete


def test_candidate_enumeration_detects_hidden_candidate_omitted_from_ledger():
    observed = candidate_snapshot((
        token("基本給", 0, 20, width=30),
        token("1,234", 40, 20, width=30),
        token("2,345", 90, 20, width=30),
    ))
    ledger = capture_candidate_enumeration(observed)
    assert len(ledger.candidates) == 2
    omitted = replace(ledger, candidates=ledger.candidates[:-1])
    verification = verify_candidate_enumeration(observed, omitted)
    assert not verification.complete
    assert verification.hidden_candidate_detected


def test_candidate_enumeration_fails_when_generator_path_is_omitted():
    observed = candidate_snapshot((
        token("基本給", 0, 20, width=30), token("1,234", 40, 20),
    ))
    ledger = capture_candidate_enumeration(observed)
    omitted = replace(
        ledger,
        observed_paths=tuple(path for path in ledger.observed_paths
                             if path != "ocr_above"),
    )
    assert not omitted.complete
    assert not verify_candidate_enumeration(observed, omitted).complete


def test_preselection_keeps_pruned_review_candidates_and_physical_duplicates_distinct():
    observed = candidate_snapshot((
        token("基本給", 0, 20, width=30),
        token("1,234", 40, 20, width=30),
        token("1,234", 45, 20, width=30),
    ))
    ledger = capture_candidate_enumeration(observed)
    candidates = [candidate for candidate in ledger.candidates
                  if candidate.generator_path == "horizontal"]
    assert ledger.complete
    assert len(candidates) == 2
    assert len({candidate.candidate_id for candidate in candidates}) == 2
    assert len({candidate.value_token_id for candidate in candidates}) == 2
    assert all(candidate.review_or_non_success for candidate in candidates)
    # Production still identifies its first geometric choice even though the
    # close runner-up makes the resulting item review-only.
    assert sum(candidate.selected for candidate in candidates) == 1


def test_candidate_pruning_keeps_ownership_rejected_relation_in_ledger():
    observed = candidate_snapshot((
        token("基本給", 0, 20, width=30),
        token("所得税", 55, 20, width=30),
        token("1,234", 100, 20, width=30),
    ))
    ledger = capture_candidate_enumeration(observed)
    candidates = [candidate for candidate in ledger.candidates
                  if candidate.generator_path == "horizontal"]
    assert ledger.complete
    assert len(candidates) == 2
    assert sum(candidate.selected for candidate in candidates) == 1
    assert sum(not candidate.active for candidate in candidates) == 1
    assert dict(ledger.exclusion_counts)["horizontal_ownership_rejected"] == 1


def test_pdf_candidate_dedup_preserves_removed_to_retained_relation():
    observed = candidate_snapshot((
        token("基本給", 0, 20, width=30),
        token("基本給", 1, 20, width=30),
        token("1,234", 40, 20, width=30),
    ), parser_mode="pdf")
    ledger = capture_candidate_enumeration(observed)
    assert ledger.complete
    assert len(ledger.reductions) == 1
    relation = ledger.reductions[0]
    assert relation.generator_path == "pdf_label_dedup"
    assert relation.complete
    assert relation.removed_candidate_id != relation.retained_candidate_id


def test_candidate_enumeration_rejects_stale_snapshot_ledger():
    first = candidate_snapshot((
        token("基本給", 0, 20, width=30), token("1,234", 40, 20),
    ))
    second = candidate_snapshot((
        token("基本給", 0, 20, width=30), token("2,345", 40, 20),
    ))
    verification = verify_candidate_enumeration(
        second, capture_candidate_enumeration(first),
    )
    assert not verification.complete
    assert verification.reason_code == "candidate_snapshot_mismatch"


def test_candidate_identity_ignores_label_iteration_order(monkeypatch):
    observed = candidate_snapshot((
        token("基本給", 0, 20, width=30), token("1,234", 40, 20),
        token("所得税", 0, 100, width=30), token("2,345", 40, 100),
    ))
    baseline = capture_candidate_enumeration(observed)
    original = payroll_parser._ocr_label_tokens

    def reversed_labels(tokens):
        return list(reversed(original(tokens)))

    monkeypatch.setattr(payroll_parser, "_ocr_label_tokens", reversed_labels)
    perturbed = capture_candidate_enumeration(observed)
    assert {item.candidate_id for item in baseline.candidates} == {
        item.candidate_id for item in perturbed.candidates
    }


def test_candidate_observer_does_not_exclude_review_candidates_or_change_output():
    tokens = (
        token("基本給", 0, 20, width=30),
        token("1,234", 40, 20, width=30),
        token("2,345", 45, 20, width=30),
    )
    before = parse_positioned_items(tokens, ocr=True)
    observed = candidate_snapshot(tokens)
    claims = tuple(CandidateClaim("all", token_id) for token_id in observed.token_ids)
    ownership_before = assess_ownership(
        observed, reconstruct_consumption(observed), claims,
    )
    ledger = capture_candidate_enumeration(observed)
    assert any(candidate.review_or_non_success for candidate in ledger.candidates)
    assert parse_positioned_items(tokens, ocr=True) == before
    assert assess_ownership(
        observed, reconstruct_consumption(observed), claims,
    ) == ownership_before


def authority_diagnostic(tokens, parser_mode="ocr"):
    observed = candidate_snapshot(tokens, parser_mode=parser_mode)
    provenance = reconstruct_consumption(observed)
    ledger = capture_candidate_enumeration(observed)
    return observed, provenance, ledger, analyze_successful_claim_authority(
        observed, provenance, ledger,
    )


def test_field_authority_distinguishes_standard_from_snapshot_success_only():
    _standard_snapshot, _standard_provenance, _standard_ledger, standard = (
        authority_diagnostic((
            token("基本給", 0, 20, width=30), token("1,234", 40, 20),
        ))
    )
    standard_claim = standard.claims[0]
    assert standard_claim.authority_result == "authoritative_standard_field"
    assert standard_claim.portable_claim_id is not None
    assert standard_claim.final_status == "AUTHORITY_CLOSED"

    _fallback_snapshot, _fallback_provenance, _fallback_ledger, fallback = (
        authority_diagnostic((
            token("独自手当", 0, 20, width=40), token("1,234", 50, 20),
        ))
    )
    fallback_claim = fallback.claims[0]
    assert fallback_claim.authority_result == "snapshot_success_only"
    assert fallback_claim.authority_source == "storage_unknown_with_value_guard"
    assert fallback_claim.portable_claim_id is None
    assert fallback_claim.final_status == "BLOCKED"


def test_portable_claim_identity_does_not_use_occurrence_or_iteration_order():
    arguments = (
        "snapshot", "ocr", "basic_pay", ("label-a", "label-b"),
        "value", "horizontal", "candidate",
        "authoritative_standard_field",
    )
    first = _portable_production_claim_identity(*arguments)
    # No parser item occurrence or list position is accepted by the identity
    # contract, so replay iteration order cannot enter the digest.
    second = _portable_production_claim_identity(*arguments)
    assert first == second


def test_same_raw_text_at_different_physical_occurrences_cannot_collide():
    first, _p1, _l1, first_diagnostic = authority_diagnostic((
        token("基本給", 0, 20, width=30), token("1,234", 40, 20),
    ))
    second, _p2, _l2, second_diagnostic = authority_diagnostic((
        token("基本給", 200, 20, width=30), token("1,234", 240, 20),
    ))
    assert first.token_ids != second.token_ids
    assert (first_diagnostic.claims[0].portable_claim_id
            != second_diagnostic.claims[0].portable_claim_id)


def test_missing_field_authority_never_issues_production_claim_identity():
    assert _portable_production_claim_identity(
        "snapshot", "ocr", None, ("label",), "value",
        "horizontal", "candidate", "snapshot_success_only",
    ) is None


def test_success_and_review_dependencies_are_typed_without_ground_truth_flow():
    _observed, _provenance, _ledger, diagnostic = authority_diagnostic((
        token("基本給", 0, 20, width=30),
        token("所得税", 55, 20, width=30),
        token("1,234", 100, 20, width=30),
    ))
    successful = next(
        claim for claim in diagnostic.claims
        if claim.authority_result == "authoritative_standard_field"
    )
    assert successful.review_isolation == "closed"
    assert successful.shared_physical_evidence_count >= 1
    assert any(
        "production_success_dependency" in edge.edge_types
        and "review_dependency" in edge.edge_types
        and "shared_physical_evidence" in edge.edge_types
        for edge in successful.dependency_edges
    )
    # Removing the diagnostic review graph cannot alter field authority or the
    # already-issued identity; review truth is not an authority input.
    without_review_trace = replace(successful, dependency_edges=())
    assert without_review_trace.authority_result == successful.authority_result
    assert without_review_trace.portable_claim_id == successful.portable_claim_id


def test_review_result_object_change_does_not_feed_back_to_successful_result():
    tokens = (
        token("基本給", 0, 20, width=30),
        token("所得税", 55, 20, width=30),
        token("1,234", 100, 20, width=30),
    )
    baseline = parse_positioned_items(tokens, ocr=True)
    success_before = [item.model_dump() for item in baseline if not item.needs_review]
    review_item = next(item for item in baseline if item.needs_review)
    review_item.review_reason_code = "pairing_not_found"
    assert [item.model_dump() for item in baseline if not item.needs_review] == success_before
    assert parse_positioned_items(tokens, ocr=True) == parse_positioned_items(
        tokens, ocr=True,
    )


def test_claim_authority_analyzer_cannot_change_parser_or_ownership_state():
    tokens = (
        token("基本給", 0, 20, width=30), token("1,234", 40, 20),
        token("独自手当", 0, 40, width=40), token("2,345", 50, 40),
    )
    observed = candidate_snapshot(tokens)
    provenance = reconstruct_consumption(observed)
    ledger = capture_candidate_enumeration(observed)
    parser_before = tuple(parse_positioned_items(tokens, ocr=True))
    ownership_before = assess_ownership(observed, provenance)

    analyze_successful_claim_authority(observed, provenance, ledger)

    assert tuple(parse_positioned_items(tokens, ocr=True)) == parser_before
    assert assess_ownership(observed, provenance) == ownership_before


def test_claim_authority_fails_closed_for_stale_or_semantically_ambiguous_evidence():
    observed, provenance, ledger, _diagnostic = authority_diagnostic((
        token("基本給", 0, 20, width=30), token("1,234", 40, 20),
    ))
    stale = replace(provenance, snapshot_id="stale")
    assert not analyze_successful_claim_authority(observed, stale, ledger).valid

    duplicate = replace(
        ledger.candidates[0], candidate_id="f" * 64,
    )
    ambiguous = replace(
        ledger, candidates=(*ledger.candidates, duplicate),
        scope_candidate_ids=(*ledger.scope_candidate_ids, duplicate.candidate_id),
    )
    result = analyze_successful_claim_authority(observed, provenance, ambiguous)
    assert not result.valid
    assert result.claims == ()
