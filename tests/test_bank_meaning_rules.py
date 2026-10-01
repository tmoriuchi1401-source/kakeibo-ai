from dataclasses import replace

import pytest

from app.bank_meaning_rules import evaluate, from_answer, parse_rows, plan_registration
from app.bank_pdf_pipeline import NormalizedBankTransaction, SOURCE
from app.bank_review_ui import submitted_answers
from test_bank_review_ui import prepared

NOW = "2026-10-01T01:00:00+00:00"


def rule_and_tx():
    _, rows = prepared()
    rows[0][4] = "登録する"
    answer = submitted_answers(rows, accounts=())[0]
    rule = from_answer(answer, approved_at=NOW)
    tx = NormalizedBankTransaction(SOURCE, "jibun-primary", "2026-01-01", "振込 特典", 100,
        1, 2, "bankpdf:au-jibun:jibun-primary:" + "a" * 24, "a" * 64, "deposit")
    return rule, tx, answer


def test_rule_schema_roundtrip_uses_existing_income_taxonomy():
    rule, _, _ = rule_and_tx()
    assert parse_rows([rule.row()]) == [rule]
    row = rule.row()
    row[15:18] = ["1", "TRUE", "0"]
    assert parse_rows([row]) == [rule]
    with pytest.raises(ValueError, match="income_category_invalid"):
        replace(rule, income_category="銀行特典").validate()


def test_raw_description_alias_direction_are_exact():
    rule, tx, _ = rule_and_tx()
    assert evaluate(tx, [rule])["state"] == "matched"
    for changed in (replace(tx, description="振込  特典"), replace(tx, account_alias="other"),
                    replace(tx, signed_amount=-100, transaction_kind="withdrawal")):
        assert evaluate(changed, [rule])["state"] == "no_sheet_rule"


def test_amount_partitions_do_not_match_different_amount():
    from app.bank_meaning_rules import rule_id
    from app.bank_review_groups import digest
    rule, tx, _ = rule_and_tx()
    condition = {**rule.condition(), "amount_partition": 100}
    rule = replace(rule, amount_partition=100, rule_id=rule_id(condition),
                   approved_group_key="bank-group:" + digest(condition))
    assert evaluate(tx, [rule])["state"] == "matched"
    assert evaluate(replace(tx, signed_amount=200), [rule])["state"] == "no_sheet_rule"


def test_conflicting_sheet_meanings_always_hold():
    rule, tx, _ = rule_and_tx()
    other = replace(rule, classification="reimbursement", income_category="")
    assert evaluate(tx, [rule, other]) == {"state": "held", "reason": "bank_meaning_rule_conflict"}


@pytest.mark.parametrize("legacy", ["transfer", "reimbursement", "needs_review", "expense"])
def test_legacy_json_sheet_conflict_has_no_precedence(legacy):
    rule, tx, _ = rule_and_tx()
    key = (rule.normalized_description, rule.direction, rule.account_alias)
    assert evaluate(tx, [rule], confirmed_non_own_classifications={(*key, legacy)}) == {
        "state": "held", "reason": "bank_meaning_legacy_conflict"}


def test_agreeing_legacy_income_and_sheet_rule_are_compatible():
    rule, tx, _ = rule_and_tx()
    key = (rule.normalized_description, rule.direction, rule.account_alias, "income")
    assert evaluate(tx, [rule], confirmed_non_own_classifications={key})["state"] == "matched"


def test_income_category_conflict_is_not_silently_other_income():
    rule, tx, _ = rule_and_tx()
    rule = replace(rule, income_category="利息")
    key = (rule.normalized_description, rule.direction, rule.account_alias, "income")
    assert evaluate(tx, [rule], confirmed_non_own_classifications={key})["state"] == "held"


def test_transfer_source_conflict_and_inactive_rule_hold_or_do_not_apply():
    rule, tx, _ = rule_and_tx()
    transfer = replace(rule, classification="transfer", income_category="", source_bank="chiba",
                       source_alias="chiba-primary")
    other = replace(transfer, source_bank="docomo-smtb", source_alias="docomo-smtb-primary")
    assert evaluate(tx, [transfer, other])["state"] == "held"
    assert evaluate(tx, [replace(transfer, active=False)])["state"] == "no_sheet_rule"


def test_rule_registration_idempotent_revives_revision_but_never_overwrites_meaning():
    rule, _, answer = rule_and_tx()
    assert plan_registration(answer, [], approved_at=NOW)["state"] == "append"
    assert plan_registration(answer, [rule], approved_at=NOW)["state"] == "already_registered"
    inactive = replace(rule, active=False, revision=3, applied_count=17)
    revived = plan_registration(answer, [inactive], approved_at=NOW)
    assert revived["state"] == "revive" and revived["rule"].revision == 4
    assert revived["rule"].applied_count == 17
    conflict = replace(rule, classification="reimbursement", income_category="")
    assert plan_registration(answer, [conflict], approved_at=NOW)["state"] == "held"


def test_once_only_is_never_a_future_rule():
    _, _, answer = rule_and_tx()
    answer["future"] = False
    with pytest.raises(ValueError, match="future_not_approved"):
        from_answer(answer, approved_at=NOW)


def test_duplicate_rule_ids_and_invalid_active_values_fail_closed():
    rule, _, _ = rule_and_tx()
    with pytest.raises(ValueError, match="duplicate_id"):
        parse_rows([rule.row(), rule.row()])
    row = rule.row()
    row[16] = "true"
    with pytest.raises(ValueError, match="active_invalid"):
        parse_rows([row])
