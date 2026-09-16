from datetime import datetime, timezone

from app.category_backfill import (
    BACKFILL_REQUEST_SHEET, BACKFILL_TARGET_SHEET, BackfillSpec, CategoryBackfillPipeline,
)
from app.category_rules import CategoryRule


def import_row(import_id="p1", merchant="請求名", amount=100):
    return [import_id, "2026-09-17T12:00:00+00:00", "PayPay", import_id, "2026-08-10", merchant, amount,
            "通常払い", "auto_expense", "M-one", "h", ""]


class BackfillDB:
    def __init__(self):
        self.expenses = {
            "M-one": (2, ["M-one", "2026-08-10", "請求名", "自動計上", 100, "その他", "未分類", "", "PayPay", "", "p1", "", "active"]),
            "M-partial": (3, ["M-partial", "2026-08-11", "請求名", "自動計上", 100, "食費", "", "", "PayPay", "", "p1", "", "active"]),
        }
        self.imports = [import_row()]
        self.requests=[]; self.targets=[]; self.category_updates=[]; self.rules=[]
    def categories(self): return [("その他", "未分類"), ("食費", "外食")]
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
