from dataclasses import dataclass

from app.payroll_models import PayrollItem, PayrollPreview
from app.payroll_storage import (
    INITIAL_ALIASES,
    INITIAL_STANDARD_ITEMS,
    PAYROLL_SCHEMAS,
    PayrollEmployerRecord,
    PayrollItemAliasRecord,
    PayrollStandardItemRecord,
    PayrollStatementItemRecord,
    PayrollStatementRecord,
    classify_statement_type,
    decide_duplicate,
    phase_a_to_storage_candidate,
    resolve_alias,
)


def preview(*, review=False, unknown=False, pay_date=None):
    return PayrollPreview(
        file_type="image", extraction_method="ocr", pay_period="2026-08",
        pay_date=pay_date, gross_pay=320000, total_deductions=50000, net_pay=270000,
        parse_status="success",
        items=[PayrollItem(
            raw_item_name="独自手当" if unknown else "基本給",
            section="earnings", raw_value="300,000", value=300000,
            standard_item_candidate=None if unknown else "basic_pay",
            confidence=45 if review else 98, needs_review=review,
            page=1, x=10, y=20, row=0, column=0,
        )],
    )


def statement(**overrides):
    values = dict(employer_id="employer-1", statement_type="salary",
                  pay_period="2026-08", source_file_id="file-1", content_hash="hash-1")
    values.update(overrides)
    return PayrollStatementRecord(**values)


def test_schema_and_generated_internal_ids_are_complete_and_unique():
    first = phase_a_to_storage_candidate(preview(), statement_label="給与明細")
    second = phase_a_to_storage_candidate(preview(), statement_label="給与明細")
    assert first.statement.statement_id != second.statement.statement_id
    assert first.items[0].item_id != second.items[0].item_id
    assert first.items[0].statement_id == first.statement.statement_id
    assert set(PAYROLL_SCHEMAS) == {
        "payroll_statements", "payroll_items", "payroll_standard_items",
        "payroll_item_aliases", "payroll_employers",
    }


def test_statement_type_is_only_classified_from_explicit_evidence():
    assert classify_statement_type("給与明細書") == "salary"
    assert classify_statement_type("夏季賞与明細") == "bonus"
    assert classify_statement_type("給与調整明細") == "adjustment"
    assert classify_statement_type("名称不明") == "other"
    assert classify_statement_type(None) == "other"


def test_pay_date_none_is_preserved_without_inference():
    result = phase_a_to_storage_candidate(preview(pay_date=None), statement_label="給与明細")
    assert result.statement.pay_date is None


def test_unknown_item_and_raw_fields_are_preserved_but_not_confirmed():
    result = phase_a_to_storage_candidate(preview(unknown=True), statement_label="給与明細")
    item = result.items[0]
    assert item.raw_item_name == "独自手当"
    assert item.raw_value == "300,000"
    assert item.standard_item_id is None
    assert item.section == "earning"
    assert item.value is None
    assert item.needs_review
    assert item.review_status == "pending"


def test_uncertain_ocr_value_is_not_promoted_to_confirmed_value():
    result = phase_a_to_storage_candidate(preview(review=True), statement_label="給与明細")
    item = result.items[0]
    assert item.raw_value == "300,000"
    assert item.value is None
    assert item.needs_review
    assert item.review_status == "pending"
    assert result.statement.needs_review


def test_item_model_itself_cannot_confirm_a_pending_value():
    item = PayrollStatementItemRecord(
        statement_id="statement", raw_item_name="OCR項目", raw_value="12,34?",
        value=12345, needs_review=True,
    )
    assert item.raw_value == "12,34?"
    assert item.value is None
    assert item.review_status == "pending"


def test_inactive_standard_item_is_retained_in_catalog():
    retired = PayrollStandardItemRecord(
        standard_item_id="retired", standard_name="旧手当", section="earning",
        value_type="money", active=False,
    )
    catalog = (*INITIAL_STANDARD_ITEMS, retired)
    assert retired in catalog
    assert not catalog[-1].active


def test_common_and_employer_specific_aliases():
    aliases = (
        *INITIAL_ALIASES,
        PayrollItemAliasRecord(raw_item_name="特別手当", standard_item_id="commuting_allowance"),
        PayrollItemAliasRecord(raw_item_name="特別手当", standard_item_id="overtime_pay",
                               employer_id="employer-1"),
    )
    assert resolve_alias("本給", aliases, "employer-1") == "basic_pay"
    assert resolve_alias("特別手当", aliases, "employer-1") == "overtime_pay"
    assert resolve_alias("特別手当", aliases, "employer-2") == "commuting_allowance"


def test_inactive_alias_is_not_used():
    alias = PayrollItemAliasRecord(raw_item_name="旧名称", standard_item_id="basic_pay",
                                   active=False)
    assert resolve_alias("旧名称", [alias]) is None


def test_duplicate_priority_source_file_id_then_content_hash():
    existing = statement(statement_id="stored")
    by_file = statement(source_file_id="file-1", content_hash="different",
                        pay_period="2026-09")
    assert decide_duplicate(by_file, [existing]).model_dump() == {
        "status": "duplicate", "reason": "source_file_id", "matched_statement_id": "stored",
    }
    by_hash = statement(source_file_id="file-2", content_hash="hash-1",
                        pay_period="2026-09")
    assert decide_duplicate(by_hash, [existing]).reason == "content_hash"


def test_same_statement_key_with_different_hash_needs_review():
    existing = statement(statement_id="stored")
    revised = statement(source_file_id="file-2", content_hash="hash-2")
    decision = decide_duplicate(revised, [existing])
    assert decision.status == "needs_review"
    assert decision.reason == "statement_key"


def test_completely_new_statement_is_new():
    existing = statement()
    new = statement(employer_id="employer-2", pay_period="2026-09",
                    source_file_id="file-2", content_hash="hash-2")
    assert decide_duplicate(new, [existing]).status == "new"


def test_phase_a_conversion_is_pure_and_has_no_external_writes():
    @dataclass
    class WriteSpy:
        calls: int = 0

        def write(self):
            self.calls += 1

    sheets = WriteSpy()
    drive = WriteSpy()
    source = preview()
    before = source.model_dump()
    result = phase_a_to_storage_candidate(
        source, employer_id="employer-1", statement_label="給与明細",
        source_type="drive", source_file_id="file-1", content_hash="hash-1",
    )
    assert result.statement.statement_type == "salary"
    assert result.statement.source_type == "drive"
    assert result.items[0].value == 300000
    assert result.items[0].review_status == "not_required"
    assert source.model_dump() == before
    assert sheets.calls == drive.calls == 0


def test_minimum_employer_model_does_not_require_real_company_data():
    employer = PayrollEmployerRecord(employer_label="勤務先A")
    assert employer.active
    assert employer.start_date is None
    assert employer.end_date is None
