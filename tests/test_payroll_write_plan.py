import pytest
from pydantic import ValidationError

from app.payroll_models import PayrollItem, PayrollPreview
from app.payroll_sheets import PayrollSheetsSnapshot, validate_sheet_schema
from app.payroll_storage import (
    PAYROLL_ITEM_COLUMNS,
    PAYROLL_SCHEMAS,
    PAYROLL_STATEMENT_COLUMNS,
    PayrollStandardItemRecord,
    PayrollStatementRecord,
    phase_a_to_storage_candidate,
)
from app.payroll_storage_preview import (
    build_append_plan,
    build_save_plan,
    build_write_plan,
)


def snapshot(*, statements=None, valid_schema=True):
    schemas = [
        validate_sheet_schema(key, columns)
        for key, columns in PAYROLL_SCHEMAS.items()
    ]
    if not valid_schema:
        schemas[0] = validate_sheet_schema("payroll_statements", None)
    return PayrollSheetsSnapshot(
        schemas=schemas,
        statements=statements or [],
        standard_items=[PayrollStandardItemRecord(
            standard_item_id="basic_pay", standard_name="基本給",
            section="earning", value_type="money",
        )],
    )


def candidate(**overrides):
    preview = PayrollPreview(
        file_type="pdf", extraction_method="pdf_text", pay_period="2026-08",
        parse_status="success", items=[PayrollItem(
            raw_item_name="基本給", section="earnings", raw_value="300,000",
            value=300000, standard_item_candidate="basic_pay",
        )],
    )
    result = phase_a_to_storage_candidate(
        preview,
        employer_id="employer-1",
        statement_type="salary",
        source_type="drive",
        source_file_id="file-1",
        content_hash="hash-1",
    )
    for key, value in overrides.items():
        setattr(result.statement, key, value)
    return result


def assert_zero_rows(plan):
    assert plan.eligibility == "ineligible"
    assert plan.header_action == plan.item_action == "none"
    assert plan.planned_header_rows == ()
    assert plan.planned_item_rows == ()
    assert plan.would_create_header is False
    assert plan.would_create_items == 0


def test_safe_new_statement_is_immutable_append_plan_for_future_writer():
    plan = build_write_plan([candidate()], snapshot())[0]

    assert plan.eligibility == "eligible"
    assert plan.status == "ready"
    assert plan.reason == "safe_new_statement"
    assert plan.header_action == plan.item_action == "append"
    assert len(plan.planned_header_rows) == len(plan.planned_item_rows) == 1
    assert plan.planned_header_rows[0].columns == PAYROLL_STATEMENT_COLUMNS
    assert plan.planned_item_rows[0].columns == PAYROLL_ITEM_COLUMNS
    assert plan.planned_header_rows[0].as_dict()["employer_id"] == "employer-1"
    with pytest.raises(ValidationError):
        plan.status = "blocked"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"needs_review": True}, "statement_needs_review"),
        ({"parse_status": "partial"}, "statement_partial"),
        ({"parse_status": "failed"}, "statement_failed"),
        ({"employer_id": None}, "employer_id_missing"),
        ({"statement_type": None}, "statement_type_missing"),
        ({"statement_type": "other"}, "statement_type_other"),
        ({"statement_type": "unsupported"}, "statement_type_unsupported"),
        ({"source_file_id": None}, "source_file_id_missing"),
        ({"content_hash": None}, "content_hash_missing"),
        ({"pay_period": None}, "pay_period_missing"),
    ],
)
def test_required_identity_and_statement_fail_closed(overrides, reason):
    plan = build_write_plan([candidate(**overrides)], snapshot())[0]
    assert reason in plan.reasons
    assert_zero_rows(plan)


def test_partial_and_review_item_with_raw_value_never_make_planned_rows():
    source = candidate(parse_status="partial")
    source.items[0].needs_review = True
    source.items[0].review_status = "pending"
    source.items[0].value = None
    assert source.items[0].raw_value == "300,000"

    plan = build_write_plan([source], snapshot())[0]

    assert "statement_partial" in plan.reasons
    assert "item_needs_review" in plan.reasons
    assert_zero_rows(plan)
    assert "raw_value" not in plan.model_dump_json()


def test_no_storage_items_is_fail_closed():
    source = candidate()
    source.items = []
    plan = build_write_plan([source], snapshot())[0]
    assert "no_planned_items" in plan.reasons
    assert_zero_rows(plan)


def test_exact_duplicate_and_content_hash_duplicate_are_skipped():
    existing = candidate().statement.model_copy(update={"statement_id": "stored"})
    exact = build_write_plan([candidate()], snapshot(statements=[existing]))[0]
    hash_duplicate = build_write_plan(
        [candidate(source_file_id="file-2", pay_period="2026-09")],
        snapshot(statements=[existing]),
    )[0]

    assert exact.status == "skipped_duplicate"
    assert exact.reason == "exact_duplicate"
    assert exact.duplicate.matched_statement_id == "stored"
    assert_zero_rows(exact)
    assert hash_duplicate.status == "skipped_duplicate"
    assert hash_duplicate.reason == "content_hash_duplicate"
    assert_zero_rows(hash_duplicate)


def test_revision_and_changed_source_identity_are_conflicts_not_duplicates():
    existing = candidate().statement.model_copy(update={"statement_id": "stored"})
    revision = build_write_plan(
        [candidate(source_file_id="file-2", content_hash="hash-2")],
        snapshot(statements=[existing]),
    )[0]
    changed_source = build_write_plan(
        [candidate(content_hash="hash-2", pay_period="2026-09")],
        snapshot(statements=[existing]),
    )[0]

    assert revision.status == "blocked"
    assert revision.reason == "revision_conflict"
    assert revision.duplicate.status == "conflict"
    assert_zero_rows(revision)
    assert changed_source.status == "blocked"
    assert changed_source.reason == "source_identity_conflict"
    assert changed_source.duplicate.status == "conflict"
    assert_zero_rows(changed_source)


def test_invalid_schema_blocks_all_rows():
    plan = build_write_plan([candidate()], snapshot(valid_schema=False))[0]
    assert plan.reason == "schema_invalid"
    assert_zero_rows(plan)


def test_would_create_values_are_always_derived_from_authoritative_actions():
    ready, blocked = build_save_plan(
        [candidate(), candidate(source_file_id="file-2", content_hash="hash-2",
                                needs_review=True)],
        snapshot(),
    )
    for projected in (ready, blocked):
        authority = projected.write_plan
        assert projected.would_create_header == (
            authority.header_action == "append"
        )
        assert projected.would_create_items == (
            len(authority.planned_item_rows)
            if authority.item_action == "append" else 0
        )
        assert projected.planned_header == (
            authority.planned_header_rows[0].as_dict()
            if authority.planned_header_rows else {}
        )
    assert blocked.items[0].planned_row == {}


def test_legacy_plans_are_only_projections_of_authoritative_plan():
    sources = [candidate(), candidate(source_file_id="file-2", needs_review=True)]
    authority = build_write_plan(sources, snapshot())
    append = build_append_plan(sources, snapshot())
    save = build_save_plan(sources, snapshot())

    assert [plan.header_rows_to_append for plan in append] == [
        len(plan.planned_header_rows) for plan in authority
    ]
    assert [plan.item_rows_to_append for plan in append] == [
        len(plan.planned_item_rows) for plan in authority
    ]
    assert [plan.write_plan for plan in save] == authority


def test_three_explicit_safe_identities_can_append_while_partial_stays_blocked():
    sources = [
        candidate(source_file_id=f"file-{index}", content_hash=f"hash-{index}")
        for index in range(1, 4)
    ]
    sources.append(candidate(
        source_file_id="file-4", content_hash="hash-4", parse_status="partial",
    ))

    plans = build_write_plan(sources, snapshot())

    assert [plan.status for plan in plans] == ["ready", "ready", "ready", "blocked"]
    assert all(plan.identity.employer_id == "employer-1" for plan in plans)
    assert_zero_rows(plans[3])


def test_write_plan_contains_only_storage_columns_not_diagnostic_fields():
    plan = build_write_plan([candidate()], snapshot())[0]
    header = plan.planned_header_rows[0].as_dict()
    item = plan.planned_item_rows[0].as_dict()

    assert tuple(header) == PAYROLL_STATEMENT_COLUMNS
    assert tuple(item) == PAYROLL_ITEM_COLUMNS
    assert "review_reasons" not in header
    assert "review_reason_code" not in item
    assert "employee" not in header
