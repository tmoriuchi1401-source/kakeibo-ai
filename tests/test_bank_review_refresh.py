from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.bank_income import classify_deposit
from app.bank_meaning_resolver import load
from app.bank_meaning_rules import RULE_SHEET
from app.bank_review_refresh import BankReviewRefresh, build_view, run_bank_review_refresh
from app.bank_review_ui import BANK_UI_HEADERS, build_rows
from app.bank_review_groups import digest, validate_snapshot
from test_bank_income import imported
from test_bank_review_requests import prepared, processor, REQUEST


def rig(*, saved_answer=False, future=False):
    db, submitted, source = prepared(future=future)
    if saved_answer:
        processor(db, source).process(submitted, REQUEST)
    source.incomes, source.imports, source.expenses = [], [], []
    source.fingerprint = "a" * 64
    source.files = ({"pdf_sha256": "b" * 64, "parsed": SimpleNamespace(transactions=tuple(source.transactions.values()))},)
    ui = SimpleNamespace(header=BANK_UI_HEADERS.copy(), rows=build_rows(source.groups), writes=[])
    def read():
        ui.before = digest(ui.rows)
        return deepcopy(ui.header), deepcopy(ui.rows)
    def write(rows, *, header):
        if digest(ui.rows) != ui.before:
            raise ValueError("category_workflow_input_changed")
        ui.writes.append((deepcopy(header), deepcopy(rows)))
        ui.header, ui.rows = deepcopy(header), deepcopy(rows)
    db.bank_review_ui_table = read
    db.replace_bank_review_ui_rows = write
    return db, source, ui, submitted


def test_unanswered_group_display_preserves_controls_and_counts_no_financial_write():
    db, source, ui, _ = rig()
    ui.rows[0][2:6] = ["収入", "利息", "今回のみ", True]
    result = BankReviewRefresh(db, lambda: deepcopy(source)).refresh(apply=True)
    assert result["bank_confirmation_groups"] == result["bank_ui_groups"] == 1
    assert result["bank_income_missing"] == result["bank_groups_resolved"] == 0
    assert ui.rows[0][2:6] == ["収入", "利息", "今回のみ", True]
    assert "用途確認 1グループ" in ui.header[1] and len(ui.writes) == 1 and not db.calls


def test_saved_meaning_missing_income_is_settlement_not_a_new_human_question():
    db, source, ui, _ = rig(saved_answer=True)
    tx = next(iter(source.transactions.values()))
    source.imports = [imported(tx)]
    result = BankReviewRefresh(db, lambda: deepcopy(source)).refresh(apply=True)
    assert result["bank_confirmation_groups"] == 0
    assert result["bank_income_missing"] == result["bank_settlement_groups"] == 1
    assert ui.rows[0][2:6] == ["収入", "その他確認済収入", "今回のみ", False]
    assert "用途確定・記帳待ち" in ui.rows[0][1]


def test_saved_income_readback_resolves_group_without_repeating_the_meaning_question():
    db, source, ui, _ = rig(saved_answer=True)
    tx = next(iter(source.transactions.values()))
    source.imports = [imported(tx)]
    source.incomes = [classify_deposit(tx, meaning_resolver=load(db, legacy_rules={})).row()]
    result = BankReviewRefresh(db, lambda: deepcopy(source)).refresh(apply=True)
    assert result["bank_groups_resolved"] == 1 and result["bank_ui_groups"] == 0
    assert result["bank_confirmation_groups"] == result["bank_income_missing"] == 0 and not ui.rows


def test_group_removed_by_completed_posting_can_clear_its_old_saved_controls():
    db, source, ui, submitted = rig(saved_answer=True)
    tx = next(iter(source.transactions.values()))
    source.imports = [imported(tx)]
    source.incomes = [classify_deposit(tx, meaning_resolver=load(db, legacy_rules={})).row()]
    source.groups = []
    ui.rows = deepcopy(submitted)
    ui.rows[0][5] = False
    result = BankReviewRefresh(db, lambda: deepcopy(source)).refresh(apply=True)
    assert not ui.rows and result["bank_groups_resolved"] == 1 and result["bank_stale_groups"] == 0


def test_later_unsent_different_choice_is_not_replaced_by_prior_saved_meaning():
    db, source, ui, _ = rig(saved_answer=True)
    ui.rows[0][2:6] = ["対象外", "", "今回のみ", True]
    result = BankReviewRefresh(db, lambda: deepcopy(source)).refresh(apply=True)
    assert result["bank_confirmation_groups"] == 1
    assert ui.rows[0][2:6] == ["対象外", "", "今回のみ", True]
    assert "未送信の入力を保持" in ui.rows[0][1]


def test_later_sender_edit_is_preserved_without_a_checked_checkbox():
    db, source, ui, submitted = rig(saved_answer=True)
    ui.rows = deepcopy(submitted)
    ui.rows[0][5] = False
    ui.rows[1][2:4] = ["千葉銀行", "chiba-primary"]
    result = BankReviewRefresh(db, lambda: deepcopy(source)).refresh(apply=True)
    assert result["bank_confirmation_groups"] == 1
    assert ui.rows[1][2:4] == ["千葉銀行", "chiba-primary"]
    assert "未送信の入力を保持" in ui.rows[0][1]


def test_partially_entered_later_answer_is_preserved_before_purpose_selection():
    db, source, ui, _ = rig(saved_answer=True)
    ui.rows[0][3:5] = ["利息", "登録する"]
    result = BankReviewRefresh(db, lambda: deepcopy(source)).refresh(apply=True)
    assert result["bank_confirmation_groups"] == 1
    assert ui.rows[0][2:5] == ["未選択", "利息", "登録する"]


@pytest.mark.parametrize("change", ["future", "sender"])
def test_completed_posting_does_not_erase_a_later_unsent_rule_or_sender_edit(change):
    db, source, ui, submitted = rig(saved_answer=True)
    tx = next(iter(source.transactions.values()))
    source.imports = [imported(tx)]
    source.incomes = [classify_deposit(tx, meaning_resolver=load(db, legacy_rules={})).row()]
    source.groups = []
    ui.rows = deepcopy(submitted)
    ui.rows[0][5] = False
    if change == "future":
        ui.rows[0][4] = "登録する"
    else:
        ui.rows[1][2:4] = ["千葉銀行", "chiba-primary"]
    result = BankReviewRefresh(db, lambda: deepcopy(source)).refresh(apply=True)
    assert result["bank_groups_resolved"] == 0 and result["bank_stale_groups"] == 1
    assert ui.rows[0][4] == ("登録する" if change == "future" else "今回のみ")
    assert ui.rows[1][2:4] == (["千葉銀行", "chiba-primary"] if change == "sender" else ["", ""])


def test_income_category_mismatch_is_not_treated_as_fully_settled():
    db, source, ui, _ = rig(saved_answer=True)
    tx = next(iter(source.transactions.values()))
    source.imports = [imported(tx)]
    saved = classify_deposit(tx, meaning_resolver=load(db, legacy_rules={})).row()
    saved[3] = "利息"
    source.incomes = [saved]
    result = BankReviewRefresh(db, lambda: deepcopy(source)).refresh(apply=True)
    assert result["bank_income_conflicts"] == result["bank_confirmation_groups"] == 1
    assert result["bank_groups_resolved"] == 0 and "記帳内容の確認が必要" in ui.rows[0][1]


def test_removed_unsubmitted_original_keeps_input_but_revokes_old_checkbox():
    db, source, ui, _ = rig()
    source.groups, source.transactions = [], {}
    ui.rows[0][2:6] = ["収入", "利息", "登録する", True]
    result = BankReviewRefresh(db, lambda: deepcopy(source)).refresh(apply=True)
    assert result["bank_stale_groups"] == 1 and ui.rows[0][2:5] == ["収入", "利息", "登録する"]
    assert ui.rows[0][5] is False and "対象が変わりました" in ui.rows[0][1]


def test_preview_does_not_refresh_cells_or_write_bank_metadata():
    db, source, ui, _ = rig()
    result = BankReviewRefresh(db, lambda: deepcopy(source)).refresh(apply=False)
    assert result["bank_confirmation_groups"] == 1 and result["bank_ui_updates"] == 0
    assert not ui.writes and not db.calls


@pytest.mark.parametrize("change", ["source", "master", "ui"])
def test_change_during_refresh_is_fail_closed_before_native_ui_write(change):
    db, source, ui, _ = rig(saved_answer=True, future=True)
    calls = []
    def read():
        calls.append(1)
        live = deepcopy(source)
        if len(calls) == 2:
            if change == "source": live.fingerprint = "b" * 64
            if change == "master": db.tables[RULE_SHEET][-1][-2] = False
            if change == "ui": ui.rows[0][2] = "個別確認"
        return live
    with pytest.raises(ValueError):
        BankReviewRefresh(db, read).refresh(apply=True)
    assert not ui.writes


def test_disabled_feature_does_not_read_or_mutate_a_sheet():
    assert run_bank_review_refresh(object(), {}, apply=True) == {"bank_review_disabled": 1}


def test_ui_refresh_checks_actual_checkout_before_credentials_or_sheet_access(monkeypatch):
    from test_bank_review_replay import ENV
    from app.drive_run_state import StateError
    monkeypatch.setattr("subprocess.check_output", lambda *a, **kw: "b" * 40)
    with pytest.raises(StateError, match="production_main_boundary_required"):
        run_bank_review_refresh(object(), ENV, apply=True)


def test_unchanged_refresh_is_no_display_write_and_previous_held_reason_stays_visible():
    db, source, ui, _ = rig()
    ui.rows[0][2:6] = ["収入", "その他確認済収入", "今回のみ", False]
    ui.rows[0][1] += "\n処理結果：確認済みJSONルールと競合しています"
    refresh = BankReviewRefresh(db, lambda: deepcopy(source))
    assert refresh.refresh(apply=True)["bank_ui_updates"] == 1
    assert refresh.refresh(apply=True)["bank_ui_updates"] == 0
    assert len(ui.writes) == 1 and "JSONルールと競合" in ui.rows[0][1]


def nonposting_rig(purpose="対象外"):
    db, source, ui, submitted = rig()
    submitted[0][2:6] = [purpose, "", "今回のみ", True]
    if purpose == "自己口座間振替": submitted[1][2:4] = ["千葉銀行", "chiba-primary"]
    _, output = processor(db, source).process(submitted, REQUEST)
    ui.rows = output
    return db, source, ui


@pytest.mark.parametrize("purpose", ["対象外", "返金", "自己口座間振替"])
@pytest.mark.parametrize("proof", ["exact", "none", "different_sha", "financial_history", "review_status", "later_edit", "ack_edit"])
def test_removed_nonfinancial_row_requires_processed_original_and_no_unresolved_history(purpose, proof):
    db, source, ui = nonposting_rig(purpose)
    tx = next(iter(source.transactions.values()))
    source.groups, source.transactions = [], {}
    snapshot = validate_snapshot(ui.rows[0][11], ui.rows[0][6], ui.rows[0][10])
    member = snapshot["members"][0]
    retired = {member["file_id"]: {"pdf_sha256": member["pdf_sha256"], "fingerprint": "a" * 64}}
    if proof == "none": retired = {}
    if proof == "different_sha": retired[member["file_id"]]["pdf_sha256"] = "other"
    if proof == "financial_history":
        income = ["BI-" + "a" * 24, tx.transaction_date, tx.signed_amount, "その他確認済収入",
            tx.description, tx.account_alias, tx.source_row_identity, tx.source, "operator_confirmed_income", tx.source_row_hash]
        # Use the canonical ID so the ledger remains valid.
        from app.bank_income import income_id
        income[0] = income_id(tx.source_row_identity)
        source.incomes = [income]
    if proof == "review_status": source.imports = [imported(tx, **{"8": "needs_review"})]
    if proof == "later_edit": ui.rows[0][4] = "登録する"
    if proof == "ack_edit": ui.rows[1][4] = True
    result = BankReviewRefresh(db, lambda: deepcopy(source), retirement_reader=lambda *_: retired).refresh(apply=True)
    ready = proof == "exact" or proof == "ack_edit" and purpose == "自己口座間振替"
    assert result["bank_groups_resolved"] == int(ready)
    assert result["bank_stale_groups"] == int(not ready)
    assert len(ui.rows) == (0 if ready else 2)


def test_processed_original_changes_before_refresh_retains_every_existing_cell():
    db, source, ui = nonposting_rig()
    source.groups, source.transactions = [], {}
    before = deepcopy(ui.rows)
    member = validate_snapshot(ui.rows[0][11], ui.rows[0][6], ui.rows[0][10])["members"][0]
    calls = []
    def retired(*_):
        calls.append(1)
        return {member["file_id"]: {"pdf_sha256": member["pdf_sha256"], "fingerprint": str(len(calls))}}
    with pytest.raises(ValueError, match="retirement_changed_before_refresh"):
        BankReviewRefresh(db, lambda: deepcopy(source), retirement_reader=retired).refresh(apply=True)
    assert ui.rows == before and not ui.writes


@pytest.mark.parametrize("status", ["needs_review", "bank_income", "bank_non_expense"])
def test_saved_nonfinancial_meaning_does_not_override_import_settlement_status(status):
    db, source, ui = nonposting_rig()
    tx = next(iter(source.transactions.values()))
    source.imports = [imported(tx, **{"8": status})]
    result = BankReviewRefresh(db, lambda: deepcopy(source)).refresh(apply=True)
    assert result["bank_settlement_groups"] == int(status == "needs_review")
    assert result["bank_confirmation_groups"] == int(status == "bank_income")
    assert result["bank_groups_resolved"] == int(status == "bank_non_expense")
