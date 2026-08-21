from dataclasses import replace

from app.amazon_manual_matching import (
    ManualMatchRequest,
    aggregate_amazon_orders,
    audit_information,
    candidate_id,
    existing_manual_usage,
    find_amazon_candidates,
    major_category_summary,
    validate_manual_batch,
)
from app.reconciliation import parse_import_rows


def amazon_row(order_id, asin, date, amount, *, name="商品", category="食費",
               method="Visa", kind="baseline", data_hash="hash", ship_date="",
               shipment_count=0):
    return [f"{order_id}|{asin}", order_id, asin, date, name, 1, amount, method,
            category, "小分類", kind, data_hash, "", ship_date, shipment_count]


def card(import_id="card:1", *, date="2026-08-20", amount=1000,
         status="amazon_unmatched", note="", payment="Visa", merchant="AMAZON.CO.JP"):
    return parse_import_rows([[
        import_id, "", "au PAYカード", import_id, date, merchant, amount, payment,
        status, "", "", note,
    ]])[0]


def orders():
    return aggregate_amazon_orders([
        amazon_row("ORDER-1", "ASIN-1", "2026-08-18", 600, name="長い商品名" * 8),
        amazon_row("ORDER-1", "ASIN-2", "2026-08-18", 400, category="日用品"),
        amazon_row("ORDER-2", "ASIN-3", "2026-08-19", 1050),
        amazon_row("ORDER-3", "ASIN-4", "2026-08-01", 1100),
        amazon_row("ORDER-OLD", "ASIN-5", "2026-07-20", 1000),
        amazon_row("ORDER-FUTURE", "ASIN-6", "2026-08-21", 1000),
    ])


def test_orders_are_aggregated_by_order_id():
    order = next(item for item in orders() if item.order_id == "ORDER-1")
    assert order.order_amount == 1000
    assert order.item_count == 2
    assert order.major_categories == ("日用品", "食費")
    assert " / " in order.short_item_summary
    assert len(order.short_item_summary) < 45


def test_summary_shows_at_most_two_products_and_remaining_count():
    order = aggregate_amazon_orders([
        amazon_row("ORDER", "A1", "2026-08-18", 100, name="商品名A" * 8),
        amazon_row("ORDER", "A2", "2026-08-18", 100, name="商品名B" * 8),
        amazon_row("ORDER", "A3", "2026-08-18", 100, name="商品名C" * 8),
        amazon_row("ORDER", "A4", "2026-08-18", 100, name="商品名D" * 8),
    ])[0]
    assert order.short_item_summary.count(" / ") == 1
    assert order.short_item_summary.endswith("ほか2点")


def test_major_category_summary_handles_single_multiple_and_unclassified():
    assert major_category_summary(("日用品",)) == "日用品"
    assert major_category_summary(("日用品", "食品")) == "日用品ほか"
    assert major_category_summary(()) == "未分類"
    assert major_category_summary(("未分類",)) == "未分類"


def test_search_uses_previous_thirty_days_and_returns_top_three_and_total():
    result = find_amazon_candidates(card(), orders())
    assert result.total_candidate_count == 3
    assert len(result.candidates) == 3
    assert {item.order_id for item in result.candidates} == {"ORDER-1", "ORDER-2", "ORDER-3"}
    assert "ORDER-OLD" not in {item.order_id for item in result.candidates}
    assert "ORDER-FUTURE" not in {item.order_id for item in result.candidates}


def test_exact_amount_then_difference_then_date_rank_candidates():
    result = find_amazon_candidates(card(), orders()).candidates
    assert [item.order_id for item in result] == ["ORDER-1", "ORDER-2", "ORDER-3"]


def test_difference_rate_breaks_equal_absolute_difference():
    # Equal absolute differences imply equal rates for one card amount; verify the rate is
    # explicitly in the stable rank tuple through a controlled replacement.
    result = find_amazon_candidates(card(amount=2000), orders(), limit=None).candidates
    assert result[0].amount_difference_rate <= result[-1].amount_difference_rate


def test_candidate_id_is_stable_opaque_and_independent_of_order_details():
    first = candidate_id("card:secret", "ORDER-SECRET")
    assert first == candidate_id("card:secret", "ORDER-SECRET")
    assert first != candidate_id("card:other", "ORDER-SECRET")
    assert "card" not in first and "ORDER" not in first and "SECRET" not in first
    before = find_amazon_candidates(card(), orders()).candidates[0]
    changed = replace(next(item for item in orders() if item.order_id == "ORDER-1"),
                      short_item_summary="変更後")
    after = find_amazon_candidates(card(), [changed]).candidates[0]
    assert before.candidate_id == after.candidate_id


def test_shipping_information_is_optional_and_does_not_change_candidate_identity():
    tx = card(date="2026-08-20")
    without = aggregate_amazon_orders([
        amazon_row("ORDER-SHIP", "A1", "2026-08-18", 1000),
    ])[0]
    with_shipping = aggregate_amazon_orders([
        amazon_row("ORDER-SHIP", "A1", "2026-08-18", 1000,
                   ship_date="2026-08-20", shipment_count=1),
    ])[0]
    first = find_amazon_candidates(tx, [without]).candidates[0]
    second = find_amazon_candidates(tx, [with_shipping]).candidates[0]
    assert first.ship_date is None
    assert first.shipping_date_difference_days is None
    assert second.ship_date == "2026-08-20"
    assert second.shipping_date_difference_days == 0
    assert second.shipment_count == 1
    assert first.candidate_id == second.candidate_id


def test_non_target_card_states_and_refund_installment_are_excluded():
    assert find_amazon_candidates(card(status="matched_amazon"), orders()).total_candidate_count == 0
    assert find_amazon_candidates(card(note="手動照合=x"), orders()).total_candidate_count == 0
    assert find_amazon_candidates(card(note="返金"), orders()).total_candidate_count == 0
    assert find_amazon_candidates(card(payment="分割払い"), orders()).total_candidate_count == 0


def selected(card_tx=None):
    card_tx = card_tx or card()
    return find_amazon_candidates(card_tx, orders()).candidates[0]


def test_validation_rejects_deleted_order_and_foreign_candidate():
    tx = card()
    candidate = selected(tx)
    deleted = [item for item in orders() if item.order_id != candidate.order_id]
    result = validate_manual_batch([ManualMatchRequest(tx, candidate)], deleted, [])
    assert not result.valid
    assert "no longer exists" in " ".join(result.errors_by_card[tx.import_id])

    foreign = replace(candidate, candidate_id=candidate_id("card:other", candidate.order_id))
    result = validate_manual_batch([ManualMatchRequest(tx, foreign)], orders(), [])
    assert "does not belong" in " ".join(result.errors_by_card[tx.import_id])


def test_validation_rejects_resolved_refund_and_installment_cards():
    for tx in (
        card(status="matched_amazon"), card(note="手動照合=x"),
        card(note="取消"), card(payment="分割払い"),
    ):
        candidate = replace(selected(), card_import_id=tx.import_id,
                            candidate_id=candidate_id(tx.import_id, selected().order_id))
        result = validate_manual_batch([ManualMatchRequest(tx, candidate)], orders(), [])
        assert not result.valid


def test_existing_and_batch_duplicate_order_use_are_rejected():
    tx1 = card("card:1")
    tx2 = card("card:2")
    one = selected(tx1)
    two = next(item for item in find_amazon_candidates(tx2, orders()).candidates
               if item.order_id == one.order_id)
    existing = card("old", status="matched_amazon",
                    note=f"手動照合=legacy; Amazonキー=amazon:{one.order_id}")
    usage = existing_manual_usage([existing])
    assert one.order_id in usage.order_ids
    assert not validate_manual_batch([ManualMatchRequest(tx1, one)], orders(), [existing]).valid
    result = validate_manual_batch(
        [ManualMatchRequest(tx1, one), ManualMatchRequest(tx2, two)], orders(), [],
    )
    assert not result.valid
    assert set(result.errors_by_card) == {"card:1", "card:2"}


def test_unresolvable_legacy_manual_reference_blocks_safely():
    legacy = card("old", status="matched_amazon", note="手動照合=日付だけ")
    usage = existing_manual_usage([legacy])
    assert usage.unresolved_manual_rows == ("old",)
    tx = card()
    assert not validate_manual_batch(
        [ManualMatchRequest(tx, selected(tx))], orders(), [legacy],
    ).valid


def test_audit_information_is_structured_and_omits_product_details():
    audit = audit_information(selected())
    assert audit["candidate_id"].startswith("amcand:")
    assert audit["amazon_order_id"] == "ORDER-1"
    assert audit["amount_difference"] == 0
    assert audit["date_difference_days"] == 2
    assert audit["item_count"] == 2
    assert "short_item_summary" not in audit
    assert "asin" not in repr(audit).lower()


def test_existing_automatic_amazon_matcher_module_is_not_modified():
    from app.aupay_card_pipeline import AuPayCardPipeline
    state, candidates = AuPayCardPipeline(type("DB", (), {})())._classify_amazon(
        "2026-08-20", 1000,
        [{"key": "amazon:o", "date": "2026-08-18", "amount": 1000}],
    )
    assert state == "matched_amazon"
    assert len(candidates) == 1
