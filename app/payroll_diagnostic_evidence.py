"""Opt-in evidence observer. No production caller, persistence, or adoption API.

Private snapshots must remain in memory. Export only CandidateRecord.safe_dict().
IDs are keyed, snapshot-scoped locators, never proof of semantic correctness.
"""
from collections import Counter
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
import secrets
import unicodedata

from .payroll_ocr import PositionedText
from .payroll_parser import amounts, parse_positioned_items
from .payroll_pdf_diagnostics import relation


@dataclass(frozen=True)
class Gate:
    status: str
    reason_code: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProductionFact:
    page: int | None
    x: float | None
    y: float | None
    name: str = field(repr=False)
    raw_value: str | None = field(repr=False)
    needs_review: bool
    reason: str | None
    mapped: bool


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    tokens: tuple[PositionedText, ...] = field(repr=False)
    token_ids: tuple[str, ...]
    facts: tuple[ProductionFact, ...] = field(repr=False)
    identity_ambiguous: frozenset[str]


def observe_tokens(tokens, *, local_key=None):
    """Run the unchanged PDF parser against exactly this immutable token snapshot.

    Explicitly reusing a private key allows repeat comparison of identical input.
    The default key is fresh; IDs are not intended to link independent sessions.
    Occurrence is combined with page, geometry, normalized text and full snapshot.
    """
    tokens = tuple(tokens)
    key = secrets.token_bytes(32) if local_key is None else local_key
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("local_key_too_short")
    def digest(payload):
        return hmac.new(key, json.dumps(payload, ensure_ascii=True, allow_nan=False,
                                       separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
    content = []
    occurrences = Counter()
    for token in tokens:
        if (not all(math.isfinite(v) for v in (token.x, token.y, token.width, token.height, token.confidence))
                or token.width <= 0 or token.height <= 0 or token.page < 1):
            raise ValueError("invalid_snapshot_geometry")
        occurrences[token.page] += 1
        content.append((token.page, occurrences[token.page], token.x, token.y,
                        token.width, token.height, unicodedata.normalize("NFC", token.text),
                        token.text, token.confidence))
    snapshot_id = digest(("payroll-pdf-evidence-v1", content))
    token_ids = tuple(digest((snapshot_id, record)) for record in content)
    physical_keys = [(t.page, t.x, t.y, t.width, t.height,
                      unicodedata.normalize("NFC", t.text)) for t in tokens]
    repeats = Counter(physical_keys)
    items = parse_positioned_items(tokens, ocr=False)
    facts = tuple(ProductionFact(i.page, i.x, i.y, i.raw_item_name, i.raw_value,
                                 i.needs_review, i.review_reason_code,
                                 i.standard_item_candidate is not None) for i in items)
    return Snapshot(snapshot_id, tokens, token_ids, facts,
                    frozenset(ref for ref, p in zip(token_ids, physical_keys) if repeats[p] > 1))


@dataclass(frozen=True)
class ConsumptionLedger:
    snapshot_id: str
    # Item occurrence is scoped by snapshot; never a global item identity.
    entries: tuple[tuple[int, tuple[str, ...]], ...]
    complete: bool

    def ownership(self, snapshot, token_id):
        if self.snapshot_id != snapshot.snapshot_id or token_id not in snapshot.token_ids:
            return Gate("unknown", "ledger_snapshot_mismatch")
        if token_id in snapshot.identity_ambiguous:
            return Gate("unknown", "physical_occurrence_ambiguous")
        if any(refs == (token_id,) for _, refs in self.entries):
            return Gate("fail", "token_already_used", ("unique_exact_production_raw_value",))
        if any(token_id in refs for _, refs in self.entries):
            return Gate("unknown", "same_text_ownership_ambiguous")
        if not self.complete:
            return Gate("unknown", "consumption_ledger_incomplete")
        return Gate("pass", "definitely_distinct_unused", ("all_successful_items_resolved",))


def consumption_ledger(snapshot):
    entries = []
    complete = True
    for occurrence, fact in enumerate(snapshot.facts):
        # Even a successful multi-value result with value=None may consume a token.
        if fact.needs_review:
            continue
        matches = tuple(ref for ref, t in zip(snapshot.token_ids, snapshot.tokens)
                        if fact.raw_value is not None and t.page == fact.page and t.text == fact.raw_value)
        entries.append((occurrence, matches))
        if len(matches) != 1 or any(ref in snapshot.identity_ambiguous for ref in matches):
            complete = False
    return ConsumptionLedger(snapshot.snapshot_id, tuple(entries), complete)


@dataclass(frozen=True)
class FrameEvidence:
    snapshot_id: str
    status: str  # verified / unsupported / unknown
    reason: str


def frame_evidence(snapshot, matrices, *, text_axes, page_rotations, user_units,
                   extraction_matches=False):
    """Visitor evidence must cover the exact snapshot, not merely contain a cm.

    Verified means common raw frame for this observer, not precise glyph bounds
    or invariance of production's absolute thresholds. Scaling needs calibration.
    """
    count = len(snapshot.tokens)
    unknown = lambda reason: FrameEvidence(snapshot.snapshot_id, "unknown", reason)
    if (not extraction_matches or count == 0 or len(matrices) != count or len(text_axes) != count
            or len(page_rotations) != count or len(user_units) != count):
        return unknown("frame_provenance_incomplete")
    for m, axes in zip(matrices, text_axes):
        if len(m) != 6 or len(axes) != 4 or not all(math.isfinite(v) for v in (*m, *axes)):
            return unknown("invalid_transform_metadata")
    if any(r != 0 for r in page_rotations) or any(u != 1 for u in user_units):
        return FrameEvidence(snapshot.snapshot_id, "unsupported", "page_transform_unsupported")
    if any(tuple(a) != (1, 0, 0, 1) for a in text_axes):
        return FrameEvidence(snapshot.snapshot_id, "unsupported", "text_transform_unsupported")
    if len({tuple(m) for m in matrices}) != 1:
        return FrameEvidence(snapshot.snapshot_id, "unsupported", "inconsistent_transform")
    a, b, c, d, _, _ = matrices[0]
    if b != 0 or c != 0 or a <= 0 or d <= 0:
        return FrameEvidence(snapshot.snapshot_id, "unsupported", "rotation_skew_or_reflection")
    if a != 1 or d != 1:
        return unknown("positive_scaling_requires_calibration")
    return FrameEvidence(snapshot.snapshot_id, "verified", "common_raw_identity_or_translation")


def inspect_pdf_frame(path, snapshot):
    """Local read-only pypdf visitor; no OCR or external service fallback.

    Validate against the existing extractor's raw tm/bbox convention. A mismatch
    does not repair coordinates; it invalidates this evidence.
    """
    from .payroll_coordinate_diagnostics import inspect_pdf_coordinate_frame
    normalized = inspect_pdf_coordinate_frame(path, snapshot.snapshot_id, snapshot.tokens)
    return FrameEvidence(snapshot.snapshot_id, normalized.status, normalized.reason)


@dataclass(frozen=True)
class Region:
    """One independently established column/section intersection, in raw frame."""
    page: int
    left: float
    top: float
    right: float
    bottom: float


@dataclass(frozen=True)
class BoundaryEvidence:
    snapshot_id: str
    regions: tuple[Region, ...]
    source: str  # synthetic_structure / operator_structure / observed_rules
    edge_uncertainty: float | None = None


def _contains(region, token, error=0):
    return (region.page == token.page and region.left <= token.x-error
            and token.x+token.width+error <= region.right
            and region.top <= token.y-error and token.y+token.height+error <= region.bottom)


def column_section_gate(snapshot, label, candidate, boundaries):
    if (boundaries is None or boundaries.snapshot_id != snapshot.snapshot_id
            or boundaries.source not in ("synthetic_structure", "operator_structure", "observed_rules")):
        return Gate("unknown", "column_section_unknown")
    for region in boundaries.regions:
        if (not all(math.isfinite(v) for v in (region.left, region.top, region.right, region.bottom))
                or region.left >= region.right or region.top >= region.bottom):
            return Gate("unknown", "invalid_boundaries")
    expected = [r for r in boundaries.regions if _contains(r, label)]
    if len(expected) != 1:
        return Gate("unknown", "column_section_ambiguous" if expected else "column_section_unknown")
    # The expected region was selected using the label only, never the candidate.
    region = expected[0]
    memberships = [r for r in boundaries.regions if _contains(r, candidate)]
    if len(memberships) > 1:
        return Gate("unknown", "candidate_column_ambiguous")
    if not _contains(region, candidate):
        return Gate("fail", "unexpected_column_or_section" if memberships else "column_boundary_crossed")
    error = boundaries.edge_uncertainty
    if error is None or not math.isfinite(error) or error < 0:
        return Gate("unknown", "bbox_uncertainty_unknown")
    if not _contains(region, label, error) or not _contains(region, candidate, error):
        return Gate("unknown", "boundary_sensitive")
    return Gate("pass", "known_column_section", ("label_selected_region", "bounded_bbox_uncertainty"))


def structural_column_section_gate(snapshot, label_id, candidate_id, coordinate_frame,
                                   structural_boundaries):
    """Evaluate only independently observed stroked-table regions.

    This is deliberately an observer bridge: it maps snapshot occurrences to the
    already-normalized diagnostic frame, then asks whether both bboxes have one
    robust, identical structural region.  It never creates regions from either
    token and has no production caller.
    """
    from .payroll_boundary_diagnostics import membership

    if coordinate_frame is None or structural_boundaries is None:
        return Gate("unknown", "column_section_unknown")
    if (coordinate_frame.snapshot_id != snapshot.snapshot_id
            or structural_boundaries.snapshot_id != snapshot.snapshot_id
            or structural_boundaries.coordinate_snapshot_id != snapshot.snapshot_id):
        return Gate("unknown", "coordinate_boundary_snapshot_mismatch")
    if coordinate_frame.status != "verified" or structural_boundaries.coordinate_status != "verified":
        return Gate("unknown", "coordinate_prerequisite_unverified")
    try:
        label_index = snapshot.token_ids.index(label_id)
        candidate_index = snapshot.token_ids.index(candidate_id)
        label = coordinate_frame.normalized_tokens[label_index]
        candidate = coordinate_frame.normalized_tokens[candidate_index]
    except (ValueError, IndexError):
        return Gate("unknown", "normalized_token_snapshot_mismatch")
    label_region = membership(label, structural_boundaries)
    candidate_region = membership(candidate, structural_boundaries)
    if label_region.status != "exactly_one":
        return Gate("unknown", "label_" + label_region.reason)
    if candidate_region.status == "boundary_crossing":
        return Gate("fail", "candidate_" + candidate_region.reason)
    if candidate_region.status != "exactly_one":
        return Gate("unknown", "candidate_" + candidate_region.reason)
    if label_region.region != candidate_region.region:
        return Gate("fail", "unexpected_column_or_section")
    return Gate("pass", "same_closed_structural_region",
                ("candidate_independent_stroked_boundary",))


@dataclass(frozen=True)
class OperatorConfirmation:
    """Explicit local annotation, never synthesized by geometry. Do not put in Git."""
    snapshot_id: str
    label_id: str
    candidate_id: str
    relation_and_role_confirmed: bool


@dataclass(frozen=True)
class CandidateRecord:
    snapshot_id: str
    candidate_id: str
    gates: tuple[tuple[str, Gate], ...]

    @property
    def outcome(self):
        statuses = [gate.status for _, gate in self.gates]
        return "reject" if "fail" in statuses else "unresolved" if "unknown" in statuses else "shadow_eligible"

    def safe_dict(self):
        return {"snapshot": self.snapshot_id, "candidate": self.candidate_id,
                "outcome": self.outcome,
                "gates": {name: {"status": gate.status, "reason_code": gate.reason_code,
                                 "evidence": list(gate.evidence)} for name, gate in self.gates}}


def evaluate_candidate(snapshot, label_id, candidate_id, *, frame=None, boundaries=None,
                       coordinate_frame=None, structural_boundaries=None, confirmation=None):
    """Evaluate evidence, never return an authoritative value or updated item.

    Geometry currently uses the existing observer window, not a newly approved
    production window. Semantic confirmation is required even if mapping exists.
    """
    refs = dict(zip(snapshot.token_ids, snapshot.tokens))
    if label_id not in refs or candidate_id not in refs:
        return CandidateRecord(snapshot.snapshot_id, "unresolved",
                               (("identity", Gate("unknown", "token_snapshot_mismatch")),))
    label, value = refs[label_id], refs[candidate_id]
    facts = [f for f in snapshot.facts if (f.page, f.x, f.y, f.name) ==
             (label.page, label.x, label.y, label.text.strip())]
    fact = facts[0] if len(facts) == 1 else None
    scope = Gate("pass", "pairing_not_found_target") if fact and fact.reason == "pairing_not_found" else Gate("fail", "fallback_not_applicable")
    identity = Gate("unknown", "physical_occurrence_ambiguous") if (
        label_id in snapshot.identity_ambiguous or candidate_id in snapshot.identity_ambiguous
    ) else Gate("pass", "snapshot_occurrence_identified")
    ledger = consumption_ledger(snapshot)
    ownership = ledger.ownership(snapshot, candidate_id)
    competing = []
    for f in snapshot.facts:
        if not f.needs_review:
            continue
        for ref, t in refs.items():
            if ((f.page, f.x, f.y, f.name) == (t.page, t.x, t.y, t.text.strip())
                    and t.page == value.page and relation(t, value).vertical_window):
                competing.append(ref)
    if ownership.status == "pass" and len(set(competing)) > 1:
        ownership = Gate("fail", "fallback_ownership_collision")
    coordinates = Gate("unknown", "coordinate_frame_unverified")
    if frame is not None and frame.snapshot_id == snapshot.snapshot_id:
        coordinates = Gate({"verified": "pass", "unsupported": "fail", "unknown": "unknown"}[frame.status], frame.reason)
    columns = column_section_gate(snapshot, label, value, boundaries)
    if structural_boundaries is not None:
        columns = structural_column_section_gate(snapshot, label_id, candidate_id,
                                                 coordinate_frame, structural_boundaries)
    import re
    candidates = [ref for ref, t in refs.items() if t.page == label.page
                  and (amounts(t.text) or re.fullmatch(r"\s*[+-]?\d+(?:[,.]\d+)*(?:円|時間|日)?\s*", t.text))
                  and relation(label, t).vertical_window]
    if label.page != value.page or not relation(label, value).vertical_window:
        geometry = Gate("fail", "geometry_outside_window")
    elif len(candidates) > 1:
        geometry = Gate("fail", "multiple_candidates")
    else:
        geometry = Gate("pass", "observer_window_only")
    # Existing grammar plus complete-token coverage; never repair or broaden it.
    numeric = Gate("pass", "existing_money_grammar_complete") if (
        len(amounts(value.text)) == 1 and re.fullmatch(r"\d{1,3}(?:,\d{3})+", value.text.strip())
    ) else Gate("fail", "numeric_policy_rejected")
    if coordinates.status != "pass":
        columns = Gate("unknown", "coordinate_prerequisite_unverified")
        geometry = Gate("unknown", "coordinate_prerequisite_unverified")
    semantic = Gate("unknown", "standard_item_unresolved" if not fact or not fact.mapped else "ground_truth_required")
    if fact and fact.mapped and confirmation is not None:
        if ((confirmation.snapshot_id, confirmation.label_id, confirmation.candidate_id) ==
                (snapshot.snapshot_id, label_id, candidate_id) and confirmation.relation_and_role_confirmed):
            semantic = Gate("pass", "operator_relation_and_role_confirmed")
    return CandidateRecord(snapshot.snapshot_id, candidate_id, (
        ("scope", scope), ("identity", identity), ("ownership", ownership),
        ("coordinates", coordinates), ("column_section", columns), ("geometry", geometry),
        ("numeric", numeric), ("semantic", semantic)))
