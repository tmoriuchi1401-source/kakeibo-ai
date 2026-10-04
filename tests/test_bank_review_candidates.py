from dataclasses import replace

import pytest

from app.bank_archive_evidence import completed_bank_postings
from app.bank_income import classify_deposit, income_id
from app.bank_pdf_pipeline import BankParseIssue, parse_jibun_bank_pages
from app.bank_reconciliation import classify_bank_transaction, normalize_bank_description
from app.bank_review_groups import candidates_from_pdf, make_groups
from test_bank_pdf_pipeline import page, transaction_row


def evidence(description="振込 特典", *, saved=False, imported=True, explicit=False):
    parsed = parse_jibun_bank_pages([page(1, transaction_row("2026/09/14", description,
        credit="500", balance="1000"))])
    tx = parsed.transactions[0]
    imports = [[tx.source_row_identity, "", tx.source, tx.source_row_identity, tx.transaction_date,
        tx.description, tx.signed_amount, "銀行口座", "bank_income", "", tx.source_row_hash, ""]] if imported else []
    incomes = [[income_id(tx.source_row_identity), tx.transaction_date, 500, "その他確認済収入",
        tx.description, tx.account_alias, tx.source_row_identity, tx.source,
        "operator_confirmed_income", tx.source_row_hash]] if saved else []
    class DB:
        def get(self, rng):
            return incomes if rng == "収入明細!A2:J" else []
    key = (normalize_bank_description(description), "incoming", tx.account_alias)
    rules = {"confirmed_non_own_classifications": {(*key, "needs_review")} if explicit else frozenset()}
    options = dict(classifications=[classify_bank_transaction(tx, **rules)],
        incomes=[classify_deposit(tx, **rules)], file_id="synthetic_pdf_id", pdf_sha256="b" * 64,
        import_rows=imports, completed_postings=completed_bank_postings(DB(), parsed.transactions, imports),
        income_rows=incomes)
    return parsed, options


def test_bank_income_label_without_ledger_is_still_a_human_candidate():
    parsed, options = evidence()
    rows = candidates_from_pdf(parsed, **options)
    assert len(rows) == 1 and rows[0]["import_exact"] and rows[0]["import_status"] == "bank_income"
    assert not rows[0]["income_exists"] and not rows[0]["saved_income_exact"]
    assert len(make_groups(rows)) == 1


def test_fully_saved_unknown_income_does_not_reenter_meaning_review():
    parsed, options = evidence(saved=True)
    assert candidates_from_pdf(parsed, **options) == []


def test_explicit_operator_review_remains_even_when_ledger_exists():
    parsed, options = evidence(saved=True, explicit=True)
    rows = candidates_from_pdf(parsed, **options)
    assert len(rows) == 1 and rows[0]["rule_match"] == "explicit_review_rule"


def test_confirmed_unposted_interest_is_settlement_not_human_meaning_review():
    parsed, options = evidence(description="普通預金利息", imported=False)
    assert options["incomes"][0].outcome == "confirmed_income"
    assert candidates_from_pdf(parsed, **options) == []


@pytest.mark.parametrize("fault", ["parse", "balance", "zero", "collision"])
def test_parse_balance_or_collision_failure_cannot_offer_a_group_approval(fault):
    parsed, options = evidence()
    if fault == "parse":
        parsed = replace(parsed, issues=(BankParseIssue(1, 1, "amount_invalid"),))
    elif fault == "balance":
        parsed = replace(parsed, balance_consistency_failures=1)
    elif fault == "zero":
        parsed = replace(parsed, transactions=())
    else:
        parsed = replace(parsed, transactions=parsed.transactions * 2)
    with pytest.raises(ValueError):
        candidates_from_pdf(parsed, **options)


def test_duplicate_original_amount_mismatch_cannot_be_approved_as_meaning():
    parsed, options = evidence()
    options["import_rows"][0][6] = 501
    with pytest.raises(ValueError, match="existing_import_mismatch"):
        candidates_from_pdf(parsed, **options)


def test_decisions_from_different_original_cannot_retarget_group():
    parsed, options = evidence()
    options["classifications"] = [replace(options["classifications"][0],
        transaction=replace(parsed.transactions[0], signed_amount=501))]
    with pytest.raises(ValueError, match="original_decision_mismatch"):
        candidates_from_pdf(parsed, **options)
