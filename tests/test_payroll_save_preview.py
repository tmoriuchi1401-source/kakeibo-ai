from app.payroll_models import PayrollItem, PayrollPreview
from app.payroll_sheets import PayrollSheetsSnapshot, validate_sheet_schema
from app.payroll_storage import (
    PAYROLL_SCHEMAS,
    PayrollEmployerRecord,
    PayrollItemAliasRecord,
    PayrollStandardItemRecord,
    PayrollStatementRecord,
    phase_a_to_storage_candidate,
)
from app.payroll_storage_preview import (
    build_save_plan,
    drive_save_preview,
    save_preview_summary,
)


class Request:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class Files:
    def __init__(self, files, writes):
        self._files = files
        self.writes = writes

    def list(self, **kwargs):
        return Request({"files": self._files})

    def update(self, **kwargs):
        self.writes.append(("update", kwargs))
        raise AssertionError("Drive write must not be called")

    def delete(self, **kwargs):
        self.writes.append(("delete", kwargs))
        raise AssertionError("Drive write must not be called")


class DriveService:
    def __init__(self, files):
        self.write_calls = []
        self.resource = Files(files, self.write_calls)

    def files(self):
        return self.resource


def snapshot(*, statements=None):
    standards = [
        PayrollStandardItemRecord(
            standard_item_id="basic_pay", standard_name="基本給",
            section="earning", value_type="money",
        ),
        PayrollStandardItemRecord(
            standard_item_id="health_insurance", standard_name="健康保険",
            section="deduction", value_type="money",
        ),
    ]
    return PayrollSheetsSnapshot(
        schemas=[validate_sheet_schema(key, columns)
                 for key, columns in PAYROLL_SCHEMAS.items()],
        statements=statements or [],
        standard_items=standards,
        aliases=[PayrollItemAliasRecord(
            raw_item_name="健康保険料", standard_item_id="health_insurance",
        )],
        employers=[PayrollEmployerRecord(
            employer_id="employer-1", employer_label="勤務先A",
        )],
    )


def parsed_statement():
    return PayrollPreview(
        file_type="pdf", extraction_method="pdf_text", pay_period="2026-08",
        pay_date="2026-08-25", parse_status="partial",
        items=[
            PayrollItem(
                raw_item_name="基本給", section="earnings", raw_value="300,000",
                value=300000, standard_item_candidate="basic_pay",
            ),
            PayrollItem(
                raw_item_name="健康保険料", section="deductions", raw_value="15,000",
                value=15000,
            ),
            PayrollItem(
                raw_item_name="調整手当A", section="unknown", raw_value="5,000",
                value=5000, needs_review=True,
            ),
        ],
    )


def candidate(*, source_file_id="file-1", content_hash="hash-1"):
    return phase_a_to_storage_candidate(
        parsed_statement(), employer_id="employer-1", statement_label="給与明細",
        source_type="drive", source_file_id=source_file_id,
        content_hash=content_hash, aliases=snapshot().aliases,
        file_name="2026-08-payroll.pdf",
    )


def test_save_plan_contains_header_and_all_prospective_item_rows():
    plan = build_save_plan([candidate()], snapshot())[0]
    assert plan.planned_header["source_file_id"] == "file-1"
    assert plan.item_count == len(plan.items) == 3
    assert all(item.planned_row["statement_id"] ==
               plan.planned_header["statement_id"] for item in plan.items)
    assert plan.file_name == "2026-08-payroll.pdf"
    assert plan.statement_date == "2026-08-25"
    assert plan.parse_method == "pdf_text"
    assert plan.employer == "勤務先A"


def test_alias_unknown_and_needs_review_are_visible_without_guessing():
    plan = build_save_plan([candidate()], snapshot())[0]
    alias, unknown = plan.items[1:]
    assert alias.standard_item_name == "健康保険"
    assert alias.planned_row["standard_item_id"] == "health_insurance"
    assert unknown.section == "unknown"
    assert unknown.standard_item_name is None
    assert unknown.planned_row["standard_item_id"] is None
    assert unknown.needs_review
    assert unknown.value == "5,000"
    assert plan.recognized_item_count == 2
    assert plan.unknown_item_count == plan.needs_review_count == 1


def test_new_statement_would_create_header_and_correct_item_count():
    plan = build_save_plan([candidate()], snapshot())[0]
    assert plan.duplicate_status == "new"
    assert plan.would_create_header is True
    assert plan.would_create_items == plan.item_count == 3


def test_existing_source_file_is_reported_and_would_not_create_rows():
    existing = PayrollStatementRecord(
        statement_id="stored", source_file_id="file-1", content_hash="old-hash",
    )
    plan = build_save_plan([candidate()], snapshot(statements=[existing]))[0]
    assert plan.duplicate_status == "existing"
    assert plan.duplicate_reason == "source_file_id"
    assert plan.header_action == "skip_duplicate"
    assert plan.would_create_header is False
    assert plan.would_create_items == 0


def test_summary_aggregates_required_b4_counts():
    plans = build_save_plan([candidate()], snapshot())
    result = save_preview_summary(
        plans, snapshot(), sampled_files=2, failed_files=1,
    )
    assert result["read_only"] is True
    assert result["sampled_files"] == 2
    assert result["parsed_files"] == 1
    assert result["failed_files"] == 1
    assert result["would_create_headers"] == 1
    assert result["would_create_items"] == 3
    assert result["duplicate_count"] == 0
    assert result["unknown_item_count"] == 1
    assert result["needs_review_count"] == 1
    assert result["recognized_item_count"] == 2
    assert result["recognized_without_value_count"] == 0


def test_items_without_raw_value_are_diagnostic_only_but_zero_is_kept():
    source = parsed_statement().model_copy(deep=True)
    source.items.extend([
        PayrollItem(
            raw_item_name="所得税", section="deductions", raw_value=None,
            value=None, standard_item_candidate="income_tax", needs_review=True,
        ),
        PayrollItem(
            raw_item_name="住民税", section="deductions", raw_value="  ",
            value=None, standard_item_candidate="resident_tax", needs_review=True,
        ),
        PayrollItem(
            raw_item_name="出勤日数", section="attendance", raw_value="0",
            value=0, standard_item_candidate="attendance_days",
        ),
        PayrollItem(
            raw_item_name="残業時間", section="attendance", raw_value="1.5",
            value=1.5, standard_item_candidate="overtime_hours",
        ),
        PayrollItem(
            raw_item_name="勤務区分", section="attendance", raw_value="通常勤務",
            value="通常勤務", needs_review=True,
        ),
        PayrollItem(
            raw_item_name="空欄独自項目", section="unknown", raw_value=None,
            value=None, needs_review=True,
        ),
    ])
    standards = [
        *snapshot().standard_items,
        PayrollStandardItemRecord(
            standard_item_id="income_tax", standard_name="所得税",
            section="deduction", value_type="money",
        ),
        PayrollStandardItemRecord(
            standard_item_id="resident_tax", standard_name="住民税",
            section="deduction", value_type="money",
        ),
        PayrollStandardItemRecord(
            standard_item_id="attendance_days", standard_name="出勤日数",
            section="attendance", value_type="days",
        ),
        PayrollStandardItemRecord(
            standard_item_id="overtime_hours", standard_name="残業時間",
            section="attendance", value_type="hours",
        ),
    ]
    target = snapshot()
    target.standard_items = standards
    storage = phase_a_to_storage_candidate(
        source, employer_id="employer-1", statement_label="給与明細",
        aliases=target.aliases,
    )

    plan = build_save_plan([storage], target)[0]

    assert [item.raw_item_name for item in plan.items] == [
        "基本給", "健康保険料", "調整手当A", "出勤日数", "残業時間", "勤務区分",
    ]
    assert plan.item_count == plan.would_create_items == 6
    assert plan.recognized_item_count == 4
    assert plan.recognized_without_value_count == 2
    assert plan.unknown_item_count == 2
    assert plan.needs_review_count == 2
    assert next(item for item in plan.items
                if item.raw_item_name == "出勤日数").value == 0


def test_union_dues_resolves_without_promoting_other_unknown_items():
    source = parsed_statement().model_copy(deep=True)
    source.items.extend([
        PayrollItem(raw_item_name="組合費", section="unknown", raw_value="7,100",
                    value=7100),
        PayrollItem(raw_item_name="一斉預金", section="unknown", raw_value="2,000",
                    value=2000),
        PayrollItem(raw_item_name="深夜勤務", section="unknown", raw_value="2,461",
                    value=2461),
        PayrollItem(raw_item_name="支給合計", section="summary", raw_value="320,000",
                    value=320000, standard_item_candidate="gross_pay"),
    ])
    target = snapshot()
    target.standard_items.append(PayrollStandardItemRecord(
        standard_item_id="union_dues", standard_name="組合費",
        section="deduction", value_type="money",
    ))
    target.aliases.append(PayrollItemAliasRecord(
        raw_item_name="組合費", standard_item_id="union_dues",
    ))
    storage = phase_a_to_storage_candidate(source, aliases=target.aliases)

    plan = build_save_plan([storage], target)[0]
    by_name = {item.raw_item_name: item for item in plan.items}
    dues = by_name["組合費"]
    assert dues.standard_item_name == "組合費"
    assert dues.section == "deduction"
    assert dues.value == 7100
    assert dues.planned_row["standard_item_id"] == "union_dues"
    assert not dues.needs_review
    for raw_name in ("一斉預金", "深夜勤務", "支給合計"):
        assert by_name[raw_name].standard_item_name is None
        assert by_name[raw_name].needs_review


def test_matching_header_summary_items_are_excluded_from_save_candidates():
    source = parsed_statement().model_copy(deep=True)
    source.gross_pay = 320000
    source.total_deductions = 50000
    source.net_pay = 270000
    source.items.extend([
        PayrollItem(raw_item_name="支給合計", section="summary", raw_value="320,000",
                    value=320000, standard_item_candidate="gross_pay"),
        PayrollItem(raw_item_name="控除合計", section="summary", raw_value="50,000",
                    value=50000, standard_item_candidate="total_deductions"),
        PayrollItem(raw_item_name="差引支給額", section="summary", raw_value="270,000",
                    value=270000, standard_item_candidate="net_pay"),
    ])

    plan = build_save_plan([phase_a_to_storage_candidate(source)], snapshot())[0]

    assert [item.raw_item_name for item in plan.items] == [
        "基本給", "健康保険料", "調整手当A",
    ]
    assert plan.item_count == plan.would_create_items == 3
    assert plan.unknown_item_count == plan.needs_review_count == 1


def test_header_summary_item_is_not_excluded_without_header_value():
    source = parsed_statement().model_copy(deep=True)
    source.gross_pay = None
    source.items.append(PayrollItem(
        raw_item_name="支給合計", section="summary", raw_value="320,000",
        value=320000, standard_item_candidate="gross_pay",
    ))

    plan = build_save_plan([phase_a_to_storage_candidate(source)], snapshot())[0]

    assert "支給合計" in {item.raw_item_name for item in plan.items}


def test_header_summary_item_is_not_excluded_without_confirmed_item_value():
    source = parsed_statement().model_copy(deep=True)
    source.gross_pay = 320000
    source.items.append(PayrollItem(
        raw_item_name="支給合計", section="summary", raw_value="320,000",
        value=None, standard_item_candidate="gross_pay", needs_review=True,
    ))

    plan = build_save_plan([phase_a_to_storage_candidate(source)], snapshot())[0]

    assert "支給合計" in {item.raw_item_name for item in plan.items}


def test_header_summary_item_is_not_excluded_when_value_differs():
    source = parsed_statement().model_copy(deep=True)
    source.gross_pay = 320000
    source.items.append(PayrollItem(
        raw_item_name="支給合計", section="summary", raw_value="319,999",
        value=319999, standard_item_candidate="gross_pay",
    ))

    plan = build_save_plan([phase_a_to_storage_candidate(source)], snapshot())[0]

    assert "支給合計" in {item.raw_item_name for item in plan.items}


def test_non_summary_item_matching_header_value_is_not_excluded():
    source = parsed_statement().model_copy(deep=True)
    source.gross_pay = 300000

    plan = build_save_plan([phase_a_to_storage_candidate(source)], snapshot())[0]

    assert "基本給" in {item.raw_item_name for item in plan.items}
    assert plan.unknown_item_count == plan.needs_review_count == 1


def test_drive_preview_never_calls_drive_write_methods():
    service = DriveService([
        {"id": "file-1", "name": "salary.pdf", "mimeType": "application/pdf"},
    ])
    result = drive_save_preview(
        "folder-123456", snapshot(), service=service,
        downloader=lambda _file_id: b"payroll",
        parser=lambda _path: parsed_statement(),
    )
    assert result["sampled_files"] == result["parsed_files"] == 1
    assert service.write_calls == []
