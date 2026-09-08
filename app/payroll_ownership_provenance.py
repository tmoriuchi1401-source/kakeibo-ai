"""Diagnostic-only physical-token ownership provenance.

This module observes the unchanged parser.  It is deliberately not imported by
production code and never produces an adoption decision.  In particular, a
token missing from reconstructed consumption is *not* unused unless the whole
success set and every consumption mapping are complete.
"""
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


def _signature(fact):
    """Only parser-visible facts; raw text and coordinates never leave memory."""
    return (fact.page, fact.x, fact.y, fact.name, fact.raw_value, fact.needs_review,
            fact.reason, fact.mapped)


def _replayed_fact(tokens, fact):
    """Look up the exact production-visible result after an observer replay."""
    target = _signature(fact)
    for item in parse_positioned_items(tuple(tokens), ocr=False):
        observed = type(fact)(item.page, item.x, item.y, item.raw_item_name, item.raw_value,
                              item.needs_review, item.review_reason_code,
                              item.standard_item_candidate is not None)
        if _signature(observed) == target:
            return observed
    return None


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
    replayed_facts = (() if fact_type is None else tuple(
        fact_type(item.page, item.x, item.y, item.raw_item_name, item.raw_value,
                  item.needs_review, item.review_reason_code,
                  item.standard_item_candidate is not None)
        for item in parse_positioned_items(snapshot.tokens, ocr=False)))
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
            if _replayed_fact(replay_tokens, fact) is None:
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
