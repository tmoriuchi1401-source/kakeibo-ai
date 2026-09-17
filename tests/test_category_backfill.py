from datetime import datetime, timezone
from pathlib import Path

from app.category_backfill import (
    BACKFILL_REQUEST_SHEET, BACKFILL_TARGET_SHEET, BackfillSpec, CategoryBackfillPipeline,
)
from app.category_rules import CategoryRule
from app.category_backfill_ui import _period, condition_label


def import_row(import_id="p1", merchant="請求名", amount=100, target="M-one"):
    return [import_id, "2026-09-17T12:00:00+00:00", "PayPay", import_id, "2026-08-10", merchant, amount,
            "通常払い", "auto_expense", target, "h", ""]


class BackfillDB:
    def __init__(self):
        self.expenses = {
            "M-one": (2, ["M-one", "2026-08-10", "請求名", "自動計上", 100, "その他", "未分類", "", "PayPay", "", "p1", "", "active"]),
            "M-partial": (3, ["M-partial", "2026-08-11", "請求名", "自動計上", 100, "食費", "", "", "PayPay", "", "p1", "", "active"]),
        }
        self.imports = [import_row()]
        self.requests=[]; self.targets=[]; self.category_updates=[]; self.rules=[]
        self.category_pairs=[("その他", "未分類"), ("食費", "外食")]
    def categories(self): return self.category_pairs
    def expense_records(self): return self.expenses
    def get(self, rng):
        if rng == "取込データ!A2:L": return self.imports
        raise AssertionError(rng)
    def ensure_category_backfill_sheets(self): pass
    def append(self, sheet, rows):
        (self.requests if sheet == BACKFILL_REQUEST_SHEET else self.targets).extend(rows)
    def category_backfill_requests(self): return self.requests
    def category_backfill_targets(self): return self.targets
    def category_rules(self): return [rule.to_row() for rule in self.rules]
    def update_rows(self, sheet, rows):
        destination = self.requests if sheet == BACKFILL_REQUEST_SHEET else self.targets
        for row_num, row in rows: destination[row_num - 2] = row
    def update_expense_categories(self, rows):
        self.category_updates.extend(rows)
        for row_num, major, minor in rows:
            for _, (known_row, expense) in self.expenses.items():
                if known_row == row_num: expense[5:7] = [major, minor]


def condition():
    return CategoryRule("adhoc", "service", "PayPay", "", "請求名", "", "", "", None,
                        ("食費", "外食"), "M-one", datetime(2026, 9, 1, tzinfo=timezone.utc), 1, True)


def service_rule(rule_id, merchant, category):
    return CategoryRule(rule_id, "service", "PayPay", "", merchant, "", "", "", None,
                        category, "M-A", datetime(2026, 9, 1, tzinfo=timezone.utc), 1, True)


def two_service_candidates():
    db = BackfillDB()
    db.category_pairs.extend([("水道・光熱", "ガス"), ("交通", "電車")])
    db.expenses = {
        "M-A": (2, ["M-A", "2026-08-10", "A喫茶店", "自動計上", 500, "その他", "未分類", "", "PayPay", "", "pA", "", "active"]),
        "M-B": (3, ["M-B", "2026-08-11", "Bガス料金", "自動計上", 600, "その他", "未分類", "", "PayPay", "", "pB", "", "active"]),
    }
    db.imports = [import_row("pA", "A喫茶店", 500, "M-A"), import_row("pB", "Bガス料金", 600, "M-B")]
    return db, service_rule("selected-A", "A喫茶店", ("食費", "外食")), service_rule("saved-B", "Bガス料金", ("水道・光熱", "ガス"))


def test_backfill_preview_is_immutable_and_apply_touches_only_previewed_fallback_ids():
    db = BackfillDB()
    pipe = CategoryBackfillPipeline(db, preview_enabled=True, apply_enabled=True,
                                    now=lambda: datetime(2026, 9, 17, tzinfo=timezone.utc), id_factory=lambda: "CB-1")
    preview = pipe.preview(BackfillSpec(condition(), "2026-08-01", "2026-08-31"))
    assert preview["state"] == "previewed" and preview["targets"] == 1
    assert preview["excluded"]["partial_or_invalid_category"] == 1
    # A later matching import is not part of the request and cannot be added by apply.
    db.expenses["M-later"] = (4, ["M-later", "2026-08-12", "請求名", "自動計上", 100, "その他", "未分類", "", "PayPay", "", "p1", "", "active"])
    assert pipe.confirm("CB-1", expected_count=1)["state"] == "confirmed"
    result = pipe.apply("CB-1", expected_count=1)
    assert result["state"] == "complete" and result["applied"] == 1
    assert db.expenses["M-one"][1][5:7] == ["食費", "外食"]
    assert db.expenses["M-later"][1][5:7] == ["その他", "未分類"]


def test_backfill_apply_holds_changed_source_and_restore_preserves_later_human_edit():
    db = BackfillDB()
    pipe = CategoryBackfillPipeline(db, preview_enabled=True, apply_enabled=True,
                                    now=lambda: datetime(2026, 9, 17, tzinfo=timezone.utc), id_factory=lambda: "CB-2")
    pipe.preview(BackfillSpec(condition(), "2026-08-01", "2026-08-31")); pipe.confirm("CB-2", expected_count=1)
    db.imports[0][5] = "changed"
    result = pipe.apply("CB-2", expected_count=1)
    assert result["state"] == "partial" and result["skipped"] == {"source_changed_concurrently": 1}
    db.imports[0][5] = "請求名"
    # Resume after the source is restored; the same target row is retried.
    assert pipe.apply("CB-2", expected_count=1)["applied"] == 1
    db.expenses["M-one"][1][5:7] = ["その他", "未分類"]
    restored = pipe.restore("CB-2")
    assert restored["restored"] == 0 and restored["skipped"] == {"edited_after_apply": 1}


def test_backfill_zero_preview_and_flags_never_apply():
    db = BackfillDB()
    disabled = CategoryBackfillPipeline(db, preview_enabled=False, apply_enabled=False)
    assert disabled.preview(BackfillSpec(condition()))["state"] == "disabled"
    enabled = CategoryBackfillPipeline(db, preview_enabled=True, apply_enabled=False)
    assert enabled.preview(BackfillSpec(condition(), "2024-01-01", "2024-01-31"))["state"] == "preview_empty"


def test_saved_rule_revision_change_holds_fixed_request_before_any_ledger_write():
    db = BackfillDB()
    saved = CategoryRule("CR-saved", "service", "PayPay", "", "請求名", "", "", "", None,
                         ("食費", "外食"), "M-one", datetime(2026, 9, 1, tzinfo=timezone.utc), 1, True)
    db.rules = [saved]
    pipe = CategoryBackfillPipeline(db, preview_enabled=True, apply_enabled=True,
                                    now=lambda: datetime(2026, 9, 17, tzinfo=timezone.utc), id_factory=lambda: "CB-3")
    assert pipe.preview(BackfillSpec(saved, "2026-08-01", "2026-08-31", saved_rule=True))["state"] == "previewed"
    pipe.confirm("CB-3", expected_count=1)
    db.rules = [CategoryRule("CR-saved", "service", "PayPay", "", "請求名", "", "", "", None,
                             ("食費", "外食"), "M-one", datetime(2026, 9, 1, tzinfo=timezone.utc), 2, True)]
    assert pipe.apply("CB-3", expected_count=1) == {"state": "held", "reason": "rule_or_category_changed"}
    assert db.category_updates == []


def test_selected_condition_only_targets_its_own_transaction_despite_other_rules():
    db, selected, other = two_service_candidates()
    pipe = CategoryBackfillPipeline(db, preview_enabled=True, apply_enabled=True, id_factory=lambda: "CB-A")
    db.rules = [other]
    result = pipe.preview(BackfillSpec(selected, "2026-08-01", "2026-08-31"))
    assert result["state"] == "previewed" and result["targets"] == 1
    assert [row[1] for row in db.targets] == ["M-A"]
    assert db.targets[0][7:9] == ["食費", "外食"]
    assert pipe.confirm("CB-A", expected_count=1)["state"] == "confirmed"
    applied = pipe.apply("CB-A", expected_count=1)
    assert applied["applied"] == 1 and db.expenses["M-B"][1][5:7] == ["その他", "未分類"]


def test_other_same_category_rule_never_adds_its_own_transaction_to_selected_preview():
    db, selected, other = two_service_candidates()
    db.rules = [service_rule(other.rule_id, other.billing_name, ("食費", "外食"))]
    result = CategoryBackfillPipeline(db, preview_enabled=True, apply_enabled=False).preview(
        BackfillSpec(selected, "2026-08-01", "2026-08-31"))
    assert result["targets"] == 1 and [row[1] for row in db.targets] == ["M-A"]


def test_other_rule_match_cannot_create_target_when_selected_condition_matches_zero():
    db, selected, other = two_service_candidates()
    db.expenses.pop("M-A"); db.imports = [db.imports[1]]; db.rules = [other]
    result = CategoryBackfillPipeline(db, preview_enabled=True, apply_enabled=False).preview(
        BackfillSpec(selected, "2026-08-01", "2026-08-31"))
    assert result["state"] == "preview_empty" and result["excluded"]["condition_not_matched"] == 1


def test_selected_match_conflicts_only_when_another_valid_rule_matches_same_transaction():
    db, selected, _ = two_service_candidates()
    db.rules = [service_rule("conflict", "A喫茶店", ("交通", "電車"))]
    result = CategoryBackfillPipeline(db, preview_enabled=True, apply_enabled=False).preview(
        BackfillSpec(selected, "2026-08-01", "2026-08-31"))
    assert result["state"] == "preview_empty" and result["excluded"]["conflicting_active_rule"] == 1
    # An invalid category pair is ignored by the shared rule matcher.
    db.rules = [service_rule("invalid", "A喫茶店", ("不存在", "無効"))]
    result = CategoryBackfillPipeline(db, preview_enabled=True, apply_enabled=False).preview(
        BackfillSpec(selected, "2026-08-01", "2026-08-31"))
    assert result["targets"] == 1


def test_same_category_rules_apply_one_target_once_and_new_conflict_skips_apply():
    db, selected, _ = two_service_candidates()
    db.rules = [service_rule("same", "A喫茶店", ("食費", "外食"))]
    pipe = CategoryBackfillPipeline(db, preview_enabled=True, apply_enabled=True, id_factory=lambda: "CB-same")
    assert pipe.preview(BackfillSpec(selected, "2026-08-01", "2026-08-31"))["targets"] == 1
    assert pipe.confirm("CB-same", expected_count=1)["state"] == "confirmed"
    assert pipe.apply("CB-same", expected_count=1)["applied"] == 1
    assert len(db.category_updates) == 1
    # A distinct request demonstrates the apply-time conflict readback.
    db, selected, _ = two_service_candidates()
    pipe = CategoryBackfillPipeline(db, preview_enabled=True, apply_enabled=True, id_factory=lambda: "CB-conflict")
    pipe.preview(BackfillSpec(selected, "2026-08-01", "2026-08-31")); pipe.confirm("CB-conflict", expected_count=1)
    db.rules = [service_rule("late-conflict", "A喫茶店", ("交通", "電車"))]
    result = pipe.apply("CB-conflict", expected_count=1)
    assert result["applied"] == 0 and result["skipped"] == {"conflicting_active_rule": 1}
    assert db.category_updates == []


def test_saved_rule_condition_change_with_same_revision_holds_fixed_request():
    db = BackfillDB()
    saved = CategoryRule("CR-saved", "service", "PayPay", "", "請求名", "", "", "", None,
                         ("食費", "外食"), "M-one", datetime(2026, 9, 1, tzinfo=timezone.utc), 1, True)
    db.rules = [saved]
    pipe = CategoryBackfillPipeline(db, preview_enabled=True, apply_enabled=True, id_factory=lambda: "CB-condition")
    pipe.preview(BackfillSpec(saved, "2026-08-01", "2026-08-31", saved_rule=True)); pipe.confirm("CB-condition", expected_count=1)
    db.rules = [service_rule("CR-saved", "変更後の請求名", ("食費", "外食"))]
    assert pipe.apply("CB-condition", expected_count=1) == {"state": "held", "reason": "rule_or_category_changed"}


def test_sheet_runner_requires_independent_opt_in_and_never_starts_imports():
    workflow = Path(".github/workflows/category-backfill-sheet.yml").read_text(encoding="utf-8")
    assert "CATEGORY_BACKFILL_SHEET_RUNNER_ENABLED == 'true'" in workflow
    assert "CATEGORY_RULE_UI_ENABLED" in workflow
    assert "env.CATEGORY_RULE_UI_ENABLED == 'true'" in workflow
    assert "category-rule-ui-refresh" in workflow
    assert "CATEGORY_BACKFILL_PREVIEW_ENABLED" in workflow
    assert "CATEGORY_BACKFILL_APPLY_ENABLED" in workflow
    assert "category-backfill-preview-checked" in workflow
    assert "category-backfill-apply-confirmed" in workflow
    assert workflow.index("category-rule-ui-refresh") < workflow.index("category-backfill-ui-refresh")
    assert "app.cli aupay-gmail" not in workflow
    assert "app.cli drive-" not in workflow


def test_month_range_and_changed_request_snapshot_require_a_new_preview():
    assert _period("2026-02..2026-03") == ("2026-02-01", "2026-03-31")
    db = BackfillDB()
    pipe = CategoryBackfillPipeline(db, preview_enabled=True, apply_enabled=True,
                                    now=lambda: datetime(2026, 9, 17, tzinfo=timezone.utc), id_factory=lambda: "CB-4")
    assert pipe.preview(BackfillSpec(condition(), "2026-08-01", "2026-08-31"))["state"] == "previewed"
    db.requests[0][5] = "2026-09-30"  # Simulates a changed fixed period in the request record.
    assert pipe.confirm("CB-4", expected_count=1) == {"state": "held", "reason": "request_snapshot_changed"}


def test_backfill_rejects_invalid_period_and_conflicting_active_rule():
    db = BackfillDB(); pipe = CategoryBackfillPipeline(db, preview_enabled=True, apply_enabled=False)
    assert pipe.preview(BackfillSpec(condition(), "2026-08-01oops", "2026-08-31")) == {"state": "held", "reason": "invalid_period"}
    assert pipe.preview(BackfillSpec(condition(), "2026-08-31", "2026-08-01")) == {"state": "held", "reason": "invalid_period"}
    db.category_pairs.append(("交通", "電車"))
    db.rules = [CategoryRule("CR-conflict", "service", "PayPay", "", "請求名", "", "", "", None,
                             ("交通", "電車"), "M-one", datetime(2026, 9, 1, tzinfo=timezone.utc), 1, True)]
    result = pipe.preview(BackfillSpec(condition(), "2026-08-01", "2026-08-31"))
    assert result["state"] == "preview_empty" and result["excluded"]["conflicting_active_rule"] == 1


def test_confirmation_condition_label_exposes_billing_merchant_and_limiters():
    label = condition_label({"kind": "service", "source": "PayPay", "account_alias": "main",
                             "billing_name": "請求名", "merchant": "店舗名", "product_id": "amazon:B1",
                             "product_name": "商品", "amount": 1200})
    assert "請求名=請求名" in label and "店舗名=店舗名" in label
    assert "口座=main" in label and "商品ID=amazon:B1" in label and "金額=1200円" in label
