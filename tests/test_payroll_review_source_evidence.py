from dataclasses import replace

from pypdf import PdfWriter

from app.payroll_models import PayrollItem, PayrollPreview
from app.payroll_ocr import ExtractedPayrollText, PositionedText
from app.payroll_review_authority import (
    apply_payroll_review_assertion,
    capture_payroll_review_evidence,
    create_payroll_review_assertion,
    preview_payroll_review_assertion,
)
from app.payroll_review_source_evidence import (
    capture_pdf_text_review_source_value,
)
from app.payroll_sheets import PayrollSheetsSnapshot, validate_sheet_schema
from app.payroll_storage import (
    PAYROLL_SCHEMAS,
    PayrollEmployerRecord,
    phase_a_to_storage_candidate,
)


KEY = b"synthetic-review-source-key-00001"


def pdf_path(tmp_path):
    path = tmp_path / "source.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=100)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def extracted(value="21"):
    tokens = (
        PositionedText("出勤日数", 1, 10, 20, 40, 10, 100),
        PositionedText(value, 1, 120, 20, 20, 10, 100),
    )
    return ExtractedPayrollText("出勤日数 " + value, "pdf", "pdf_text", tokens)


def fixture(path):
    import hashlib

    preview = PayrollPreview(
        file_type="pdf", extraction_method="pdf_text", pay_period="2026-08",
        parse_status="success", items=[PayrollItem(
            raw_item_name="出勤日数", section="attendance", value=None,
            raw_value=None, needs_review=True,
            review_reason_code="pairing_not_found",
        )],
    )
    candidate = phase_a_to_storage_candidate(
        preview, employer_id="employer-1", statement_type="salary",
        source_type="drive", source_file_id="file-1",
        content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    snapshot = PayrollSheetsSnapshot(
        schemas=[validate_sheet_schema(key, columns)
                 for key, columns in PAYROLL_SCHEMAS.items()],
        employers=[PayrollEmployerRecord(
            employer_id="employer-1", employer_label="Example Employer",
        )],
    )
    return candidate, snapshot


def test_exact_source_value_binding_allows_exclusion_preview_without_apply(
    monkeypatch, tmp_path,
):
    path = pdf_path(tmp_path)
    monkeypatch.setattr(
        "app.payroll_review_source_evidence.extract_payroll_text",
        lambda _path: extracted(),
    )
    candidate, snapshot = fixture(path)
    binding = capture_pdf_text_review_source_value(
        path, candidate, 0, "21", local_key=KEY,
    )
    evidence = capture_payroll_review_evidence(
        candidate, snapshot, 0, local_key=KEY,
        source_value_binding=binding,
    )
    assertion = create_payroll_review_assertion(
        evidence, decision="exclude_non_item", operator_id="reviewer-1",
        local_key=KEY,
    )

    result = preview_payroll_review_assertion(
        candidate, snapshot, evidence, assertion, local_key=KEY,
    )
    unconfirmed = apply_payroll_review_assertion(
        candidate, snapshot, evidence, assertion, local_key=KEY,
    )

    assert result.accepted
    assert binding.relation == "exact_same_row_right"
    assert binding.source_value == "21"
    assert not unconfirmed.applied
    assert candidate.items[0].needs_review

    applied = apply_payroll_review_assertion(
        candidate, snapshot, evidence, assertion, local_key=KEY, confirmed=True,
    )
    replay = preview_payroll_review_assertion(
        applied.candidate, snapshot, evidence, assertion, local_key=KEY,
    )
    assert applied.applied
    assert applied.candidate.items[0].raw_value is None
    assert not replay.accepted
    assert replay.reason_code == "review_evidence_not_current"


def test_source_value_binding_rejects_wrong_value_ambiguity_and_tampering(
    monkeypatch, tmp_path,
):
    path = pdf_path(tmp_path)
    candidate, snapshot = fixture(path)
    monkeypatch.setattr(
        "app.payroll_review_source_evidence.extract_payroll_text",
        lambda _path: extracted(),
    )
    try:
        capture_pdf_text_review_source_value(
            path, candidate, 0, "22", local_key=KEY,
        )
    except ValueError as exc:
        assert str(exc) == "review_value_occurrence_ambiguous"
    else:
        raise AssertionError("wrong value accepted")

    monkeypatch.setattr(
        "app.payroll_review_source_evidence.extract_payroll_text",
        lambda _path: ExtractedPayrollText(
            "duplicate", "pdf", "pdf_text",
            extracted().tokens + (PositionedText("21", 1, 130, 20, 20, 10, 100),),
        ),
    )
    try:
        capture_pdf_text_review_source_value(
            path, candidate, 0, "21", local_key=KEY,
        )
    except ValueError as exc:
        assert str(exc) == "review_value_occurrence_ambiguous"
    else:
        raise AssertionError("ambiguous value accepted")

    monkeypatch.setattr(
        "app.payroll_review_source_evidence.extract_payroll_text",
        lambda _path: extracted(),
    )
    binding = capture_pdf_text_review_source_value(
        path, candidate, 0, "21", local_key=KEY,
    )
    tampered = replace(binding, source_value="22")
    try:
        capture_payroll_review_evidence(
            candidate, snapshot, 0, local_key=KEY,
            source_value_binding=tampered,
        )
    except ValueError as exc:
        assert str(exc) == "source_value_binding_signature_invalid"
    else:
        raise AssertionError("tampered binding accepted")
