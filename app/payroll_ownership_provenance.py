"""Diagnostic-only physical-token ownership provenance.

This module observes the unchanged parser.  It is deliberately not imported by
production code and never produces an adoption decision.  In particular, a
token missing from reconstructed consumption is *not* unused unless the whole
success set and every consumption mapping are complete.
"""
from collections import Counter
from dataclasses import dataclass

from .payroll_parser import parse_positioned_items


@dataclass(frozen=True)
class CandidateClaim:
    """One fallback label requesting one snapshot-scoped physical token."""
    label_id: str
    candidate_id: str


@dataclass(frozen=True)
class ConsumptionMapping:
    """A successful parser item and its proven consumed physical token, if any."""
    item_occurrence: int
    token_id: str | None
    reason_code: str


@dataclass(frozen=True)
class ConsumptionProvenance:
    snapshot_id: str
    token_count: int
    page_scope_complete: bool
    source_materialization_matches: bool
    success_set_complete: bool
    mappings: tuple[ConsumptionMapping, ...]

    @property
    def complete(self):
        return (self.page_scope_complete and self.source_materialization_matches
                and self.success_set_complete
                and all(mapping.token_id is not None for mapping in self.mappings))

    @property
    def consumed_token_ids(self):
        return frozenset(mapping.token_id for mapping in self.mappings if mapping.token_id)


@dataclass(frozen=True)
class OwnershipAssessment:
    state: str
    reason_code: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class IncompleteReasonAggregate:
    """Anonymous diagnostic count; never carries text, geometry, or locators."""
    reason_code: str
    count: int
    missing_evidence: str
    instrumentation_resolvable: bool
    requires_semantic_ground_truth: bool
    safe_resolution_if_closed: str


@dataclass(frozen=True)
class OwnershipIncompleteDiagnostic:
    ledger_incomplete_count: int
    mapping_blockers: tuple[tuple[str, int], ...]
    reasons: tuple[IncompleteReasonAggregate, ...]

    @property
    def instrumentation_resolvable_count(self):
        return sum(reason.count for reason in self.reasons
                   if reason.instrumentation_resolvable
                   and not reason.requires_semantic_ground_truth)

    @property
    def semantic_ground_truth_required_count(self):
        return sum(reason.count for reason in self.reasons
                   if reason.requires_semantic_ground_truth)

    def safe_dict(self):
        total = self.ledger_incomplete_count
        return {
            "ledger_incomplete_count": total,
            "mapping_blockers": dict(self.mapping_blockers),
            "reasons": [
                {
                    "reason_code": reason.reason_code,
                    "count": reason.count,
                    "percentage": round(reason.count * 100 / total, 2) if total else 0.0,
                    "missing_evidence": reason.missing_evidence,
                    "instrumentation_resolvable": reason.instrumentation_resolvable,
                    "requires_semantic_ground_truth": reason.requires_semantic_ground_truth,
                    "safe_resolution_if_closed": reason.safe_resolution_if_closed,
                }
                for reason in self.reasons
            ],
            "instrumentation_resolvable_count": self.instrumentation_resolvable_count,
            "semantic_ground_truth_required_count": self.semantic_ground_truth_required_count,
        }


_INCOMPLETE_REASON_CONTRACT = {
    "successful_value_counterfactual_present_but_success_ledger_unclosed": (
        "complete_physical_label_and_success_mapping_provenance",
        True, False, "eligible_for_used_decision_only_after_all_success_gates_close",
    ),
    "successful_decision_dependency_role_untyped": (
        "raw_token_to_logical_label_value_or_context_role_trace",
        True, False, "record_as_typed_parser_dependency_not_automatically_as_value_use",
    ),
    "non_success_decision_dependency_outside_consumption_ledger": (
        "adoption_relevance_and_candidate_field_relation_for_review_or_non_success_fact",
        False, True, "remain_outside_ownership_adoption_until_review_scope_is_authoritative",
    ),
    "no_decision_effect_observed_parser_observation_or_domain_exclusion_untraced": (
        "complete_parser_observation_or_explicit_domain_exclusion_trace",
        True, False, "eligible_for_exclusion_only_after_success_and_competition_closure",
    ),
    "snapshot_provenance_unverified": (
        "same_snapshot_source_materialization_and_token_scope",
        True, False, "rerun_against_one_verified_snapshot",
    ),
    "page_scope_incomplete": (
        "complete_source_page_scope",
        True, False, "rerun_after_complete_page_capture",
    ),
    "success_set_replay_unverified": (
        "complete_same_snapshot_production_success_replay",
        True, False, "rerun_after_success_set_equivalence_is_proven",
    ),
    "currently_unclassifiable": (
        "observer_reason_coverage",
        False, True, "remain_ledger_incomplete",
    ),
}


def _signature(fact):
    """Only parser-visible facts; raw text and coordinates never leave memory."""
    return (fact.page, fact.x, fact.y, fact.name, fact.raw_value, fact.needs_review,
            fact.reason, fact.mapped)


def reconstruct_consumption(snapshot, *, source_snapshot_id=None,
                            page_scope_complete=True, success_set_complete=True):
    """Prove consumption only through parser counterfactual replay.

    For a successful result, every same-page raw-text occurrence is temporarily
    removed in turn.  A token is consumed only when exactly one removal makes
    that *same production result* disappear.  This calls the existing parser;
    it does not recreate its pairing algorithm or introduce new authority.
    """
    source_matches = source_snapshot_id in (None, snapshot.snapshot_id)
    fact_type = type(snapshot.facts[0]) if snapshot.facts else None
    ocr_mode = getattr(snapshot, "parser_mode", "pdf") == "ocr"
    def replayed(tokens, fact):
        target = _signature(fact)
        for item in parse_positioned_items(tuple(tokens), ocr=ocr_mode):
            observed = type(fact)(item.page, item.x, item.y, item.raw_item_name, item.raw_value,
                                  item.needs_review, item.review_reason_code,
                                  item.standard_item_candidate is not None)
            if _signature(observed) == target:
                return observed
        return None
    replayed_facts = (() if fact_type is None else tuple(
        fact_type(item.page, item.x, item.y, item.raw_item_name, item.raw_value,
                  item.needs_review, item.review_reason_code,
                  item.standard_item_candidate is not None)
        for item in parse_positioned_items(snapshot.tokens, ocr=ocr_mode)))
    # Facts supplied from another parse/materialization are not a complete
    # success set merely because their fields happen to look plausible.
    facts_match = (len(snapshot.facts) == len(replayed_facts)
                   and all(_signature(left) == _signature(right)
                           for left, right in zip(snapshot.facts, replayed_facts)))
    mappings = []
    for occurrence, fact in enumerate(snapshot.facts):
        if fact.needs_review:
            continue
        if fact.raw_value is None:
            mappings.append(ConsumptionMapping(occurrence, None, "successful_item_raw_provenance_missing"))
            continue
        label_matches = [token_id for token_id, token in zip(snapshot.token_ids, snapshot.tokens)
                         if (token.page, token.x, token.y, token.text.strip())
                         == (fact.page, fact.x, fact.y, fact.name)]
        if len(label_matches) != 1 or label_matches[0] in snapshot.identity_ambiguous:
            mappings.append(ConsumptionMapping(occurrence, None, "successful_label_identity_ambiguous"))
            continue
        possible = [(index, token_id) for index, (token_id, token) in enumerate(zip(snapshot.token_ids, snapshot.tokens))
                    if token.page == fact.page and token.text == fact.raw_value]
        necessary = []
        for index, token_id in possible:
            replay_tokens = snapshot.tokens[:index] + snapshot.tokens[index + 1:]
            if replayed(replay_tokens, fact) is None:
                necessary.append(token_id)
        if len(necessary) == 1 and necessary[0] not in snapshot.identity_ambiguous:
            mappings.append(ConsumptionMapping(occurrence, necessary[0], "unique_parser_counterfactual"))
        elif not possible:
            mappings.append(ConsumptionMapping(occurrence, None, "successful_raw_token_absent_from_snapshot"))
        else:
            mappings.append(ConsumptionMapping(occurrence, None, "consumption_identity_not_unique"))
    return ConsumptionProvenance(snapshot.snapshot_id, len(snapshot.token_ids), page_scope_complete,
                                 source_matches, success_set_complete and facts_match, tuple(mappings))


def assess_ownership(snapshot, provenance, claims=()):
    """Return one conservative state per requested candidate physical token."""
    claims = tuple(claims)
    claims_by_token = {}
    for claim in claims:
        claims_by_token.setdefault(claim.candidate_id, []).append(claim)
    results = {}
    for token_id, token_claims in claims_by_token.items():
        if token_id not in snapshot.token_ids:
            result = OwnershipAssessment("ledger_incomplete", "candidate_not_in_snapshot")
        elif (provenance.snapshot_id != snapshot.snapshot_id
              or provenance.token_count != len(snapshot.token_ids)
              or not provenance.source_materialization_matches):
            result = OwnershipAssessment("ledger_incomplete", "snapshot_provenance_unknown")
        elif token_id in snapshot.identity_ambiguous:
            result = OwnershipAssessment("ownership_ambiguous", "physical_occurrence_ambiguous")
        elif token_id in provenance.consumed_token_ids:
            result = OwnershipAssessment("definitely_used", "unique_production_consumption",
                                         ("unique_parser_counterfactual",))
        elif not provenance.complete:
            result = OwnershipAssessment("ledger_incomplete", "consumption_provenance_incomplete")
        elif len({claim.label_id for claim in token_claims}) > 1:
            result = OwnershipAssessment("candidate_competed", "fallback_candidate_competition")
        else:
            result = OwnershipAssessment("definitely_unused", "complete_physical_exclusion",
                                         ("not_consumed_by_complete_success_set",))
        results[token_id] = result
    return results


def _parser_item_signature(item):
    return (item.page, item.x, item.y, item.raw_item_name, item.raw_value,
            item.needs_review, item.review_reason_code,
            item.standard_item_candidate is not None)


def _reason_aggregate(reason_code, count):
    missing, resolvable, ground_truth, resolution = _INCOMPLETE_REASON_CONTRACT[reason_code]
    return IncompleteReasonAggregate(
        reason_code, count, missing, resolvable, ground_truth, resolution,
    )


def diagnose_incomplete_ownership(snapshot, provenance, claims):
    """Explain ledger-incomplete tokens without promoting their ownership state.

    The observer replays the unchanged parser after removing one physical token.
    A changed result proves a decision dependency for this snapshot only. An
    unchanged result does *not* prove unused: it may mean filtering, redundancy,
    or an unobserved parser-domain exclusion. The returned object contains only
    fixed reason strings and aggregate counts.
    """
    claims = tuple(claims)
    assessments = assess_ownership(snapshot, provenance, claims)
    incomplete = [
        (index, token_id, token)
        for index, (token_id, token) in enumerate(zip(snapshot.token_ids, snapshot.tokens))
        if token_id in assessments and assessments[token_id].state == "ledger_incomplete"
    ]
    blocker_counts = Counter(
        mapping.reason_code for mapping in provenance.mappings
        if mapping.token_id is None
    )
    total = len(incomplete)
    if not total:
        return OwnershipIncompleteDiagnostic(0, tuple(sorted(blocker_counts.items())), ())

    global_reason = None
    if (provenance.snapshot_id != snapshot.snapshot_id
            or provenance.token_count != len(snapshot.token_ids)
            or not provenance.source_materialization_matches):
        global_reason = "snapshot_provenance_unverified"
    elif not provenance.page_scope_complete:
        global_reason = "page_scope_incomplete"
    elif not provenance.success_set_complete:
        global_reason = "success_set_replay_unverified"
    if global_reason:
        return OwnershipIncompleteDiagnostic(
            total, tuple(sorted(blocker_counts.items())),
            (_reason_aggregate(global_reason, total),),
        )

    ocr_mode = getattr(snapshot, "parser_mode", "pdf") == "ocr"
    baseline = parse_positioned_items(snapshot.tokens, ocr=ocr_mode)
    baseline_all = Counter(_parser_item_signature(item) for item in baseline)
    baseline_success = Counter(
        _parser_item_signature(item) for item in baseline if not item.needs_review
    )
    baseline_review = Counter(
        _parser_item_signature(item) for item in baseline if item.needs_review
    )
    unresolved_success = [
        snapshot.facts[mapping.item_occurrence]
        for mapping in provenance.mappings
        if mapping.token_id is None
        and 0 <= mapping.item_occurrence < len(snapshot.facts)
    ]
    reason_counts = Counter()
    for index, _token_id, token in incomplete:
        replay = parse_positioned_items(
            snapshot.tokens[:index] + snapshot.tokens[index + 1:], ocr=ocr_mode,
        )
        replay_all = Counter(_parser_item_signature(item) for item in replay)
        replay_success = Counter(
            _parser_item_signature(item) for item in replay if not item.needs_review
        )
        replay_review = Counter(
            _parser_item_signature(item) for item in replay if item.needs_review
        )
        success_effect = any(
            replay_success[signature] < count
            for signature, count in baseline_success.items()
        )
        review_effect = any(
            replay_review[signature] < count
            for signature, count in baseline_review.items()
        )
        raw_success_candidate = any(
            fact.raw_value is not None
            and token.page == fact.page
            and token.text == fact.raw_value
            for fact in unresolved_success
        )
        if raw_success_candidate and success_effect:
            reason = "successful_value_counterfactual_present_but_success_ledger_unclosed"
        elif success_effect:
            reason = "successful_decision_dependency_role_untyped"
        elif review_effect or replay_all != baseline_all:
            reason = "non_success_decision_dependency_outside_consumption_ledger"
        elif replay_all == baseline_all:
            reason = "no_decision_effect_observed_parser_observation_or_domain_exclusion_untraced"
        else:
            reason = "currently_unclassifiable"
        reason_counts[reason] += 1
    return OwnershipIncompleteDiagnostic(
        total,
        tuple(sorted(blocker_counts.items())),
        tuple(_reason_aggregate(reason, count)
              for reason, count in sorted(reason_counts.items())),
    )
