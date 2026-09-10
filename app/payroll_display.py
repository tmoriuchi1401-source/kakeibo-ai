from __future__ import annotations

from types import MappingProxyType
from typing import Iterable

from .payroll_storage import INITIAL_STANDARD_ITEMS, PAYROLL_SCHEMAS


PAYROLL_COLUMN_DISPLAY_LABELS = MappingProxyType({
    "statement_id": "給与明細ID",
    "employer_id": "勤務先ID",
    "statement_type": "給与明細種別",
    "pay_period": "支給対象期間",
    "pay_date": "支給日",
    "gross_pay": "総支給額",
    "total_deductions": "控除合計",
    "net_pay": "差引支給額",
    "parse_status": "解析状態",
    "needs_review": "要確認",
    "source_type": "取込元種別",
    "source_file_id": "取込元ファイルID",
    "content_hash": "内容ハッシュ",
    "imported_at": "取込日時",
    "parser_version": "パーサーバージョン",
    "item_id": "給与明細項目ID",
    "raw_item_name": "帳票項目名",
    "standard_item_id": "標準項目ID",
    "section": "区分",
    "raw_value": "帳票記載値",
    "value": "確定値",
    "confidence": "信頼度",
    "review_status": "確認状態",
    "display_order": "表示順",
    "standard_name": "標準項目名",
    "value_type": "値種別",
    "active": "有効",
    "created_at": "作成日時",
    "alias_id": "項目別名ID",
    "employer_label": "勤務先名",
    "start_date": "適用開始日",
    "end_date": "適用終了日",
})

PAYROLL_STANDARD_ITEM_DISPLAY_LABELS = MappingProxyType({
    item.standard_item_id: item.standard_name for item in INITIAL_STANDARD_ITEMS
})

PAYROLL_SECTION_DISPLAY_LABELS = MappingProxyType({
    "earning": "支給",
    "deduction": "控除",
    "attendance": "勤怠",
    "reference": "参考",
    "unknown": "不明",
})


def payroll_display_header(sheet_key: str) -> tuple[str, ...]:
    """Return Japanese labels without changing the canonical schema."""
    return tuple(
        PAYROLL_COLUMN_DISPLAY_LABELS[column]
        for column in PAYROLL_SCHEMAS[sheet_key]
    )


def canonicalize_payroll_header(
    sheet_key: str,
    visible_columns: Iterable[str],
) -> tuple[str, ...]:
    """Accept one complete canonical or Japanese header, never a mixed header."""
    actual = tuple(str(column).strip() for column in visible_columns)
    canonical = tuple(PAYROLL_SCHEMAS[sheet_key])
    if actual == canonical or actual == payroll_display_header(sheet_key):
        return canonical
    return actual


def payroll_standard_item_display_label(standard_item_id: str) -> str | None:
    """Return a catalog label only for a known canonical standard item ID."""
    return PAYROLL_STANDARD_ITEM_DISPLAY_LABELS.get(standard_item_id)
