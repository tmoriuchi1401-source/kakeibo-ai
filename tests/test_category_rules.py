from datetime import datetime, timezone
from dataclasses import replace

from app.auto_expense import AutoExpensePipeline
from app.category_rule_pipeline import CategoryRuleApprovalPipeline, RuleApprovalRequest
from app.category_rule_ui import CategoryRuleUIPipeline
from app.category_rules import CategoryRule, match_transaction, rule_id_for
from app.reconciliation import parse_import_rows
from app.sheets import (
    CATEGORY_RULE_UI_HELPER_A1,
    CATEGORY_RULE_UI_HELPER_END_A1,
    EXPENSE_CATEGORY_HELPER_LEDGER_MINOR_END_A1,
    category_rule_ui_control_requests,
)
from app.sheets_ui_actions import expense_category_validation_requests


def import_row(import_id="p1", source="PayPay", merchant="請求名", amount=100, imported_at="2026-09-17T12:00:00+00:00"):
    return [import_id, imported_at, source, import_id, "2026-09-17", merchant, amount,
            "通常払い", "auto_expense", "M-source", "h", ""]


def rule(*, kind="service", source="PayPay", merchant="請求名", category=("食費", "外食"), approved="2026-09-16T00:00:00+00:00", amount=None, active=True):
    base = CategoryRule("", kind, source, "", merchant if kind == "service" else "", merchant if kind != "service" else "",
                        "", "商品A" if kind == "product" else "", amount, category, "M-source",
                        datetime.fromisoformat(approved), 1, active)
    return replace(base, rule_id=rule_id_for(base))


def test_exact_service_rule_is_new_only_and_no_partial_name_match():
    approved = rule()
    categories = {("その他", "未分類"), ("食費", "外食")}
    assert match_transaction([approved], parse_import_rows([import_row()])[0], categories, aggregate_only=True).state == "matched"
    assert match_transaction([approved], parse_import_rows([import_row(merchant="請求名A")])[0], categories, aggregate_only=True).state == "no_match"
    assert match_transaction([approved], parse_import_rows([import_row(merchant="KDDIご利用料金")])[0], categories, aggregate_only=True).state == "no_match"
    assert match_transaction([approved], parse_import_rows([import_row(imported_at="2026-09-15T00:00:00+00:00")])[0], categories, aggregate_only=True).state == "held"


def test_service_rule_never_matches_product_detail_and_cannot_be_registered_from_one():
    approved = rule()
    tx = parse_import_rows([import_row()])[0]
    assert match_transaction([approved], tx, {("食費", "外食")}, product_name="商品明細").state == "no_match"
    db = RuleDB(); db.expenses["M-source"][1][3] = "商品明細"
    assert CategoryRuleApprovalPipeline(db, save_enabled=True).register(
        RuleApprovalRequest("M-source", ("食費", "外食"), "service")) == {
            "state": "held", "reason": "service_requires_aggregate_only"}


def test_naive_sheet_timestamp_is_interpreted_as_jst_for_approval_boundary():
    approved = rule(approved="2026-09-16T15:30:00+00:00")  # 00:30 JST on 17th
    tx = parse_import_rows([import_row(imported_at="2026-09-17 00:00:00")])[0]
    assert match_transaction([approved], tx, {("食費", "外食")}, aggregate_only=True).state == "held"


def test_conflicting_rules_hold_and_inactive_rule_stops_future_application():
    first = rule(category=("食費", "外食"))
    second = rule(category=("交通", "電車"))
    categories = {("食費", "外食"), ("交通", "電車"), ("その他", "未分類")}
    tx = parse_import_rows([import_row()])[0]
    assert match_transaction([first, second], tx, categories, aggregate_only=True).state == "conflict"
    assert match_transaction([rule(active=False)], tx, categories, aggregate_only=True).state == "no_match"


def test_store_total_rejects_product_and_product_requires_specific_identity():
    categories = {("食費", "外食")}
    tx = parse_import_rows([import_row(merchant="店")])[0]
    total = rule(kind="store_total", merchant="店")
    assert match_transaction([total], tx, categories).state == "no_match"
    assert match_transaction([total], tx, categories, aggregate_only=True).state == "matched"
    product = rule(kind="product", merchant="店")
    assert match_transaction([product], tx, categories).state == "no_match"


def test_namespaced_product_id_matches_one_amazon_item_not_order_total():
    product = CategoryRule("CR-product", "product", "Amazon", "", "", "Amazon.co.jp", "amazon:B001",
                           "個別商品", None, ("食費", "外食"), "M-source",
                           datetime(2026, 9, 16, tzinfo=timezone.utc), 1, True)
    categories = {("食費", "外食")}
    item = parse_import_rows([import_row("amazon:order-1:B001", "Amazon", "Amazon.co.jp")])[0]
    total = parse_import_rows([import_row("amazon:order-1", "Amazon", "Amazon.co.jp")])[0]
    assert match_transaction([product], item, categories).state == "matched"
    assert match_transaction([product], total, categories).state == "no_match"


def test_exact_merchant_and_product_name_never_degrades_to_merchant_only():
    product = CategoryRule("CR-name", "product", "Amazon", "", "", "Amazon.co.jp", "", "商品A", None,
                           ("食費", "外食"), "M-source", datetime(2026, 9, 16, tzinfo=timezone.utc), 1, True)
    tx = parse_import_rows([import_row("amazon:o:B", "Amazon", "Amazon.co.jp")])[0]
    categories = {("食費", "外食")}
    assert match_transaction([product], tx, categories).state == "no_match"
    assert match_transaction([product], tx, categories, product_name="商品A").state == "matched"
    assert match_transaction([product], tx, categories, product_name="商品A増量").state == "no_match"




class RuleDB:
    def __init__(self):
        self.expenses = {"M-source": (2, ["M-source", "2026-09-16", "請求名", "自動計上", 100, "食費", "外食", "", "PayPay", "", "p1", "", "active"])}
        self.imports = [import_row()]
        self.rule_rows = []
        self.calls = []
    def expense_records(self): return self.expenses
    def categories(self): return [("その他", "未分類"), ("食費", "外食")]
    def get(self, rng):
        if rng == "取込データ!A2:L": return self.imports
        if "カテゴリ自動分類ルール!A" in rng: return [self.rule_rows[0]]
        raise AssertionError(rng)
    def category_rules(self): return self.rule_rows
    def ensure_category_rule_sheet(self): self.calls.append("ensure")
    def append(self, sheet, rows): self.rule_rows.extend(rows)
    def update_rows(self, sheet, rows): self.calls.extend(rows)


def test_registration_requires_feature_and_rereads_category_before_exact_rule_save():
    db = RuleDB()
    request = RuleApprovalRequest("M-source", ("食費", "外食"), "service")
    assert CategoryRuleApprovalPipeline(db, save_enabled=False).register(request)["state"] == "disabled"
    result = CategoryRuleApprovalPipeline(db, save_enabled=True, now=lambda: datetime(2026, 9, 16, tzinfo=timezone.utc)).register(request)
    assert result["state"] == "registered" and len(db.rule_rows) == 1
    assert CategoryRuleApprovalPipeline(db, save_enabled=True).register(request)["state"] == "already_registered"
    db.expenses["M-source"][1][5:7] = ["その他", "未分類"]
    assert CategoryRuleApprovalPipeline(db, save_enabled=True).register(request)["reason"] == "category_changed_concurrently"


def test_registration_rejects_review_or_duplicate_source_even_with_matching_expense_id():
    db = RuleDB()
    db.imports[0][8] = "matched_receipt"
    request = RuleApprovalRequest("M-source", ("食費", "外食"), "service")
    assert CategoryRuleApprovalPipeline(db, save_enabled=True).register(request)["reason"] == "source_not_eligible_for_learning"


def test_amount_and_bank_account_mismatch_are_not_widened():
    approved = CategoryRule("CR-bank", "service", "Bank", "account-a", "料金", "", "", "", 100,
                            ("食費", "外食"), "M-source", datetime(2026, 9, 16, tzinfo=timezone.utc), 1, True)
    categories = {("食費", "外食")}
    wrong_amount = parse_import_rows([import_row("bankpdf:bank:account-a:1", "Bank", "料金", 101)])[0]
    wrong_account = parse_import_rows([import_row("bankpdf:bank:account-b:1", "Bank", "料金", 100)])[0]
    assert match_transaction([approved], wrong_amount, categories).state == "no_match"
    assert match_transaction([approved], wrong_account, categories).state == "no_match"
    malformed = replace(approved, account_alias="")
    assert match_transaction([malformed], parse_import_rows([import_row("bankpdf:bank:account-a:1", "Bank", "料金", 100)])[0], categories).state == "no_match"


def test_deactivate_leaves_existing_expense_unchanged_and_stops_future_match():
    db = RuleDB()
    saved = CategoryRuleApprovalPipeline(db, save_enabled=True).register(
        RuleApprovalRequest("M-source", ("食費", "外食"), "service"))
    result = CategoryRuleApprovalPipeline(db, save_enabled=True).deactivate(saved["rule_id"])
    assert result["state"] == "deactivated"
    assert db.expenses["M-source"][1][5:7] == ["食費", "外食"]


def test_auto_pipeline_uses_rule_only_when_enabled_and_never_overwrites_existing_category():
    db = RuleDB()
    CategoryRuleApprovalPipeline(db, save_enabled=True, now=lambda: datetime(2026, 9, 16, tzinfo=timezone.utc)).register(
        RuleApprovalRequest("M-source", ("食費", "外食"), "service"))
    # A fresh import is eligible when explicitly enabled.
    class AutoDB:
        def __init__(self):
            row=import_row("new"); row[8]="unclassified_paypay"; row[9]=""
            self.rows=[row]; self.appended=[]; self.updated=[]
        def get(self, rng): return self.rows
        def categories(self): return [("その他", "未分類"), ("食費", "外食")]
        def category_rules(self): return db.rule_rows
        def expense_index(self): return {}
        def expense_records(self): return {}
        def ensure_expense_status_column(self): pass
        def append(self, sheet, rows): self.appended.extend(rows)
        def update_rows(self, sheet, rows): self.updated.extend(rows)
    disabled = AutoDB(); AutoExpensePipeline(disabled).apply()
    assert disabled.appended[0][5:7] == ["その他", "未分類"]
    enabled = AutoDB(); AutoExpensePipeline(enabled, category_rule_auto_apply_enabled=True).apply()
    assert enabled.appended[0][5:7] == ["食費", "外食"]
    assert "承認ルール=CR-" in enabled.appended[0][11]


def test_mobile_ui_is_opt_in_and_checkbox_is_initially_off():
    db = RuleDB()
    class UIDB(RuleDB):
        def __init__(self): super().__init__(); self.ui=[]
        def category_rule_ui_rows(self): return self.ui
        def ensure_category_rule_ui_sheet(self, header): self.header=header
        def clear(self, rng): self.ui=[]
        def append(self, sheet, rows):
            if sheet == "カテゴリ自動分類": self.ui.extend(rows)
            else: self.rule_rows.extend(rows)
    disabled = UIDB()
    assert CategoryRuleUIPipeline(disabled, ui_enabled=False, save_enabled=False).refresh()["state"] == "disabled"
    enabled = UIDB()
    result = CategoryRuleUIPipeline(enabled, ui_enabled=True, save_enabled=False).refresh()
    assert result["candidates"] == 1
    assert enabled.ui[0][4] is False and enabled.ui[0][6] == "M-source" and enabled.ui[0][10] == "service"
    assert enabled.ui[0][1].endswith("未登録")
    assert CategoryRuleUIPipeline(enabled, ui_enabled=True, save_enabled=False).apply_checked()["state"] == "disabled"
    assert CategoryRuleUIPipeline(enabled, ui_enabled=True, save_enabled=True).apply_checked()["checked"] == 0
    assert enabled.rule_rows == []


def test_mobile_ui_marks_exact_registered_and_conflicting_rule_without_checking_it():
    db = RuleDB()
    CategoryRuleApprovalPipeline(db, save_enabled=True).register(
        RuleApprovalRequest("M-source", ("食費", "外食"), "service"))
    class UIDB(RuleDB):
        def __init__(self): self.__dict__ = db.__dict__; self.ui=[]
        def category_rule_ui_rows(self): return self.ui
        def ensure_category_rule_ui_sheet(self, header): pass
        def clear(self, rng): self.ui=[]
        def append(self, sheet, rows): self.ui.extend(rows)
    ui = UIDB()
    CategoryRuleUIPipeline(ui, ui_enabled=True, save_enabled=True).refresh()
    assert ui.ui[0][4] is False and ui.ui[0][1].endswith("登録済み")


def test_checked_ui_condition_is_never_silently_replaced_on_refresh():
    db = RuleDB()
    class UIDB(RuleDB):
        def __init__(self): self.__dict__ = db.__dict__; self.ui=[]
        def category_rule_ui_rows(self): return self.ui
        def ensure_category_rule_ui_sheet(self, header): pass
        def clear(self, rng): self.ui=[]
        def append(self, sheet, rows): self.ui.extend(rows)
    ui = UIDB()
    pipe = CategoryRuleUIPipeline(ui, ui_enabled=True, save_enabled=True)
    pipe.refresh(); ui.ui[0][4] = True
    ui.imports[0][5] = "変更後の請求名"
    pipe.refresh()
    assert ui.ui[0][4] is False
    assert "再承認が必要" in ui.ui[0][1] and "変更後の請求名" in ui.ui[0][1]


def test_unclassified_group_can_save_only_the_user_selected_future_rule():
    class UIDB(RuleDB):
        def __init__(self):
            super().__init__(); self.ui=[]
            self.expenses["M-source"][1][5:7] = ["その他", "未分類"]
        def category_rule_ui_rows(self): return self.ui
        def ensure_category_rule_ui_sheet(self, header): self.header=header
        def clear(self, rng): self.ui=[]
        def append(self, sheet, rows):
            if sheet == "カテゴリ自動分類": self.ui.extend(rows)
            else: self.rule_rows.extend(rows)
        def update_rows(self, sheet, rows): self.calls.extend(rows)
    db = UIDB(); pipe = CategoryRuleUIPipeline(db, ui_enabled=True, save_enabled=True)
    refreshed = pipe.refresh()
    assert refreshed["unclassified_groups"] == 1
    assert db.ui[0][2:6] == ["", "", False, False]
    # Selection is an input proposal.  Refresh binds it into the fresh
    # snapshot but does not write the ledger or a rule.
    db.ui[0][2:4] = ["食費", "外食"]
    db.ui[0][4] = True
    pipe.refresh()
    assert db.ui[0][4] is True
    assert db.rule_rows == [] and db.expenses["M-source"][1][5:7] == ["その他", "未分類"]
    result = pipe.apply_checked()
    assert result["results"][0][1]["state"] == "registered"
    assert len(db.rule_rows) == 1
    assert db.expenses["M-source"][1][5:7] == ["その他", "未分類"]


def test_rendered_rule_rows_get_row_relative_dropdowns_and_checkboxes():
    requests = category_rule_ui_control_requests(sheet_id=10, helper_sheet_id=11, row_count=3)
    helper = requests[0]["updateCells"]
    assert helper["range"] == {"sheetId": 11, "startRowIndex": 1, "endRowIndex": 4,
                               "startColumnIndex": 701, "endColumnIndex": 702}
    assert "カテゴリ自動分類'!C2" in helper["rows"][0]["values"][0]["userEnteredValue"]["formulaValue"]
    assert "カテゴリ自動分類'!C4" in helper["rows"][2]["values"][0]["userEnteredValue"]["formulaValue"]
    validations = [request["setDataValidation"] for request in requests if "setDataValidation" in request]
    major = next(item for item in validations if item["range"]["startColumnIndex"] == 2)
    checks = next(item for item in validations if item["range"]["startColumnIndex"] == 4)
    minors = [item for item in validations if item["range"]["startColumnIndex"] == 3]
    assert major["rule"]["condition"]["type"] == "ONE_OF_RANGE"
    assert checks["rule"]["condition"]["type"] == "BOOLEAN"
    assert len(minors) == 3 and all(item["rule"]["condition"]["type"] == "ONE_OF_RANGE" for item in minors)
    assert minors[0]["rule"]["condition"]["values"][0]["userEnteredValue"].endswith(
        f"${CATEGORY_RULE_UI_HELPER_A1}$2:${CATEGORY_RULE_UI_HELPER_END_A1}$2"
    )
    assert minors[-1]["rule"]["condition"]["values"][0]["userEnteredValue"].endswith(
        f"${CATEGORY_RULE_UI_HELPER_A1}$4:${CATEGORY_RULE_UI_HELPER_END_A1}$4"
    )
    assert EXPENSE_CATEGORY_HELPER_LEDGER_MINOR_END_A1 == "ZY"


def test_same_row_ledger_and_rule_ui_minor_dropdowns_use_disjoint_helper_ranges():
    """A ledger 食費 row and a rule-UI 通信 row must never share spill cells."""
    ledger_rules = [request["setDataValidation"] for request in expense_category_validation_requests()
                    if "setDataValidation" in request]
    ledger_row_three = next(rule for rule in ledger_rules
                            if rule["range"]["startColumnIndex"] == 6
                            and rule["range"]["startRowIndex"] == 2)
    ui_rules = [request["setDataValidation"] for request in
                category_rule_ui_control_requests(sheet_id=10, helper_sheet_id=11, row_count=3)
                if "setDataValidation" in request]
    ui_row_three = next(rule for rule in ui_rules
                        if rule["range"]["startColumnIndex"] == 3
                        and rule["range"]["startRowIndex"] == 2)
    ledger_source = ledger_row_three["rule"]["condition"]["values"][0]["userEnteredValue"]
    ui_source = ui_row_three["rule"]["condition"]["values"][0]["userEnteredValue"]
    assert ledger_source.endswith("!B3:ZY3")
    assert ui_source.endswith("!$ZZ$3:$ALL$3")

    helper = category_rule_ui_control_requests(sheet_id=10, helper_sheet_id=11, row_count=3)[0]["updateCells"]
    assert "カテゴリ自動分類'!C3" in helper["rows"][1]["values"][0]["userEnteredValue"]["formulaValue"]


def test_classified_past_choice_survives_refresh_and_reaches_past_preview_ui():
    from app.category_backfill_ui import CategoryBackfillUIPipeline

    class CombinedDB(RuleDB):
        def __init__(self): super().__init__(); self.ui=[]; self.backfill=[]
        def category_rule_ui_rows(self): return self.ui
        def category_backfill_ui_rows(self): return self.backfill
        def ensure_category_rule_ui_sheet(self, header): pass
        def ensure_category_backfill_ui_sheet(self, header): pass
        def clear(self, rng):
            if "カテゴリ過去反映" in rng: self.backfill=[]
            else: self.ui=[]
        def append(self, sheet, rows):
            if sheet == "カテゴリ自動分類": self.ui.extend(rows)
            elif sheet == "カテゴリ過去反映": self.backfill.extend(rows)
            else: self.rule_rows.extend(rows)
    db = CombinedDB(); rule_ui = CategoryRuleUIPipeline(db, ui_enabled=True, save_enabled=True)
    rule_ui.refresh(); db.ui[0][5] = True
    rule_ui.refresh()
    assert db.ui[0][5] is True
    backfill = CategoryBackfillUIPipeline(db, ui_enabled=True, apply_enabled=False).refresh()
    assert backfill["conditions"] == 1
    assert db.backfill[0][3] == "表示中の条件（過去分のみ）"


def test_runner_order_preserves_both_choices_until_their_own_processors_consume_them():
    from app.category_backfill_ui import CategoryBackfillUIPipeline

    class RunnerDB(RuleDB):
        def __init__(self):
            super().__init__(); self.ui=[]; self.backfill=[]; self.replacements=[]
            self.expenses["M-source"][1][5:7] = ["その他", "未分類"]
        def category_rule_ui_rows(self): return self.ui
        def category_backfill_ui_rows(self): return self.backfill
        def replace_category_rule_ui_rows(self, rows, header):
            self.replacements.append(rows); self.ui = [list(row) for row in rows]
        def ensure_category_backfill_ui_sheet(self, header): pass
        def clear(self, rng): self.backfill=[]
        def append(self, sheet, rows):
            if sheet == "カテゴリ過去反映": self.backfill.extend(rows)
            else: self.rule_rows.extend(rows)
        def update_rows(self, sheet, rows):
            if sheet == "カテゴリ自動分類":
                for row_num, row in rows: self.ui[row_num-2] = row
            elif sheet == "カテゴリ過去反映":
                for row_num, row in rows: self.backfill[row_num-2] = row
            else: super().update_rows(sheet, rows)
    db = RunnerDB(); rule_ui = CategoryRuleUIPipeline(db, ui_enabled=True, save_enabled=True)
    rule_ui.refresh()
    db.ui[0][2:6] = ["食費", "外食", True, True]
    # First runner stage binds the person's category to a fresh snapshot while
    # retaining both independent choices; it uses replacement, not INSERT_ROWS.
    rule_ui.refresh()
    assert db.replacements and db.ui[0][4:6] == [True, True]
    future = rule_ui.apply_checked()
    assert future["results"][0][1]["state"] == "registered"
    assert db.ui[0][4:6] == [False, True]
    # The subsequent past-preview stage still receives the surviving F check.
    backfill = CategoryBackfillUIPipeline(db, ui_enabled=True, apply_enabled=False).refresh()
    assert backfill["conditions"] == 2  # saved future rule + displayed past-only condition
    assert any(row[3] == "表示中の条件（過去分のみ）" for row in db.backfill)
