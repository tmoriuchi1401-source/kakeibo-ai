from copy import deepcopy
from dataclasses import replace
import hashlib
from types import SimpleNamespace

import pytest

from app.amazon_money import MoneyError, decide, source_alias
from app.amazon_money_migration import MigrationReader, build_manifest, initialize
from app.amazon_money_writer import MoneyWriter
from app.monthly_projection import ProjectionError
from test_amazon_money import Ledger, record
from test_projection_refresh import Store


DAY = "2026-09-20"


def expense(key="old", amount=1000, source="amazon:order", status="active"):
    return [key, "2026-08-01", "Amazon.co.jp", "本人の商品名", amount,
        "本人分類", "細分類", "old", "Amazon", "", source, "本人メモ", status]


def imported(value, target="old", status="matched_amazon"):
    return [value.source_id, "old-time", "au PAYカード", value.source_id, value.day,
        "AMAZON.CO.JP", value.amount, "通常払い", status, target, "old-hash", "会員=本会員"]


def fixture():
    value = record(day="2026-08-03", order_id="order")
    raw = {"spreadsheet_id": "synthetic", "expenses": [expense()], "imports": [imported(value)]}
    bindings = [{"record": value, "expense_ids": ["old"]}]
    return raw, bindings


def manifest(raw, bindings=()):
    return build_manifest(raw, cutover_day=DAY, bindings=bindings)


def test_aggregate_retirement_preserves_ids_and_personal_fields_without_double_counting():
    raw, bindings = fixture()
    raw["expenses"] = [expense("aggregate", status="superseded_amazon_items"),
        expense("a", 400), expense("b", 600)]
    raw["imports"][0][9] = "a"
    bindings[0]["expense_ids"] = ["b", "a"]
    before = deepcopy(raw)
    result = manifest(raw, bindings)
    group = result["book"]["legacy"]["order"]
    assert group["amount"] == 1000 and group["expense_ids"] == ["a", "b"]
    assert group["state"] == "settled"
    assert result["ledger_totals"] == {"2026-08:purchase": {"count": 2, "amount": 1000}}
    assert len(result["legacy_expenses"]) == 3 and raw == before
    assert decide(bindings[0]["record"], result["book"]).action == "duplicate"


def test_unbound_purchase_stays_open_and_later_charge_does_not_post():
    raw, bindings = fixture()
    result = manifest(raw)
    assert result["counts"]["open_groups"] == 1
    late = replace(bindings[0]["record"], day=DAY, reference="transaction:new", source_id="later")
    assert decide(late, result["book"]).reason == "legacy_payment_identity_required"
    assert decide(replace(late, order_id=""), result["book"]).action == "review"
    assert result["legacy_imports"]["source-1"]["target_id"] == "old"


def test_same_day_amount_does_not_create_alias_without_existing_fixed_link():
    raw, bindings = fixture()
    raw["imports"][0][9] = ""
    with pytest.raises(MoneyError, match="binding_missing"):
        manifest(raw, bindings)


def test_original_amazon_key_is_an_exact_anchor_without_rerunning_similarity_match():
    raw, bindings = fixture()
    key = "saved-csv-item-key"
    identity = "A-" + hashlib.sha256(key.encode()).hexdigest()[:24]
    raw["expenses"][0][0] = identity
    bindings[0]["expense_ids"] = [identity]
    raw["imports"][0][9] = ""
    raw["imports"][0][11] = "会員=本会員; Amazonキー=" + key + "; 旧照合済み"
    assert manifest(raw, bindings)["counts"]["bound_records"] == 1


def test_split_claim_capacity_and_complete_group_required():
    raw, bindings = fixture()
    first = replace(bindings[0]["record"], amount=400)
    second = replace(first, source_id="second", reference="transaction:second", amount=600)
    raw["expenses"] = [expense("a", 400), expense("b", 600)]
    raw["imports"] = [imported(first, "a"), imported(second, "b")]
    bindings = [{"record": r, "expense_ids": ["a", "b"]} for r in [first, second]]
    assert manifest(raw, bindings)["book"]["legacy"]["order"]["state"] == "settled"
    assert manifest(raw, bindings[:1])["book"]["legacy"]["order"]["state"] == "open"
    bindings[0]["expense_ids"] = ["a"]
    with pytest.raises(MoneyError, match="group_incomplete"):
        manifest(raw, bindings)
    bindings[0]["expense_ids"] = ["a", "b"]
    third = replace(first, source_id="third", reference="transaction:third", amount=1)
    raw["imports"].append(imported(third, "a"))
    with pytest.raises(MoneyError, match="claim_exceeds_purchase"):
        manifest(raw, bindings + [{"record": third, "expense_ids": ["a", "b"]}])


def refund_fixture():
    raw, bindings = fixture()
    refund = record(source_id="refund-source", reference="transaction:refund", amount=-300,
        kind="refund", day="2026-09-01", order_id="order")
    raw["expenses"].append(expense("refund", -300, source=refund.source_id))
    raw["imports"].append(imported(refund, "refund", "auto_expense"))
    bindings.append({"record": refund, "expense_ids": ["refund"], "related_id": "legacy:order"})
    return raw, bindings


def test_old_refund_is_linked_and_counts_toward_new_refund_capacity_once():
    raw, bindings = refund_fixture()
    result = manifest(raw, bindings)
    store, ledger = Store(), Ledger()
    initialize(store, lambda: deepcopy(raw), result, cutover_day=DAY, bindings=bindings)
    ledger.rows["支出明細"] = {r[0]: deepcopy(r) for r in raw["expenses"]}
    ledger.rows["取込データ"] = {r[0]: deepcopy(r) for r in raw["imports"]}
    writer = MoneyWriter(store, ledger)
    assert writer.apply([b["record"] for b in bindings], limit=2)["money_duplicate"] == 2
    assert ledger.calls == []
    extra = record(source_id="new-refund", reference="transaction:new-refund", amount=-701,
        kind="refund", day=DAY, order_id="order")
    assert writer.preview([extra])[0].reason == "refund_exceeds_purchase"
    assert writer.apply([replace(extra, amount=-700)], limit=1)["money_posted"] == 1
    assert sum(r[4] for r in ledger.rows["支出明細"].values()) == 0


def test_unbound_negative_rows_block_activation_not_silently_ignored():
    raw, bindings = refund_fixture()
    with pytest.raises(MoneyError, match="refund_identity_required"):
        manifest(raw, bindings[:1])
    bindings[-1]["related_id"] = "legacy:unknown"
    with pytest.raises(MoneyError, match="refund_binding_invalid"):
        manifest(raw, bindings)


@pytest.mark.parametrize("change", ["amount", "day", "source", "unconfirmed", "mixed", "supplement", "order"])
def test_stale_or_non_authoritative_binding_rejected(change):
    raw, bindings = fixture()
    changes = {"amount": {"amount": 999}, "day": {"day": "2026-08-04"},
        "source": {"source": "amazon", "payment": "gift_balance"}, "unconfirmed": {"confirmed": False},
        "mixed": {"payment": "mixed"}, "supplement": {"source": "amazon"}, "order": {"order_id": "other"}}
    bindings[0]["record"] = replace(bindings[0]["record"], **changes[change])
    with pytest.raises(MoneyError):
        manifest(raw, bindings)


@pytest.mark.parametrize("which", ["expenses", "imports"])
def test_duplicate_canonical_ids_fail_even_when_content_equal(which):
    raw, bindings = fixture()
    raw[which].append(deepcopy(raw[which][0]))
    with pytest.raises(MoneyError, match="identity_invalid"):
        manifest(raw, bindings)


@pytest.mark.parametrize("amount", ["1.5", True, "NaN", "infinity"])
def test_invalid_yen_not_rounded(amount):
    raw, bindings = fixture()
    raw["expenses"][0][4] = amount
    with pytest.raises(ProjectionError):
        manifest(raw, bindings)


@pytest.mark.parametrize("key", ["money-migration", "money"])
@pytest.mark.parametrize("after_save", [False, True])
def test_initialization_recovers_unknown_result_without_canonical_write(key, after_save):
    raw, bindings = fixture()
    result, store = manifest(raw, bindings), Store()
    store.fail_key, store.after_save = key, after_save
    with pytest.raises(RuntimeError):
        initialize(store, lambda: deepcopy(raw), result, cutover_day=DAY, bindings=bindings)
    initialize(store, lambda: deepcopy(raw), result, cutover_day=DAY, bindings=bindings)
    writes = list(store.writes)
    stats = initialize(store, lambda: deepcopy(raw), result, cutover_day=DAY, bindings=bindings)
    assert store.writes == writes and stats == {"money_initialized": 0, "expense_rows_written": 0, "import_rows_written": 0}
    assert set(store.data) == {"money", "money-migration"}


def test_latest_source_and_after_intent_changes_stop_without_money_initialization():
    raw, bindings = fixture()
    result, store = manifest(raw, bindings), Store()
    changed = deepcopy(raw)
    changed["expenses"][0][11] = "新しい本人入力"
    with pytest.raises(MoneyError, match="snapshot_changed"):
        initialize(store, lambda: changed, result, cutover_day=DAY, bindings=bindings)
    assert store.writes == []
    calls = iter([raw, changed])
    with pytest.raises(MoneyError, match="snapshot_changed"):
        initialize(store, lambda: next(calls), result, cutover_day=DAY, bindings=bindings)
    assert store.writes == ["money-migration"] and "money" not in store.data


def test_existing_live_book_and_different_manifest_never_reset():
    raw, bindings = fixture()
    result, store = manifest(raw, bindings), Store()
    initialize(store, lambda: raw, result, cutover_day=DAY, bindings=bindings)
    store.data["money"]["records"]["new"] = {"state": "posted"}
    before = deepcopy(store.data)
    with pytest.raises(MoneyError, match="book_exists"):
        initialize(store, lambda: raw, result, cutover_day=DAY, bindings=bindings)
    assert store.data == before
    with pytest.raises(MoneyError, match="manifest_conflict"):
        initialize(store, lambda: raw, manifest(raw), cutover_day=DAY)


def test_swapped_original_item_amounts_with_same_total_require_refund_review():
    raw, _ = fixture()
    raw["expenses"] = [expense("a", 400), expense("b", 600)]
    result, store, ledger = manifest(raw), Store(), Ledger()
    initialize(store, lambda: raw, result, cutover_day=DAY)
    ledger.rows["支出明細"] = {r[0]: deepcopy(r) for r in raw["expenses"]}
    ledger.rows["支出明細"]["a"][4] = 500
    ledger.rows["支出明細"]["b"][4] = 500
    refund = record(amount=-100, kind="refund", order_id="order")
    assert MoneyWriter(store, ledger).preview([refund])[0].reason == "refund_origin_ledger_changed"


def test_reader_uses_fresh_extent_beyond_5000_and_only_two_canonical_tabs():
    calls = []
    class DB:
        sid = "synthetic"
        def __init__(self):
            self.svc = SimpleNamespace(spreadsheets=lambda: self)
        def get(self, **kwargs):
            assert kwargs["fields"] == "sheets(properties(title,gridProperties(rowCount)))"
            return {"sheets": [{"properties": {"title": title, "gridProperties": {"rowCount": end}}}
                for title, end in [("支出明細", 6002), ("取込データ", 5002), ("Medical", 99999), ("Payroll", 99999)]]}
        def _execute_sheet_read(self, fn):
            return fn()
        def get_raw(self, rng):
            calls.append(rng)
            return [expense("last")] if rng == "'支出明細'!A6002:M6002" else []
    result = MigrationReader(DB())()
    assert len(calls) == 7 and result["expenses"][0][0] == "last"
    assert all("Medical" not in r and "Payroll" not in r for r in calls)


def test_snapshot_is_row_order_independent_and_preserves_all_history():
    raw, _ = fixture()
    raw["expenses"] += [expense("ancient", 10, "amazon:ancient")]
    raw["expenses"][-1][1] = "2010-01-01"
    original = manifest(raw)
    raw["expenses"].reverse()
    assert manifest(raw) == original and original["ledger_totals"]["2010-01:purchase"]["amount"] == 10


def test_other_spreadsheet_with_same_rows_cannot_use_source_manifest():
    raw, bindings = fixture()
    result, store = manifest(raw, bindings), Store()
    raw["spreadsheet_id"] = "different-canonical"
    with pytest.raises(MoneyError, match="snapshot_changed"):
        initialize(store, lambda: raw, result, cutover_day=DAY, bindings=bindings)
    assert store.writes == []


@pytest.mark.parametrize("which", ["expenses", "imports"])
def test_prior_money_writer_rows_cannot_be_reinitialized_as_legacy(which):
    raw, _ = fixture()
    if which == "expenses":
        raw["expenses"][0][10] = "AM-" + "1" * 32
        raw["expenses"][0][8] = "au PAYカード"
        raw["expenses"][0][2] = "Amazon"
    else:
        raw["imports"][0][8] = "canonical_amazon_money"
    with pytest.raises(MoneyError, match="already_posted"):
        manifest(raw)


def test_zero_value_legacy_rows_are_preserved_but_do_not_claim_a_payment():
    raw, _ = fixture()
    raw["expenses"] = [expense(amount=0)]
    result = manifest(raw)
    assert result["book"]["legacy"] == {} and result["legacy_expenses"]["old"]["amount"] == 0
    assert result["ledger_totals"] == {"2026-08:zero": {"count": 1, "amount": 0}}
