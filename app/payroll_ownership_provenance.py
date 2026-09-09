"""Diagnostic-only physical-token ownership provenance.

This module observes the unchanged parser.  It is deliberately not imported by
production code and never produces an adoption decision.  In particular, a
token missing from reconstructed consumption is *not* unused unless the whole
success set and every consumption mapping are complete.
"""
from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json

from .payroll_parser import (
    _diagnostic_candidate_observer, _ocr_dot_amounts,
    _ocr_parser_labels_with_components, amounts, compact, parse_positioned_items,
)


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


@dataclass(frozen=True)
class ParserObservationTrace:
    """Snapshot-local trace for one physical occurrence; never authoritative."""
    token_id: str = field(repr=False)
    disposition: str
    typed_role: str | None
    evidence_closed: bool
    requires_semantic_ground_truth: bool
    reason_code: str


@dataclass(frozen=True)
class SuccessfulMappingTrace:
    """Diagnostic closure for one successful parser fact."""
    item_occurrence: int
    logical_field: str | None
    label_component_ids: tuple[str, ...] = field(repr=False)
    value_token_id: str | None = field(repr=False)
    counterfactual_dependency_ids: tuple[str, ...] = field(repr=False)
    complete: bool
    reason_code: str


@dataclass(frozen=True)
class OwnershipInstrumentationDiagnostic:
    """Anonymous aggregate view over optional parser observation traces."""
    snapshot_id: str = field(repr=False)
    observations: tuple[ParserObservationTrace, ...] = field(repr=False)
    successful_mappings: tuple[SuccessfulMappingTrace, ...] = field(repr=False)

    @property
    def evidence_closed_count(self):
        return sum(trace.evidence_closed for trace in self.observations)

    @property
    def instrumentation_incomplete_count(self):
        return sum(not trace.evidence_closed
                   and not trace.requires_semantic_ground_truth
                   for trace in self.observations)

    @property
    def semantic_ground_truth_required_count(self):
        return sum(trace.requires_semantic_ground_truth for trace in self.observations)

    @property
    def successful_mapping_complete_count(self):
        return sum(mapping.complete for mapping in self.successful_mappings)

    def safe_dict(self):
        dispositions = Counter(trace.disposition for trace in self.observations)
        roles = Counter(trace.typed_role for trace in self.observations
                        if trace.typed_role is not None)
        blockers = Counter(mapping.reason_code for mapping in self.successful_mappings
                           if not mapping.complete)
        return {
            "traced_token_count": len(self.observations),
            "evidence_closed_count": self.evidence_closed_count,
            "instrumentation_incomplete_count": self.instrumentation_incomplete_count,
            "semantic_ground_truth_required_count": self.semantic_ground_truth_required_count,
            "dispositions": dict(sorted(dispositions.items())),
            "typed_roles": dict(sorted(roles.items())),
            "successful_mapping_count": len(self.successful_mappings),
            "successful_mapping_complete_count": self.successful_mapping_complete_count,
            "successful_mapping_blockers": dict(sorted(blockers.items())),
        }


@dataclass(frozen=True)
class EnumeratedParserCandidate:
    """One parser-generated label/value relation with stable physical lineage."""
    candidate_id: str
    generator_path: str
    label_component_ids: tuple[str, ...] = field(repr=False)
    value_token_id: str | None = field(repr=False)
    active: bool = False
    selected: bool = False
    review_or_non_success: bool = False
    provenance_complete: bool = False


@dataclass(frozen=True)
class CandidateReductionRelation:
    """A diagnostic relation for a production dedup/pruning operation."""
    generator_path: str
    removed_candidate_id: str
    retained_candidate_id: str | None
    complete: bool


@dataclass(frozen=True)
class CandidateEnumerationLedger:
    """Snapshot-bound observation of the production candidate universe."""
    snapshot_id: str = field(repr=False)
    parser_mode: str
    token_count: int
    expected_paths: tuple[str, ...]
    observed_paths: tuple[str, ...]
    candidates: tuple[EnumeratedParserCandidate, ...] = field(repr=False)
    reductions: tuple[CandidateReductionRelation, ...] = field(repr=False)
    exclusion_counts: tuple[tuple[str, int], ...]
    input_scope_token_ids: tuple[str, ...] = field(repr=False)
    domain_excluded_token_ids: tuple[str, ...] = field(repr=False)
    eligible_label_count: int
    accounted_label_count: int
    scope_candidate_ids: tuple[str, ...] = field(repr=False)
    run_complete: bool
    result_signature: tuple = field(repr=False)

    @property
    def complete(self):
        candidate_ids = {candidate.candidate_id for candidate in self.candidates}
        return bool(
            self.run_complete
            and set(self.expected_paths) == set(self.observed_paths)
            and self.eligible_label_count == self.accounted_label_count
            and len(set(self.input_scope_token_ids)
                    | set(self.domain_excluded_token_ids)) == self.token_count
            and not (set(self.input_scope_token_ids)
                     & set(self.domain_excluded_token_ids))
            and candidate_ids == set(self.scope_candidate_ids)
            and all(candidate.provenance_complete for candidate in self.candidates)
            and all(relation.complete for relation in self.reductions)
        )

    def safe_dict(self):
        return {
            "parser_mode": self.parser_mode,
            "token_count": self.token_count,
            "candidate_count": len(self.candidates),
            "generator_path_coverage": {
                path: path in self.observed_paths for path in self.expected_paths
            },
            "exclusion_counts": dict(self.exclusion_counts),
            "eligible_label_count": self.eligible_label_count,
            "accounted_label_count": self.accounted_label_count,
            "input_token_scope_closed": (
                len(set(self.input_scope_token_ids)
                    | set(self.domain_excluded_token_ids)) == self.token_count
                and not (set(self.input_scope_token_ids)
                         & set(self.domain_excluded_token_ids))
            ),
            "preselection_closed": (
                {candidate.candidate_id for candidate in self.candidates}
                == set(self.scope_candidate_ids)
            ),
            "reduction_count": len(self.reductions),
            "reduction_accounting_complete": all(
                relation.complete for relation in self.reductions
            ),
            "physical_provenance_complete": all(
                candidate.provenance_complete for candidate in self.candidates
            ),
            "complete": self.complete,
        }


@dataclass(frozen=True)
class CandidateEnumerationVerification:
    complete: bool
    reason_code: str
    replay_deterministic: bool
    hidden_candidate_detected: bool


def _candidate_identity(snapshot_id, path, label_ids, value_id):
    payload = json.dumps(
        ("payroll-parser-candidate-v1", snapshot_id, path, label_ids, value_id),
        ensure_ascii=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _capture_candidate_enumeration(snapshot):
    """Observe the production generator; never recreate its candidate semantics."""
    events = []
    with _diagnostic_candidate_observer(
        lambda event, payload: events.append((event, payload))
    ):
        result = tuple(parse_positioned_items(
            snapshot.tokens,
            ocr=getattr(snapshot, "parser_mode", "pdf") == "ocr",
        ))

    token_ids_by_object = {
        id(token): token_id
        for token_id, token in zip(snapshot.token_ids, snapshot.tokens)
    }
    label_components = {}
    if getattr(snapshot, "parser_mode", "pdf") == "ocr":
        for label, indexes in _ocr_parser_labels_with_components(snapshot.tokens):
            key = (label.page, label.x, label.y, label.width, compact(label.text))
            label_components.setdefault(key, set()).add(tuple(
                snapshot.token_ids[index] for index in indexes
                if 0 <= index < len(snapshot.token_ids)
            ))
    else:
        for token_id, token in zip(snapshot.token_ids, snapshot.tokens):
            key = (token.page, token.x, token.y, token.width, compact(token.text))
            label_components.setdefault(key, set()).add((token_id,))

    def label_ids(label):
        key = (label.page, label.x, label.y, label.width, compact(label.text))
        options = {parts for parts in label_components.get(key, set()) if parts}
        return next(iter(options)) if len(options) == 1 else ()

    def make_candidate(path, label, number, *, active=False, selected=False,
                       review=False):
        components = tuple(label_ids(label))
        value_id = token_ids_by_object.get(id(number)) if number is not None else None
        identifier = _candidate_identity(
            snapshot.snapshot_id, path, components, value_id,
        )
        complete = bool(
            components and value_id
            and value_id not in snapshot.identity_ambiguous
            and not any(part in snapshot.identity_ambiguous for part in components)
        )
        return EnumeratedParserCandidate(
            identifier, path, components, value_id, active, selected, review, complete,
        )

    start = next((payload for event, payload in events if event == "run_start"), {})
    expected_paths = tuple(start.get("expected_paths", ()))
    observed_paths = {
        payload["generator_path"]
        for event, payload in events
        if event in {"path_complete", "logical_path_complete", "summary_complete",
                     "dedup_complete"}
        and payload.get("generator_path")
    }
    excluded_labels = [payload for event, payload in events if event == "label_excluded"]
    scopes = [payload for event, payload in events if event == "candidate_scope"]
    pool = next((payload for event, payload in events if event == "label_pool"), {})
    eligible_label_count = len(tuple(pool.get("labels", ()))) - len(excluded_labels)
    exclusions = Counter(payload.get("reason", "label_excluded")
                         for payload in excluded_labels)
    input_scope_token_ids = set()
    for label in tuple(pool.get("labels", ())):
        input_scope_token_ids.update(label_ids(label))
    for key in ("comma_money", "ocr_dot_money"):
        for number, _values in start.get(key, ()):
            value_id = token_ids_by_object.get(id(number))
            if value_id:
                input_scope_token_ids.add(value_id)
    candidates_by_id = {}
    scope_candidate_ids = set()
    selected_by_item = {}

    selection_by_label = {
        id(payload["label"]): payload
        for event, payload in events if event == "selection_complete"
    }
    for scope in scopes:
        label = scope["label"]
        active_path = scope.get("active_path")
        chosen = scope.get("chosen")
        review = bool(selection_by_label.get(id(label), {}).get("review", True))
        path_numbers = set()
        for path, entries in scope.get("paths", {}).items():
            for number, _values, _distance in entries:
                path_numbers.add(id(number))
                selected = bool(path == active_path and chosen is not None
                                and number is chosen[0])
                candidate = make_candidate(
                    path, label, number, active=path == active_path,
                    selected=selected, review=review,
                )
                candidates_by_id[candidate.candidate_id] = candidate
                scope_candidate_ids.add(candidate.candidate_id)
        accepted_horizontal = {
            id(entry[0]) for entry in scope.get("paths", {}).get("horizontal", ())
        }
        # The production ownership guard runs after horizontal geometry has
        # generated a relation. Keep rejected relations in the pre-selection
        # universe instead of reducing them to token-level exclusion only.
        for number, _values, _distance in scope.get("horizontal_in_range", ()):
            if id(number) in accepted_horizontal:
                continue
            candidate = make_candidate(
                "horizontal", label, number, review=True,
            )
            candidates_by_id[candidate.candidate_id] = candidate
            scope_candidate_ids.add(candidate.candidate_id)
        raw_horizontal = {id(entry[0]) for entry in scope.get("horizontal_in_range", ())}
        for number, _values in scope.get("same_page", ()):
            value_id = token_ids_by_object.get(id(number))
            if value_id:
                input_scope_token_ids.add(value_id)
            if id(number) in path_numbers:
                continue
            reason = ("horizontal_ownership_rejected"
                      if id(number) in raw_horizontal else "outside_pairing_geometry")
            exclusions[reason] += 1

    # Logical-row generation records candidates before its uniqueness pruning.
    for event, payload in events:
        if event != "logical_path_complete":
            continue
        accepted_numbers = {id(number) for _key, number in payload.get("accepted", ())}
        for label, number in payload.get("generated", ()):
            candidate = make_candidate(
                "pdf_logical_row", label, number,
                active=id(number) in accepted_numbers,
            )
            existing = candidates_by_id.get(candidate.candidate_id)
            if existing is None:
                candidates_by_id[candidate.candidate_id] = candidate
            scope_candidate_ids.add(candidate.candidate_id)
            if id(number) not in accepted_numbers:
                exclusions["logical_uniqueness_pruned"] += 1

    # Summary recovery is a distinct post-primary production generator path.
    for event, payload in events:
        if event != "summary_complete":
            continue
        proposed = {(id(entry[1]), id(number))
                    for entry, number, _value in payload.get("proposals", ())}
        accepted = {(id(entry[1]), id(number))
                    for entry, number, _value in payload.get("accepted", ())}
        for entry, matches, rejected in payload.get("scopes", ()):
            label = entry[1]
            for number, _value in matches:
                pair = (id(label), id(number))
                candidate = make_candidate(
                    "pdf_summary_below", label, number,
                    active=pair in proposed, selected=pair in accepted,
                    review=pair not in accepted,
                )
                candidates_by_id[candidate.candidate_id] = candidate
                scope_candidate_ids.add(candidate.candidate_id)
            for number, _value, reason in rejected:
                candidate = make_candidate(
                    "pdf_summary_below", label, number, review=True,
                )
                candidates_by_id[candidate.candidate_id] = candidate
                scope_candidate_ids.add(candidate.candidate_id)
                exclusions[reason] += 1

    # Bind selected parser items to the candidate that produced them, then retain
    # an explicit removed->retained relation through production deduplication.
    for event, payload in events:
        if event != "selection_complete" or payload.get("chosen") is None:
            continue
        chosen = payload["chosen"]
        path = payload.get("active_path")
        candidate = make_candidate(
            path, payload["label"], chosen[0], active=True, selected=True,
            review=bool(payload.get("review")),
        )
        selected_by_item[id(payload["item"])] = candidate.candidate_id
    for event, payload in events:
        if event == "summary_complete":
            for entry, number, _value in payload.get("accepted", ()):
                candidate = make_candidate(
                    "pdf_summary_below", entry[1], number,
                    active=True, selected=True, review=False,
                )
                selected_by_item[id(entry[0])] = candidate.candidate_id

    reductions = []
    for event, payload in events:
        if event != "dedup_complete":
            continue
        path = payload["generator_path"]
        before = tuple(payload.get("before", ()))
        after = tuple(payload.get("after", ()))
        before_items = tuple(entry[0] if isinstance(entry, tuple) else entry
                             for entry in before)
        after_items = tuple(entry[0] if isinstance(entry, tuple) else entry
                            for entry in after)
        retained_objects = {id(item) for item in after_items}
        for removed in (item for item in before_items if id(item) not in retained_objects):
            removed_id = selected_by_item.get(id(removed))
            retained = next((item for item in after_items
                             if compact(item.raw_item_name) == compact(removed.raw_item_name)
                             and item.raw_value == removed.raw_value), None)
            retained_id = selected_by_item.get(id(retained)) if retained is not None else None
            reductions.append(CandidateReductionRelation(
                path, removed_id or "unresolved", retained_id,
                bool(removed_id and retained_id),
            ))

    run_complete = any(event == "run_complete" for event, _payload in events)
    domain_excluded_token_ids = set(snapshot.token_ids) - input_scope_token_ids
    if domain_excluded_token_ids:
        exclusions["outside_label_value_candidate_grammars"] += len(
            domain_excluded_token_ids
        )
    signature = tuple(_parser_item_signature(item) for item in result)
    return CandidateEnumerationLedger(
        snapshot.snapshot_id, getattr(snapshot, "parser_mode", "pdf"),
        len(snapshot.token_ids), tuple(expected_paths), tuple(sorted(observed_paths)),
        tuple(sorted(candidates_by_id.values(), key=lambda item: item.candidate_id)),
        tuple(sorted(reductions, key=lambda item: (
            item.generator_path, item.removed_candidate_id,
        ))),
        tuple(sorted(exclusions.items())), tuple(sorted(input_scope_token_ids)),
        tuple(sorted(domain_excluded_token_ids)), eligible_label_count, len(scopes),
        tuple(sorted(scope_candidate_ids)), run_complete, signature,
    )


def capture_candidate_enumeration(snapshot):
    """Return a diagnostic-only ledger of the actual production candidate paths."""
    return _capture_candidate_enumeration(snapshot)


def verify_candidate_enumeration(snapshot, ledger):
    """Replay the parser and fail closed on stale, omitted, or hidden candidates."""
    parser_mode = getattr(snapshot, "parser_mode", "pdf")
    if (ledger.snapshot_id != snapshot.snapshot_id
            or ledger.token_count != len(snapshot.token_ids)
            or ledger.parser_mode != parser_mode):
        return CandidateEnumerationVerification(
            False, "candidate_snapshot_mismatch", False, False,
        )
    fresh = _capture_candidate_enumeration(snapshot)
    replay_same = (
        ledger.expected_paths == fresh.expected_paths
        and ledger.observed_paths == fresh.observed_paths
        and ledger.candidates == fresh.candidates
        and ledger.reductions == fresh.reductions
        and ledger.exclusion_counts == fresh.exclusion_counts
        and ledger.input_scope_token_ids == fresh.input_scope_token_ids
        and ledger.domain_excluded_token_ids == fresh.domain_excluded_token_ids
        and ledger.scope_candidate_ids == fresh.scope_candidate_ids
        and ledger.result_signature == fresh.result_signature
    )
    if not ledger.complete:
        return CandidateEnumerationVerification(
            False, "candidate_enumeration_ledger_incomplete", replay_same,
            set(fresh.scope_candidate_ids) - {
                candidate.candidate_id for candidate in ledger.candidates
            } != set(),
        )
    if not replay_same:
        hidden = bool(
            set(fresh.scope_candidate_ids)
            - {candidate.candidate_id for candidate in ledger.candidates}
        )
        return CandidateEnumerationVerification(
            False, "candidate_enumeration_replay_mismatch", False, hidden,
        )
    return CandidateEnumerationVerification(
        True, "candidate_enumeration_closed", True, False,
    )


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


def trace_incomplete_ownership(snapshot, provenance, claims=()):
    """Close diagnostic observation evidence without changing ownership states.

    Every result is scoped to one immutable physical-token snapshot.  The
    observer calls the unchanged production parser and records only roles that
    are demonstrated by parser input stages and per-occurrence counterfactuals.
    It never treats absence from parser output as unused or adoption-relevant.
    """
    claims = tuple(claims)
    assessments = assess_ownership(snapshot, provenance, claims)
    incomplete_ids = {
        token_id for token_id, result in assessments.items()
        if result.state == "ledger_incomplete"
    }
    if not incomplete_ids:
        return OwnershipInstrumentationDiagnostic(snapshot.snapshot_id, (), ())

    provenance_valid = (
        provenance.snapshot_id == snapshot.snapshot_id
        and provenance.token_count == len(snapshot.token_ids)
        and provenance.source_materialization_matches
        and provenance.page_scope_complete
        and provenance.success_set_complete
    )
    if not provenance_valid:
        traces = tuple(
            ParserObservationTrace(
                token_id, "untraced", None, False, False,
                "snapshot_or_replay_provenance_unverified",
            )
            for token_id in snapshot.token_ids if token_id in incomplete_ids
        )
        return OwnershipInstrumentationDiagnostic(snapshot.snapshot_id, traces, ())

    ocr_mode = getattr(snapshot, "parser_mode", "pdf") == "ocr"
    baseline = tuple(parse_positioned_items(snapshot.tokens, ocr=ocr_mode))
    baseline_all = Counter(_parser_item_signature(item) for item in baseline)
    baseline_success = Counter(
        _parser_item_signature(item) for item in baseline if not item.needs_review
    )
    baseline_review = Counter(
        _parser_item_signature(item) for item in baseline if item.needs_review
    )

    effects = {}
    for index, token_id in enumerate(snapshot.token_ids):
        replay = tuple(parse_positioned_items(
            snapshot.tokens[:index] + snapshot.tokens[index + 1:], ocr=ocr_mode,
        ))
        replay_all = Counter(_parser_item_signature(item) for item in replay)
        replay_success = Counter(
            _parser_item_signature(item) for item in replay if not item.needs_review
        )
        replay_review = Counter(
            _parser_item_signature(item) for item in replay if item.needs_review
        )
        lost_success = frozenset(
            signature for signature, count in baseline_success.items()
            if replay_success[signature] < count
        )
        effects[token_id] = (
            lost_success,
            any(replay_review[signature] < count
                for signature, count in baseline_review.items()),
            replay_all != baseline_all,
        )

    parser_observed = set()
    context_observed = set()
    label_parts_by_signature = {}
    if ocr_mode:
        labels_with_parts = _ocr_parser_labels_with_components(snapshot.tokens)
        for label, indexes in labels_with_parts:
            part_ids = tuple(
                snapshot.token_ids[index]
                for index in indexes
                if 0 <= index < len(snapshot.token_ids)
            )
            parser_observed.update(part_ids)
            key = (label.page, label.x, label.y, compact(label.text))
            label_parts_by_signature.setdefault(key, set()).add(part_ids)
        for token_id, token in zip(snapshot.token_ids, snapshot.tokens):
            if amounts(token.text) or _ocr_dot_amounts(token):
                parser_observed.add(token_id)
            if token.text.strip() and set(token.text.strip()) <= {"|", "｜"}:
                context_observed.add(token_id)
    else:
        for token_id, token in zip(snapshot.token_ids, snapshot.tokens):
            if amounts(token.text):
                parser_observed.add(token_id)
            else:
                parser_observed.add(token_id)
                label_parts_by_signature.setdefault(
                    (token.page, token.x, token.y, compact(token.text)), set(),
                ).add((token_id,))

    mappings = []
    roles_by_token = {}
    for occurrence, item in enumerate(baseline):
        if item.needs_review:
            continue
        signature = _parser_item_signature(item)
        label_key = (item.page, item.x, item.y, compact(item.raw_item_name))
        label_options = {
            parts for parts in label_parts_by_signature.get(label_key, set()) if parts
        }
        label_ids = next(iter(label_options)) if len(label_options) == 1 else ()
        dependency_ids = tuple(
            token_id for token_id in snapshot.token_ids
            if signature in effects[token_id][0]
        )
        value_ids = tuple(
            token_id for token_id, token in zip(snapshot.token_ids, snapshot.tokens)
            if (token_id in dependency_ids and item.raw_value is not None
                and token.page == item.page and token.text == item.raw_value)
        )
        value_id = value_ids[0] if len(value_ids) == 1 else None
        # An unmapped production success is still one snapshot-local logical
        # fact.  The occurrence reference closes physical provenance without
        # inventing a standard field or semantic ground truth.
        logical_field = (
            item.standard_item_candidate
            or f"snapshot_success_occurrence:{occurrence}"
        )
        complete = bool(
            len(label_options) == 1
            and value_id is not None
            and baseline_success[signature] == 1
            and all(token_id in dependency_ids for token_id in label_ids)
            and value_id in dependency_ids
            and not any(token_id in snapshot.identity_ambiguous
                        for token_id in (*label_ids, value_id))
        )
        if complete:
            reason = "successful_mapping_counterfactual_closed"
        elif len(label_options) != 1:
            reason = "logical_label_component_provenance_incomplete"
        elif value_id is None:
            reason = "successful_value_occurrence_incomplete"
        elif baseline_success[signature] != 1:
            reason = "successful_fact_occurrence_ambiguous"
        elif any(token_id in snapshot.identity_ambiguous
                 for token_id in (*label_ids, value_id)):
            reason = "successful_physical_identity_ambiguous"
        else:
            reason = "successful_counterfactual_dependency_incomplete"
        mapping = SuccessfulMappingTrace(
            occurrence, logical_field, tuple(label_ids), value_id,
            dependency_ids, complete, reason,
        )
        mappings.append(mapping)
        if complete:
            for token_id in label_ids:
                roles_by_token.setdefault(token_id, set()).add("label_component")
            roles_by_token.setdefault(value_id, set()).add("value")
            for token_id in dependency_ids:
                if token_id not in label_ids and token_id != value_id:
                    roles_by_token.setdefault(token_id, set()).add("decision_context")

    traces = []
    for token_id, token in zip(snapshot.token_ids, snapshot.tokens):
        if token_id not in incomplete_ids:
            continue
        lost_success, review_effect, any_effect = effects[token_id]
        roles = roles_by_token.get(token_id, set())
        if len(roles) == 1:
            role = next(iter(roles))
            traces.append(ParserObservationTrace(
                token_id, role, role, True, False,
                "successful_mapping_counterfactual_closed",
            ))
            continue
        if lost_success:
            traces.append(ParserObservationTrace(
                token_id, "untraced", None, False, False,
                "successful_dependency_mapping_incomplete",
            ))
            continue
        if review_effect or any_effect:
            traces.append(ParserObservationTrace(
                token_id, "ground_truth_required", None, False, True,
                "non_success_dependency_requires_authoritative_relevance",
            ))
            continue
        redundant_peer = any(
            other_id != token_id
            and other_id in parser_observed
            and other.page == token.page
            and compact(other.text) == compact(token.text)
            and abs(other.x - token.x) <= 12
            and abs(other.y - token.y) <= 12
            and abs(other.width - token.width) <= max(other.width, token.width) * .1
            and abs(other.height - token.height) <= max(other.height, token.height) * .1
            and not effects[other_id][2]
            for other_id, other in zip(snapshot.token_ids, snapshot.tokens)
        )
        if redundant_peer:
            traces.append(ParserObservationTrace(
                token_id, "redundant", "redundant", True, False,
                "parser_equivalent_peer_and_counterfactual_invariance",
            ))
        elif token_id in context_observed:
            traces.append(ParserObservationTrace(
                token_id, "observed", "decision_context", True, False,
                "explicit_structural_context_observation_no_decision_effect",
            ))
        elif token_id in parser_observed:
            traces.append(ParserObservationTrace(
                token_id, "observed", None, True, False,
                "parser_input_stage_observed_no_decision_effect",
            ))
        else:
            traces.append(ParserObservationTrace(
                token_id, "explicit_domain_exclusion", None, True, False,
                "excluded_by_parser_label_value_and_context_grammars",
            ))
    return OwnershipInstrumentationDiagnostic(
        snapshot.snapshot_id, tuple(traces), tuple(mappings),
    )
