from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .google_clients import sheets_service
from .payroll_sheets import PayrollSheetsReadRepository, SHEET_TITLES
from .payroll_storage import INITIAL_ALIASES, INITIAL_STANDARD_ITEMS, PAYROLL_SCHEMAS


SheetAction = Literal["create", "initialize_header", "skip", "blocked"]


class PayrollSheetInitializationPlan(BaseModel):
    sheet_key: str
    sheet_title: str
    action: SheetAction
    header: list[str]
    reason: str | None = None


class PayrollSchemaInitializationPlan(BaseModel):
    schema_ok: bool
    blocked: bool
    sheets: list[PayrollSheetInitializationPlan]
    existing_sheets: list[str] = Field(default_factory=list)
    initial_standard_item_ids: list[str] = Field(default_factory=list)
    initial_alias_ids: list[str] = Field(default_factory=list)

    @property
    def sheets_to_create(self) -> list[str]:
        return [sheet.sheet_title for sheet in self.sheets if sheet.action == "create"]

    @property
    def headers_to_write(self) -> list[str]:
        return [sheet.sheet_title for sheet in self.sheets
                if sheet.action in {"create", "initialize_header"}]


def build_schema_initialization_plan(
    repository: PayrollSheetsReadRepository,
) -> PayrollSchemaInitializationPlan:
    titles = repository.sheet_titles()
    sheets = []
    for key, title in SHEET_TITLES.items():
        expected = list(PAYROLL_SCHEMAS[key])
        if title not in titles:
            action: SheetAction = "create"
            reason = "sheet_missing"
        else:
            actual = repository.header(title)
            if actual == expected:
                action = "skip"
                reason = None
            elif not actual:
                action = "initialize_header"
                reason = "empty_header"
            else:
                action = "blocked"
                reason = "schema_mismatch"
        sheets.append(PayrollSheetInitializationPlan(
            sheet_key=key, sheet_title=title, action=action,
            header=expected, reason=reason,
        ))

    blocked = any(sheet.action == "blocked" for sheet in sheets)
    standard_ids = set()
    alias_ids = set()
    if not blocked:
        standard_sheet = next(sheet for sheet in sheets
                              if sheet.sheet_key == "payroll_standard_items")
        alias_sheet = next(sheet for sheet in sheets
                           if sheet.sheet_key == "payroll_item_aliases")
        if standard_sheet.action == "skip":
            standard_ids = {item.standard_item_id
                            for item in repository.read_standard_items()}
        if alias_sheet.action == "skip":
            alias_ids = {alias.alias_id for alias in repository.read_aliases()}

    return PayrollSchemaInitializationPlan(
        schema_ok=all(sheet.action == "skip" for sheet in sheets),
        blocked=blocked,
        sheets=sheets,
        existing_sheets=sorted(title for title in titles if title in SHEET_TITLES.values()),
        initial_standard_item_ids=[item.standard_item_id for item in INITIAL_STANDARD_ITEMS
                                   if item.standard_item_id not in standard_ids],
        initial_alias_ids=[alias.alias_id for alias in INITIAL_ALIASES
                           if alias.alias_id not in alias_ids],
    )


def schema_plan_preview(plan: PayrollSchemaInitializationPlan) -> dict:
    return {
        "schema_ok": plan.schema_ok,
        "blocked": plan.blocked,
        "sheets_to_create": plan.sheets_to_create,
        "existing_sheets": plan.existing_sheets,
        "headers_to_write": plan.headers_to_write,
        "initial_standard_item_count": len(plan.initial_standard_item_ids),
        "initial_alias_count": len(plan.initial_alias_ids),
        "sheets": [
            {
                "sheet_title": sheet.sheet_title,
                "action": sheet.action,
                "reason": sheet.reason,
                "header": sheet.header,
            }
            for sheet in plan.sheets
        ],
    }


class PayrollSchemaWriteRepository:
    """Writer restricted to initial Payroll sheets, headers, and master rows."""

    def __init__(self, spreadsheet_id: str, *, service=None):
        self.spreadsheet_id = spreadsheet_id
        self.service = service or sheets_service()

    def create_sheets(self, titles: list[str]) -> None:
        allowed = set(SHEET_TITLES.values())
        if any(title not in allowed for title in titles):
            raise ValueError("Payroll以外のシートは作成できません")
        if not titles:
            return
        self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {
                "title": title, "gridProperties": {"frozenRowCount": 1},
            }}} for title in titles]},
        ).execute()

    def write_header(self, sheet_title: str, header: list[str]) -> None:
        key = next((key for key, title in SHEET_TITLES.items()
                    if title == sheet_title), None)
        if key is None or header != list(PAYROLL_SCHEMAS[key]):
            raise ValueError("Payroll schema以外のheaderは書き込めません")
        self.service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{sheet_title}'!A1",
            valueInputOption="RAW",
            body={"values": [header]},
        ).execute()

    def append_initial_master_rows(self, sheet_key: str, rows: list[list]) -> None:
        if sheet_key not in {"payroll_standard_items", "payroll_item_aliases"}:
            raise ValueError("初期master以外の行はappendできません")
        if not rows:
            return
        title = SHEET_TITLES[sheet_key]
        self.service.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{title}'!A:A",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()


def _row(record, sheet_key: str) -> list:
    values = record.model_dump(mode="json")
    return [values.get(column) for column in PAYROLL_SCHEMAS[sheet_key]]


def apply_schema_initialization(
    plan: PayrollSchemaInitializationPlan,
    writer: PayrollSchemaWriteRepository,
    *,
    confirmed: bool,
) -> dict:
    if not confirmed:
        raise RuntimeError("--apply が必要です")
    if plan.blocked:
        raise RuntimeError("schema不一致のPayrollシートがあるため適用できません")

    writer.create_sheets(plan.sheets_to_create)
    for sheet in plan.sheets:
        if sheet.action in {"create", "initialize_header"}:
            writer.write_header(sheet.sheet_title, sheet.header)

    standard_by_id = {item.standard_item_id: item for item in INITIAL_STANDARD_ITEMS}
    alias_by_id = {alias.alias_id: alias for alias in INITIAL_ALIASES}
    standard_rows = [_row(standard_by_id[item_id], "payroll_standard_items")
                     for item_id in plan.initial_standard_item_ids]
    alias_rows = [_row(alias_by_id[alias_id], "payroll_item_aliases")
                  for alias_id in plan.initial_alias_ids]
    writer.append_initial_master_rows("payroll_standard_items", standard_rows)
    writer.append_initial_master_rows("payroll_item_aliases", alias_rows)
    return {
        "applied": True,
        "created_sheet_count": len(plan.sheets_to_create),
        "written_header_count": len(plan.headers_to_write),
        "inserted_standard_item_count": len(standard_rows),
        "inserted_alias_count": len(alias_rows),
        "statement_rows_written": 0,
        "statement_item_rows_written": 0,
    }
