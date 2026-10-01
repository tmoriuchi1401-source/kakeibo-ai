from copy import deepcopy
import json

import pytest

from app.bank_review_groups import digest, make_groups, validate_snapshot
from app.bank_review_ui import BANK_MARKER, BANK_UI_HEADERS, build_rows, controls, submitted_answers
from app.category_sheet_requests import merge_results, parse_snapshot
from app.sheets import CATEGORY_WORKFLOW_MARKERS, SheetsDB


def transaction(identity="one", **changes):
    row = dict(identity=identity, source_hash="a" * 64, bank="au-jibun", source="bank",
        account_alias="jibun-primary", direction="incoming", date="2026-01-01",
        description="振込 特典", signed_amount=100, transaction_kind="deposit",
        classification="income", reason="bank_reward", income_outcome="needs_review",
        income_reason="deposit_purpose_unconfirmed", rule_match="no_exact_settling_rule",
        bucket="reward", file_id="synthetic_file_id", page=1, row=2,
        import_exists=False, import_exact=False, import_status="", income_exists=False,
        saved_income_exact=False)
    row.update(changes)
    return row


def base_blocks():
    return {key: (header, []) for key, header in SheetsDB._category_workflow_defaults(None).items()}


def physical(blocks):
    markers = {**CATEGORY_WORKFLOW_MARKERS, "bank": BANK_MARKER}
    rows = []
    for section, (header, data) in blocks.items():
        rows += [[markers[section]] + [""] * 11,
            SheetsDB._workflow_physical_row(section, header, compact=True, header=True)]
        rows += [SheetsDB._workflow_physical_row(section, row, compact=True) for row in data]
    return [["TRUE" if value is True else "FALSE" if value is False else str(value)
             for value in row] for row in rows]


def prepared():
    groups = make_groups([transaction()])
    rows = build_rows(groups)
    rows[0][2:6] = ["収入", "その他確認済収入", "今回のみ", True]
    return groups, rows


def test_exact_groups_keep_members_and_stable_condition_keys():
    one = transaction()
    two = transaction("two", date="2026-01-02", signed_amount=200)
    first = make_groups([one, two])[0]
    assert first["count"] == 2 and first["total_amount"] == 300
    assert first["key"] == make_groups([one])[0]["key"]
    assert first["membership_digest"] != make_groups([one])[0]["membership_digest"]
    assert make_groups([two, one]) == make_groups([one, two])


@pytest.mark.parametrize("changed", [dict(description="振込  特典"), dict(account_alias="jibun-other"),
    dict(counterparty="other"), dict(bank="chiba")])
def test_similar_text_alias_or_counterpart_never_combines(changed):
    assert len(make_groups([transaction(), transaction("two", **changed)])) == 2


def test_automatic_amounts_stay_separate_and_cannot_widen_rule_key():
    groups = make_groups([transaction("one", bucket="automatic", reason="automatic_own_account_deposit",
        signed_amount=40000), transaction("two", bucket="automatic", reason="automatic_own_account_deposit",
        signed_amount=200000)])
    assert len(groups) == 2 and groups[0]["key"] != groups[1]["key"]
    for group in groups:
        assert validate_snapshot(json.dumps(group["snapshot"]), group["key"], group["membership_digest"])


def test_wide_ambiguous_funding_partitions_validate_independently():
    groups = make_groups([transaction("one", bucket="review", reason="ambiguous_incoming_transfer",
        signed_amount=10000), transaction("two", bucket="review", reason="ambiguous_incoming_transfer",
        signed_amount=1000000)])
    assert len(groups) == 2
    for group in groups:
        assert validate_snapshot(json.dumps(group["snapshot"]), group["key"], group["membership_digest"])


@pytest.mark.parametrize("changed", [dict(signed_amount=True), dict(signed_amount=0),
    dict(signed_amount=-100), dict(date="2026-99-99"), dict(transaction_kind="withdrawal")])
def test_invalid_original_values_are_rejected(changed):
    with pytest.raises(ValueError):
        make_groups([transaction(**changed)])


def test_duplicate_identity_cannot_count_twice():
    with pytest.raises(ValueError, match="duplicate_review_identity"):
        make_groups([transaction(), transaction()])


def test_same_condition_different_current_evidence_is_conflict():
    with pytest.raises(ValueError, match="condition_conflict"):
        make_groups([transaction(), transaction("two", reason="operator_confirmed_non_own_review")])


def test_answer_reuse_requires_current_snapshot():
    groups, rows = prepared()
    assert build_rows(groups, rows)[0][2:6] == rows[0][2:6]
    changed = make_groups([transaction(import_exists=True, import_status="bank_income")])
    assert changed[0]["key"] == groups[0]["key"]
    assert build_rows(changed, rows)[0][2:6] == ["未選択", "", "未選択", False]


def test_only_checked_rows_submit_no_future_consent_is_inferred():
    _, rows = prepared()
    answers = submitted_answers(rows, accounts=())
    assert len(answers) == 1 and answers[0]["choice"] == "income" and not answers[0]["future"]
    rows[0][5] = False
    assert submitted_answers(rows, accounts=()) == []
    rows[0][5] = "TRUE"
    rows[0][4] = "登録する"
    assert submitted_answers(rows, accounts=())[0]["future"]


def test_transfer_needs_trusted_source_bank_alias_no_account_numbers():
    _, rows = prepared()
    rows[0][2:5] = ["自己口座間振替", "", "登録する"]
    with pytest.raises(ValueError, match="source_account_unconfirmed"):
        submitted_answers(rows, accounts=())
    rows[1][2:4] = ["chiba", "chiba-primary"]
    assert submitted_answers(rows, accounts={("chiba", "chiba-primary")})[0]["source_alias"] == "chiba-primary"
    rows[1][3] = "1234567"
    with pytest.raises(ValueError, match="source_account_unconfirmed"):
        submitted_answers(rows, accounts={("chiba", "1234567")})


def test_new_source_bank_and_alias_need_explicit_captured_ownership_confirmation():
    _, rows = prepared()
    rows[0][2:5] = ["自己口座間振替", "", "今回のみ"]
    rows[1][2:4] = ["確認用銀行", "another-primary"]
    with pytest.raises(ValueError, match="source_account_unconfirmed"):
        submitted_answers(rows, accounts=())
    rows[1][4] = True
    answer = submitted_answers(rows, accounts=())[0]
    assert (answer["source_bank"], answer["source_alias"]) == ("確認用銀行", "another-primary")
    rows[0][5] = False
    assert submitted_answers(rows, accounts=()) == []


@pytest.mark.parametrize("column,value", [(2, "1234567"), (3, "1234567"), (2, "確認用銀行1234567")])
def test_owner_checkbox_does_not_accept_account_numbers(column, value):
    _, rows = prepared()
    rows[0][2:5] = ["自己口座間振替", "", "今回のみ"]
    rows[1][2:5] = ["確認用銀行", "another-primary", True]
    rows[1][column] = value
    with pytest.raises(ValueError, match="source_account_unconfirmed"):
        submitted_answers(rows, accounts=())


def test_source_ownership_checkbox_cannot_apply_to_income_or_submit_alone():
    _, rows = prepared()
    rows[1][4] = True
    with pytest.raises(ValueError, match="source_account_not_applicable"):
        submitted_answers(rows, accounts=())
    rows[1][4] = 1
    with pytest.raises(ValueError, match="rows_invalid"):
        submitted_answers(rows, accounts=())


def test_ownership_input_is_retained_only_with_the_exact_group_snapshot():
    groups, rows = prepared()
    rows[1][2:5] = ["確認用銀行", "another-primary", True]
    assert build_rows(groups, rows)[1][2:5] == rows[1][2:5]
    changed = make_groups([transaction(import_exists=True, import_status="bank_income")])
    assert build_rows(changed, rows)[1][2:5] == ["", "", False]


def test_income_requires_existing_taxonomy_and_not_on_transfer():
    _, rows = prepared()
    rows[0][3] = "新しい分類"
    with pytest.raises(ValueError, match="income_category_missing"):
        submitted_answers(rows, accounts=())
    rows[0][2] = "自己口座間振替"
    with pytest.raises(ValueError, match="not_applicable"):
        submitted_answers(rows, accounts=())


def test_snapshot_amount_changed_even_with_recomputed_digest_cannot_retarget_key():
    _, rows = prepared()
    proof = json.loads(rows[0][11])
    proof["condition"]["description"] = "別の摘要"
    rows[0][11] = json.dumps(proof)
    rows[0][10] = rows[1][10] = digest(proof)
    with pytest.raises(ValueError, match="snapshot_invalid"):
        submitted_answers(rows, accounts=())


def test_optional_bank_block_roundtrip_and_no_confirm_contamination():
    _, rows = prepared()
    blocks = base_blocks()
    blocks["bank"] = (BANK_UI_HEADERS, rows)
    captured = parse_snapshot(physical(blocks))
    assert not captured["confirm"][1]
    assert len(captured["bank"][1]) == 2
    assert submitted_answers(captured["bank"][1], accounts=())[0]["choice"] == "income"
    assert "bank" not in parse_snapshot(physical(base_blocks()))


def test_historical_category_submission_preserves_new_bank_approvals():
    _, rows = prepared()
    captured = base_blocks()
    live = deepcopy(captured)
    live["bank"] = (BANK_UI_HEADERS, rows)
    merged, _ = merge_results(captured, captured, live)
    assert merged["bank"] == live["bank"]


def test_bank_later_user_edits_are_unsubmitted_and_retained():
    _, rows = prepared()
    captured = base_blocks()
    captured["bank"] = (BANK_UI_HEADERS, rows)
    result, live = deepcopy(captured), deepcopy(captured)
    result["bank"][1][0][5] = False
    live["bank"][1][0][2:5] = ["返金", "", "今回のみ"]
    merged, retained = merge_results(captured, result, live)
    assert merged["bank"][1][0][2:6] == ["返金", "", "今回のみ", True]
    assert retained == 1


def test_bank_positions_are_after_confirm_and_inputs_stay_within_six_columns():
    _, rows = prepared()
    blocks = base_blocks()
    blocks["bank"] = (BANK_UI_HEADERS, rows)
    db = object.__new__(SheetsDB)
    db.sheet_titles = lambda: ["_カテゴリ実行受付"]
    positions = db._workflow_positions(blocks)
    assert positions["bank"]["marker"] > positions["confirm"]["start"]
    requests = controls(42, positions["bank"]["start"], rows)
    validations = [r["setDataValidation"] for r in requests if "setDataValidation" in r]
    assert all(v["range"]["endColumnIndex"] <= 6 for v in validations)
    assert any(v["rule"]["condition"]["type"] == "BOOLEAN" for v in validations)


def test_sender_row_cannot_submit_independently():
    _, rows = prepared()
    rows[1][5] = True
    with pytest.raises(ValueError, match="rows_invalid"):
        submitted_answers(rows, accounts=())


def test_checkbox_display_text_is_not_a_later_edit_and_completed_check_clears():
    _, rows = prepared()
    live = base_blocks()
    live["bank"] = (BANK_UI_HEADERS, rows)
    captured = parse_snapshot(physical(live))
    result = deepcopy(captured)
    result["bank"][1][0][5] = False
    merged, retained = merge_results(captured, result, live)
    assert merged["bank"][1][0][5] is False
    assert retained == 0


def test_sheet_reader_keeps_bank_rows_outside_confirmation_and_rejects_duplicate_marker():
    _, rows = prepared()
    source = base_blocks()
    source["bank"] = (BANK_UI_HEADERS, rows)
    values = physical(source)
    db = object.__new__(SheetsDB)
    db.sheet_titles = lambda: ["カテゴリ操作"]
    db._compact_category_helper = lambda: False
    db.get = lambda _: deepcopy(values)
    blocks = db._category_workflow_blocks()
    assert not blocks["confirm"][1] and len(blocks["bank"][1]) == 2
    values.append([BANK_MARKER] + [""] * 11)
    from app.drive_run_state import StateError
    with pytest.raises(StateError, match="markers_invalid"):
        db._category_workflow_blocks()


def test_unwired_bank_approvals_fail_before_category_writer_runs():
    from test_category_sheet_requests import prepared as category_prepared, blocks as category_blocks, Store, REQUEST, ENV
    from app.category_sheet_requests import execute_request
    from app.drive_run_state import StateError
    db, _ = category_prepared()
    captured = category_blocks(db)
    _, rows = prepared()
    captured["bank"] = (BANK_UI_HEADERS, rows)
    store = Store(captured)
    with pytest.raises(StateError, match="snapshot_invalid"):
        execute_request(db, REQUEST, env=ENV, store=store, refresh_projection=lambda: pytest.fail("refresh"))
    assert not db.rule_rows and not db.category_updates
    assert store.states == ["error"]
