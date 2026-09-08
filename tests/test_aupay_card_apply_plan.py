import pytest

from app.aupay_card_apply_plan import (
    CanonicalApplyPlan,
    build_canonical_apply_plan,
    require_executable_apply_plan,
)
from app.transaction_plan import normalize_card_transaction, reconcile_transactions


def mail_id(char, number=1):
    return f"aupaycard-mail:{char * 24}:{number:03d}"


def row(identity, merchant="店", amount=1000, date="2026-08-08", number=1):
    return {
        "import_id": identity,
        "date": date,
        "merchant": merchant,
        "amount": amount,
        "payment_type": "メール通知",
        "member": "本会員",
        "memo": f"メール明細No.{number:03d}",
    }


def collection(**overrides):
    value = {
        "collection_complete": True,
        "listing_complete": True,
        "collection_truncated": False,
        "gmail_list_failed": 0,
        "gmail_read_failed": 0,
        "needs_review": 0,
    }
    value.update(overrides)
    return value


def plan(rows, existing=None, collection_summary=None):
    reconciled = reconcile_transactions(rows, existing or [])
    return build_canonical_apply_plan(collection_summary or collection(), reconciled)


def csv_row(identity="aupaycard:csv1", merchant="店", amount=1000, date="2026-08-08"):
    return [identity, "", "au PAYカード", "", date, merchant, amount, "一括"]


def test_raw_and_reconciled_objects_are_not_executor_authority():
    raw = normalize_card_transaction(row(mail_id("a")))
    reconciled = reconcile_transactions([row(mail_id("a"))])["transactions"][0]

    with pytest.raises(TypeError, match="canonical_apply_plan_required"):
        require_executable_apply_plan(raw, apply=True)
    with pytest.raises(TypeError, match="canonical_apply_plan_required"):
        require_executable_apply_plan(reconciled, apply=True)
    with pytest.raises(TypeError):
        CanonicalApplyPlan()


def test_noncanonical_resend_is_not_projected_and_pair_yields_one_candidate():
    result = plan([row(mail_id("b")), row(mail_id("a"))])

    assert result.status == "ready"
    assert len(result.candidates) == 1
    assert result.candidates[0].identity == mail_id("a")
    assert result.candidates[0].source_identities == (mail_id("a"), mail_id("b"))
    assert result.noncanonical_resend_count == 1
    assert len(result.candidates[0].to_import_row()) == 12
    assert result.candidates[0].to_import_row()[10] == result.candidates[0].business_fingerprint


@pytest.mark.parametrize("collection_summary, reason", [
    (collection(collection_complete=False), "collection_incomplete"),
    (collection(collection_complete=False, gmail_read_failed=1), "gmail_read_failure"),
    (collection(collection_complete=False, collection_truncated=True), "collection_truncated"),
])
def test_collection_failures_block_all_candidates(collection_summary, reason):
    result = plan([row(mail_id("a"))], collection_summary=collection_summary)

    assert result.status == "blocked"
    assert result.candidates == ()
    assert reason in result.blocked_reasons


def test_parser_review_blocks_plan_without_silent_partial_apply():
    result = plan(
        [row(mail_id("a"))],
        collection_summary=collection(needs_review=8),
    )

    assert result.parser_review_count == 8
    assert result.candidates == ()
    assert "parser_review_present" in result.blocked_reasons


def test_identity_collision_blocks_plan():
    result = plan([
        row(mail_id("a"), merchant="A"),
        row(mail_id("a"), merchant="B"),
    ])

    assert result.identity_collision_count == 1
    assert result.candidates == ()
    assert "identity_collision" in result.blocked_reasons


def test_rejected_required_field_blocks_plan():
    bad = row(mail_id("a"))
    bad["amount"] = 0
    result = plan([row(mail_id("b")), bad])

    assert result.rejected_transaction_count == 1
    assert result.candidates == ()
    assert "reconciliation_rejected_transaction" in result.blocked_reasons


def test_cross_source_ambiguous_blocks_and_is_never_a_candidate():
    result = plan(
        [row(mail_id("a"))],
        [csv_row("aupaycard:1"), csv_row("aupaycard:2")],
    )

    assert result.withheld_ambiguous_count == 1
    assert result.eligible_canonical_count == 0
    assert result.candidates == ()
    assert "cross_source_ambiguous" in result.blocked_reasons


def test_cross_source_strong_match_remains_candidate_and_evidence_only():
    result = plan([row(mail_id("a"))], [csv_row()])

    assert result.status == "ready"
    assert len(result.candidates) == 1
    assert result.candidates[0].cross_source_state == "cross_source_strong_match"
    assert result.candidates[0].cross_source_candidate_identities == ("aupaycard:csv1",)


def test_cross_source_no_match_is_normal_candidate():
    result = plan([row(mail_id("a"))])

    assert result.status == "ready"
    assert len(result.candidates) == 1
    assert result.candidates[0].cross_source_state == "cross_source_no_match"


def test_exact_existing_sheet_identity_is_authoritative_duplicate():
    identity = mail_id("a")
    existing = [[identity, "", "au PAYカード", identity, "2026-08-08", "店", 1000]]
    result = plan([row(identity)], existing)

    assert result.status == "ready"
    assert result.existing_identity_duplicate_count == 1
    assert result.eligible_canonical_count == 0
    assert result.candidates == ()


def test_plan_is_deterministic_across_input_order():
    rows = [
        row(mail_id("b")),
        row(mail_id("a")),
        row(mail_id("c", 2), merchant="別店舗", number=2),
    ]

    assert plan(rows) == plan(list(reversed(rows)))


def test_same_day_amount_merchant_different_detail_number_stays_two_candidates():
    result = plan([
        row(mail_id("a", 1), number=1),
        row(mail_id("b", 2), number=2),
    ])

    assert result.status == "ready"
    assert len(result.candidates) == 2
    assert {candidate.identity for candidate in result.candidates} == {
        mail_id("a", 1), mail_id("b", 2),
    }


def test_executor_requires_explicit_apply_even_for_ready_safe_plan():
    result = plan([row(mail_id("a"))])

    assert isinstance(result, CanonicalApplyPlan)
    with pytest.raises(RuntimeError, match="explicit_apply_required"):
        require_executable_apply_plan(result, apply=False)
    assert require_executable_apply_plan(result, apply=True) is result
