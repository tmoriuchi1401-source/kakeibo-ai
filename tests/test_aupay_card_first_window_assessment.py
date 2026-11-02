"""Synthetic evidence for the first-window Apply NO-GO; no new Apply surface.

Amounts mirror the public aggregate only. Identities, merchants, mail and review
reasons are invented; these fixtures cannot identify the four production holds.
"""
import base64
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from app.aupay_card_apply_plan import build_canonical_apply_plan
from app.aupay_card_batch import create_batch_projection
from app.aupay_card_recurring import (
    _classify_candidates, build_incremental_window, run_recurring_ingestion,
)
from app.aupay_card_writer import TargetBinding, full_plan_binding_reference_v2
from app.aupay_mail_pipeline import AuPayCardMailPipeline
from app.auto_expense import AutoExpensePipeline
from app.amazon_money_review import MoneyReviews, record_from_entry
from app.amazon_money_runtime import money_review_items
from app.transaction_plan import reconcile_transactions
from test_amazon_money import mail
from test_amazon_money_runtime import opt_in
from test_auto_expense import FakeDB
from test_aupay_card_window_preview import CHECKPOINT, END, NOW, START, Gmail, parts


def raw(index, amount, *, money=False, variant="a"):
    merchant = "AMAZON.CO.JP" if money else f"合成商店{index}"
    body = ("本会員さま ご利用分\nNo.1 --------\n▼ご利用日\n2026年10月20日\n"
            f"▼ご利用先\n{merchant}\n▼ご利用金額\n{amount}円")
    message = mail("【ご利用詳細】au PAY カード", body,
                   sender="info@kddi-fs.com",
                   message_id=f"<synthetic-{variant}-{index}@example.invalid>")
    return base64.urlsafe_b64encode(message).decode()


def fixture(tmp_path, monkeypatch):
    p = parts(tmp_path)
    store, ledger, writer = opt_in(monkeypatch)
    # A SYNTHETIC reason, not a claim about production's four exceptions.
    store.data["money"]["legacy"] = {
        "synthetic-open": {"state": "open", "amount": 9999,
                           "expense_ids": ["synthetic-legacy-expense"]},
    }
    duplicates = [18000] * 5 + [20493]
    new = [4000] * 32 + [21218]
    ordinary = [raw(i, amount) for i, amount in enumerate(duplicates + new)]
    held = [raw(100+i, amount, money=True)
            for i, amount in enumerate([1000, 1100, 1200, 1034])]
    transactions, collection = collect(ordinary[:6])
    seed = build_canonical_apply_plan(collection, reconcile_transactions(transactions, []))
    p[5].rows = [c.to_import_row(imported_at=START, status="auto_expense")
                 for c in seed.candidates]
    return p, store, ledger, writer, ordinary, held


def collect(raws):
    return AuPayCardMailPipeline._collect(
        Gmail(raws), "synthetic", 500, sleeper=lambda _: None,
        request_interval=0, money_records=[], money_notices=[],
    )


def run(p, raws, *, preview, now=NOW, gmail=None):
    return run_recurring_ingestion(
        gmail_service=gmail or Gmail(raws), db=p[5], state=p[2],
        key_provider=p[3], authority_provider=p[4], state_dir=p[1], repo_root=p[0],
        now=now, dry_run=preview, sleeper=lambda _: None,
        **({"preview_window": (START, END)} if preview else {}),
    )


def test_33_new_6_duplicate_4_held_are_disjoint_and_preview_does_not_save_holds(tmp_path, monkeypatch):
    p, store, ledger, _, ordinary, held = fixture(tmp_path, monkeypatch)
    before = deepcopy(store.data)
    result = run(p, ordinary + held, preview=True)
    assert result["failure"] == 0
    a = result["aggregation"]
    assert (a["candidates"]["count"], a["candidates"]["amount_yen"]) == (43, 264045)
    for group, count, amount in (("new", 33, 149218), ("duplicate", 6, 110493), ("held", 4, 4334)):
        assert a["by_classification"][group] == dict(count=count, amount_yen=amount, reason=None)
    assert a["planned_financial_writes"]["transactions"] == a["by_classification"]["new"]
    assert store.data == before and ledger.calls == [] and p[5].write_calls == 0
    assert p[2].successful_window_end() == CHECKPOINT
    assert store.read("money")["records"] == {}  # Preview is not a hold archive.


def test_existing_batch_projection_selects_only_33_new_candidates(tmp_path, monkeypatch):
    p, _, _, _, ordinary, _ = fixture(tmp_path, monkeypatch)
    transactions, collection = collect(ordinary)
    plan = build_canonical_apply_plan(collection, reconcile_transactions(transactions, p[5].rows))
    statuses, reviews = _classify_candidates(plan, p[5])
    projection = create_batch_projection(plan, candidate_identities=tuple(statuses),
        statuses=statuses, binding=TargetBinding(expected_spreadsheet_id=p[5].sid),
        audit_key=p[3].load())
    assert reviews == 0 and projection.batch_size == 33
    assert sum(c.amount_yen for c in projection.plan.candidates) == 149218
    assert not set(projection.candidate_identities) & {row[0] for row in p[5].rows}


def test_equal_public_totals_do_not_prove_candidate_identity(tmp_path, monkeypatch):
    p, _, _, _, ordinary, held = fixture(tmp_path, monkeypatch)
    first = run(p, ordinary + held, preview=True)
    changed = ordinary[:6] + [raw(6+i, amount, variant="different")
                              for i, amount in enumerate([4000]*32 + [21218])]
    second = run(p, changed + held, preview=True)
    assert first["aggregation"] == second["aggregation"]
    refs = []
    for raws in (ordinary, changed):
        transactions, collection = collect(raws)
        plan = build_canonical_apply_plan(collection, reconcile_transactions(transactions, p[5].rows))
        refs.append(full_plan_binding_reference_v2(plan, p[3].load()))
    assert refs[0] != refs[1]


def test_fixed_apply_is_rejected_before_authority_or_gmail(tmp_path, monkeypatch):
    p, _, _, _, ordinary, held = fixture(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="explicit_card_window_requires_preview"):
        run_recurring_ingestion(gmail_service=object(), db=object(), state=p[2],
            key_provider=object(), authority_provider=object(), state_dir=p[1], repo_root=p[0],
            now=NOW, dry_run=False, preview_window=(START, END))
    with pytest.raises(RuntimeError, match="incremental_window_exceeds_authority"):
        run(p, ordinary+held, preview=False)
    assert p[2].successful_window_end() == CHECKPOINT and p[5].write_calls == 0


def test_normal_writer_persists_reviews_and_readback_before_checkpoint_only_at_synthetic_boundary(tmp_path, monkeypatch):
    p, store, ledger, writer, ordinary, held = fixture(tmp_path, monkeypatch)
    # END is a synthetic clock. Backdating production --now is NOT a procedure.
    result = run(p, ordinary+held, preview=False, now=END)
    assert result["failure"] == 0 and result["written"] == 33
    assert result["already_present"] == 6 and result["money_review"] == 4
    assert result["capability_final"] == "sealed" and result["journal_final"] == "confirmed"
    assert result["lease_final"] == "released" and result["write_requests"] == 1
    assert p[2].successful_window_end() == END and len(p[5].rows) == 39
    assert sum(row[6] for row in p[5].rows) == 259711
    assert ledger.calls == []  # No money import/expense write.
    saved = store.read("money")["records"]
    assert len(saved) == 4 and sum(r["amount"] for r in saved.values()) == 4334
    assert {r["reason"] for r in saved.values()} == {"legacy_unsettled_purchase_possible"}
    # Existing record-based review does not depend on the later Gmail window.
    requests = MoneyReviews(writer)
    for i, (identity, entry) in enumerate(sorted(saved.items())):
        restored = record_from_entry(entry)
        assert restored.money_id == identity and restored.fingerprint == entry["fingerprint"]
        assert restored.original_url.startswith("https://mail.google.com/")
        request = "REQ-" + f"{i+1:032x}"
        requests.prepare(request, identity, "hold")
        assert requests.apply(request)["state"] == "applied"
    assert len(money_review_items(store)) == 4 and ledger.calls == []
    assert all(item.status == "保留" for item in money_review_items(store))


def test_advanced_checkpoint_excludes_early_mail_and_cannot_anchor_old_window(tmp_path, monkeypatch):
    p, _, _, _, ordinary, held = fixture(tmp_path, monkeypatch)
    run(p, ordinary+held, preview=False, now=END)
    next_window = build_incremental_window(p[4].load(), p[2], END + timedelta(days=1))
    assert next_window.start == END - timedelta(days=2)
    assert next_window.start > START  # An early hold cannot be recovered by overlap alone.
    with pytest.raises(RuntimeError, match="preview_window_checkpoint_mismatch"):
        run(p, ordinary+held, preview=True)


def test_existing_core_intentionally_does_not_post_canonical_card_auto_expense_rows(tmp_path, monkeypatch):
    p, _, _, _, ordinary, held = fixture(tmp_path, monkeypatch)
    run(p, ordinary+held, preview=False, now=END)
    db = FakeDB(p[5].rows)
    core = AutoExpensePipeline(db)
    assert core.preview()["candidates"] == 0
    assert core.apply()["expenses_created"] == 0 and db.appended == []


@pytest.mark.parametrize("outcome", ["timeout", "silent_no_append"])
def test_unknown_or_unconfirmed_writer_never_advances_checkpoint_or_retries(tmp_path, monkeypatch, outcome):
    p, _, _, _, ordinary, held = fixture(tmp_path, monkeypatch)
    api = p[5].svc.spreadsheets().values()
    def execute():
        p[5].write_calls += 1
        if outcome == "timeout":
            raise TimeoutError("synthetic response unknown")
        return {"updates": {"updatedRows": 0}}
    monkeypatch.setattr(api, "execute", execute)
    result = run(p, ordinary+held, preview=False, now=END)
    assert result["failure"] == 1 and result["journal_final"] == "outcome_unknown"
    assert result["write_requests"] == p[5].write_calls == 1
    assert result["capability_final"] == "sealed"
    assert p[2].successful_window_end() == CHECKPOINT and len(p[5].rows) == 6


def test_fresh_existing_rows_reduce_selection_instead_of_forcing_33_writes(tmp_path, monkeypatch):
    p, _, _, _, ordinary, held = fixture(tmp_path, monkeypatch)
    transactions, collection = collect(ordinary)
    plan = build_canonical_apply_plan(collection, reconcile_transactions(transactions, p[5].rows))
    candidate = plan.candidates[0]
    p[5].rows.append(candidate.to_import_row(imported_at=START, status="auto_expense"))
    result = run(p, ordinary+held, preview=False, now=END)
    assert result["failure"] == 0 and result["written"] == 32
    assert result["already_present"] == 7
    assert len([row for row in p[5].rows if row[0] == candidate.identity]) == 1


def test_strict_preview_rejects_missing_raw_without_saving_reviews(tmp_path, monkeypatch):
    p, store, ledger, _, ordinary, held = fixture(tmp_path, monkeypatch)
    before = deepcopy(store.data)
    result = run(p, ordinary+held, preview=True,
                 gmail=Gmail(ordinary+held, missing_raw=True))
    assert result["failure"] == 1 and p[5].write_calls == 0
    assert p[2].successful_window_end() == CHECKPOINT
    assert store.data == before and ledger.calls == []


def test_unwrapped_normal_collector_is_not_evidence_of_strict_completeness(tmp_path, monkeypatch):
    p, store, ledger, _, ordinary, held = fixture(tmp_path, monkeypatch)
    result = run(p, ordinary+held, preview=False, now=END,
                 gmail=Gmail(ordinary+held, missing_raw=True))
    # Characterize a blocker, not an acceptable Apply contract. The future fixed
    # Apply must use the strict collector; never reinterpret this as safe noop.
    assert result["failure"] == 0 and result["status"] == "noop"
    assert p[2].successful_window_end() == END
    assert p[5].write_calls == 0 and store.read("money")["records"] == {} and ledger.calls == []


@pytest.mark.parametrize("change", ["expired", "wrong_target", "wrong_source"])
def test_invalid_authority_stops_before_financial_write(tmp_path, monkeypatch, change):
    p, _, _, _, ordinary, held = fixture(tmp_path, monkeypatch)
    path = p[4].path
    policy = json.loads(path.read_text(encoding="utf-8"))
    policy.update({"expired": {"expires_at": START.isoformat()},
                   "wrong_target": {"expected_spreadsheet_id": "other-synthetic-sheet"},
                   "wrong_source": {"source": "bank_pdf_drive"}}[change])
    path.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(RuntimeError):
        run(p, ordinary+held, preview=False, now=END)
    assert p[2].successful_window_end() == CHECKPOINT and p[5].write_calls == 0
