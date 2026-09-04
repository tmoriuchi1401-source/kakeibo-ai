import pytest

from app.payroll_master_sync_materialization import (
    payroll_master_sync_to_materialization_plan,
)
from app.payroll_master_sync import build_master_sync_plan
from app.payroll_models import PayrollItem, PayrollPreview
from app.payroll_sheets import PayrollSheetsSnapshot, validate_sheet_schema
from app.payroll_storage import (
    PAYROLL_SCHEMAS,
    PayrollStandardItemRecord,
    phase_a_to_storage_candidate,
)
from app.payroll_storage_preview import build_write_plan
from app.payroll_write_plan_materialization import (
    payroll_write_plan_to_materialization_plan,
)


def snapshot():
    return PayrollSheetsSnapshot(
        schemas=[
            validate_sheet_schema(key, columns)
            for key, columns in PAYROLL_SCHEMAS.items()
        ],
        standard_items=[PayrollStandardItemRecord(
            standard_item_id="basic_pay",
            standard_name="基本給",
            section="earning",
            value_type="money",
        )],
    )


def write_plan(**overrides):
    preview = PayrollPreview(
        file_type="pdf",
        extraction_method="pdf_text",
        pay_period="2026-08",
        parse_status="success",
        items=[PayrollItem(
            raw_item_name="基本給",
            section="earnings",
            raw_value="300,000",
            value=300000,
            standard_item_candidate="basic_pay",
        )],
    )
    candidate = phase_a_to_storage_candidate(
        preview,
        employer_id="employer-1",
        statement_type="salary",
        source_type="drive",
        source_file_id="file-1",
        content_hash="hash-1",
    )
    for name, value in overrides.items():
        setattr(candidate.statement, name, value)
    return build_write_plan([candidate], snapshot())[0]


def test_ready_write_plan_projects_authoritative_header_then_items_without_io():
    authority = write_plan()

    materialized = payroll_write_plan_to_materialization_plan(authority)

    assert materialized.source is not None
    assert materialized.source.identity_kind == "source_file_id"
    assert materialized.source.identity_value == authority.identity.source_file_id
    assert materialized.source.content_hash == authority.identity.content_hash
    assert [operation.operation_id for operation in materialized.operations] == [
        "append_statement_header", "append_statement_items",
    ]
    assert [operation.target["sheet_key"] for operation in materialized.operations] == [
        "payroll_statements", "payroll_items",
    ]
    header, items = materialized.operations
    assert header.payload["rows"][0]["statement_id"] == authority.identity.statement_id
    assert "imported_at" not in header.payload["rows"][0]
    expected_header = authority.planned_header_rows[0].as_dict()
    expected_header.pop("imported_at")
    assert dict(header.payload["rows"][0]) == expected_header
    assert [dict(row) for row in items.payload["rows"]] == [
        row.as_dict() for row in authority.planned_item_rows
    ]
    assert header.preconditions[-2].expected == "append"
    assert items.preconditions[-2].expected == "append"


def test_plan_identity_is_deterministic_and_tracks_semantic_rows_not_diagnostics():
    authority = write_plan()
    baseline = payroll_write_plan_to_materialization_plan(authority)
    same = payroll_write_plan_to_materialization_plan(authority)
    diagnostic_only = payroll_write_plan_to_materialization_plan(
        authority.model_copy(update={"reasons": ("safe_new_statement", "diagnostic_only")}),
    )
    changed_header = authority.planned_header_rows[0].model_copy(update={
        "values": tuple(
            "different-employer" if column == "employer_id" else value
            for column, value in zip(
                authority.planned_header_rows[0].columns,
                authority.planned_header_rows[0].values,
            )
        ),
    })
    changed_item = authority.planned_item_rows[0].model_copy(update={
        "values": tuple(
            123456 if column == "value" else value
            for column, value in zip(
                authority.planned_item_rows[0].columns,
                authority.planned_item_rows[0].values,
            )
        ),
    })
    changed_runtime = authority.planned_header_rows[0].model_copy(update={
        "values": tuple(
            "2099-01-01T00:00:00Z" if column == "imported_at" else value
            for column, value in zip(
                authority.planned_header_rows[0].columns,
                authority.planned_header_rows[0].values,
            )
        ),
    })

    assert baseline.plan_id == same.plan_id == diagnostic_only.plan_id
    assert payroll_write_plan_to_materialization_plan(
        authority.model_copy(update={"planned_header_rows": (changed_header,)}),
    ).plan_id != baseline.plan_id
    assert payroll_write_plan_to_materialization_plan(
        authority.model_copy(update={"planned_item_rows": (changed_item,)}),
    ).plan_id != baseline.plan_id
    assert payroll_write_plan_to_materialization_plan(
        authority.model_copy(update={"planned_header_rows": (changed_runtime,)}),
    ).plan_id == baseline.plan_id


@pytest.mark.parametrize("overrides", [
    {"needs_review": True},
    {"source_file_id": None},
    {"content_hash": None},
])
def test_non_ready_statement_plans_are_not_materialization_intent(overrides):
    with pytest.raises(ValueError, match="not_ready_for_materialization"):
        payroll_write_plan_to_materialization_plan(write_plan(**overrides))


def test_ready_plan_with_missing_source_identity_fails_closed():
    authority = write_plan()
    missing_source = authority.model_copy(update={
        "identity": authority.identity.model_copy(update={"source_file_id": None}),
    })

    with pytest.raises(ValueError, match="source_identity_required"):
        payroll_write_plan_to_materialization_plan(missing_source)


def test_statement_adapter_does_not_change_source_less_master_sync_behavior():
    master_sync = payroll_master_sync_to_materialization_plan(build_master_sync_plan(snapshot()))

    assert master_sync.source is None
