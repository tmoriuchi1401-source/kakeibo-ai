import json

from app.payroll_models import PayrollItem, PayrollPreview
from app.payroll_sheets import (
    SHEET_TITLES,
    PayrollSheetsReadRepository,
    PayrollSheetsSnapshot,
    validate_sheet_schema,
    usable_aliases,
)
from app.payroll_storage import (
    PAYROLL_SCHEMAS,
    PayrollEmployerRecord,
    PayrollItemAliasRecord,
    PayrollStandardItemRecord,
    PayrollStatementRecord,
    phase_a_to_storage_candidate,
    resolve_alias,
)
from app.payroll_storage_preview import (
    build_append_plan,
    enforce_active_standard_items,
    preview_summary,
)


class Request:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class FakeValues:
    def __init__(self, ranges, writes):
        self.ranges = ranges
        self.writes = writes

    def get(self, *, spreadsheetId, range):
        return Request({"values": self.ranges.get(range, [])})

    def append(self, **kwargs):
        self.writes.append(("append", kwargs))
        raise AssertionError("Sheets write must not be called")

    def update(self, **kwargs):
        self.writes.append(("update", kwargs))
        raise AssertionError("Sheets write must not be called")

    def clear(self, **kwargs):
        self.writes.append(("clear", kwargs))
        raise AssertionError("Sheets write must not be called")


class FakeSpreadsheets:
    def __init__(self, titles, ranges, writes):
        self.titles = titles
        self.values_resource = FakeValues(ranges, writes)
        self.writes = writes

    def get(self, **kwargs):
        return Request({"sheets": [
            {"properties": {"title": title}} for title in self.titles
        ]})

    def values(self):
        return self.values_resource

    def batchUpdate(self, **kwargs):
        self.writes.append(("batchUpdate", kwargs))
        raise AssertionError("Sheets write must not be called")


class FakeSheetsService:
    def __init__(self, titles, ranges):
        self.write_calls = []
        self.resource = FakeSpreadsheets(titles, ranges, self.write_calls)

    def spreadsheets(self):
        return self.resource


def complete_ranges(rows=None):
    ranges = {
        f"'{title}'!1:1": [list(PAYROLL_SCHEMAS[key])]
        for key, title in SHEET_TITLES.items()
    }
    for key, title in SHEET_TITLES.items():
        ranges[f"'{title}'!A2:ZZ"] = list((rows or {}).get(key, []))
    return ranges


def model_row(model, schema_key):
    values = model.model_dump(mode="json")
    return [values.get(column, "") for column in PAYROLL_SCHEMAS[schema_key]]


def candidate(**statement_overrides):
    phase_a = PayrollPreview(
        file_type="pdf", extraction_method="pdf_text", pay_period="2026-08",
        gross_pay=320000, total_deductions=50000, net_pay=270000,
        parse_status="success", items=[PayrollItem(
            raw_item_name="基本給", section="earnings", raw_value="300,000",
            value=300000, standard_item_candidate="basic_pay", needs_review=False,
        )],
    )
    result = phase_a_to_storage_candidate(
        phase_a, employer_id="employer-1", statement_label="給与明細",
        source_type="drive", source_file_id="new-file", content_hash="new-hash",
    )
    for key, value in statement_overrides.items():
        setattr(result.statement, key, value)
    return result


def snapshot(*, schemas=None, statements=None):
    return PayrollSheetsSnapshot(
        schemas=schemas or [validate_sheet_schema(key, columns)
                            for key, columns in PAYROLL_SCHEMAS.items()],
        statements=statements or [],
        standard_items=[PayrollStandardItemRecord(
            standard_item_id="basic_pay", standard_name="基本給",
            section="earning", value_type="money",
        )],
    )


def test_read_only_repository_validates_all_schemas_and_never_writes():
    service = FakeSheetsService(set(SHEET_TITLES.values()), complete_ranges())
    result = PayrollSheetsReadRepository("sheet-id", service=service).snapshot()
    assert result.schema_ok
    assert len(result.schemas) == 5
    assert service.write_calls == []


def test_schema_reports_missing_sheet_and_all_required_columns():
    result = validate_sheet_schema("payroll_items", None)
    assert result.sheet_missing
    assert not result.schema_ok
    assert result.missing_columns == list(PAYROLL_SCHEMAS["payroll_items"])


def test_repository_reports_a_missing_sheet_without_creating_it():
    titles = set(SHEET_TITLES.values()) - {SHEET_TITLES["payroll_items"]}
    service = FakeSheetsService(titles, complete_ranges())
    result = PayrollSheetsReadRepository("sheet-id", service=service).snapshot()
    missing = next(schema for schema in result.schemas
                   if schema.sheet_key == "payroll_items")
    assert missing.sheet_missing
    assert not result.schema_ok
    assert service.write_calls == []


def test_schema_reports_missing_extra_and_wrong_order():
    expected = list(PAYROLL_SCHEMAS["payroll_statements"])
    actual = [expected[1], expected[0], *expected[2:-1], "extra_column"]
    result = validate_sheet_schema("payroll_statements", actual)
    assert not result.schema_ok
    assert result.missing_columns == [expected[-1]]
    assert result.unexpected_columns == ["extra_column"]
    assert not result.column_order_ok


def test_repository_reads_existing_models_aliases_and_employers():
    stored = PayrollStatementRecord(
        statement_id="stored", employer_id="employer-1", statement_type="salary",
        pay_period="2026-08", source_file_id="file-1", content_hash="hash-1",
    )
    standard = PayrollStandardItemRecord(
        standard_item_id="basic_pay", standard_name="基本給",
        section="earning", value_type="money",
    )
    alias = PayrollItemAliasRecord(
        alias_id="alias-1", raw_item_name="本給", standard_item_id="basic_pay",
    )
    employer = PayrollEmployerRecord(
        employer_id="employer-1", employer_label="勤務先A",
    )
    rows = {
        "payroll_statements": [model_row(stored, "payroll_statements")],
        "payroll_standard_items": [model_row(standard, "payroll_standard_items")],
        "payroll_item_aliases": [model_row(alias, "payroll_item_aliases")],
        "payroll_employers": [model_row(employer, "payroll_employers")],
    }
    service = FakeSheetsService(set(SHEET_TITLES.values()), complete_ranges(rows))
    result = PayrollSheetsReadRepository("sheet-id", service=service).snapshot()
    assert result.statements[0].statement_id == "stored"
    assert result.standard_items[0].standard_name == "基本給"
    assert result.aliases[0].raw_item_name == "本給"
    assert result.employers[0].employer_id == "employer-1"
    assert service.write_calls == []


def test_loaded_aliases_ignore_inactive_alias_and_inactive_standard_item():
    standards = [
        PayrollStandardItemRecord(standard_item_id="active", standard_name="有効",
                                  section="earning", value_type="money"),
        PayrollStandardItemRecord(standard_item_id="inactive", standard_name="無効",
                                  section="earning", value_type="money", active=False),
    ]
    aliases = [
        PayrollItemAliasRecord(raw_item_name="共通", standard_item_id="active"),
        PayrollItemAliasRecord(raw_item_name="停止別名", standard_item_id="active", active=False),
        PayrollItemAliasRecord(raw_item_name="過去項目", standard_item_id="inactive"),
    ]
    usable = usable_aliases(standards, aliases)
    assert [alias.raw_item_name for alias in usable] == ["共通"]


def test_inactive_standard_item_cannot_remain_resolved_in_a_plan_candidate():
    source = candidate()
    inactive = [PayrollStandardItemRecord(
        standard_item_id="basic_pay", standard_name="基本給",
        section="earning", value_type="money", active=False,
    )]
    result = enforce_active_standard_items(source, inactive)
    assert result.items[0].standard_item_id is None
    assert result.items[0].value is None
    assert result.items[0].review_status == "pending"
    assert result.statement.needs_review
    assert source.items[0].standard_item_id == "basic_pay"


def test_loaded_employer_alias_takes_priority_over_common_alias():
    standards = [
        PayrollStandardItemRecord(standard_item_id=value, standard_name=value,
                                  section="earning", value_type="money")
        for value in ("common", "specific")
    ]
    aliases = [
        PayrollItemAliasRecord(raw_item_name="手当", standard_item_id="common"),
        PayrollItemAliasRecord(raw_item_name="手当", standard_item_id="specific",
                               employer_id="employer-1"),
    ]
    usable = usable_aliases(standards, aliases)
    assert resolve_alias("手当", usable, "employer-1") == "specific"


def test_duplicate_file_id_and_hash_are_skipped_in_priority_order():
    existing = PayrollStatementRecord(
        statement_id="stored", employer_id="employer-1", statement_type="salary",
        pay_period="2026-08", source_file_id="same-file", content_hash="same-hash",
    )
    by_file = candidate(source_file_id="same-file", content_hash="different")
    by_hash = candidate(source_file_id="different", content_hash="same-hash",
                        pay_period="2026-09")
    plans = build_append_plan([by_file, by_hash], snapshot(statements=[existing]))
    assert [(plan.action, plan.duplicate_reason) for plan in plans] == [
        ("skip_duplicate", "source_file_id"),
        ("skip_duplicate", "content_hash"),
    ]


def test_same_statement_key_with_different_hash_is_reviewed():
    existing = PayrollStatementRecord(
        employer_id="employer-1", statement_type="salary", pay_period="2026-08",
        source_file_id="old-file", content_hash="old-hash",
    )
    plan = build_append_plan([candidate()], snapshot(statements=[existing]))[0]
    assert plan.action == "needs_review"
    assert plan.duplicate_reason == "statement_key"
    assert "possible_reissue_or_revision" in plan.review_reason


def test_new_confirmed_statement_gets_append_plan_without_writing():
    plan = build_append_plan([candidate()], snapshot())[0]
    assert plan.action == "append"
    assert plan.header_rows_to_append == 1
    assert plan.item_rows_to_append == 1
    assert plan.resolved_item_count == 1


def test_statement_review_and_unresolved_employer_are_not_appended():
    reviewed = candidate(needs_review=True)
    unresolved = candidate(employer_id=None)
    plans = build_append_plan([reviewed, unresolved], snapshot())
    assert all(plan.action == "needs_review" for plan in plans)
    assert "statement_needs_review" in plans[0].review_reason
    assert "employer_unresolved" in plans[1].review_reason


def test_invalid_schema_blocks_all_rows_without_auto_repair():
    invalid = [validate_sheet_schema("payroll_statements", None)]
    plan = build_append_plan([candidate()], snapshot(schemas=invalid))[0]
    assert plan.action == "blocked_schema"
    assert plan.header_rows_to_append == plan.item_rows_to_append == 0


def test_preview_is_anonymous_and_contains_required_counts():
    plans = build_append_plan([candidate()], snapshot())
    output = preview_summary(plans, snapshot())
    encoded = json.dumps(output, ensure_ascii=False)
    assert output["statements_found"] == output["append_count"] == 1
    assert set(output["statements"][0]) == {
        "action", "statement_type", "pay_period", "item_count",
        "resolved_item_count", "review_item_count", "duplicate_reason", "review_reason",
    }
    assert "new-file" not in encoded
    assert "employer-1" not in encoded
