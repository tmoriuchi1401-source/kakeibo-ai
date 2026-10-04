from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.bank_meaning_rules import RULE_SHEET, RULE_HEADERS
from app.bank_meaning_resolver import load
from app.bank_review_store import BankReviewStore
from app.bank_review_refresh import BankReviewRefresh
from app.bank_rule_usage import USAGE_SHEET, USAGE_HEADERS, parse_rows, plan_applications
from test_bank_review_refresh import rig
from test_bank_review_requests import NOW


def fixture():
    db, source, ui, _ = rig(saved_answer=True, future=True)
    return db, source, ui, BankReviewStore(db)


def plan(db, source, store):
    return plan_applications(source, load(db, legacy_rules=source.legacy_rules), store.read(), applied_at=NOW.isoformat())


def test_rule_applications_and_count_commit_atomically_then_replay_adds_zero():
    db, source, ui, store = fixture()
    before_calls = len(db.calls)
    refresh = BankReviewRefresh(db, lambda: deepcopy(source), clock=lambda: NOW)
    first = refresh.refresh(apply=True)
    assert first["bank_rule_applications_new"] == first["bank_rule_counts_updated"] == 1
    assert first["bank_usage_metadata_writes"] == 2
    assert len(db.calls) == before_calls + 1
    master = store.read()
    assert master.records(RULE_SHEET)[0].applied_count == 1
    assert len(master.records(USAGE_SHEET)) == 1
    assert db.sheets[USAGE_SHEET]["hidden"] is True
    assert master.tables[USAGE_SHEET][0] == USAGE_HEADERS
    second = refresh.refresh(apply=True)
    assert second["bank_rule_applications_new"] == second["bank_rule_counts_updated"] == second["bank_usage_metadata_writes"] == 0
    assert len(db.calls) == before_calls + 1
    # Meaning application does not pretend that income settlement finished.
    assert first["bank_income_missing"] == first["bank_settlement_groups"] == 1
    assert first["bank_confirmation_groups"] == 0


def test_preview_counts_candidates_but_never_persists_history_or_count():
    db, source, ui, store = fixture()
    before = deepcopy(db.tables)
    result = BankReviewRefresh(db, lambda: deepcopy(source), clock=lambda: NOW).refresh(apply=False)
    assert result["bank_rule_applications_new"] == 1 and result["bank_usage_metadata_writes"] == 0
    assert db.tables == before and not ui.writes


def test_once_only_answer_has_no_automatic_rule_application():
    db, source, ui, _ = rig(saved_answer=True)
    result = BankReviewRefresh(db, lambda: deepcopy(source)).refresh(apply=True)
    assert result["bank_rule_applications_new"] == result["bank_usage_metadata_writes"] == 0
    assert USAGE_SHEET not in db.sheets


@pytest.mark.parametrize("case", ["held", "inactive", "review"])
def test_conflict_inactive_and_review_never_gain_an_application(case):
    db, source, ui, store = fixture()
    if case == "inactive":
        db.tables[RULE_SHEET][1][16] = False
    elif case == "held":
        group = source.groups[0]
        source.legacy_rules = {"confirmed_non_own_classifications": {(group["normalized_description"], group["direction"], group["account_alias"], "needs_review")}}
    else:
        # The planner also refuses a matched individual-review result.
        class Review:
            def __call__(self, tx):
                return {"state": "matched", "classification": "needs_review", "rule_ids": (db.tables[RULE_SHEET][1][0],)}
        edits, counts = plan_applications(source, Review(), store.read(), applied_at=NOW.isoformat())
        assert not any(edits.values()) and counts["bank_rule_applications_new"] == 0
        return
    edits, counts = plan(db, source, store)
    assert counts["bank_rule_applications_new"] == 0 and not any(edits.values())


def test_overlapping_pdf_page_change_does_not_count_same_identity_twice():
    db, source, ui, store = fixture()
    master = store.read()
    edits, _ = plan(db, source, store)
    store.commit(master, edits)
    tx = next(iter(source.transactions.values()))
    changed = replace(tx, source_page=4, source_row=7)
    source.transactions[tx.source_row_identity] = changed
    source.files = ({"pdf_sha256": "c" * 64, "parsed": SimpleNamespace(transactions=(changed,))},)
    repeated, counts = plan(db, source, store)
    assert not any(repeated.values()) and counts["bank_rule_applications_new"] == 0


def test_new_same_description_transaction_adds_one_even_after_old_original_left_inbox():
    db, source, ui, store = fixture()
    master = store.read()
    edits, _ = plan(db, source, store)
    store.commit(master, edits)
    first = next(iter(source.transactions.values()))
    later = replace(first, transaction_date="2026-02-01", source_row_identity="bankpdf:au-jibun:jibun-primary:" + "c" * 24, source_row_hash="c" * 64)
    source.transactions = {later.source_row_identity: later}
    source.files = ({"pdf_sha256": "c" * 64, "parsed": SimpleNamespace(transactions=(later,))},)
    next_edits, counts = plan(db, source, store)
    assert counts["bank_rule_applications_new"] == 1
    assert next_edits[RULE_SHEET][0][1][-1] == 2
    store.commit(store.read(), next_edits)
    assert len(store.read().records(USAGE_SHEET)) == 2


def test_rule_revival_preserves_unique_count_across_revisions():
    db, source, ui, store = fixture()
    master = store.read()
    edits, _ = plan(db, source, store)
    store.commit(master, edits)
    db.tables[RULE_SHEET][1][15] = 2
    repeat, counts = plan(db, source, store)
    assert not any(repeat.values()) and counts["bank_rule_applications_new"] == 0
    assert store.read().records(USAGE_SHEET)[0].revision == 1


def test_blank_rule_rows_do_not_change_the_transport_destination():
    db, source, ui, store = fixture()
    db.tables[RULE_SHEET].insert(1, [""] * len(RULE_HEADERS))
    edits, _ = plan(db, source, store)
    assert edits[RULE_SHEET][0][0] == 3
    store.commit(store.read(), edits)
    assert db.tables[RULE_SHEET][1] == [""] * len(RULE_HEADERS)
    assert db.tables[RULE_SHEET][2][-1] == 1


@pytest.mark.parametrize("fault", ["count", "missing_original", "different_original", "sha", "changed_identity"])
def test_unsupported_count_or_original_evidence_prevents_any_metadata_write(fault):
    db, source, ui, store = fixture()
    before = len(db.calls)
    if fault == "count": db.tables[RULE_SHEET][1][-1] = 1
    if fault == "missing_original": source.files = ()
    if fault == "different_original": source.files[0]["parsed"].transactions = ()
    if fault == "sha": source.files[0]["pdf_sha256"] = "bad"
    if fault == "changed_identity":
        tx = next(iter(source.transactions.values()))
        source.transactions = {"wrong": tx}
    with pytest.raises(ValueError): plan(db, source, store)
    assert len(db.calls) == before and not ui.writes


@pytest.mark.parametrize("change", ["source", "master"])
def test_refresh_rechecks_before_history_and_count_write(change):
    db, source, ui, store = fixture()
    before = len(db.calls)
    calls = []
    def read():
        calls.append(1)
        current = deepcopy(source)
        if len(calls) == 2:
            if change == "source": current.fingerprint = "changed"
            else: db.tables[RULE_SHEET][1][16] = False
        return current
    with pytest.raises(ValueError): BankReviewRefresh(db, read).refresh(apply=True)
    assert len(db.calls) == before and USAGE_SHEET not in db.tables and not ui.writes


@pytest.mark.parametrize("fault", ["lost_response", "readback", "history_tampering", "duplicate"])
def test_history_errors_are_held_without_automatic_retry(fault):
    db, source, ui, store = fixture()
    master = store.read()
    edits, _ = plan(db, source, store)
    before = len(db.calls)
    if fault == "lost_response": db.error = "after"
    if fault == "readback": db.after_write = lambda current: current.tables[RULE_SHEET][1].__setitem__(17, 2)
    if fault in {"lost_response", "readback"}:
        with pytest.raises((TimeoutError, ValueError)): store.commit(master, edits)
        assert len(db.calls) == before + 1
        if fault == "lost_response":
            db.error = None
            repeated, _ = plan(db, source, store)
            assert not any(repeated.values())
        return
    store.commit(master, edits)
    if fault == "history_tampering": db.tables[USAGE_SHEET][1][-1] = "d" * 64
    else: db.tables[USAGE_SHEET].append(deepcopy(db.tables[USAGE_SHEET][1]))
    with pytest.raises(ValueError): plan(db, source, store)
    assert len(db.calls) == before + 1


def test_usage_history_cannot_be_overwritten_even_with_a_valid_new_row():
    db, source, ui, store = fixture()
    edits, _ = plan(db, source, store)
    store.commit(store.read(), edits)
    entry = store.read().records(USAGE_SHEET)[0]
    changed = replace(entry, pdf_sha256="f" * 64).row()
    before = len(db.calls)
    with pytest.raises(ValueError, match="immutable"):
        store.commit(store.read(), {USAGE_SHEET: [(2, changed)]})
    assert len(db.calls) == before


def test_usage_append_without_atomic_count_update_is_rejected_before_google_write():
    db, source, ui, store = fixture()
    edits, _ = plan(db, source, store)
    before = len(db.calls)
    with pytest.raises(ValueError, match="count_mismatch"):
        store.commit(store.read(), {USAGE_SHEET: edits[USAGE_SHEET]})
    assert len(db.calls) == before and USAGE_SHEET not in db.sheets
