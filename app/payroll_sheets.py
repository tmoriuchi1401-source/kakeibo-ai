from __future__ import annotations

from typing import Iterable, TypeVar

from pydantic import BaseModel, Field, ValidationError

from .google_clients import read_only_sheets_service
from .payroll_storage import (
    PAYROLL_SCHEMAS,
    PayrollEmployerRecord,
    PayrollItemAliasRecord,
    PayrollStandardItemRecord,
    PayrollStatementRecord,
)


SHEET_TITLES = {
    "payroll_statements": "給与明細ヘッダ",
    "payroll_items": "給与明細項目",
    "payroll_standard_items": "給与標準項目",
    "payroll_item_aliases": "給与項目別名",
    "payroll_employers": "勤務先マスタ",
}


class PayrollSheetSchemaResult(BaseModel):
    sheet_key: str
    sheet_title: str
    schema_ok: bool
    sheet_missing: bool = False
    missing_columns: list[str] = Field(default_factory=list)
    unexpected_columns: list[str] = Field(default_factory=list)
    column_order_ok: bool = False


class PayrollSheetsSnapshot(BaseModel):
    schemas: list[PayrollSheetSchemaResult]
    statements: list[PayrollStatementRecord] = Field(default_factory=list)
    standard_items: list[PayrollStandardItemRecord] = Field(default_factory=list)
    aliases: list[PayrollItemAliasRecord] = Field(default_factory=list)
    employers: list[PayrollEmployerRecord] = Field(default_factory=list)

    @property
    def schema_ok(self) -> bool:
        return all(result.schema_ok for result in self.schemas)


def validate_sheet_schema(
    sheet_key: str,
    actual_columns: Iterable[str] | None,
) -> PayrollSheetSchemaResult:
    expected = list(PAYROLL_SCHEMAS[sheet_key])
    title = SHEET_TITLES[sheet_key]
    if actual_columns is None:
        return PayrollSheetSchemaResult(
            sheet_key=sheet_key, sheet_title=title, schema_ok=False,
            sheet_missing=True, missing_columns=expected, column_order_ok=False,
        )
    actual = [str(column).strip() for column in actual_columns]
    missing = [column for column in expected if column not in actual]
    unexpected = [column for column in actual if column not in expected]
    order_ok = actual == expected
    return PayrollSheetSchemaResult(
        sheet_key=sheet_key, sheet_title=title,
        schema_ok=not missing and not unexpected and order_ok,
        missing_columns=missing, unexpected_columns=unexpected,
        column_order_ok=order_ok,
    )


RecordT = TypeVar("RecordT", bound=BaseModel)


class PayrollSheetsReadRepository:
    """Payroll-only Sheets reader. It intentionally exposes no write methods."""

    def __init__(self, spreadsheet_id: str, *, service=None):
        self.spreadsheet_id = spreadsheet_id
        self.service = service or read_only_sheets_service()

    def sheet_titles(self) -> set[str]:
        result = self.service.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id,
            fields="sheets.properties.title",
        ).execute()
        return {sheet["properties"]["title"] for sheet in result.get("sheets", [])}

    def _values(self, range_name: str) -> list[list]:
        return self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id, range=range_name,
        ).execute().get("values", [])

    def header(self, sheet_title: str) -> list[str]:
        rows = self._values(f"'{sheet_title}'!1:1")
        return [str(value) for value in rows[0]] if rows else []

    def validate_schemas(self) -> list[PayrollSheetSchemaResult]:
        titles = self.sheet_titles()
        return [
            validate_sheet_schema(
                key, self.header(title) if title in titles else None,
            )
            for key, title in SHEET_TITLES.items()
        ]

    def _records(self, sheet_key: str, model: type[RecordT]) -> list[RecordT]:
        title = SHEET_TITLES[sheet_key]
        rows = self._values(f"'{title}'!A2:ZZ")
        columns = list(PAYROLL_SCHEMAS[sheet_key])
        records = []
        for raw_row in rows:
            row = list(raw_row) + [None] * max(0, len(columns) - len(raw_row))
            values = {
                column: (None if value == "" else value)
                for column, value in zip(columns, row)
            }
            try:
                records.append(model.model_validate(values))
            except ValidationError:
                # A malformed stored row is not made valid by guessing. Schema
                # validation remains independent and preview stays read-only.
                continue
        return records

    def read_statement_headers(self) -> list[PayrollStatementRecord]:
        return self._records("payroll_statements", PayrollStatementRecord)

    def read_standard_items(self) -> list[PayrollStandardItemRecord]:
        return self._records("payroll_standard_items", PayrollStandardItemRecord)

    def read_aliases(self) -> list[PayrollItemAliasRecord]:
        return self._records("payroll_item_aliases", PayrollItemAliasRecord)

    def read_employers(self) -> list[PayrollEmployerRecord]:
        return self._records("payroll_employers", PayrollEmployerRecord)

    def snapshot(self) -> PayrollSheetsSnapshot:
        schemas = self.validate_schemas()
        available = {result.sheet_key for result in schemas if not result.sheet_missing}
        return PayrollSheetsSnapshot(
            schemas=schemas,
            statements=(self.read_statement_headers()
                        if "payroll_statements" in available else []),
            standard_items=(self.read_standard_items()
                            if "payroll_standard_items" in available else []),
            aliases=(self.read_aliases()
                     if "payroll_item_aliases" in available else []),
            employers=(self.read_employers()
                       if "payroll_employers" in available else []),
        )


def usable_aliases(
    standard_items: Iterable[PayrollStandardItemRecord],
    aliases: Iterable[PayrollItemAliasRecord],
) -> tuple[PayrollItemAliasRecord, ...]:
    active_standard_ids = {
        item.standard_item_id for item in standard_items if item.active
    }
    return tuple(alias for alias in aliases
                 if alias.active and alias.standard_item_id in active_standard_ids)
