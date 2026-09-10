from dataclasses import replace

import pytest

from app.payroll_models import PayrollItem, PayrollPreview
from app.payroll_production_runner import run_payroll_production_preview
from app.payroll_review_integration import PayrollReviewReloadRequest
from app.payroll_sheets import PayrollSheetsSnapshot, validate_sheet_schema
from app.payroll_source_reconciliation import (
    create_payroll_source_reconciliation_decision,
)
from app.payroll_storage import (
    PAYROLL_SCHEMAS,
    PayrollEmployerRecord,
    PayrollStandardItemRecord,
    phase_a_to_storage_candidate,
)


KEY = b"payroll-production-runner-test-key"


def candidate(
    source: str,
    content: str,
    *,
    pay_period: str = "2026-09",
    employer_id: str | None = "employer-1",
    statement_type: str | None = "salary",
    review: bool = False,
):
    preview = PayrollPreview(
        file_type="pdf",
        extraction_method="pdf_text",
        pay_period=pay_period,
        gross_pay=300000,
        total_deductions=50000,
        net_pay=250000,
        parse_status="success",
        items=[PayrollItem(
            raw_item_name="基本給",
            section="earnings",
            raw_value="300,000",
            value=300000,
            standard_item_candidate="basic_pay",
            needs_review=review,
            review_reason_code="low_confidence" if review else None,
        )],
    )
    return phase_a_to_storage_candidate(
        preview,
        employer_id=employer_id,
        statement_type=statement_type,
        source_type="drive",
        source_file_id=source,
        content_hash=content,
        standard_items=[PayrollStandardItemRecord(
            standard_item_id="basic_pay",
            standard_name="基本給",
            section="earning",
            value_type="money",
        )],
    )


def snapshot(stored):
    return PayrollSheetsSnapshot(
        schemas=[
            validate_sheet_schema(key, columns)
            for key, columns in PAYROLL_SCHEMAS.items()
        ],
        statements=[stored],
        standard_items=[PayrollStandardItemRecord(
            standard_item_id="basic_pay",
            standard_name="基本給",
            section="earning",
            value_type="money",
        )],
        employers=[PayrollEmployerRecord(
            employer_id="employer-1",
            employer_label="Employer",
        )],
    )


def test_bounded_run_classifies_ready_duplicate_alternate_review_and_conflict():
    source_3 = candidate("source-3", "3" * 64, pay_period="2026-08")
    sheets = snapshot(source_3.statement)
    exact = candidate("source-3", "3" * 64, pay_period="2026-08")
    alternate = candidate(
        "source-4", "4" * 64, pay_period="2026-08", review=True,
    )
    ready = candidate("source-6", "6" * 64, pay_period="2026-09")
    review = candidate(
        "source-7", "7" * 64, pay_period="2026-10", review=True,
    )
    conflict = candidate("source-8", "8" * 64, pay_period="2026-08")
    decision = create_payroll_source_reconciliation_decision(
        target=source_3.statement,
        alternate=alternate,
        operator_id="operator-1",
        created_at_utc="2026-09-11T00:00:00+00:00",
        local_key=KEY,
    )

    report = run_payroll_production_preview(
        [ready, exact, alternate, review, conflict],
        sheets,
        reconciliation_decisions=[decision],
        reconciliation_key=KEY,
    )

    assert [result.outcome for result in report.results] == [
        "WRITE_READY",
        "EXACT_DUPLICATE",
        "ALTERNATE_SOURCE_DUPLICATE",
        "NEEDS_REVIEW",
        "CONFLICT",
    ]
    assert report.writer_candidate_count == 1
    assert report.writer_invocation_count == 0
    assert report.actual_header_rows == report.actual_item_rows == 0
    assert report.results[0].content_hash == "6" * 64
    assert report.results[0].employer_id == "employer-1"
    assert report.results[0].statement_type == "salary"
    assert report.results[0].pay_period == "2026-09"
    assert report.results[2].target_statement_id == source_3.statement.statement_id
    assert all(
        result.planned_header_rows == result.planned_item_rows == 0
        for result in report.results[1:]
    )


@pytest.mark.parametrize("field", ["employer_id", "statement_type"])
def test_missing_business_authority_is_needs_review_and_never_ready(field):
    source_3 = candidate("source-3", "3" * 64, pay_period="2026-08")
    source = candidate("source-9", "9" * 64, pay_period="2026-11")
    setattr(source.statement, field, None)

    report = run_payroll_production_preview([source], snapshot(source_3.statement))

    assert report.results[0].outcome == "NEEDS_REVIEW"
    assert report.writer_candidate_count == report.writer_invocation_count == 0
    assert report.results[0].planned_header_rows == 0
    assert report.results[0].planned_item_rows == 0


def test_stale_review_journal_cannot_make_a_writer_candidate(tmp_path):
    source_3 = candidate("source-3", "3" * 64, pay_period="2026-08")
    source = candidate("source-a", "a" * 64, pay_period="2026-11")
    request = PayrollReviewReloadRequest(
        expected_content_hash="a" * 64,
        journal_path=tmp_path / "missing-journal.json",
        hmac_key_path=tmp_path / "missing-key",
        repository_root=tmp_path,
    )

    report = run_payroll_production_preview(
        [source],
        snapshot(source_3.statement),
        review_reload_request=request,
        review_journal_enabled=True,
    )

    assert report.results[0].outcome == "NEEDS_REVIEW"
    assert report.results[0].reason == "review_journal_key_load_failed"
    assert report.writer_candidate_count == report.writer_invocation_count == 0
    assert report.results[0].planned_header_rows == 0
    assert report.results[0].planned_item_rows == 0


def test_invalid_reconciliation_signature_remains_conflict():
    source_3 = candidate("source-3", "3" * 64, pay_period="2026-08")
    alternate = candidate("source-4", "4" * 64, pay_period="2026-08")
    signed = create_payroll_source_reconciliation_decision(
        target=source_3.statement,
        alternate=alternate,
        operator_id="operator-1",
        created_at_utc="2026-09-11T00:00:00+00:00",
        local_key=KEY,
    )

    report = run_payroll_production_preview(
        [alternate],
        snapshot(source_3.statement),
        reconciliation_decisions=[replace(signed, signature="0" * 64)],
        reconciliation_key=KEY,
    )

    assert report.results[0].outcome == "CONFLICT"
    assert report.results[0].reason == "reconciliation_signature_invalid"
    assert report.writer_candidate_count == report.writer_invocation_count == 0
    assert report.results[0].planned_header_rows == 0
    assert report.results[0].planned_item_rows == 0
