import json
import sys

import pytest

import app.cli as cli
from app.payroll_schema import (
    PayrollSchemaWriteRepository,
    apply_schema_initialization,
    build_schema_initialization_plan,
    schema_plan_preview,
)
from app.payroll_sheets import PayrollSheetsReadRepository, SHEET_TITLES
from app.payroll_storage import INITIAL_ALIASES, INITIAL_STANDARD_ITEMS, PAYROLL_SCHEMAS


class Request:
    def __init__(self, callback):
        self.callback = callback

    def execute(self):
        return self.callback()


class StatefulValues:
    def __init__(self, state):
        self.state = state

    def get(self, *, spreadsheetId, range):
        return Request(lambda: {"values": self.state["ranges"].get(range, [])})

    def update(self, *, spreadsheetId, range, valueInputOption, body):
        def execute():
            self.state["operations"].append(("update", range))
            self.state["ranges"][range.replace("!A1", "!1:1")] = body["values"]
            return {}
        return Request(execute)

    def append(self, *, spreadsheetId, range, valueInputOption, insertDataOption, body):
        def execute():
            self.state["operations"].append(("append", range))
            data_range = range.replace("!A:A", "!A2:ZZ")
            self.state["ranges"].setdefault(data_range, []).extend(body["values"])
            return {}
        return Request(execute)


class StatefulSpreadsheets:
    def __init__(self, state):
        self.state = state
        self.values_resource = StatefulValues(state)

    def get(self, **kwargs):
        return Request(lambda: {"sheets": [
            {"properties": {"title": title}} for title in sorted(self.state["titles"])
        ]})

    def values(self):
        return self.values_resource

    def batchUpdate(self, *, spreadsheetId, body):
        def execute():
            added = [request["addSheet"]["properties"]["title"]
                     for request in body["requests"]]
            self.state["operations"].append(("batchUpdate", tuple(added)))
            self.state["titles"].update(added)
            return {}
        return Request(execute)


class StatefulSheetsService:
    def __init__(self, titles=()):
        self.state = {"titles": set(titles), "ranges": {}, "operations": []}
        self.resource = StatefulSpreadsheets(self.state)

    def spreadsheets(self):
        return self.resource


def repositories(service):
    return (
        PayrollSheetsReadRepository("sheet-id", service=service),
        PayrollSchemaWriteRepository("sheet-id", service=service),
    )


def install_correct_header(service, key):
    title = SHEET_TITLES[key]
    service.state["titles"].add(title)
    service.state["ranges"][f"'{title}'!1:1"] = [list(PAYROLL_SCHEMAS[key])]


def test_apply_creates_five_sheets_headers_and_only_initial_master_rows():
    service = StatefulSheetsService(titles={"支出明細"})
    reader, writer = repositories(service)
    plan = build_schema_initialization_plan(reader)
    assert set(plan.sheets_to_create) == set(SHEET_TITLES.values())
    assert len(plan.initial_standard_item_ids) == len(INITIAL_STANDARD_ITEMS) == 26
    assert len(plan.initial_alias_ids) == len(INITIAL_ALIASES) == 22

    result = apply_schema_initialization(plan, writer, confirmed=True)
    assert result["created_sheet_count"] == 5
    assert result["written_header_count"] == 5
    assert result["inserted_standard_item_count"] == 26
    assert result["inserted_alias_count"] == 22
    assert result["statement_rows_written"] == 0
    assert result["statement_item_rows_written"] == 0
    assert "支出明細" in service.state["titles"]
    assert service.state["ranges"].get("'勤務先マスタ'!A2:ZZ", []) == []
    append_ranges = [target for operation, target in service.state["operations"]
                     if operation == "append"]
    assert append_ranges == ["'給与標準項目'!A:A", "'給与項目別名'!A:A"]


def test_second_apply_is_idempotent():
    service = StatefulSheetsService()
    reader, writer = repositories(service)
    apply_schema_initialization(build_schema_initialization_plan(reader), writer,
                                confirmed=True)
    operation_count = len(service.state["operations"])
    second = build_schema_initialization_plan(reader)
    assert second.schema_ok
    assert second.sheets_to_create == []
    assert second.headers_to_write == []
    assert second.initial_standard_item_ids == []
    assert second.initial_alias_ids == []
    result = apply_schema_initialization(second, writer, confirmed=True)
    assert result["created_sheet_count"] == 0
    assert len(service.state["operations"]) == operation_count


def test_partial_existing_schema_resumes_only_missing_and_empty_sheets():
    service = StatefulSheetsService(titles={"支出明細"})
    install_correct_header(service, "payroll_statements")
    empty_title = SHEET_TITLES["payroll_items"]
    service.state["titles"].add(empty_title)
    reader, writer = repositories(service)
    plan = build_schema_initialization_plan(reader)
    actions = {sheet.sheet_key: sheet.action for sheet in plan.sheets}
    assert actions["payroll_statements"] == "skip"
    assert actions["payroll_items"] == "initialize_header"
    assert sum(action == "create" for action in actions.values()) == 3
    apply_schema_initialization(plan, writer, confirmed=True)
    assert build_schema_initialization_plan(reader).schema_ok
    assert "支出明細" in service.state["titles"]


def test_mismatched_existing_schema_blocks_without_writes():
    service = StatefulSheetsService()
    title = SHEET_TITLES["payroll_statements"]
    service.state["titles"].add(title)
    service.state["ranges"][f"'{title}'!1:1"] = [["wrong", "columns"]]
    reader, writer = repositories(service)
    plan = build_schema_initialization_plan(reader)
    assert plan.blocked
    assert next(sheet for sheet in plan.sheets
                if sheet.sheet_key == "payroll_statements").action == "blocked"
    with pytest.raises(RuntimeError, match="schema不一致"):
        apply_schema_initialization(plan, writer, confirmed=True)
    assert service.state["operations"] == []


def test_apply_requires_explicit_confirmation_and_preview_never_writes():
    service = StatefulSheetsService()
    reader, writer = repositories(service)
    plan = build_schema_initialization_plan(reader)
    output = schema_plan_preview(plan)
    assert output["initial_standard_item_count"] == 26
    assert output["initial_alias_count"] == 22
    assert service.state["operations"] == []
    with pytest.raises(RuntimeError, match="--apply"):
        apply_schema_initialization(plan, writer, confirmed=False)
    assert service.state["operations"] == []


def test_cli_apply_command_without_flag_does_not_construct_writer(monkeypatch, capsys):
    service = StatefulSheetsService()
    reader, _ = repositories(service)

    class FakeSettings:
        spreadsheet_id = "sheet-id"

        def validate(self, **kwargs):
            return None

    monkeypatch.setattr(cli, "Settings", FakeSettings)
    monkeypatch.setattr(cli, "PayrollSheetsReadRepository", lambda spreadsheet_id: reader)
    monkeypatch.setattr(
        cli, "PayrollSchemaWriteRepository",
        lambda spreadsheet_id: (_ for _ in ()).throw(AssertionError("writer constructed")),
    )
    monkeypatch.setattr(sys, "argv", ["app.cli", "payroll-schema-apply"])
    cli.main()
    output = json.loads(capsys.readouterr().out)
    assert output["applied"] is False
    assert service.state["operations"] == []
