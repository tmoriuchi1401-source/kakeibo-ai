from dataclasses import replace
import hashlib
import json
import sys
from uuid import UUID

import pytest

from app.payroll_models import PayrollItem, PayrollPreview
from app.payroll_review_authority import (
    PayrollReviewSourceValueBinding,
    SOURCE_VALUE_BINDING_VERSION,
    _mac,
    _source_value_binding_payload,
    apply_payroll_review_assertion,
    capture_payroll_review_evidence,
    create_payroll_review_assertion,
)
from app.payroll_review_integration import (
    PayrollReviewReloadRequest,
    reload_payroll_review_decisions,
)
from app.payroll_review_persistence import (
    build_payroll_review_decision_journal,
    capture_persisted_payroll_review_decision,
    preview_payroll_review_journal_write,
    write_payroll_review_journal,
)
from app.payroll_sheets import PayrollSheetsSnapshot, validate_sheet_schema
from app.payroll_storage import (
    PAYROLL_SCHEMAS,
    PayrollEmployerRecord,
    PayrollStandardItemRecord,
    phase_a_to_storage_candidate,
)
from app.payroll_storage_preview import build_write_plan, drive_save_preview
from app.payroll_write_plan_materialization import (
    payroll_write_plan_to_materialization_plan,
)
from app.payroll_writer import preview_payroll_write


KEY = b"review-integration-key-000000000"
NOW = "2026-09-10T12:00:00+09:00"
SOURCE_BYTES = b"payroll"
CONTENT_HASH = hashlib.sha256(SOURCE_BYTES).hexdigest()


class DriveService:
    def __init__(self):
        self.write_calls = []

    class Files:
        def __init__(self, outer):
            self.outer = outer

        def list(self, **_kwargs):
            return self

        def execute(self):
            return {"files": [{
                "id": "file-1", "name": "salary.pdf",
                "mimeType": "application/pdf",
            }]}

    def files(self):
        return self.Files(self)


def snapshot():
    return PayrollSheetsSnapshot(
        schemas=[validate_sheet_schema(key, columns)
                 for key, columns in PAYROLL_SCHEMAS.items()],
        standard_items=[PayrollStandardItemRecord(
            standard_item_id="basic_pay", standard_name="基本給",
            section="earning", value_type="money",
        )],
        employers=[PayrollEmployerRecord(
            employer_id="employer-1", employer_label="Example Employer",
        )],
    )


def parsed():
    return PayrollPreview(
        file_type="pdf", extraction_method="pdf_text", pay_period="2026-08",
        parse_status="success", items=[
            PayrollItem(
                raw_item_name="基本給", section="earning", raw_value="300,000",
                value=300000, standard_item_candidate="basic_pay",
            ),
            PayrollItem(
                raw_item_name="課税処理計", section="unknown", raw_value="740,669",
                value=740669, needs_review=True,
                review_reason_code="ambiguous_ownership",
            ),
        ],
    )


def candidate():
    return phase_a_to_storage_candidate(
        parsed(), employer_id="employer-1", statement_type="salary",
        source_type="drive", source_file_id="file-1",
        content_hash=CONTENT_HASH, standard_items=snapshot().standard_items,
    )


def source_binding(value_candidate):
    item = value_candidate.items[1]
    values = {
        "contract_version": SOURCE_VALUE_BINDING_VERSION,
        "content_hash": value_candidate.statement.content_hash,
        "parser_mode": value_candidate.parse_method,
        "page_count": 1,
        "item_occurrence": 1,
        "raw_label_digest": _mac(KEY, ("raw_label", item.raw_item_name)),
        "source_value": "0",
        "source_value_digest": _mac(KEY, ("source_value", "0")),
        "label_token_id": _mac(KEY, ("label", 1)),
        "value_token_id": _mac(KEY, ("value", 1)),
        "page": 1,
        "relation": "exact_same_row_right",
        "rerun_digest": _mac(KEY, ("rerun", 1)),
    }
    unsigned = PayrollReviewSourceValueBinding(**values, signature="")
    return PayrollReviewSourceValueBinding(
        **values, signature=_mac(KEY, _source_value_binding_payload(unsigned)),
    )


def journal_fixture(tmp_path):
    source = candidate()
    sheets = snapshot()
    evidence = capture_payroll_review_evidence(
        source, sheets, 1, local_key=KEY,
        source_value_binding=source_binding(source),
    )
    assertion = create_payroll_review_assertion(
        evidence, decision="exclude_non_item", operator_id="reviewer-1",
        local_key=KEY,
    )
    record = capture_persisted_payroll_review_decision(
        source, sheets, evidence, assertion,
        raw_label=source.items[1].raw_item_name,
        parser_raw_value=source.items[1].raw_value,
        local_key=KEY, created_at_utc=NOW, applied_at_utc=NOW,
    )
    journal = build_payroll_review_decision_journal(
        [record], local_key=KEY, created_at_utc=NOW,
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    key_path = state / "key.hex"
    key_path.write_text(KEY.hex(), encoding="ascii")
    journal_path = state / "journal.json"
    preview = preview_payroll_review_journal_write(
        journal, journal_path, repository_root=repo, local_key=KEY,
    )
    assert write_payroll_review_journal(
        preview, repository_root=repo, local_key=KEY, confirmed=True,
    ).written
    request = PayrollReviewReloadRequest(
        CONTENT_HASH, journal_path, key_path, repo,
    )
    return source, sheets, request, journal_path, key_path


def test_disabled_does_not_inspect_request_or_call_any_loader():
    source = candidate()

    class Trap:
        def __getattribute__(self, _name):
            raise AssertionError("disabled request inspected")

    def loader(*_args, **_kwargs):
        raise AssertionError("disabled loader called")

    result = reload_payroll_review_decisions(
        source, snapshot(), Trap(), enabled=False,
        key_loader=loader, journal_loader=loader,
    )
    assert result.candidate is source
    assert result.evidence.status == "disabled"
    assert result.evidence.key_load_count == result.evidence.journal_read_count == 0


def test_enabled_requires_typed_request_without_attempting_io():
    source = candidate()

    def loader(*_args, **_kwargs):
        raise AssertionError("missing request caused I/O")

    result = reload_payroll_review_decisions(
        source, snapshot(), enabled=True,
        key_loader=loader, journal_loader=loader,
    )
    assert result.candidate is source
    assert result.evidence.status == "rejected"
    assert result.evidence.reason_code == "review_journal_reload_request_required"
    assert result.evidence.key_load_count == result.evidence.journal_read_count == 0


def test_environment_variable_alone_cannot_enable_reload(monkeypatch):
    source = candidate()
    monkeypatch.setenv("PAYROLL_REVIEW_JOURNAL_ENABLED", "true")

    def loader(*_args, **_kwargs):
        raise AssertionError("environment enabled journal I/O")

    result = reload_payroll_review_decisions(
        source, snapshot(), enabled=False,
        key_loader=loader, journal_loader=loader,
    )
    assert result.candidate is source
    assert result.evidence.status == "disabled"


def test_disabled_preserves_plan_writer_preview_and_materialization_semantics():
    source = candidate()
    sheets = snapshot()
    before_plan = build_write_plan([source], sheets)[0]
    before_writer = preview_payroll_write([before_plan]).model_dump(mode="json")
    before_materialization = (
        payroll_write_plan_to_materialization_plan(before_plan)
        if before_plan.status == "ready" else None
    )
    disabled = reload_payroll_review_decisions(source, sheets, enabled=False)
    after_plan = build_write_plan([disabled.candidate], sheets)[0]
    after_writer = preview_payroll_write([after_plan]).model_dump(mode="json")
    after_materialization = (
        payroll_write_plan_to_materialization_plan(after_plan)
        if after_plan.status == "ready" else None
    )
    assert disabled.candidate is source
    assert after_plan == before_plan
    assert after_writer == before_writer
    assert after_materialization == before_materialization


def test_enabled_exact_journal_replays_before_plan(tmp_path):
    source, sheets, request, _journal, _key = journal_fixture(tmp_path)
    result = reload_payroll_review_decisions(
        source, sheets, request, enabled=True,
    )
    assert result.evidence.status == "applied"
    assert result.evidence.journal_read_count == 1
    assert result.evidence.persisted_decision_count == 1
    assert result.evidence.applied_decision_count == 1
    assert result.evidence.rejected_decision_count == 0
    assert source.items[1].needs_review
    assert not result.candidate.items[1].needs_review
    assert result.candidate.items[1].standard_item_id is None
    assert build_write_plan([source], sheets)[0].status == "blocked"
    assert build_write_plan([result.candidate], sheets)[0].status == "ready"


def test_production_preview_off_is_byte_for_byte_equivalent_and_no_journal_io(
    monkeypatch,
):
    import app.payroll_storage as payroll_storage

    service = DriveService()
    kwargs = dict(
        service=service, downloader=lambda _file_id: SOURCE_BYTES,
        parser=lambda _path: parsed(), employer_id="employer-1",
        statement_type="salary",
    )
    identifiers = [UUID(int=value) for value in range(1, 10)]

    def reset_identifiers():
        values = iter(identifiers)
        monkeypatch.setattr(payroll_storage, "uuid4", lambda: next(values))

    reset_identifiers()
    baseline = drive_save_preview("folder-123456", snapshot(), **kwargs)

    class Trap:
        def __getattribute__(self, _name):
            raise AssertionError("disabled production request inspected")

    reset_identifiers()
    disabled = drive_save_preview(
        "folder-123456", snapshot(), review_journal_enabled=False,
        review_reload_request=Trap(), **kwargs,
    )
    assert disabled == baseline
    assert json.dumps(disabled, sort_keys=True) == json.dumps(baseline, sort_keys=True)
    assert "review_journal_reload" not in disabled
    assert service.write_calls == []


def test_production_preview_on_uses_exact_journal_and_reports_read_only_metadata(
    tmp_path,
):
    _source, sheets, request, _journal, _key = journal_fixture(tmp_path)
    service = DriveService()
    output = drive_save_preview(
        "folder-123456", sheets, service=service,
        downloader=lambda _file_id: SOURCE_BYTES,
        parser=lambda _path: parsed(), employer_id="employer-1",
        statement_type="salary", review_journal_enabled=True,
        review_reload_request=request,
    )
    metadata = output["review_journal_reload"]
    assert metadata["journal_read_count"] == 1
    assert metadata["applied_decision_count"] == 1
    assert metadata["rejected_decision_count"] == 0
    assert metadata["writer_invocation_count"] == 0
    assert metadata["apply_invocation_count"] == 0
    assert output["write_plans"][0]["status"] == "ready"
    assert output["would_create_headers"] == 1
    assert output["would_create_items"] == 1
    assert service.write_calls == []


def test_nonselected_source_does_not_read_key_or_journal(tmp_path):
    source, sheets, request, _journal, _key = journal_fixture(tmp_path)
    other = source.model_copy(deep=True)
    other.statement.content_hash = "b" * 64

    def loader(*_args, **_kwargs):
        raise AssertionError("nonselected source caused I/O")

    result = reload_payroll_review_decisions(
        other, sheets, request, enabled=True,
        key_loader=loader, journal_loader=loader,
    )
    assert result.candidate is other
    assert result.evidence.status == "not_applicable"
    assert result.evidence.key_load_count == result.evidence.journal_read_count == 0


@pytest.mark.parametrize("case", [
    "content", "parser", "parser_mode", "schema", "employer", "type",
    "period", "occurrence", "raw_label", "raw_value",
])
def test_candidate_or_schema_drift_keeps_all_review_fail_closed(tmp_path, case):
    source, sheets, request, _journal, _key = journal_fixture(tmp_path)
    changed = source.model_copy(deep=True)
    changed_sheets = sheets.model_copy(deep=True)
    if case == "content":
        changed.statement.content_hash = "c" * 64
        request = replace(request, expected_content_hash="c" * 64)
    elif case == "parser":
        changed.statement.parser_version = "changed"
    elif case == "parser_mode":
        changed.parse_method = "ocr"
    elif case == "schema":
        changed_sheets.standard_items[0].standard_name = "changed"
    elif case == "employer":
        changed.statement.employer_id = "changed"
    elif case == "type":
        changed.statement.statement_type = "bonus"
    elif case == "period":
        changed.statement.pay_period = "changed"
    elif case == "occurrence":
        changed.items.reverse()
    elif case == "raw_label":
        changed.items[1].raw_item_name = "changed"
    elif case == "raw_value":
        changed.items[1].raw_value = "0"
    before = changed.model_dump(mode="json")
    result = reload_payroll_review_decisions(
        changed, changed_sheets, request, enabled=True,
    )
    assert result.evidence.status == "rejected"
    assert result.evidence.applied_decision_count == 0
    assert result.evidence.rejected_decision_count == 1
    assert result.candidate.model_dump(mode="json") == before
    assert sum(item.needs_review for item in result.candidate.items) == 1


@pytest.mark.parametrize("case", [
    "missing_journal", "malformed", "wrong_key", "hmac_tamper",
    "stale_revision", "duplicate", "unknown_decision",
])
def test_journal_or_key_failure_never_clears_review(tmp_path, case):
    source, sheets, request, journal_path, key_path = journal_fixture(tmp_path)
    if case == "missing_journal":
        request = replace(request, journal_path=journal_path.with_name("missing.json"))
    elif case == "malformed":
        journal_path.write_text("not-json", encoding="utf-8")
    elif case == "wrong_key":
        key_path.write_text((b"x" * 32).hex(), encoding="ascii")
    else:
        values = json.loads(journal_path.read_text(encoding="utf-8"))
        if case == "hmac_tamper":
            values["records"][0]["raw_label"] = "tampered"
        elif case == "stale_revision":
            values["records"][0]["assertion"]["assertion_revision"] = 2
        elif case == "duplicate":
            values["records"].append(values["records"][0])
        elif case == "unknown_decision":
            values["records"][0]["assertion"]["decision"] = "invent_value"
        journal_path.write_text(json.dumps(values), encoding="utf-8")
    before = source.model_dump(mode="json")
    result = reload_payroll_review_decisions(
        source, sheets, request, enabled=True,
    )
    assert result.evidence.status == "rejected"
    assert result.evidence.applied_decision_count == 0
    assert result.candidate.model_dump(mode="json") == before
    assert source.items[1].needs_review


def test_duplicate_replay_is_rejected_without_second_application(tmp_path):
    source, sheets, request, _journal, _key = journal_fixture(tmp_path)
    first = reload_payroll_review_decisions(source, sheets, request, enabled=True)
    second = reload_payroll_review_decisions(
        first.candidate, sheets, request, enabled=True,
    )
    assert first.evidence.applied_decision_count == 1
    assert second.evidence.status == "rejected"
    assert second.evidence.applied_decision_count == 0
    assert second.evidence.rejected_decision_count == 1


def test_cli_requires_explicit_flag_and_all_typed_inputs(monkeypatch, capsys):
    import app.cli as cli

    class FakeSettings:
        spreadsheet_id = "sheet-id"
        payroll_drive_folder_id = "folder-id"

        def validate(self, **_kwargs):
            pass

    class FakeRepository:
        def __init__(self, _spreadsheet_id):
            pass

        def snapshot(self):
            return snapshot()

    captured = {}
    monkeypatch.setattr(cli, "Settings", FakeSettings)
    monkeypatch.setattr(cli, "PayrollSheetsReadRepository", FakeRepository)
    monkeypatch.setattr(
        cli, "drive_save_preview",
        lambda *_args, **kwargs: captured.update(kwargs) or {"read_only": True},
    )

    monkeypatch.setattr(sys, "argv", ["kakeibo", "payroll-save-preview"])
    cli.main()
    assert "review_journal_enabled" not in captured
    assert json.loads(capsys.readouterr().out) == {"read_only": True}

    captured.clear()
    monkeypatch.setattr(sys, "argv", [
        "kakeibo", "payroll-save-preview", "--enable-review-journal",
        "--review-journal-file", "outside/journal.json",
        "--review-hmac-key-file", "outside/key.hex",
        "--review-source-content-hash", CONTENT_HASH,
    ])
    cli.main()
    assert captured["review_journal_enabled"] is True
    request = captured["review_reload_request"]
    assert isinstance(request, PayrollReviewReloadRequest)
    assert request.expected_content_hash == CONTENT_HASH
    assert json.loads(capsys.readouterr().out) == {"read_only": True}

    captured.clear()
    monkeypatch.setattr(sys, "argv", [
        "kakeibo", "payroll-save-preview", "--enable-review-journal",
    ])
    with pytest.raises(SystemExit):
        cli.main()
    assert captured == {}
