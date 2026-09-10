import pytest

from app.aupay_card_apply_plan import (
    CanonicalApplyCandidate,
    CanonicalApplyPlan,
    build_canonical_apply_plan,
    project_one_candidate_plan,
    require_executable_apply_plan,
)
from app.transaction_plan import normalize_card_transaction, reconcile_transactions


def mail_id(char, number=1):
    return f"aupaycard-mail:{char * 24}:{number:03d}"


def row(identity, merchant="店", amount=1000, date="2026-08-08", number=1,
        transaction_kind="purchase"):
    return {
        "import_id": identity,
        "date": date,
        "merchant": merchant,
        "amount": amount,
        "transaction_kind": transaction_kind,
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
    with pytest.raises(TypeError):
        CanonicalApplyCandidate()
    with pytest.raises(TypeError, match="projection_only"):
        CanonicalApplyCandidate._from_canonical(reconciled, authority=object())


def test_return_and_ambiguous_reconciled_items_cannot_bypass_plan_projection():
    returned = reconcile_transactions([
        row(mail_id("r"), amount=-1000, transaction_kind="return"),
    ])["transactions"][0]
    ambiguous = reconcile_transactions(
        [row(mail_id("a"))],
        [csv_row("aupaycard:1"), csv_row("aupaycard:2")],
    )["transactions"][0]

    with pytest.raises(TypeError, match="projection_only"):
        CanonicalApplyCandidate._from_canonical(returned, authority=object())
    with pytest.raises(TypeError, match="projection_only"):
        CanonicalApplyCandidate._from_canonical(ambiguous, authority=object())


def test_noncanonical_resend_is_not_projected_and_pair_yields_one_candidate():
    result = plan([row(mail_id("b")), row(mail_id("a"))])

    assert result.status == "ready"
    assert len(result.candidates) == 1
    assert result.candidates[0].identity == mail_id("a")
    assert result.candidates[0].source_identities == (mail_id("a"), mail_id("b"))
    assert result.noncanonical_resend_count == 1
    assert len(result.candidates[0].to_import_row()) == 12
    assert result.candidates[0].to_import_row()[10] == result.candidates[0].business_fingerprint


def test_public_projection_selects_exactly_one_without_mutating_full_plan():
    full_plan = plan([
        row(mail_id("a"), merchant="A"),
        row(mail_id("b"), merchant="B", amount=2000),
    ])
    before = full_plan.summary()

    projected = project_one_candidate_plan(full_plan, mail_id("b"))

    assert len(full_plan.candidates) == 2
    assert full_plan.summary() == before
    assert len(projected.candidates) == 1
    assert projected.candidates[0].identity == mail_id("b")
    assert projected.canonical_transaction_count == 1
    assert projected.canonical_accounting_valid


def test_public_projection_rejects_missing_and_withheld_identity():
    ready = plan([row(mail_id("a"))])
    withheld = plan([
        row(mail_id("r"), amount=-1000, transaction_kind="return"),
    ])

    with pytest.raises(RuntimeError, match="candidate_not_found"):
        project_one_candidate_plan(ready, mail_id("z"))
    with pytest.raises(RuntimeError, match="candidate_not_eligible"):
        project_one_candidate_plan(withheld, mail_id("r"))


@pytest.mark.parametrize("collection_summary, reason", [
    (collection(collection_complete=False), "collection_incomplete"),
    (collection(collection_complete=True, listing_complete=False), "gmail_listing_incomplete"),
    (collection(collection_complete=False, gmail_list_failed=1), "gmail_list_failure"),
    (collection(collection_complete=False, gmail_read_failed=1), "gmail_read_failure"),
    (collection(collection_complete=False, collection_truncated=True), "collection_truncated"),
])
def test_collection_failures_block_all_candidates(collection_summary, reason):
    result = plan([row(mail_id("a"))], collection_summary=collection_summary)

    assert result.status == "blocked"
    assert result.candidates == ()
    assert reason in result.blocked_reasons
    assert dict(result.item_status_counts) == {"withheld_global_failure": 1}
    assert result.global_withheld_count == 1


def test_parser_line_review_is_manifested_without_hiding_safe_canonical_item():
    result = plan(
        [row(mail_id("a"))],
        collection_summary=collection(
            needs_review=1,
            parser_review_mail_count=1,
            review_line_items=1,
        ),
    )

    assert result.status == "ready_with_withheld"
    assert result.parser_review_count == 1
    assert result.parser_review_line_item_count == 1
    assert len(result.candidates) == 1
    assert result.blocked_reasons == ()


def test_identity_collision_blocks_plan():
    result = plan([
        row(mail_id("a"), merchant="A"),
        row(mail_id("a"), merchant="B"),
    ])

    assert result.identity_collision_count == 1
    assert result.candidates == ()
    assert "identity_collision" in result.blocked_reasons


def test_reconciliation_invariant_failure_blocks_all_candidates():
    reconciled = reconcile_transactions([row(mail_id("a"))])
    reconciled["summary"]["canonical_transactions"] = 2

    result = build_canonical_apply_plan(collection(), reconciled)

    assert result.status == "blocked"
    assert result.candidates == ()
    assert "reconciliation_inconsistent" in result.blocked_reasons
    assert result.global_withheld_count == 1


def test_rejected_required_field_blocks_plan():
    bad = row(mail_id("a"))
    bad["amount"] = 0
    result = plan([row(mail_id("b")), bad])

    assert result.rejected_transaction_count == 1
    assert result.candidates == ()
    assert "reconciliation_rejected_transaction" in result.blocked_reasons


def test_cross_source_ambiguous_is_withheld_and_is_never_a_candidate():
    result = plan(
        [row(mail_id("a"))],
        [csv_row("aupaycard:1"), csv_row("aupaycard:2")],
    )

    assert result.withheld_ambiguous_count == 1
    assert result.eligible_canonical_count == 0
    assert result.status == "ready_with_withheld"
    assert result.candidates == ()
    assert result.blocked_reasons == ()
    assert dict(result.item_status_counts) == {"withheld_cross_source_ambiguous": 1}
    assert dict(result.item_withheld_reason_counts) == {"cross_source_ambiguous": 1}


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

    assert result.status == "ready_with_withheld"
    assert result.existing_identity_duplicate_count == 1
    assert result.duplicate_existing_identity_count == 1
    assert result.eligible_canonical_count == 0
    assert result.candidates == ()
    assert dict(result.item_status_counts) == {"duplicate_existing_identity": 1}


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


def test_return_is_withheld_without_becoming_apply_candidate():
    result = plan([
        row(mail_id("a"), amount=-1000, transaction_kind="return"),
    ])

    assert result.status == "ready_with_withheld"
    assert result.return_transaction_count == 1
    assert result.withheld_return_count == 1
    assert result.eligible_canonical_count == 0
    assert result.candidates == ()
    assert result.blocked_reasons == ()
    assert dict(result.item_status_counts) == {"withheld_return": 1}
    assert dict(result.item_withheld_reason_counts) == {
        "return_requires_accounting_policy": 1,
    }


def test_mixed_plan_projects_safe_purchase_and_manifests_withheld_items():
    rows = [
        row(mail_id("a"), merchant="安全な購入", amount=1200),
        row(mail_id("b"), merchant="返品", amount=-500, transaction_kind="return"),
        row(mail_id("c"), merchant="曖昧", amount=900),
        row(mail_id("d"), merchant="登録済み", amount=700),
    ]
    existing = [
        csv_row("aupaycard:csv-a", merchant="別候補A", amount=900),
        csv_row("aupaycard:csv-b", merchant="別候補B", amount=900),
        [mail_id("d"), "", "au PAYカード", mail_id("d"),
         "2026-08-08", "登録済み", 700],
    ]

    result = plan(rows, existing)

    assert result.status == "ready_with_withheld"
    assert [candidate.identity for candidate in result.candidates] == [mail_id("a")]
    assert dict(result.item_status_counts) == {
        "duplicate_existing_identity": 1,
        "eligible": 1,
        "withheld_cross_source_ambiguous": 1,
        "withheld_return": 1,
    }
    assert result.canonical_accounting_valid
    assert (
        len(result.candidates)
        + result.withheld_return_count
        + result.withheld_ambiguous_count
        + result.duplicate_existing_identity_count
        + result.withheld_review_count
        + result.invalid_item_count
        + result.global_withheld_count
        == result.canonical_transaction_count
    )
