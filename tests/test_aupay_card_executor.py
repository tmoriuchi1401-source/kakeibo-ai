from dataclasses import fields

import pytest

from app.aupay_card_apply_plan import (
    CanonicalApplyPlan,
    build_canonical_apply_plan,
)
from app.aupay_card_executor import (
    CandidateState,
    ExecutionSubset,
    ExistingCanonicalRecord,
    IdentityRead,
    PriorCandidateOutcome,
    SheetsCanonicalIdentityReader,
    execute_canonical_apply_plan,
    verify_post_write,
)
from app.transaction_plan import reconcile_transactions


AUDIT_KEY = b"test-only-audit-and-selection-key-32-bytes"


def mail_id(char, number=1):
    return f"aupaycard-mail:{char * 24}:{number:03d}"


def row(identity, *, merchant="店", amount=1000, number=1,
        transaction_kind="purchase"):
    return {
        "import_id": identity,
        "date": "2026-08-08",
        "merchant": merchant,
        "amount": amount,
        "transaction_kind": transaction_kind,
        "payment_type": "メール通知",
        "member": "本会員",
        "memo": f"メール明細No.{number:03d}",
    }


def collection(**overrides):
    result = {
        "collection_complete": True,
        "listing_complete": True,
        "collection_truncated": False,
        "gmail_list_failed": 0,
        "gmail_read_failed": 0,
        "needs_review": 0,
    }
    result.update(overrides)
    return result


def make_plan(rows, existing=None, collection_summary=None):
    reconciliation = reconcile_transactions(rows, existing or [])
    return build_canonical_apply_plan(
        collection_summary or collection(), reconciliation,
    )


def clone_plan(original, **changes):
    forged = object.__new__(CanonicalApplyPlan)
    for field in fields(CanonicalApplyPlan):
        object.__setattr__(
            forged, field.name, changes.get(field.name, getattr(original, field.name)),
        )
    return forged


class FakeReader:
    def __init__(self, reads=None, *, fail=False):
        self.reads = dict(reads or {})
        self.fail = fail
        self.read_count = 0
        self.external_write_count = 0

    def read_identities(self, identities):
        self.read_count += 1
        if self.fail:
            raise TimeoutError("synthetic read failure")
        return {
            identity: self.reads.get(identity, IdentityRead())
            for identity in identities
        }

    def append(self, *_args, **_kwargs):
        self.external_write_count += 1
        raise AssertionError("executor reached a writer")

    def update(self, *_args, **_kwargs):
        self.external_write_count += 1
        raise AssertionError("executor reached a writer")


def execute(value, reader=None, **kwargs):
    return execute_canonical_apply_plan(
        value, reader or FakeReader(), audit_key=AUDIT_KEY, **kwargs,
    )


def test_invalid_plan_type_and_schema_are_rejected():
    with pytest.raises(TypeError, match="canonical_apply_plan_required"):
        execute({"candidates": []})

    valid = make_plan([row(mail_id("a"))])
    with pytest.raises(RuntimeError, match="apply_plan_schema_invalid"):
        execute(clone_plan(valid, schema_version=999))


def test_forged_plan_and_candidate_authority_are_rejected():
    valid = make_plan([row(mail_id("a"))])
    with pytest.raises(TypeError, match="unauthorized_apply_plan"):
        execute(clone_plan(valid, _authority=object()))

    object.__setattr__(valid.candidates[0], "_authority", object())
    with pytest.raises(TypeError, match="unauthorized_apply_candidate"):
        execute(valid)


def test_global_blocked_and_accounting_invalid_plans_are_rejected():
    blocked = make_plan(
        [row(mail_id("a"))],
        collection_summary=collection(collection_complete=False),
    )
    with pytest.raises(RuntimeError, match="apply_plan_blocked"):
        execute(blocked)

    valid = make_plan([row(mail_id("b"))])
    with pytest.raises(RuntimeError, match="apply_plan_accounting_invalid"):
        execute(clone_plan(valid, canonical_accounting_valid=False))


def test_return_or_ambiguous_decision_cannot_be_mixed_into_candidates():
    returned = make_plan([
        row(mail_id("r"), amount=-1000, transaction_kind="return"),
    ])
    purchase = make_plan([row(mail_id("p"))])
    forged_return = clone_plan(
        returned,
        candidates=purchase.candidates,
        eligible_canonical_count=1,
    )
    with pytest.raises(RuntimeError, match="candidate_manifest_mismatch"):
        execute(forged_return)

    existing = [
        ["aupaycard:csv-a", "", "au PAYカード", "", "2026-08-08", "A", 1000],
        ["aupaycard:csv-b", "", "au PAYカード", "", "2026-08-08", "B", 1000],
    ]
    ambiguous = make_plan([row(mail_id("a"))], existing)
    forged_ambiguous = clone_plan(
        ambiguous,
        candidates=purchase.candidates,
        eligible_canonical_count=1,
    )
    with pytest.raises(RuntimeError, match="candidate_manifest_mismatch"):
        execute(forged_ambiguous)


def test_ready_with_withheld_executes_only_safe_purchase():
    result_plan = make_plan([
        row(mail_id("a")),
        row(mail_id("r"), amount=-500, transaction_kind="return"),
    ])

    result = execute(result_plan)

    assert result_plan.status == "ready_with_withheld"
    assert result.input_candidate_count == 1
    assert result.withheld_count == 1
    assert result.revalidated_new_count == 1
    assert result.would_write_count == 1


def test_exact_existing_identity_is_excluded_and_conflict_is_fail_closed():
    result_plan = make_plan([row(mail_id("a"))])
    candidate = result_plan.candidates[0]
    exact = ExistingCanonicalRecord.from_candidate(candidate)

    already = execute(result_plan, FakeReader({
        candidate.identity: IdentityRead((exact,)),
    }))
    assert already.execution_status == "dry_run_noop"
    assert already.already_present_count == 1
    assert already.would_write_count == 0

    conflict_record = ExistingCanonicalRecord(
        **{**exact.__dict__, "amount_yen": exact.amount_yen + 1},
    )
    conflict = execute(result_plan, FakeReader({
        candidate.identity: IdentityRead((conflict_record,)),
    }))
    assert conflict.execution_status == "stopped_review_required"
    assert conflict.conflict_count == 1
    assert conflict.would_write_count == 0


def test_one_conflict_stops_the_selected_batch_instead_of_partially_writing():
    result_plan = make_plan([row(mail_id("a")), row(mail_id("b"), amount=2000)])
    conflicting = ExistingCanonicalRecord.from_candidate(result_plan.candidates[0])
    conflicting = ExistingCanonicalRecord(
        **{**conflicting.__dict__, "merchant": "different"},
    )

    result = execute(result_plan, FakeReader({
        result_plan.candidates[0].identity: IdentityRead((conflicting,)),
    }))

    assert result.revalidated_new_count == 1
    assert result.conflict_count == 1
    assert result.execution_status == "stopped_review_required"
    assert result.would_write_count == 0


def test_new_identity_is_would_write_and_apply_true_still_cannot_write():
    result_plan = make_plan([row(mail_id("a"))])
    reader = FakeReader()

    dry_run = execute(result_plan, reader)
    apply_attempt = execute(result_plan, reader, apply=True)

    assert dry_run.execution_status == "dry_run_ready"
    assert dry_run.would_write_count == 1
    assert apply_attempt.execution_status == "apply_blocked_writer_unavailable"
    assert apply_attempt.apply_requested
    assert not apply_attempt.writer_connected
    assert reader.external_write_count == 0
    assert "production_writer_not_connected" in apply_attempt.reason_codes


def test_same_plan_retry_revalidates_and_does_not_duplicate_an_exact_identity():
    result_plan = make_plan([row(mail_id("a"))])
    candidate = result_plan.candidates[0]
    reader = FakeReader()

    first = execute(result_plan, reader)
    reader.reads[candidate.identity] = IdentityRead((
        ExistingCanonicalRecord.from_candidate(candidate),
    ))
    second = execute(result_plan, reader)

    assert first.would_write_count == 1
    assert second.would_write_count == 0
    assert second.already_present_count == 1
    assert reader.read_count == 2
    assert reader.external_write_count == 0


def test_outcome_unknown_requires_readback_before_retry_decision():
    result_plan = make_plan([row(mail_id("a"))])
    candidate = result_plan.candidates[0]
    prior = [PriorCandidateOutcome(candidate.identity, CandidateState.OUTCOME_UNKNOWN)]

    present = execute(result_plan, FakeReader({
        candidate.identity: IdentityRead((ExistingCanonicalRecord.from_candidate(candidate),)),
    }), prior_outcomes=prior)
    absent = execute(result_plan, FakeReader(), prior_outcomes=prior)
    unknown_reader = FakeReader({
        candidate.identity: IdentityRead(readable=False, reason_code="timeout"),
    })
    unknown = execute(result_plan, unknown_reader, prior_outcomes=prior)

    assert present.already_present_count == 1
    assert "outcome_unknown_confirmed_present" in present.reason_codes
    assert absent.revalidated_new_count == 1
    assert "outcome_unknown_absent_retryable" in absent.reason_codes
    assert unknown.execution_status == "stopped_review_required"
    assert unknown.candidate_results[0].state == CandidateState.OUTCOME_UNKNOWN
    assert unknown.would_write_count == 0
    assert unknown_reader.external_write_count == 0


def test_plan_duplicate_identity_is_rejected():
    valid = make_plan([row(mail_id("a"))])
    duplicated = clone_plan(
        valid,
        candidates=(valid.candidates[0], valid.candidates[0]),
        eligible_canonical_count=2,
    )
    with pytest.raises(RuntimeError, match="duplicate_candidate_identity"):
        execute(duplicated)


def test_execution_order_and_canary_subset_are_deterministic():
    rows = [
        row(mail_id("d"), amount=4000),
        row(mail_id("b"), amount=2000),
        row(mail_id("c"), amount=3000),
        row(mail_id("a"), amount=1000),
    ]
    forward = make_plan(rows)
    reverse = make_plan(list(reversed(rows)))

    all_forward = execute(forward)
    all_reverse = execute(reverse)
    canary_one = execute(forward, subset=ExecutionSubset(limit=2))
    canary_two = execute(reverse, subset=ExecutionSubset(limit=2))

    assert all_forward.candidate_results == all_reverse.candidate_results
    assert canary_one.candidate_results == canary_two.candidate_results
    assert canary_one.selected_candidate_count == 2
    assert canary_one.input_candidate_count == 4


def test_reader_failure_stops_all_selected_candidates_without_writes():
    result_plan = make_plan([row(mail_id("a")), row(mail_id("b"), amount=2000)])
    reader = FakeReader(fail=True)

    result = execute(result_plan, reader, apply=True)

    assert result.execution_status == "stopped_review_required"
    assert result.failed_review_count == 2
    assert result.would_write_count == 0
    assert reader.external_write_count == 0


def test_sheets_reader_verifies_schema_before_treating_identity_as_absent():
    result_plan = make_plan([row(mail_id("a"))])

    class WrongSchemaDB:
        def __init__(self):
            self.calls = []

        def get(self, rng):
            self.calls.append(rng)
            return [["unexpected header"]]

    db = WrongSchemaDB()
    result = execute(result_plan, SheetsCanonicalIdentityReader(db))

    assert result.execution_status == "stopped_review_required"
    assert result.failed_review_count == 1
    assert result.would_write_count == 0
    assert db.calls == ["取込データ!A1:L1"]


def test_post_write_verification_never_guesses_an_unknown_outcome():
    result_plan = make_plan([row(mail_id("a"))])
    candidate = result_plan.candidates[0]

    confirmed = verify_post_write(
        result_plan,
        candidate.identity,
        IdentityRead((ExistingCanonicalRecord.from_candidate(candidate),)),
        audit_key=AUDIT_KEY,
    )
    absent = verify_post_write(
        result_plan, candidate.identity, IdentityRead(), audit_key=AUDIT_KEY,
    )
    unknown = verify_post_write(
        result_plan, candidate.identity, IdentityRead(readable=False),
        audit_key=AUDIT_KEY,
    )

    assert confirmed.state == CandidateState.WRITE_CONFIRMED
    assert absent.state == CandidateState.FAILED
    assert unknown.state == CandidateState.OUTCOME_UNKNOWN


def test_audit_reference_is_keyed_and_contains_no_business_values():
    result_plan = make_plan([row(mail_id("a"), merchant="秘密店舗", amount=9876)])

    first = execute(result_plan)
    second = execute(result_plan)
    other_key = execute_canonical_apply_plan(
        result_plan, FakeReader(), audit_key=b"another-test-only-audit-key-at-least-32",
    )

    assert first.plan_audit_ref == second.plan_audit_ref
    assert first.plan_audit_ref != other_key.plan_audit_ref
    assert "秘密店舗" not in repr(first)
    assert "9876" not in first.plan_audit_ref

    with pytest.raises(ValueError, match="audit_key"):
        execute_canonical_apply_plan(result_plan, FakeReader(), audit_key=b"short")
