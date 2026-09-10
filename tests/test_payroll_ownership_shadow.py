from types import SimpleNamespace

import pytest

from app import cli
from app.payroll_diagnostic_evidence import observe_tokens
from app.payroll_models import PayrollPreview
from app.payroll_ocr import PositionedText
from app.payroll_ownership_integration import (
    DISABLED_PAYROLL_OWNERSHIP_SHADOW_REPORT,
    drive_payroll_ownership_shadow,
    evaluate_payroll_ownership_shadow_artifact,
    load_local_payroll_ownership_hmac_key,
)
from app.payroll_parser import parse_positioned_items
from app.payroll_sheets import PayrollSheetsSnapshot, validate_sheet_schema
from app.payroll_storage import (
    PAYROLL_SCHEMAS,
    PayrollEmployerRecord,
    PayrollStandardItemRecord,
    phase_a_to_storage_candidate,
)
from app.payroll_storage_preview import build_write_plan


KEY = b"shadow-evaluation-test-key-00000"


def sheets_snapshot():
    return PayrollSheetsSnapshot(
        schemas=[
            validate_sheet_schema(key, columns)
            for key, columns in PAYROLL_SCHEMAS.items()
        ],
        standard_items=[PayrollStandardItemRecord(
            standard_item_id="basic_pay",
            standard_name="basic",
            section="earning",
            value_type="money",
        )],
    )


def shadow_inputs():
    tokens = (
        PositionedText("基本給", 1, 0, 20, 30, 10, 100),
        PositionedText("1,234", 1, 40, 20, 30, 10, 100),
    )
    ownership_snapshot = observe_tokens(
        tokens,
        local_key=KEY,
        parser_mode="ocr",
        snapshot_context=("shadow-test", "ocr"),
    )
    preview = PayrollPreview(
        file_type="image",
        extraction_method="ocr",
        pay_period="2026-08",
        gross_pay=1234,
        total_deductions=0,
        net_pay=1234,
        items=list(parse_positioned_items(tokens, ocr=True)),
        parse_status="success",
    )
    storage = phase_a_to_storage_candidate(
        preview,
        employer_id="employer-1",
        statement_type="salary",
        source_type="drive",
        source_file_id="file-1",
        content_hash="content-hash-1",
        standard_items=sheets_snapshot().standard_items,
    )
    plan = build_write_plan([storage], sheets_snapshot())[0]
    return preview, storage, plan, ownership_snapshot


def test_shadow_artifact_attests_and_all_negative_controls_reject():
    preview, storage, plan, ownership_snapshot = shadow_inputs()

    result = evaluate_payroll_ownership_shadow_artifact(
        preview,
        storage,
        plan,
        ownership_snapshot,
        enabled=True,
        local_key=KEY,
        source_replay_closed=True,
        capture_reason="ownership_ready",
    )

    assert result.plan_status == "ready"
    assert result.authoritative_standard_claim_count == 1
    assert result.adoption_candidate_count == 1
    assert result.attestation_success_count == 1
    assert result.false_attestation_count == 0
    assert result.differential_unchanged
    assert dict(result.negative_controls) == {
        "employer_mismatch": 1,
        "field_mismatch": 1,
        "hidden_candidate": 1,
        "parser_mode_mismatch": 1,
        "review_contamination": 1,
        "snapshot_mismatch": 1,
        "stale_evidence": 1,
        "value_mismatch": 1,
    }


def test_shadow_disabled_does_not_touch_key_drive_parser_or_capture():
    class Poison:
        def __getattr__(self, _name):
            raise AssertionError("disabled shadow touched an input")

    result = drive_payroll_ownership_shadow(
        "not-inspected",
        Poison(),
        enabled=False,
        local_key=None,
        service=Poison(),
        downloader=Poison(),
        parser=Poison(),
        capture=Poison(),
        capture_pdf_text=Poison(),
    )

    assert result is DISABLED_PAYROLL_OWNERSHIP_SHADOW_REPORT
    assert result.safe_dict()["writer_invocation_count"] == 0
    assert result.safe_dict()["apply_invocation_count"] == 0


def test_drive_shadow_evaluates_pdf_text_capture_without_writer_or_apply():
    preview, _storage, _plan, ownership_snapshot = shadow_inputs()
    preview.file_type = "pdf"
    preview.extraction_method = "pdf_text"
    capture = SimpleNamespace(
        snapshot=ownership_snapshot.__class__(
            ownership_snapshot.snapshot_id,
            ownership_snapshot.tokens,
            ownership_snapshot.token_ids,
            ownership_snapshot.facts,
            ownership_snapshot.identity_ambiguous,
            "pdf",
        ),
        ownership_ready=True,
        reason="ownership_ready",
    )

    report = drive_payroll_ownership_shadow(
        "folder-00001", sheets_snapshot(), enabled=True, local_key=KEY,
        employer_id="employer-1", statement_type="salary",
        service=FakeDriveService(),
        downloader=lambda _file_id: b"synthetic-source-bytes",
        parser=lambda _path: preview,
        capture=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("OCR capture called for PDF text")
        ),
        capture_pdf_text=lambda _path, *, local_key: capture,
    ).safe_dict()

    assert report["evaluated_files"] == 1
    assert report["parser_modes"] == {"pdf_text": 1}
    assert report["candidate_rejections"] != {
        "production_pdf_text_ownership_capture_unavailable": 1,
    }
    assert report["false_attestation_count"] == 0
    assert report["differential_unchanged"]
    assert report["writer_invocation_count"] == report["apply_invocation_count"] == 0


def test_drive_shadow_does_not_capture_when_statement_type_is_missing():
    preview, _storage, _plan, _ownership_snapshot = shadow_inputs()

    report = drive_payroll_ownership_shadow(
        "folder-00001", sheets_snapshot(), enabled=True, local_key=KEY,
        employer_id="employer-1", statement_type=None,
        service=FakeDriveService(),
        downloader=lambda _file_id: b"synthetic-source-bytes",
        parser=lambda _path: preview,
        capture=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ownership capture called without statement type")
        ),
        capture_pdf_text=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("PDF capture called without statement type")
        ),
    ).safe_dict()

    assert report["claim_count"] == 0
    assert report["adoption_candidate_count"] == 0
    assert report["candidate_rejections"] == {"statement_type_missing": 1}
    assert report["false_attestation_count"] == 0
    assert report["writer_invocation_count"] == report["apply_invocation_count"] == 0


class FakeDriveService:
    def files(self):
        return self

    def list(self, **_kwargs):
        return self

    def execute(self):
        return {"files": [{"id": "file-1", "name": "statement.png"}]}


def test_drive_shadow_builds_typed_request_without_writer_or_apply():
    preview, _storage, _plan, ownership_snapshot = shadow_inputs()
    capture = SimpleNamespace(
        snapshot=ownership_snapshot,
        ownership_ready=True,
        reason="ownership_ready",
    )

    report = drive_payroll_ownership_shadow(
        "folder-00001",
        sheets_snapshot(),
        enabled=True,
        local_key=KEY,
        employer_id="employer-1",
        statement_type="salary",
        service=FakeDriveService(),
        downloader=lambda _file_id: b"synthetic-source-bytes",
        parser=lambda _path: preview,
        capture=lambda _path, *, local_key: capture,
    ).safe_dict()

    assert report["sampled_files"] == report["evaluated_files"] == 1
    assert report["attestation_success_count"] == 1
    assert report["false_attestation_count"] == 0
    assert report["differential_unchanged"]
    assert report["writer_invocation_count"] == report["apply_invocation_count"] == 0


def test_drive_shadow_uses_business_authority_without_ownership_inventing_it():
    preview, _storage, _plan, ownership_snapshot = shadow_inputs()
    preview.company_name = "勤務先A株式会社"
    preview.statement_label = "給与明細書"
    target = sheets_snapshot()
    target.employers = [PayrollEmployerRecord(
        employer_id="employer-1", employer_label="勤務先A株式会社",
    )]
    capture = SimpleNamespace(
        snapshot=ownership_snapshot,
        ownership_ready=True,
        reason="ownership_ready",
    )

    report = drive_payroll_ownership_shadow(
        "folder-00001", target, enabled=True, local_key=KEY,
        service=FakeDriveService(),
        downloader=lambda _file_id: b"synthetic-source-bytes",
        parser=lambda _path: preview,
        capture=lambda _path, *, local_key: capture,
    ).safe_dict()

    assert report["attestation_success_count"] == 1
    assert report["false_attestation_count"] == 0
    assert report["differential_unchanged"]
    assert report["writer_invocation_count"] == report["apply_invocation_count"] == 0


def test_hmac_key_loader_rejects_repo_secret_and_never_echoes_key(tmp_path):
    key_file = tmp_path / "ownership.key"
    key_file.write_text(KEY.hex(), encoding="ascii")

    with pytest.raises(ValueError, match="must_be_outside_repository") as error:
        load_local_payroll_ownership_hmac_key(
            key_file,
            repository_root=tmp_path,
        )

    assert KEY.hex() not in str(error.value)
    outside_root = tmp_path / "repository"
    outside_root.mkdir()
    assert load_local_payroll_ownership_hmac_key(
        key_file,
        repository_root=outside_root,
    ) == KEY


@pytest.mark.parametrize("caller_enabled, setting_enabled", [
    (False, True),
    (True, False),
])
def test_cli_requires_caller_and_setting_enablement_without_initializing_services(
    monkeypatch, capsys, caller_enabled, setting_enabled,
):
    class FakeSettings:
        payroll_ownership_attestation_enabled = setting_enabled

        def validate(self, **_kwargs):
            raise AssertionError("disabled shadow initialized services")

    monkeypatch.setattr(cli, "Settings", FakeSettings)
    monkeypatch.setattr(
        cli,
        "drive_payroll_ownership_shadow",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("disabled shadow called production reader")
        ),
    )
    argv = ["kakeibo", "payroll-ownership-shadow"]
    if caller_enabled:
        argv.append("--enable")
    monkeypatch.setattr("sys.argv", argv)

    cli.main()

    output = capsys.readouterr().out
    assert '"enabled": false' in output
    assert '"read_only": true' in output
