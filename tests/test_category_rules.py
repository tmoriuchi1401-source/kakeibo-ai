from datetime import datetime, timezone
from dataclasses import replace

from app.auto_expense import AutoExpensePipeline
from app.category_rule_pipeline import CategoryRuleApprovalPipeline, RuleApprovalRequest
from app.category_rule_ui import CategoryRuleUIPipeline
from app.category_rules import CategoryRule, match_transaction, rule_id_for
from app.reconciliation import parse_import_rows


def import_row(import_id="p1", source="PayPay", merchant="請求名", amount=100, imported_at="2026-09-17T12:00:00+00:00"):
    return [import_id, imported_at, source, import_id, "2026-09-17", merchant, amount,
            "通常払い", "unclassified_paypay", "", "h", ""]


def rule(*, kind="service", source="PayPay", merchant="請求名", category=("食費", "外食"), approved="2026-09-16T00:00:00+00:00", amount=None, active=True):
    base = CategoryRule("", kind, source, "", merchant if kind == "service" else "", merchant if kind != "service" else "",
                        "", "商品A" if kind == "product" else "", amount, category, "M-source",
                        datetime.fromisoformat(approved), 1, active)
    return replace(base, rule_id=rule_id_for(base))


def test_exact_service_rule_is_new_only_and_no_partial_name_match():
    approved = rule()
    categories = {("その他", "未分類"), ("食費", "外食")}
    assert match_transaction([approved], parse_import_rows([import_row()])[0], categories).state == "matched"
    assert match_transaction([approved], parse_import_rows([import_row(merchant="請求名A")])[0], categories).state == "no_match"
    assert match_transaction([approved], parse_import_rows([import_row(imported_at="2026-09-15T00:00:00+00:00")])[0], categories).state == "held"


def test_conflicting_rules_hold_and_inactive_rule_stops_future_application():
    first = rule(category=("食費", "外食"))
    second = rule(category=("交通", "電車"))
    categories = {("食費", "外食"), ("交通", "電車"), ("その他", "未分類")}
    tx = parse_import_rows([import_row()])[0]
    assert match_transaction([first, second], tx, categories).state == "conflict"
    assert match_transaction([rule(active=False)], tx, categories).state == "no_match"


def test_store_total_rejects_product_and_product_requires_specific_identity():
    categories = {("食費", "外食")}
    tx = parse_import_rows([import_row(merchant="店")])[0]
    total = rule(kind="store_total", merchant="店")
    assert match_transaction([total], tx, categories).state == "matched"
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


def test_auto_pipeline_uses_rule_only_when_enabled_and_never_overwrites_existing_category():
    db = RuleDB()
    CategoryRuleApprovalPipeline(db, save_enabled=True, now=lambda: datetime(2026, 9, 16, tzinfo=timezone.utc)).register(
        RuleApprovalRequest("M-source", ("食費", "外食"), "service"))
    # A fresh import is eligible when explicitly enabled.
    class AutoDB:
        def __init__(self): self.rows=[import_row("new")]; self.appended=[]; self.updated=[]
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
    assert enabled.ui[0][7] is False and enabled.ui[0][6] == "service"
    assert CategoryRuleUIPipeline(enabled, ui_enabled=True, save_enabled=False).apply_checked()["state"] == "disabled"
