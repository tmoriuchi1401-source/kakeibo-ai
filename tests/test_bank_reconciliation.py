import json
import sys

import pytest

from app.bank_pdf_pipeline import (
    BankPdfResult,
    NormalizedBankTransaction,
)
from app.bank_reconciliation import (
    CARD_STATEMENT_AUTHORITY_STATUS,
    PAYPAY_BANK_AUTHORITY_STATUS,
    BankPdfShadowPipeline,
    build_bank_preview_plan,
    build_bank_shadow_result,
    classify_bank_transaction,
    reconcile_bank_classification,
)
from app.cli import main
from app.reconciliation import parse_import_rows
from app.settings import Settings


def bank(description, amount=-1000, identity="bank:1"):
    return NormalizedBankTransaction(
        source="auじぶん銀行PDF",
        account_alias="test-account",
        transaction_date="2026-09-01",
        description=description,
        signed_amount=amount,
        source_page=1,
        source_row=1,
        source_row_identity=identity,
        source_row_hash="a" * 64,
        transaction_kind="withdrawal" if amount < 0 else "deposit",
    )


def import_row(identity, source, amount, status, date="2026-09-01", merchant="匿名"):
    return [
        identity, "", source, identity, date, merchant, amount,
        "", status, "", "hash", "",
    ]


def reconcile(transaction, rows=()):
    classified = classify_bank_transaction(transaction)
    return reconcile_bank_classification(classified, parse_import_rows(list(rows)))


def test_card_settlement_matches_only_explicit_statement_authority():
    transaction = bank("口座振替 AU PAY カード")
    decision = reconcile(transaction, [
        import_row("card-statement:1", "au PAYカード", 1000,
                   CARD_STATEMENT_AUTHORITY_STATUS),
    ])

    assert decision.classification.classification == "card_settlement"
    assert decision.reconciliation_status == "matched"
    assert decision.matched_source == "au PAYカード"
    assert decision.matched_identity == "card-statement:1"


def test_card_settlement_identified_but_unlinked_from_individual_purchases():
    decision = reconcile(bank("口座振替 AU PAY カード"), [
        import_row("aupaycard:purchase", "au PAYカード", 1000, "auto_expense"),
    ])

    assert decision.classification.classification == "card_settlement"
    assert decision.reconciliation_status == "identified_unlinked"
    assert not decision.matched_identity


def test_direct_debit_is_expense_after_card_rules():
    decision = reconcile(bank("口座振替 公共サービス"))
    assert decision.classification.classification == "expense"
    assert decision.classification.reason == "direct_debit"
    assert decision.reconciliation_status == "not_applicable"


def test_salary_bonus_and_interest_are_clear_income():
    cases = {
        "給与 匿名勤務先": "salary",
        "賞与 匿名勤務先": "bonus",
        "普通預金利息": "bank_interest",
    }
    for description, reason in cases.items():
        decision = classify_bank_transaction(bank(description, 1000))
        assert decision.classification == "income"
        assert decision.reason == reason


def test_explicit_bank_reward_is_income_not_transfer_candidate():
    for description in ("振込 キヤンペーントクテン", "振込 マトメテキンリユウグウ"):
        decision = classify_bank_transaction(bank(description, 1000))
        assert decision.classification == "income"
        assert decision.reason == "bank_reward"


def test_confirmed_internal_transfer_requires_exact_configured_description():
    transaction = bank("振込 匿名資金移動", 1000)
    unconfirmed = classify_bank_transaction(transaction)
    confirmed = classify_bank_transaction(
        transaction,
        confirmed_internal_transfers=frozenset({
            ("振込 匿名資金移動", "incoming"),
        }),
    )

    assert unconfirmed.classification == "needs_review"
    assert unconfirmed.reason == "ambiguous_incoming_transfer"
    assert confirmed.classification == "transfer"
    assert confirmed.reason == "confirmed_internal_transfer"

    wrong_direction = classify_bank_transaction(
        bank("振込 匿名資金移動", -1000),
        confirmed_internal_transfers=frozenset({
            ("振込 匿名資金移動", "incoming"),
        }),
    )
    assert wrong_direction.classification == "needs_review"


def test_private_transfer_config_requires_exact_description_and_direction():
    settings = Settings(bank_internal_transfers_json=json.dumps([
        {"description": "振込 匿名資金移動", "direction": "incoming"},
    ], ensure_ascii=False))

    assert settings.bank_confirmed_internal_transfers() == frozenset({
        ("振込 匿名資金移動", "incoming"),
    })


@pytest.mark.parametrize("value", [
    "not-json",
    '["description only"]',
    '[{"description":"匿名","direction":"both"}]',
])
def test_private_transfer_config_fails_closed(value):
    settings = Settings(bank_internal_transfers_json=value)

    with pytest.raises(RuntimeError, match="BANK_CONFIRMED_INTERNAL_TRANSFERS_JSON"):
        settings.bank_confirmed_internal_transfers()


def test_ambiguous_transfer_and_atm_withdrawal_have_separate_semantics():
    transfer = classify_bank_transaction(bank("振込 匿名宛先"))
    atm = classify_bank_transaction(bank("ATM 現金引出"))

    assert (transfer.classification, transfer.reason) == (
        "needs_review", "ambiguous_outgoing_transfer",
    )
    assert (atm.classification, atm.reason) == (
        "cash_withdrawal", "atm_cash_withdrawal",
    )


def test_loan_repayment_is_separate_and_withheld_from_write_preview():
    decision = reconcile(bank("約定返済"))

    assert decision.classification.classification == "loan_repayment"
    assert decision.classification.reason == "contractual_loan_repayment"
    assert decision.reconciliation_status == "not_applicable"
    assert decision.write_eligibility == "withheld"


def test_paypay_candidate_matches_only_explicit_transfer_authority():
    transaction = bank("PAYPAY チャージ")
    unmatched = reconcile(transaction, [
        import_row("paypay:purchase", "PayPay", 1000, "auto_expense"),
    ])
    matched = reconcile(transaction, [
        import_row("paypay:funding", "PayPay", 1000,
                   PAYPAY_BANK_AUTHORITY_STATUS),
    ])

    assert unmatched.classification.classification == "transfer"
    assert unmatched.reconciliation_status == "unmatched"
    assert matched.reconciliation_status == "matched"
    assert matched.matched_identity == "paypay:funding"


def test_unrelated_same_amount_and_false_candidates_are_rejected():
    transaction = bank("口座振替 AU PAY カード")
    unrelated = reconcile(transaction, [
        import_row("card:purchase", "au PAYカード", 1000, "auto_expense"),
        import_row("paypay:funding", "PayPay", 1000,
                   PAYPAY_BANK_AUTHORITY_STATUS),
    ])
    wrong_date = reconcile(transaction, [
        import_row("card:statement", "au PAYカード", 1000,
                   CARD_STATEMENT_AUTHORITY_STATUS, date="2026-09-02"),
    ])
    ambiguous = reconcile(transaction, [
        import_row("card:statement:1", "au PAYカード", 1000,
                   CARD_STATEMENT_AUTHORITY_STATUS),
        import_row("card:statement:2", "au PAYカード", 1000,
                   CARD_STATEMENT_AUTHORITY_STATUS),
    ])

    assert unrelated.reconciliation_status == "identified_unlinked"
    assert wrong_date.reconciliation_status == "identified_unlinked"
    assert ambiguous.reconciliation_status == "identified_unlinked"


def test_shadow_summary_contains_only_counts_and_reason_taxonomy():
    transactions = (
        bank("口座振替 AU PAY カード", identity="bank:1"),
        bank("ATM 現金引出", identity="bank:2"),
    )
    parsed = BankPdfResult(
        pages=1,
        candidate_rows=2,
        transactions=transactions,
        issues=(),
        balance_consistency_failures=0,
        duplicate_candidates=0,
        canonical_transactions=tuple(item.to_canonical() for item in transactions),
    )

    summary = build_bank_shadow_result(parsed, []).summary()

    assert summary["classification"]["card_settlement"] == 1
    assert summary["classification"]["cash_withdrawal"] == 1
    assert summary["classification"]["needs_review"] == 0
    assert summary["reconciliation"]["identified_unlinked"] == 1
    assert summary["needs_review_reasons"] == {}
    assert summary["write_eligibility"] == {
        "preview_candidate": 0, "withheld": 2,
    }
    rendered = repr(summary)
    assert "AU PAY" not in rendered
    assert "現金引出" not in rendered
    assert "bank:1" not in rendered


def test_replay_is_counted_without_changing_classification(monkeypatch):
    transaction = bank("口座振替 公共サービス")
    parsed = BankPdfResult(
        pages=1,
        candidate_rows=1,
        transactions=(transaction,),
        issues=(),
        balance_consistency_failures=0,
        duplicate_candidates=1,
        canonical_transactions=(),
    )

    class DB:
        def get(self, range_name):
            assert range_name == "取込データ!A2:L"
            return [import_row("bank:1", "auじぶん銀行PDF", 1000, "accepted")]

    monkeypatch.setattr("app.bank_reconciliation.BankPdfPipeline.parse",
                        lambda *args, **kwargs: parsed)
    summary = BankPdfShadowPipeline(DB()).preview("statement.pdf")

    assert summary["duplicate"] == 1
    assert summary["classification"]["expense"] == 1
    assert summary["write_eligibility"] == {
        "preview_candidate": 0, "withheld": 1,
    }


def test_income_and_expense_are_only_preview_candidates():
    income = reconcile(bank("給与 匿名勤務先", 1000))
    expense = reconcile(bank("口座振替 公共サービス"))
    cash = reconcile(bank("ATM 現金引出"))

    assert income.write_eligibility == "preview_candidate"
    assert expense.write_eligibility == "preview_candidate"
    assert cash.write_eligibility == "withheld"


def parsed_for(*transactions):
    return BankPdfResult(
        pages=1,
        candidate_rows=len(transactions),
        transactions=tuple(transactions),
        issues=(),
        balance_consistency_failures=0,
        duplicate_candidates=0,
        canonical_transactions=tuple(item.to_canonical() for item in transactions),
    )


def test_production_preview_plan_only_admits_income_and_expense():
    transactions = (
        bank("給与 匿名勤務先", 1000, "bank:income"),
        bank("口座振替 公共サービス", -1000, "bank:expense"),
        bank("口座振替 AU PAY カード", -1000, "bank:card"),
        bank("振込 匿名宛先", -1000, "bank:transfer"),
        bank("約定返済", -1000, "bank:loan"),
        bank("ATM 現金引出", -1000, "bank:cash"),
        bank("不明摘要", -1000, "bank:review"),
    )
    shadow = build_bank_shadow_result(parsed_for(*transactions), [])

    plan = build_bank_preview_plan(shadow, [])

    assert plan.parsed == len(transactions)
    assert plan.eligible_before_dedupe == 2
    assert plan.withheld_by_classification == 5
    assert plan.new_plan_candidates == 2
    assert plan.write_attempted == 0


def test_existing_identity_is_removed_from_new_plan():
    transaction = bank("口座振替 公共サービス", identity="bank:existing")
    shadow = build_bank_shadow_result(parsed_for(transaction), [])
    existing = parse_import_rows([
        import_row("bank:existing", "auじぶん銀行PDF", 1000, "accepted"),
    ])

    plan = build_bank_preview_plan(shadow, existing)

    assert plan.eligible_before_dedupe == 1
    assert plan.existing_duplicate == 1
    assert plan.new_plan_candidates == 0
    assert plan.candidate_identities == ()


def test_existing_identity_does_not_change_classification_withheld_count():
    transaction = bank("口座振替 公共サービス", identity="bank:existing")
    existing = parse_import_rows([
        import_row("bank:existing", "auじぶん銀行PDF", 1000, "accepted"),
    ])
    shadow = build_bank_shadow_result(parsed_for(transaction), existing)

    plan = build_bank_preview_plan(shadow, existing)

    assert plan.eligible_before_dedupe == 1
    assert plan.withheld_by_classification == 0
    assert plan.existing_duplicate == 1
    assert plan.new_plan_candidates == 0


def test_identity_collision_is_ambiguous_and_withheld():
    first = bank("口座振替 公共サービスA", identity="bank:collision")
    second = bank("口座振替 公共サービスB", identity="bank:collision")
    shadow = build_bank_shadow_result(parsed_for(first, second), [])

    plan = build_bank_preview_plan(shadow, [])

    assert plan.eligible_before_dedupe == 2
    assert plan.ambiguous_collision == 1
    assert plan.new_plan_candidates == 0
    assert plan.write_attempted == 0


def test_production_preview_plan_replay_is_deterministic():
    transactions = (
        bank("給与 匿名勤務先", 1000, "bank:income"),
        bank("口座振替 公共サービス", -1000, "bank:expense"),
    )
    shadow = build_bank_shadow_result(parsed_for(*transactions), [])

    first = build_bank_preview_plan(shadow, []).summary()
    second = build_bank_preview_plan(shadow, []).summary()

    assert first == second


def test_production_preview_never_invokes_writer(monkeypatch):
    transaction = bank("給与 匿名勤務先", 1000)
    parsed = parsed_for(transaction)

    class ReadOnlyDB:
        def get(self, range_name):
            assert range_name == "取込データ!A2:L"
            return []

        def append(self, *_args, **_kwargs):
            raise AssertionError("production preview reached writer")

        def update_rows(self, *_args, **_kwargs):
            raise AssertionError("production preview reached writer")

    monkeypatch.setattr("app.bank_reconciliation.BankPdfPipeline.parse",
                        lambda *args, **kwargs: parsed)

    result = BankPdfShadowPipeline(ReadOnlyDB()).production_preview(
        "statement.pdf",
    )

    assert result["new_plan_candidates"] == 1
    assert result["write_attempted"] == 0


def test_shadow_cli_uses_read_only_sheets_and_prints_summary(monkeypatch, capsys):
    expected = {
        "read_only": True,
        "classification": {"needs_review": 1},
    }
    read_service = object()
    db = object()
    monkeypatch.setattr("app.cli.Settings.validate", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.cli.Settings.spreadsheet_id", "sheet-id")
    monkeypatch.setattr("app.cli.read_only_sheets_service", lambda: read_service)
    monkeypatch.setattr(
        "app.cli.SheetsDB",
        lambda spreadsheet_id, service=None: db
        if spreadsheet_id == "sheet-id" and service is read_service else None,
    )
    monkeypatch.setattr(BankPdfShadowPipeline, "preview",
                        lambda self, *args, **kwargs: expected)
    monkeypatch.setattr(sys, "argv", [
        "app.cli", "bank-pdf-shadow-preview", "statement.pdf",
    ])

    main()

    assert json.loads(capsys.readouterr().out.strip()) == expected
