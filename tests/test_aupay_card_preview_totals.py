"""Amounts from existing classifications only; public output contains no detail."""
from copy import deepcopy
import base64
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from app import aupay_card_recurring as card
from app.aupay_card_preview_totals import build_totals, money_totals, public_totals, metric
from app.aupay_card_apply_plan import build_canonical_apply_plan
from app.transaction_plan import reconcile_transactions
from app.aupay_mail_pipeline import AuPayCardMailPipeline
from app.aupay_card_window_preview import checked_preview, main
from app.amazon_money import MoneyDecision
from app.amazon_money_runtime import run_money_records
from test_aupay_card_window_preview import parts, Gmail, run, CHECKPOINT, START, END, amazon_message
from test_aupay_card_recurring import raw_message
from test_amazon_money import record
from test_amazon_money_runtime import opt_in


def raw(identity, amount, *, merchant="PRIVATE_SHOP", kind="purchase"):
    return dict(import_id=identity, date="2026-10-20", merchant=merchant, amount=amount,
                payment_type="au PAYカード", transaction_kind=kind, memo="PRIVATE_BODY", member="PRIVATE_MEMBER")


def classified(rows, existing=(), *, collection=None):
    reconciliation = reconcile_transactions(rows, list(existing))
    collection = collection or dict(AuPayCardMailPipeline._empty_summary(), collection_complete=True, listing_complete=True)
    plan = build_canonical_apply_plan(collection, reconciliation)
    statuses = {c.identity: "auto_expense" for c in plan.candidates}
    return plan, reconciliation, statuses, collection


def snapshot():
    return dict(card="state", parent="ledger", checkpoint=CHECKPOINT.isoformat(),
                financial={"sha256": "digest", "rows": {}}, money_digest="money")


def test_mixed_new_existing_held_and_return_are_a_disjoint_signed_total():
    rows = [raw("PRIVATE_NEW_ID", 1200), raw("PRIVATE_DUP_ID", 800),
            raw("PRIVATE_HELD_ID", 600), raw("PRIVATE_RETURN_ID", -300, kind="return")]
    p, reconciliation, statuses, collection = classified(rows, [["PRIVATE_DUP_ID"]])
    # Existing candidate classification, not a new match/parse in aggregation.
    statuses.pop("PRIVATE_HELD_ID")
    result = public_totals(build_totals(p, reconciliation, statuses, collection))
    assert result["candidates"] == metric(4, 2300)
    assert {k: result["by_classification"][k] for k in ("new", "duplicate", "held", "excluded")} == {
        "new": metric(1, 1200), "duplicate": metric(1, 800), "held": metric(1, 600), "excluded": metric(1, -300)}
    assert result["planned_financial_writes"]["transactions"] == metric(1, 1200)
    assert result["planned_financial_writes"]["import_rows"] == metric(1, 1200)
    assert result["planned_financial_writes"]["expense_rows"] == metric()
    assert result["planned_financial_writes"]["requests"] == metric(None, None, "request_count_requires_write_readback")
    assert result["actual_financial_writes"] == dict(request_count=0, row_count=0, amount_yen=0)
    encoded = json.dumps(result, ensure_ascii=False)
    assert "PRIVATE" not in encoded and "2026-10-20" not in encoded
    assert rows[0]["merchant"] == "PRIVATE_SHOP"  # no input mutation


def test_money_decisions_reused_once_no_writer_or_prepare_and_no_double_count():
    decisions = [MoneyDecision(record(source_id="PRIVATE_MONEY_NEW_ID", amount=1500), "post", "confirmed_money", ("PRIVATE_A", "PRIVATE_B")),
                 MoneyDecision(record(source_id="PRIVATE_DUP_ID", amount=900), "duplicate", "already_recorded"),
                 MoneyDecision(record(source_id="PRIVATE_REVIEW_ID", amount=-400, kind="refund"), "review", "PRIVATE_REASON")]
    calls = []
    class Preview:
        def preview(self, records): calls.append("preview"); return decisions
        def prepare(self): pytest.fail("prepare_called")
        def apply(self, *a, **kw): pytest.fail("apply_called")
    stats = run_money_records(Preview(), [], dry_run=True, limit=100, aggregate_preview=True)
    assert calls == ["preview"]
    p, r, s, c = classified([raw("PRIVATE_ORDINARY_ID", 1200)])
    total = public_totals(build_totals(p, r, s, c, money=stats["money_aggregation"], money_input_count=3))
    assert total["candidates"] == metric(4, 3200)
    assert total["by_classification"]["new"] == metric(2, 2700)
    assert total["by_classification"]["duplicate"] == metric(1, 900)
    assert total["by_classification"]["held"] == metric(1, -400)
    planned = total["planned_financial_writes"]
    assert planned["transactions"] == planned["import_rows"] == metric(2, 2700)
    assert planned["expense_rows"] == metric(2, 1500)
    assert planned["all_rows"] == metric(4, None, "overlapping_table_amounts_not_additive")
    assert "PRIVATE" not in json.dumps(total)


def test_unparsed_and_repeated_amounts_are_unknown_not_zero():
    p, r, s, c = classified([raw("PRIVATE_NEW_ID", 500)])
    c.update(parser_review_mail_count=2, review_line_items=1, duplicate_gmail_message=1)
    r["summary"].update(rejected=1, same_source_duplicates=2)
    total = public_totals(build_totals(p, r, s, c, money_notice_count=1))
    for name in ("parser_review_messages", "parser_review_lines", "duplicate_gmail_messages",
                 "rejected_transactions", "same_source_duplicates", "money_notice_messages"):
        assert total["unquantified"][name]["count"] > 0
        assert total["unquantified"][name]["amount_yen"] is None
        assert total["unquantified"][name]["reason"] == "classification_amount_not_available"
    assert total["candidates"] == metric(1, 500)  # classified canonical subset, not mail total


def test_noncanonical_resend_is_not_counted_twice():
    p, r, s, c = classified([raw("aupaycard-mail:"+"a"*24+":001", 700), raw("aupaycard-mail:"+"b"*24+":001", 700)])
    total = build_totals(p, r, s, c)
    assert total["candidates"] == metric(1, 700)
    assert total["unquantified"]["noncanonical_resends"] == metric(1, None, "classification_amount_not_available")


def test_missing_counter_is_unknown_and_other_money_actions_stay_separate():
    p, r, s, c = classified([])
    del c["parser_review_mail_count"]
    decisions = [MoneyDecision(record(amount=1000), "linked", "PRIVATE_REASON"),
                 MoneyDecision(record(amount=2000), "transfer", "PRIVATE_REASON"),
                 MoneyDecision(record(amount=3000), "supplement", "PRIVATE_REASON")]
    total = public_totals(build_totals(p, r, s, c, money=money_totals(decisions), money_input_count=3))
    assert total["unquantified"]["parser_review_messages"] == metric(None, None, "classification_amount_not_available")
    for group, amount in (("linked", 1000), ("transfer", 2000), ("supplement", 3000)):
        assert total["by_classification"][group] == metric(1, amount)
    assert total["candidates"] == metric(3, 6000)
    assert total["planned_financial_writes"]["transactions"] == metric()


def test_resume_and_missing_allocation_do_not_invent_remaining_rows():
    for action, ids, reason in (("resume", ("PRIVATE_A",), "resume_requires_intent_readback"),
                                ("post", (), "expense_allocation_not_in_preview")):
        values = money_totals([MoneyDecision(record(), action, "PRIVATE_REASON", ids)])
        assert values["expense_rows"] == metric(None, None, reason)


def test_aggregate_requested_with_apply_fails_before_calling_any_writer():
    with pytest.raises(RuntimeError, match="aggregation_requires_preview"):
        run_money_records(None, [], dry_run=False, limit=100, aggregate_preview=True)


@pytest.mark.parametrize("amount", [True, 1.5, "100", None])
def test_noninteger_yen_never_reaches_public_output(amount):
    with pytest.raises(RuntimeError, match="aggregation_invalid"):
        money_totals([MoneyDecision(replace(record(), amount=amount), "duplicate", "PRIVATE_REASON")])


@pytest.mark.parametrize("where", ["root", "metric", "reason", "planned", "inconsistent", "actual"])
def test_public_schema_rejects_details_and_invalid_totals(where):
    total = build_totals(*classified([raw("PRIVATE_NEW_ID", 1200)]))
    if where == "root": total["merchant"] = "PRIVATE_SHOP"
    if where == "metric": total["candidates"]["transaction_id"] = "PRIVATE_ID"
    if where == "reason": total["candidates"]["reason"] = "PRIVATE_BODY"
    if where == "planned": total["planned_financial_writes"]["transactions"]["amount_yen"] = 999
    if where == "inconsistent": total["by_classification"]["new"]["amount_yen"] = 999
    if where == "actual": total["actual_financial_writes"]["row_count"] = 1
    with pytest.raises(RuntimeError, match="aggregation_invalid|aggregation_inconsistent"):
        public_totals(total)


def test_source_mixed_new_and_duplicate_public_entry_has_only_totals(tmp_path, monkeypatch, capsys):
    p = parts(tmp_path)
    first, second = raw_message(merchant="PRIVATE_NEW_SHOP", amount="1200"), raw_message(merchant="PRIVATE_OLD_SHOP", amount="800")
    second = base64.urlsafe_b64encode(base64.urlsafe_b64decode(second+"===").replace(
        b"<recurring-fixture@example.invalid>", b"<PRIVATE_OLD_ID@example.invalid>")).decode()
    # Build existing identity from the same synthetic parser; no Apply seeds it.
    parsed, _ = AuPayCardMailPipeline._collect(Gmail([first, second]), "synthetic", 500,
                                               sleeper=lambda _: None, request_interval=0)
    p[5].rows = [[parsed[1]["import_id"]]]
    monkeypatch.setattr(card, "SealedSheetsBatchTransport", lambda *a, **kw: pytest.fail("writer_started"))
    monkeypatch.setattr(card, "execute_production_batch_once", lambda *a, **kw: pytest.fail("apply_started"))
    original_record = p[2].record
    def no_checkpoint(summary, *, advance_checkpoint):
        assert advance_checkpoint is False
        return original_record(summary, advance_checkpoint=False)
    monkeypatch.setattr(p[2], "record", no_checkpoint)
    result = run(p, Gmail([first, second]))
    assert result["failure"] == 0
    aggregate = result["aggregation"]
    assert aggregate["candidates"] == metric(2, 2000)
    assert aggregate["by_classification"]["new"] == metric(1, 1200)
    assert aggregate["by_classification"]["duplicate"] == metric(1, 800)
    safe = checked_preview(snapshot, lambda: result)
    import app.aupay_card_window_preview as entry
    monkeypatch.setattr(entry.socket, "setdefaulttimeout", lambda *a: None)
    monkeypatch.setattr(entry.logging, "disable", lambda *a: None)
    monkeypatch.setattr(entry.signal, "signal", lambda *a: None)
    monkeypatch.setattr(entry.subprocess, "check_output", lambda *a, **kw: "a"*40)
    monkeypatch.setattr(entry, "GitHub", lambda *a: None)
    monkeypatch.setattr(entry.sys, "argv", ["preview"])
    def private(*args):
        print("PRIVATE_RAW_BODY")
        return safe
    monkeypatch.setattr(entry, "run", private)
    assert main() == 0
    public = capsys.readouterr()
    rendered = json.loads(public.out)
    assert public.err == "" and "PRIVATE" not in public.out and "aupaycard-mail:" not in public.out
    assert rendered["aggregation"]["planned_financial_writes"]["transactions"] == metric(1, 1200)
    assert rendered["aggregation"]["actual_financial_writes"] == dict(request_count=0, row_count=0, amount_yen=0)
    assert p[2].successful_window_end() == CHECKPOINT and p[5].write_calls == 0


def test_existing_money_source_aggregation_adds_no_second_preview_or_writes(tmp_path, monkeypatch):
    store, ledger, writer = opt_in(monkeypatch)
    original = writer.preview; calls = []
    def preview(records): calls.append(1); return original(records)
    monkeypatch.setattr(writer, "preview", preview)
    monkeypatch.setattr(writer, "apply", lambda *a, **kw: pytest.fail("apply_called"))
    writer.prepare = lambda: pytest.fail("prepare_called")
    before = deepcopy(store.data)
    p = parts(tmp_path); result = run(p, Gmail([amazon_message()]))
    assert result["failure"] == 0 and calls == [1]
    assert result["aggregation"]["money"]["new"] == metric(1, 1000)
    assert result["aggregation"]["planned_financial_writes"]["expense_rows"] == metric(1, 1000)
    assert store.data == before and ledger.calls == [] and p[5].write_calls == 0
    assert p[2].successful_window_end() == CHECKPOINT
