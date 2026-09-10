from dataclasses import replace
import json
import sys

import pytest

from app.payroll_models import PayrollItem, PayrollPreview
from app.payroll_production_runner import run_payroll_production_preview
from app.payroll_reconciliation_persistence import (
    JOURNAL_VERSION,
    build_reconciliation_journal,
    deserialize_reconciliation_journal,
    load_reconciliation_journal,
    load_reconciliation_key,
    preview_reconciliation_journal_write,
    serialize_reconciliation_journal,
    write_new_reconciliation_key,
    write_reconciliation_journal,
)
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


KEY = b"reconciliation-persistence-key-1"
NOW = "2026-09-11T03:00:00+00:00"


def candidate(source, content, *, review_items=0):
    items = [PayrollItem(
        raw_item_name=f"項目{index}",
        section="earnings",
        raw_value="300,000",
        value=300000,
        standard_item_candidate="basic_pay",
        needs_review=bool(review_items),
        review_reason_code="low_confidence" if review_items else None,
    ) for index in range(review_items or 1)]
    preview = PayrollPreview(
        file_type="pdf",
        extraction_method="ocr" if review_items else "pdf_text",
        pay_period="2026-08",
        gross_pay=740669,
        total_deductions=172381,
        net_pay=568288,
        parse_status="success",
        items=items,
    )
    return phase_a_to_storage_candidate(
        preview,
        employer_id="employer-1",
        statement_type="salary",
        source_type="drive",
        source_file_id=source,
        content_hash=content,
        standard_items=[standard_item()],
    )


def standard_item():
    return PayrollStandardItemRecord(
        standard_item_id="basic_pay",
        standard_name="基本給",
        section="earning",
        value_type="money",
    )


def sheets(target):
    return PayrollSheetsSnapshot(
        schemas=[
            validate_sheet_schema(key, columns)
            for key, columns in PAYROLL_SCHEMAS.items()
        ],
        statements=[target.statement],
        standard_items=[standard_item()],
        employers=[PayrollEmployerRecord(
            employer_id="employer-1", employer_label="Employer",
        )],
    )


def decision(key=KEY):
    primary = candidate("source-3", "3" * 64)
    alternate = candidate("source-4", "4" * 64, review_items=13)
    value = create_payroll_source_reconciliation_decision(
        target=primary.statement,
        alternate=alternate,
        operator_id="operator-1",
        created_at_utc=NOW,
        local_key=key,
    )
    return primary, alternate, value


def test_repo_external_round_trip_is_one_record_and_contains_no_key(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    key_path = tmp_path / "state" / "reconciliation.key"
    journal_path = tmp_path / "state" / "reconciliation.json"
    write_new_reconciliation_key(key_path, repository_root=repo)
    local_key = load_reconciliation_key(key_path, repository_root=repo)
    primary, alternate, signed = decision(local_key)
    journal = build_reconciliation_journal(
        [signed], local_key=local_key, created_at_utc=NOW,
    )

    preview = preview_reconciliation_journal_write(
        journal, journal_path, repository_root=repo, local_key=local_key,
    )
    assert preview.status == "ready"
    assert preview.record_count == 1
    assert not write_reconciliation_journal(
        preview, repository_root=repo, local_key=local_key,
    ).written
    assert not journal_path.exists()

    result = write_reconciliation_journal(
        preview, repository_root=repo, local_key=local_key, confirmed=True,
    )
    loaded = load_reconciliation_journal(
        journal_path, local_key=local_key, repository_root=repo,
    )
    assert result.written
    assert len(loaded.records) == 1
    assert loaded.records[0].target_statement_id == primary.statement.statement_id
    assert loaded.records[0].alternate_content_hash == alternate.statement.content_hash
    assert local_key.hex() not in journal_path.read_text(encoding="ascii")
    assert preview_reconciliation_journal_write(
        loaded, journal_path, repository_root=repo, local_key=local_key,
    ).status == "already_present"
    with pytest.raises(ValueError, match="already_exists"):
        write_new_reconciliation_key(key_path, repository_root=repo)


def test_conflict_repo_local_and_stale_preview_are_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _primary, _alternate, signed = decision()
    journal = build_reconciliation_journal(
        [signed], local_key=KEY, created_at_utc=NOW,
    )
    with pytest.raises(ValueError, match="outside_repository"):
        preview_reconciliation_journal_write(
            journal, repo / "journal.json",
            repository_root=repo, local_key=KEY,
        )
    with pytest.raises(ValueError, match="outside_repository"):
        write_new_reconciliation_key(
            repo / "key", repository_root=repo,
        )

    target = tmp_path / "state" / "journal.json"
    preview = preview_reconciliation_journal_write(
        journal, target, repository_root=repo, local_key=KEY,
    )
    forged = replace(preview, target_path=tmp_path / "other.json")
    with pytest.raises(ValueError, match="preview_stale_or_invalid"):
        write_reconciliation_journal(
            forged, repository_root=repo, local_key=KEY, confirmed=True,
        )
    target.parent.mkdir()
    target.write_text("conflict", encoding="ascii")
    with pytest.raises(ValueError, match="preview_stale_or_invalid"):
        write_reconciliation_journal(
            preview, repository_root=repo, local_key=KEY, confirmed=True,
        )


@pytest.mark.parametrize(
    "change",
    ["malformed", "journal_signature", "record_signature", "journal_version",
     "decision_revision", "unknown_decision", "duplicate_records"],
)
def test_malformed_tampered_stale_unknown_and_duplicate_fail_closed(change):
    _primary, _alternate, signed = decision()
    journal = build_reconciliation_journal(
        [signed], local_key=KEY, created_at_utc=NOW,
    )
    if change == "malformed":
        payload = b"not-json"
    else:
        values = json.loads(serialize_reconciliation_journal(journal))
        if change == "journal_signature":
            values["journal_signature"] = "0" * 64
        elif change == "record_signature":
            values["records"][0]["signature"] = "0" * 64
        elif change == "journal_version":
            values["journal_version"] = "stale"
        elif change == "decision_revision":
            values["records"][0]["decision_revision"] = 2
        elif change == "unknown_decision":
            values["records"][0]["decision"] = "infer_same_statement"
        elif change == "duplicate_records":
            values["records"].append(values["records"][0])
        payload = json.dumps(values).encode("ascii")
    with pytest.raises(ValueError):
        deserialize_reconciliation_journal(payload, local_key=KEY)

    with pytest.raises(ValueError):
        deserialize_reconciliation_journal(
            serialize_reconciliation_journal(journal),
            local_key=b"wrong-reconciliation-key-00000",
        )


def test_external_journal_drives_source_3_and_4_zero_write_outcomes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    key_path = tmp_path / "state" / "reconciliation.key"
    key_path.parent.mkdir()
    key_path.write_text(KEY.hex(), encoding="ascii")
    journal_path = tmp_path / "state" / "reconciliation.json"
    primary, alternate, signed = decision()
    journal = build_reconciliation_journal(
        [signed], local_key=KEY, created_at_utc=NOW,
    )
    preview = preview_reconciliation_journal_write(
        journal, journal_path, repository_root=repo, local_key=KEY,
    )
    write_reconciliation_journal(
        preview, repository_root=repo, local_key=KEY, confirmed=True,
    )

    report = run_payroll_production_preview(
        [primary.model_copy(deep=True), alternate.model_copy(deep=True)],
        sheets(primary),
        reconciliation_journal_path=journal_path,
        reconciliation_key_path=key_path,
        repository_root=repo,
    )

    assert report.reconciliation_source == "external_journal"
    assert report.reconciliation_record_count == 1
    assert [result.outcome for result in report.results] == [
        "EXACT_DUPLICATE", "ALTERNATE_SOURCE_DUPLICATE",
    ]
    assert report.results[1].target_statement_id == primary.statement.statement_id
    assert report.results[1].review_item_count == 13
    assert all(
        (result.planned_header_rows, result.planned_item_rows,
         result.planned_update_rows) == (0, 0, 0)
        for result in report.results
    )
    assert report.writer_invocation_count == 0
    assert report.actual_header_rows == report.actual_item_rows == 0
    assert report.actual_update_rows == 0


@pytest.mark.parametrize("failure", ["missing", "malformed", "wrong_key"])
def test_journal_reload_failure_leaves_alternate_as_conflict(tmp_path, failure):
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    key_path = state / "key"
    key_path.write_text(KEY.hex(), encoding="ascii")
    journal_path = state / "journal.json"
    primary, alternate, signed = decision()
    journal = build_reconciliation_journal(
        [signed], local_key=KEY, created_at_utc=NOW,
    )
    if failure == "malformed":
        journal_path.write_text("not-json", encoding="ascii")
    elif failure == "wrong_key":
        journal_path.write_bytes(serialize_reconciliation_journal(journal))
        key_path.write_text((b"different-reconciliation-key-000").hex(), encoding="ascii")

    report = run_payroll_production_preview(
        [alternate], sheets(primary),
        reconciliation_journal_path=journal_path,
        reconciliation_key_path=key_path,
        repository_root=repo,
    )

    assert report.reconciliation_source == "rejected"
    assert report.reconciliation_record_count == 0
    assert report.results[0].outcome == "CONFLICT"
    assert report.results[0].reason == "revision_conflict"
    assert report.writer_candidate_count == report.writer_invocation_count == 0


def test_cli_requires_explicit_journal_and_environment_alone_cannot_enable(
    tmp_path, monkeypatch, capsys,
):
    import app.cli as cli

    primary, alternate, signed = decision()
    state = tmp_path / "state"
    state.mkdir()
    key_path = state / "key"
    key_path.write_text(KEY.hex(), encoding="ascii")
    journal_path = state / "journal.json"
    journal = build_reconciliation_journal(
        [signed], local_key=KEY, created_at_utc=NOW,
    )
    journal_path.write_bytes(serialize_reconciliation_journal(journal))

    class FakeSettings:
        spreadsheet_id = "sheet"
        payroll_drive_folder_id = "folder"

        def validate(self, **_kwargs):
            return None

    class FakeReader:
        def __init__(self, _spreadsheet_id):
            pass

        def snapshot(self):
            return sheets(primary)

    monkeypatch.setattr(cli, "Settings", FakeSettings)
    monkeypatch.setattr(cli, "PayrollSheetsReadRepository", FakeReader)
    monkeypatch.setattr(
        cli, "drive_storage_candidates", lambda *_args, **_kwargs: [alternate],
    )
    monkeypatch.setenv("PAYROLL_RECONCILIATION_JOURNAL", str(journal_path))
    monkeypatch.setenv("PAYROLL_RECONCILIATION_HMAC_KEY", str(key_path))

    monkeypatch.setattr(sys, "argv", ["app.cli", "payroll-production-run"])
    cli.main()
    disabled = json.loads(capsys.readouterr().out)
    assert disabled["reconciliation_source"] == "disabled"
    assert disabled["results"][0]["outcome"] == "CONFLICT"

    monkeypatch.setattr(sys, "argv", [
        "app.cli", "payroll-production-run",
        "--reconciliation-journal-file", str(journal_path),
        "--reconciliation-hmac-key-file", str(key_path),
    ])
    cli.main()
    enabled = json.loads(capsys.readouterr().out)
    assert enabled["reconciliation_source"] == "external_journal"
    assert enabled["results"][0]["outcome"] == "ALTERNATE_SOURCE_DUPLICATE"
    assert enabled["writer_invocation_count"] == 0
