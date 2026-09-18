from datetime import datetime, timezone
from pathlib import Path
from httplib2 import Response
from googleapiclient.errors import HttpError
import pytest

from app.category_backfill import (
    BACKFILL_REQUEST_SHEET, BACKFILL_TARGET_SHEET, BackfillSpec, CategoryBackfillPipeline,
)
from app.category_rules import CategoryRule
from app.category_backfill_ui import CategoryBackfillUIPipeline, _month_period, _period, condition_label
from app.sheets import SheetsDB


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


def test_backfill_digest_survives_sheet_numeric_round_trip_before_confirmation():
    db = BackfillDB()
    pipe = CategoryBackfillPipeline(db, preview_enabled=True, apply_enabled=True,
                                    now=lambda: datetime(2026, 9, 17, tzinfo=timezone.utc), id_factory=lambda: "CB-round")
    assert pipe.preview(BackfillSpec(condition(), "2026-08-01", "2026-08-31"))["state"] == "previewed"
    # Values API reads these Sheet numbers back as strings; this is not a
    # modification of the immutable fixed target.
    db.targets[0][2] = str(db.targets[0][2])
    db.targets[0][4] = str(db.targets[0][4])
    assert pipe.confirm("CB-round", expected_count=1)["state"] == "confirmed"
    assert pipe.apply("CB-round", expected_count=1)["applied"] == 1


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
    assert "CATEGORY_RULE_SAVE_ENABLED" in workflow
    assert "env.CATEGORY_RULE_UI_ENABLED == 'true'" in workflow
    assert "category-rule-ui-refresh" in workflow
    assert "category-rule-ui-apply" in workflow
    assert "CATEGORY_BACKFILL_PREVIEW_ENABLED" in workflow
    assert "CATEGORY_BACKFILL_APPLY_ENABLED" in workflow
    assert "category-backfill-preview-checked" in workflow
    assert "category-backfill-apply-confirmed" in workflow
    assert "display_only" in workflow
    assert workflow.count("github.event.inputs.display_only != 'true'") == 3
    assert workflow.index("category-rule-ui-refresh") < workflow.index("category-backfill-ui-refresh")
    assert "app.cli aupay-gmail" not in workflow
    assert "app.cli drive-" not in workflow


def test_month_range_and_changed_request_snapshot_require_a_new_preview():
    assert _period("対象月:2026-02") == ("2026-02-01", "2026-02-28")
    assert _period("2026-02..2026-03") == ("2026-02-01", "2026-03-31")
    assert _period("全期間") == ("", "")
    db = BackfillDB()
    pipe = CategoryBackfillPipeline(db, preview_enabled=True, apply_enabled=True,
                                    now=lambda: datetime(2026, 9, 17, tzinfo=timezone.utc), id_factory=lambda: "CB-4")
    assert pipe.preview(BackfillSpec(condition(), "2026-08-01", "2026-08-31"))["state"] == "previewed"
    db.requests[0][5] = "2026-09-30"  # Simulates a changed fixed period in the request record.
    assert pipe.confirm("CB-4", expected_count=1) == {"state": "held", "reason": "request_snapshot_changed"}


def test_start_end_month_periods_include_boundaries_and_all_history_ignores_start():
    assert _month_period("2026-02", "2026-02") == ("2026-02-01", "2026-02-28")
    assert _month_period("2024-02", "2024-02") == ("2024-02-01", "2024-02-29")
    assert _month_period("2025-12", "2026-01") == ("2025-12-01", "2026-01-31")
    assert _month_period("not-a-month", "過去すべて") == ("", "")
    assert _month_period("2026-09", "2026-08") is None
    assert _month_period("", "2026-08") is None
    assert _month_period("2026-08", "invalid") is None


def test_all_history_preview_uses_unbounded_fixed_period_and_confirmation_labels_it():
    class ConfirmationDB(BackfillDB):
        def __init__(self):
            super().__init__(); self.confirm=[]
        def category_backfill_confirmation_rows(self): return self.confirm
        def ensure_category_backfill_confirmation_sheet(self, header): pass
        def clear(self, rng): self.confirm=[]
        def append(self, sheet, rows):
            if sheet == "カテゴリ過去反映確認": self.confirm.extend(rows)
            else: super().append(sheet, rows)

    db=ConfirmationDB()
    pipeline=CategoryBackfillPipeline(db, preview_enabled=True, apply_enabled=False, id_factory=lambda: "CB-all")
    assert pipeline.preview(BackfillSpec(condition(), "", ""))["state"] == "previewed"
    result=CategoryBackfillUIPipeline(db, ui_enabled=True, apply_enabled=False).refresh_confirmations()
    assert result["requests"] == 2 and "過去すべて" in db.confirm[0][0]


def test_legacy_periods_migrate_by_fixed_key_without_misreading_old_checkbox_as_end_month():
    class MigrationDB(BackfillDB):
        def __init__(self):
            super().__init__(); self.rules=[service_rule("CR-one", "請求名", ("食費", "外食"))]; self.replaced=[]
        def category_backfill_ui_table(self):
            payload=CategoryBackfillUIPipeline._payload(self.rules[0], saved_rule=True)
            return (["条件・カテゴリ", "対象期間", "プレビューする", "種別", "ルールID", "revision", "条件JSON"],
                    [["旧", "2026-04..2026-08", True, "保存済みルール", "CR-one", 1, payload]])
        def category_rule_ui_rows(self): return []
        def replace_category_backfill_ui_rows(self, rows, header): self.replaced=(rows, header)

    db=MigrationDB(); result=CategoryBackfillUIPipeline(db, ui_enabled=True, apply_enabled=False).refresh()
    assert result["conditions"] == 1
    rows,header=db.replaced
    assert header[:4] == ["条件・カテゴリ", "開始月", "終了月", "プレビューする"]
    assert rows[0][1:4] == ["2026-04", "2026-08", True]
    assert rows[0][5] == "CR-one" and rows[0][7]


def test_non_month_aligned_legacy_period_is_retained_but_requires_new_unchecked_controls():
    class MigrationDB(BackfillDB):
        def __init__(self):
            super().__init__(); self.rules=[service_rule("CR-one", "請求名", ("食費", "外食"))]; self.rows=[]
        def category_backfill_ui_table(self):
            payload=CategoryBackfillUIPipeline._payload(self.rules[0], saved_rule=True)
            return (["条件・カテゴリ", "対象期間", "プレビューする", "種別", "ルールID", "revision", "条件JSON"],
                    [["旧", "2026-04-02..2026-08-30", True, "保存済みルール", "CR-one", 1, payload]])
        def category_rule_ui_rows(self): return []
        def replace_category_backfill_ui_rows(self, rows, header): self.rows=rows

    db=MigrationDB(); CategoryBackfillUIPipeline(db, ui_enabled=True, apply_enabled=False).refresh()
    assert db.rows[0][1:4] == ["", "", False]
    assert db.rows[0][8] == "2026-04-02..2026-08-30"
    assert "再設定待ち" in db.rows[0][0]


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


def test_grouped_unclassified_past_checkbox_is_independent_from_future_rule_save():
    class UIDB(BackfillDB):
        def __init__(self):
            super().__init__(); self.ui=[]; self.backfill_ui=[]
        def category_rule_ui_rows(self): return self.ui
        def category_backfill_ui_rows(self): return self.backfill_ui
        def ensure_category_backfill_ui_sheet(self, header): self.header=header
        def clear(self, rng): self.backfill_ui=[]
        def append(self, sheet, rows):
            if sheet == "カテゴリ過去反映": self.backfill_ui.extend(rows)
            else: super().append(sheet, rows)
    db = UIDB()
    snapshot = '{"proposal":"fallback_group","kind":"service","source":"PayPay","account_alias":"","merchant":"請求名","category":["食費","外食"]}'
    # Future checked by itself does not expose a historical request.
    db.ui = [["条件", "未分類 1件", "食費", "外食", True, False,
              "group:one", "M-one", "2026-08-10", "PayPay", "service", snapshot]]
    pipe = CategoryBackfillUIPipeline(db, ui_enabled=True, apply_enabled=False)
    assert pipe.refresh()["conditions"] == 0
    # The separate past action exposes a past-only condition, not a saved rule.
    db.ui[0][5] = True
    assert pipe.refresh()["conditions"] == 1
    assert db.backfill_ui[0][4] == "表示中の条件（過去分のみ）"
    assert db.rules == [] and db.category_updates == []


def test_preview_consumes_its_source_past_choice_by_fixed_condition_key():
    class UIDB(BackfillDB):
        def __init__(self):
            super().__init__(); self.ui=[]; self.backfill_ui=[]; self.consumed=[]
        def category_rule_ui_rows(self): return self.ui
        def category_backfill_ui_rows(self): return self.backfill_ui
        def ensure_category_backfill_ui_sheet(self, header): pass
        def clear(self, rng): self.backfill_ui=[]
        def append(self, sheet, rows):
            if sheet == "カテゴリ過去反映": self.backfill_ui.extend(rows)
            else: super().append(sheet, rows)
        def update_rows(self, sheet, rows):
            if sheet == "カテゴリ過去反映":
                for row_num, row in rows: self.backfill_ui[row_num-2] = row
            else: super().update_rows(sheet, rows)
        def consume_category_rule_ui_past_choice(self, key, result):
            self.consumed.append((key, result["state"]))
            self.ui[0][5] = False
            return True
    db = UIDB()
    snapshot = '{"proposal":"fallback_group","kind":"service","source":"PayPay","account_alias":"","merchant":"請求名","category":["食費","外食"]}'
    db.ui = [["条件", "未分類 1件", "食費", "外食", False, True,
              "group:one", "M-one", "2026-08-10", "PayPay", "service", snapshot]]
    pipe = CategoryBackfillUIPipeline(db, ui_enabled=True, apply_enabled=False)
    pipe.refresh(); db.backfill_ui[0][1:4] = ["2026-08", "2026-08", True]
    result = pipe.preview_checked()
    assert result["results"][0]["state"] == "previewed"
    assert db.consumed == [("group:one", "previewed")]
    assert db.ui[0][5] is False


def test_backfill_ui_replacement_preserves_checked_period_without_append_rows():
    class ReplacingDB(BackfillDB):
        def __init__(self):
            super().__init__(); self.ui=[]; self.replacements=[]
            self.rules=[service_rule("CR-one", "請求名", ("食費", "外食"))]
        def category_rule_ui_rows(self): return []
        def category_backfill_ui_rows(self): return self.ui
        def replace_category_backfill_ui_rows(self, rows, header):
            self.replacements.append((rows, header)); self.ui=[list(row) for row in rows]
        def append(self, sheet, rows):
            assert sheet != "カテゴリ過去反映", "generated UI must not INSERT_ROWS"
            super().append(sheet, rows)

    db=ReplacingDB(); pipe=CategoryBackfillUIPipeline(db, ui_enabled=True, apply_enabled=False)
    assert pipe.refresh()["conditions"] == 1
    db.ui[0][1:4] = ["2026-01", "2026-12", True]
    assert pipe.refresh()["conditions"] == 1
    assert db.ui[0][1:4] == ["2026-01", "2026-12", True]
    # A later row-count increase keeps the first fixed key's input in place.
    db.rules.append(service_rule("CR-two", "別の請求名", ("食費", "外食")))
    assert pipe.refresh()["conditions"] == 2
    assert db.ui[0][1:4] == ["2026-01", "2026-12", True]
    assert len(db.replacements) == 3


def test_confirmation_replacement_controls_only_request_rows_and_preserves_check():
    class ReplacingDB(BackfillDB):
        def __init__(self):
            super().__init__(); self.confirm=[]; self.replacements=[]; self.target_reads=0
        def category_backfill_confirmation_rows(self): return self.confirm
        def category_backfill_targets(self):
            self.target_reads += 1
            return super().category_backfill_targets()
        def replace_category_backfill_confirmation_rows(self, rows, header):
            self.replacements.append((rows, header)); self.confirm=[list(row) for row in rows]
        def append(self, sheet, rows):
            assert sheet != "カテゴリ過去反映確認", "generated confirmation must not INSERT_ROWS"
            super().append(sheet, rows)

    db=ReplacingDB()
    request_ids=iter(("CB-replace-one", "CB-replace-two"))
    pipeline=CategoryBackfillPipeline(db, preview_enabled=True, apply_enabled=False,
                                      id_factory=lambda: next(request_ids))
    pipeline.preview(BackfillSpec(condition(), "2026-08-01", "2026-08-31"))
    pipeline.preview(BackfillSpec(condition(), "2026-08-01", "2026-08-31"))
    pipe=CategoryBackfillUIPipeline(db, ui_enabled=True, apply_enabled=False)
    assert pipe.refresh_confirmations()["requests"] == 4
    assert db.target_reads == 1
    assert db.confirm[0][4] == "CB-replace-one" and db.confirm[1][2] == ""
    db.confirm[0][2] = True
    assert pipe.refresh_confirmations()["requests"] == 4
    assert db.confirm[0][2] is True and db.confirm[1][2] == ""
    assert len(db.replacements) == 2


def test_sheets_backfill_control_render_clears_stale_boxes_and_marks_only_action_rows():
    class Call:
        def __init__(self, value): self.value=value
        def execute(self): return self.value
    class Service:
        def __init__(self): self.requests=[]
        def spreadsheets(self): return self
        def get(self, **kwargs):
            return Call({"sheets":[{"properties":{"sheetId":91,"title":"カテゴリ過去反映確認"}}]})
        def batchUpdate(self, **kwargs): self.requests.extend(kwargs["body"]["requests"]); return Call({})

    db=object.__new__(SheetsDB); db.sid="synthetic"; db.svc=Service()
    db.ensure_sheet=lambda title, header: None
    db._configure_backfill_mobile_sheet("カテゴリ過去反映確認", ["A","B","C","D","E"], 4, [2, 4])
    validations=[request["setDataValidation"] for request in db.svc.requests if "setDataValidation" in request]
    assert validations[0]["range"] == {"sheetId":91,"startRowIndex":1,"endRowIndex":1000,
                                        "startColumnIndex":2,"endColumnIndex":3}
    assert "rule" not in validations[0]
    assert [item["range"]["startRowIndex"] for item in validations[1:]] == [1, 3]
    assert all(item["rule"]["condition"]["type"] == "BOOLEAN" for item in validations[1:])


def test_sheets_backfill_month_dropdowns_and_checkbox_are_rendered_only_for_condition_rows():
    class Call:
        def __init__(self, value): self.value=value
        def execute(self): return self.value
    class Service:
        def __init__(self): self.requests=[]
        def spreadsheets(self): return self
        def get(self, **kwargs):
            return Call({"sheets":[{"properties":{"sheetId":92,"title":"カテゴリ過去反映",
                "gridProperties":{"columnCount":26}}}]})
        def batchUpdate(self, **kwargs): self.requests.extend(kwargs["body"]["requests"]); return Call({})

    db=object.__new__(SheetsDB); db.sid="synthetic"; db.svc=Service()
    db.ensure_sheet=lambda title, header: None
    db._configure_backfill_mobile_sheet("カテゴリ過去反映", ["A","B","C","D","E","F","G","H","I"], 4, [2, 4], [4])
    validations=[request["setDataValidation"] for request in db.svc.requests if "setDataValidation" in request]
    start=[item for item in validations if item["range"]["startColumnIndex"] == 1]
    end=[item for item in validations if item["range"]["startColumnIndex"] == 2]
    checkbox=[item for item in validations if item["range"]["startColumnIndex"] == 3]
    assert len(start) == 3 and len(end) == len(checkbox) == 2
    assert "rule" not in start[0] and start[0]["range"]["endColumnIndex"] == 4
    assert [item["range"]["startRowIndex"] for item in start[1:]] == [1, 3]
    assert all(item["rule"] == {
        "condition":{"type":"ONE_OF_RANGE","values":[
            {"userEnteredValue":"='カテゴリ過去反映'!$Z$2:$Z$1000"}
        ]}, "strict":True, "showCustomUi":True,
    } for item in start[1:])
    assert all(item["rule"]["condition"]["values"] == [
        {"userEnteredValue":"='カテゴリ過去反映'!$AA$2:$AA$1000"}
    ] for item in end[1:])
    assert all(item["rule"]["condition"]["type"] == "BOOLEAN" for item in checkbox[1:])
    updates=[request["updateCells"] for request in db.svc.requests if "updateCells" in request]
    assert any(update["range"]["startColumnIndex"] == 25 and
               update["rows"][0]["values"][0]["userEnteredValue"] == {
                   "stringValue":"過去反映・開始月候補"
               }
               for update in updates)
    formulas=[update["rows"][0]["values"][0]["userEnteredValue"]["formulaValue"]
              for update in updates if update["range"]["startRowIndex"] == 1]
    assert any("ホーム'!$I$3:$I$5001" in formula and "$B$2:$B$1000" in formula for formula in formulas)
    assert any('"過去すべて"' in formula and "$C$2:$C$1000" in formula for formula in formulas)
    assert any(request.get("updateDimensionProperties",{}).get("range",{}).get("startIndex") == 25 and
               request["updateDimensionProperties"]["range"]["endIndex"] == 27 and
               request["updateDimensionProperties"]["properties"] == {"hiddenByUser":True}
               for request in db.svc.requests)
    assert any(request.get("updateDimensionProperties",{}).get("range") == {
                   "sheetId":92,"dimension":"COLUMNS","startIndex":0,"endIndex":4
               } and request["updateDimensionProperties"]["properties"] == {"hiddenByUser":False}
               for request in db.svc.requests)
    assert {"appendDimension":{"sheetId":92,"dimension":"COLUMNS","length":1}} in db.svc.requests
    faded=[request["repeatCell"] for request in db.svc.requests if "repeatCell" in request
           and request["repeatCell"]["range"].get("startColumnIndex") == 1
           and request["repeatCell"]["range"].get("startRowIndex") == 3]
    assert faded and faded[0]["cell"]["userEnteredFormat"]["textFormat"]["italic"] is True


def test_sheets_read_metadata_is_cached_and_only_429_is_retried():
    class Call:
        def __init__(self, value=None, error=None): self.value=value; self.error=error
        def execute(self):
            if self.error: raise self.error
            return self.value
    class Service:
        def __init__(self): self.metadata_calls=0
        def spreadsheets(self): return self
        def get(self, **kwargs):
            self.metadata_calls += 1
            return Call({"sheets":[{"properties":{"title":"A"}}]})

    service=Service(); sleeps=[]; db=SheetsDB("synthetic", service=service, read_sleeper=sleeps.append)
    assert db.sheet_titles() == ["A"]
    assert db.sheet_titles() == ["A"]
    assert service.metadata_calls == 1 and db.sheet_read_metrics() == {"logical":1,"attempts":1,"retries":0}

    attempts=iter((Call(error=HttpError(Response({"status":"429"}), b"quota")), Call({"ok":True})))
    assert db._execute_sheet_read(lambda: next(attempts)) == {"ok":True}
    assert sleeps == [1]
    assert db.sheet_read_metrics() == {"logical":2,"attempts":3,"retries":1}

    with pytest.raises(HttpError):
        db._execute_sheet_read(lambda: Call(error=HttpError(Response({"status":"400"}), b"bad request")))
    assert db.sheet_read_metrics() == {"logical":3,"attempts":4,"retries":1}

    exhausted=[]; retry_db=SheetsDB("synthetic", service=service, read_sleeper=exhausted.append)
    with pytest.raises(HttpError):
        retry_db._execute_sheet_read(
            lambda: Call(error=HttpError(Response({"status":"429"}), b"quota"))
        )
    assert exhausted == [1, 2, 4]
    assert retry_db.sheet_read_metrics() == {"logical":1,"attempts":4,"retries":3}


def test_multiple_previews_reuse_display_reads_but_apply_input_is_not_cached():
    class CountingDB(BackfillDB):
        def __init__(self):
            super().__init__(); self.calls={"categories":0,"imports":0,"rules":0,"expenses":0}
        def categories(self): self.calls["categories"] += 1; return super().categories()
        def get(self, rng): self.calls["imports"] += 1; return super().get(rng)
        def category_rules(self): self.calls["rules"] += 1; return super().category_rules()
        def expense_records(self): self.calls["expenses"] += 1; return super().expense_records()

    db, first, second = two_service_candidates()
    counting=CountingDB(); counting.expenses=db.expenses; counting.imports=db.imports
    counting.category_pairs=db.category_pairs
    ids=iter(("CB-first", "CB-second"))
    pipe=CategoryBackfillPipeline(counting, preview_enabled=True, apply_enabled=True, id_factory=lambda: next(ids))
    display_cache={}
    assert pipe.preview(BackfillSpec(first, "2026-08-01", "2026-08-31"), display_read_cache=display_cache)["state"] == "previewed"
    assert pipe.preview(BackfillSpec(second, "2026-08-01", "2026-08-31"), display_read_cache=display_cache)["state"] == "previewed"
    assert counting.calls == {"categories":1,"imports":1,"rules":1,"expenses":1}
    # Apply always rereads current source data rather than using preview input.
    assert pipe.confirm("CB-first", expected_count=1)["state"] == "confirmed"
    assert pipe.apply("CB-first", expected_count=1)["state"] == "complete"
    assert all(counting.calls[name] >= 2 for name in counting.calls)
