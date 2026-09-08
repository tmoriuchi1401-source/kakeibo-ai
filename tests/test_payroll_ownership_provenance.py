"""Synthetic-only tests for diagnostic physical ownership provenance."""
from dataclasses import replace

from app.payroll_ocr import PositionedText
from app.payroll_diagnostic_evidence import observe_tokens
from app.payroll_ownership_provenance import CandidateClaim, assess_ownership, reconstruct_consumption
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
