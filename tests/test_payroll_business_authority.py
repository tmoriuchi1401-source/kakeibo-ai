from app.payroll_business_authority import resolve_payroll_business_authority
from app.payroll_models import PayrollPreview
from app.payroll_storage import PayrollEmployerRecord


def preview(*, company_name=None, statement_label=None):
    return PayrollPreview(
        file_type="pdf", extraction_method="pdf_text",
        company_name=company_name, statement_label=statement_label,
    )


def test_unique_active_exact_match_is_business_authority():
    result = resolve_payroll_business_authority(
        preview(company_name="会社Ａ & 株式会社", statement_label="給与明細書"),
        [PayrollEmployerRecord(
            employer_id="employer-1", employer_label="会社A＆株式会社",
        )],
    )

    assert result.employer_id == "employer-1"
    assert result.statement_type == "salary"
    assert result.employer_source == "active_employer_master_exact_match"
    assert result.statement_type_source == "parser_explicit_statement_label"


def test_company_evidence_without_master_match_stays_unresolved():
    result = resolve_payroll_business_authority(
        preview(company_name="会社A株式会社"), [],
    )

    assert result.employer_id is None
    assert result.employer_reason == "active_employer_match_missing"


def test_near_match_is_not_treated_as_employer_authority():
    result = resolve_payroll_business_authority(
        preview(company_name="会社A株式会社"),
        [PayrollEmployerRecord(
            employer_id="employer-1", employer_label="会社A株式会社東京支店",
        )],
    )

    assert result.employer_id is None
    assert result.employer_reason == "active_employer_match_missing"


def test_duplicate_normalized_active_matches_fail_closed():
    result = resolve_payroll_business_authority(
        preview(company_name="会社A＆株式会社"),
        [
            PayrollEmployerRecord(
                employer_id="employer-1", employer_label="会社Ａ & 株式会社",
            ),
            PayrollEmployerRecord(
                employer_id="employer-2", employer_label="会社A＆株式会社",
            ),
        ],
    )

    assert result.employer_id is None
    assert result.employer_reason == "active_employer_match_ambiguous"


def test_inactive_employer_and_unsupported_type_do_not_create_authority():
    result = resolve_payroll_business_authority(
        preview(company_name="会社A株式会社", statement_label="支給明細書"),
        [PayrollEmployerRecord(
            employer_id="inactive", employer_label="会社A株式会社", active=False,
        )],
    )

    assert result.employer_id is None
    assert result.statement_type is None


def test_operator_authority_is_preserved_without_master_or_parser_evidence():
    result = resolve_payroll_business_authority(
        preview(), [], employer_id="operator-employer", statement_type="bonus",
    )

    assert result.employer_id == "operator-employer"
    assert result.statement_type == "bonus"
    assert result.employer_source == result.statement_type_source == "operator"
