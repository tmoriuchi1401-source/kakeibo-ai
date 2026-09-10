import json
import sys

import app.cli as cli
from app.payroll_display import (
    PAYROLL_COLUMN_DISPLAY_LABELS,
    PAYROLL_SECTION_DISPLAY_LABELS,
    PAYROLL_STANDARD_ITEM_DISPLAY_LABELS,
    canonicalize_payroll_header,
    payroll_display_header,
    payroll_standard_item_display_label,
)
from app.payroll_display_preview import build_payroll_display_update_preview
from app.payroll_sheets import SHEET_TITLES, validate_sheet_schema
from app.payroll_storage import INITIAL_STANDARD_ITEMS, PAYROLL_SCHEMAS


class Reader:
    def __init__(self, headers):
        self.spreadsheet_id = "sheet-id"
        self.headers = headers

    def sheet_titles(self):
        return set(self.headers)

    def header(self, sheet_title):
        return list(self.headers[sheet_title])


def headers(factory):
    return {
        title: tuple(factory(key))
        for key, title in SHEET_TITLES.items()
    }


def test_display_mapping_covers_every_canonical_schema_field_once():
    canonical_fields = {
        column for columns in PAYROLL_SCHEMAS.values() for column in columns
    }
    assert set(PAYROLL_COLUMN_DISPLAY_LABELS) == canonical_fields
    for sheet_key, canonical in PAYROLL_SCHEMAS.items():
        displayed = payroll_display_header(sheet_key)
        assert len(displayed) == len(canonical)
        assert len(set(displayed)) == len(displayed)
        assert canonicalize_payroll_header(sheet_key, displayed) == canonical


def test_required_business_labels_use_existing_payroll_terminology():
    assert PAYROLL_COLUMN_DISPLAY_LABELS["gross_pay"] == "総支給額"
    assert PAYROLL_COLUMN_DISPLAY_LABELS["net_pay"] == "差引支給額"
    assert payroll_standard_item_display_label("basic_pay") == "基本給"
    assert payroll_standard_item_display_label("overtime_pay") == "時間外手当"
    assert payroll_standard_item_display_label("health_insurance") == "健康保険"
    assert payroll_standard_item_display_label("employees_pension") == "厚生年金"
    assert payroll_standard_item_display_label("employment_insurance") == "雇用保険"
    assert payroll_standard_item_display_label("income_tax") == "所得税"
    assert payroll_standard_item_display_label("resident_tax") == "住民税"
    assert payroll_standard_item_display_label("unknown_field") is None
    assert PAYROLL_SECTION_DISPLAY_LABELS == {
        "earning": "支給", "deduction": "控除", "attendance": "勤怠",
        "reference": "参考", "unknown": "不明",
    }


def test_standard_item_display_mapping_is_exactly_the_existing_master():
    assert PAYROLL_STANDARD_ITEM_DISPLAY_LABELS == {
        item.standard_item_id: item.standard_name for item in INITIAL_STANDARD_ITEMS
    }


def test_schema_accepts_complete_canonical_or_display_header_but_not_mixed():
    for sheet_key, canonical in PAYROLL_SCHEMAS.items():
        displayed = payroll_display_header(sheet_key)
        assert validate_sheet_schema(sheet_key, canonical).schema_ok
        assert validate_sheet_schema(sheet_key, displayed).schema_ok
        mixed = (displayed[0], *canonical[1:])
        assert not validate_sheet_schema(sheet_key, mixed).schema_ok


def test_existing_canonical_headers_preview_exactly_43_display_cell_updates():
    preview = build_payroll_display_update_preview(
        Reader(headers(lambda key: PAYROLL_SCHEMAS[key]))
    )
    assert not preview.blocked
    assert preview.changed_header_cell_count == 43
    assert preview.changed_data_cell_count == 0
    assert preview.row_additions == preview.row_deletions == 0
    assert all(update.action == "update" for update in preview.updates)
    by_key = {update.sheet_key: update for update in preview.updates}
    assert by_key["payroll_statements"].range_name == "'給与明細ヘッダ'!A1:O1"
    assert by_key["payroll_items"].range_name == "'給与明細項目'!A1:K1"
    assert by_key["payroll_standard_items"].range_name == "'給与標準項目'!A1:F1"
    assert by_key["payroll_item_aliases"].range_name == "'給与項目別名'!A1:F1"
    assert by_key["payroll_employers"].range_name == "'勤務先マスタ'!A1:E1"


def test_display_preview_is_idempotent_and_blocks_unknown_headers():
    displayed = headers(payroll_display_header)
    preview = build_payroll_display_update_preview(Reader(displayed))
    assert not preview.blocked
    assert preview.changed_header_cell_count == 0
    assert all(update.action == "noop" for update in preview.updates)

    displayed[SHEET_TITLES["payroll_items"]] = ("unexpected",)
    blocked = build_payroll_display_update_preview(Reader(displayed))
    assert blocked.blocked
    item = next(update for update in blocked.updates
                if update.sheet_key == "payroll_items")
    assert item.action == "blocked"
    assert item.changed_cell_count == 0


def test_cli_display_preview_is_read_only(monkeypatch, capsys):
    reader = Reader(headers(lambda key: PAYROLL_SCHEMAS[key]))

    class FakeSettings:
        spreadsheet_id = "sheet-id"

        def validate(self, **kwargs):
            return None

    monkeypatch.setattr(cli, "Settings", FakeSettings)
    monkeypatch.setattr(
        cli, "PayrollSheetsReadRepository", lambda spreadsheet_id: reader,
    )
    monkeypatch.setattr(sys, "argv", ["app.cli", "payroll-display-preview"])
    cli.main()
    output = json.loads(capsys.readouterr().out)
    assert output["blocked"] is False
    assert output["changed_header_cell_count"] == 43
    assert output["changed_data_cell_count"] == 0
