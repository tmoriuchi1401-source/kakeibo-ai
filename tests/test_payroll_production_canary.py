import hashlib
import sys

import pytest

from app.payroll_models import PayrollItem, PayrollPreview
from app.payroll_production_canary import (
    PayrollCanaryError,
    apply_payroll_canary,
    build_payroll_canary_preview,
)
from app.payroll_review_authority import (
    PayrollReviewSourceValueBinding,
    SOURCE_VALUE_BINDING_VERSION,
    _mac,
    _source_value_binding_payload,
    capture_payroll_review_evidence,
    create_payroll_review_assertion,
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
from app.payroll_writer import PayrollAppendOutcome


KEY = b"production-canary-review-key-001"
NOW = "2026-09-10T12:00:00+09:00"
CONTENT_HASH = hashlib.sha256(b"one-source").hexdigest()


def snapshot():
    return PayrollSheetsSnapshot(
        schemas=[validate_sheet_schema(key, columns)
                 for key, columns in PAYROLL_SCHEMAS.items()],
        standard_items=[PayrollStandardItemRecord(
            standard_item_id="basic_pay", standard_name="Basic",
            section="earning", value_type="money",
        )],
        employers=[PayrollEmployerRecord(
            employer_id="employer-1", employer_label="Employer",
        )],
    )


def candidate():
    parsed = PayrollPreview(
        file_type="pdf", extraction_method="pdf_text", pay_period="2026-08",
        parse_status="success", items=[
            PayrollItem(
                raw_item_name="Basic", section="earning", raw_value="300000",
                value=300000, standard_item_candidate="basic_pay",
            ),
            PayrollItem(
                raw_item_name="Non item", section="unknown", raw_value="0",
                value=0, needs_review=True,
                review_reason_code="ambiguous_ownership",
            ),
        ],
    )
    return phase_a_to_storage_candidate(
        parsed, employer_id="employer-1", statement_type="salary",
        source_type="drive", source_file_id="file-1",
        content_hash=CONTENT_HASH, standard_items=snapshot().standard_items,
    )


def journal_files(tmp_path):
    source = candidate()
    sheets = snapshot()
    item = source.items[1]
    values = {
        "contract_version": SOURCE_VALUE_BINDING_VERSION,
        "content_hash": CONTENT_HASH,
        "parser_mode": source.parse_method,
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
    binding = PayrollReviewSourceValueBinding(
        **values, signature=_mac(KEY, _source_value_binding_payload(unsigned)),
    )
    evidence = capture_payroll_review_evidence(
        source, sheets, 1, local_key=KEY, source_value_binding=binding,
    )
    assertion = create_payroll_review_assertion(
        evidence, decision="exclude_non_item", operator_id="reviewer",
        local_key=KEY,
    )
    record = capture_persisted_payroll_review_decision(
        source, sheets, evidence, assertion,
        raw_label=item.raw_item_name, parser_raw_value=item.raw_value,
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
    write_preview = preview_payroll_review_journal_write(
        journal, journal_path, repository_root=repo, local_key=KEY,
    )
    assert write_payroll_review_journal(
        write_preview, repository_root=repo, local_key=KEY, confirmed=True,
    ).written
    return source, sheets, repo, journal_path, key_path


class Reader:
    def __init__(self, sheets):
        self.sheets = sheets
        self.rows = {"payroll_statements": [], "payroll_items": []}

    def data_rows(self, key):
        return [list(row) for row in self.rows[key]]

    def data_row_count(self, key):
        return len(self.rows[key])

    def snapshot(self):
        return self.sheets


class Writer:
    def __init__(self, reader, preview, corrupt_items=False):
        self.reader = reader
        self.preview = preview
        self.calls = []
        self.corrupt_items = corrupt_items

    def append_header_rows(self, rows):
        self.calls.append("header")
        self.reader.rows["payroll_statements"].extend(row.values for row in rows)
        return PayrollAppendOutcome(
            status="confirmed_success", requested_rows=len(rows),
            confirmed_rows=len(rows),
            updated_range=self.preview.expected_header_range.replace("'", ""),
        )

    def append_item_rows(self, rows):
        self.calls.append("items")
        values = [list(row.values) for row in rows]
        if self.corrupt_items:
            values[0][-1] = 999
        self.reader.rows["payroll_items"].extend(values)
        return PayrollAppendOutcome(
            status="confirmed_success", requested_rows=len(rows),
            confirmed_rows=len(rows),
            updated_range=self.preview.expected_item_range.replace("'", ""),
        )


def make_preview(tmp_path):
    source, sheets, repo, journal_path, key_path = journal_files(tmp_path)
    reader = Reader(sheets)
    preview = build_payroll_canary_preview(
        spreadsheet_id="spreadsheet-1", snapshot=sheets,
        raw_candidates=[source], expected_content_hash=CONTENT_HASH,
        journal_path=journal_path, hmac_key_path=key_path,
        repository_root=repo, expected_employer_id="employer-1",
        expected_statement_type="salary", reader=reader,
    )
    return reader, preview


def test_preview_is_exact_ready_single_statement_and_privacy_safe(tmp_path):
    _reader, preview = make_preview(tmp_path)
    assert preview.review_before == 1
    assert preview.review_after == 0
    assert preview.persisted_decisions == preview.applied_decisions == 1
    assert preview.rejected_decisions == 0
    assert preview.status == "ready"
    assert preview.header_rows == preview.item_rows == 1
    assert preview.duplicate_status == "new"
    assert preview.expected_header_range.endswith("A2:O2")
    assert preview.expected_item_range.endswith("A2:K2")
    serialized = preview.model_dump(mode="json")
    assert "plan" not in serialized and "candidate" not in serialized
    assert "raw_candidate" not in serialized
    assert CONTENT_HASH not in str(serialized)


def test_approval_hash_is_stable_across_fresh_generated_row_ids(tmp_path):
    _reader, first = make_preview(tmp_path)
    (tmp_path / "second").mkdir()
    source, sheets, repo, journal_path, key_path = journal_files(
        tmp_path / "second"
    )
    second = build_payroll_canary_preview(
        spreadsheet_id="spreadsheet-1", snapshot=sheets,
        raw_candidates=[source], expected_content_hash=CONTENT_HASH,
        journal_path=journal_path, hmac_key_path=key_path,
        repository_root=repo, expected_employer_id="employer-1",
        expected_statement_type="salary", reader=Reader(sheets),
    )
    assert first.plan.identity.statement_id != second.plan.identity.statement_id
    assert first.plan_hash == second.plan_hash


def test_apply_requires_exact_preview_hash_before_writer_call(tmp_path):
    reader, preview = make_preview(tmp_path)
    writer = Writer(reader, preview)
    with pytest.raises(PayrollCanaryError, match="confirmation_required"):
        apply_payroll_canary(
            preview, expected_plan_hash="wrong", reader=reader,
            latest_plan=lambda: preview.plan, writer=writer, confirmed=True,
        )
    assert writer.calls == []


def test_apply_writes_one_statement_and_post_reads_exact_rows(tmp_path):
    reader, preview = make_preview(tmp_path)
    writer = Writer(reader, preview)
    report = apply_payroll_canary(
        preview, expected_plan_hash=preview.plan_hash, reader=reader,
        latest_plan=lambda: preview.plan, writer=writer, confirmed=True,
    )
    assert writer.calls == ["header", "items"]
    assert report.writer_status == "completed"
    assert report.actual_header_rows == report.actual_item_rows == 1
    assert report.post_read_exact
    assert not report.unexpected_changes
    assert not report.partial_success


def test_duplicate_preread_stops_before_writer(tmp_path):
    reader, preview = make_preview(tmp_path)
    reader.rows["payroll_statements"].append(
        list(preview.plan.planned_header_rows[0].values)
    )
    writer = Writer(reader, preview)
    with pytest.raises(PayrollCanaryError, match="duplicate_pre_read"):
        apply_payroll_canary(
            preview, expected_plan_hash=preview.plan_hash, reader=reader,
            latest_plan=lambda: preview.plan, writer=writer, confirmed=True,
        )
    assert writer.calls == []


def test_post_read_mismatch_is_reported_without_extra_write(tmp_path):
    reader, preview = make_preview(tmp_path)
    writer = Writer(reader, preview, corrupt_items=True)
    report = apply_payroll_canary(
        preview, expected_plan_hash=preview.plan_hash, reader=reader,
        latest_plan=lambda: preview.plan, writer=writer, confirmed=True,
    )
    assert writer.calls == ["header", "items"]
    assert not report.post_read_exact
    assert report.unexpected_changes


def test_stale_latest_plan_performs_zero_write(tmp_path):
    reader, preview = make_preview(tmp_path)
    writer = Writer(reader, preview)
    stale = preview.plan.model_copy(update={
        "eligibility": "ineligible", "status": "blocked",
        "reason": "statement_needs_review",
        "reasons": ("statement_needs_review",),
        "header_action": "none", "item_action": "none",
        "planned_header_rows": (), "planned_item_rows": (),
    })
    report = apply_payroll_canary(
        preview, expected_plan_hash=preview.plan_hash, reader=reader,
        latest_plan=lambda: stale, writer=writer, confirmed=True,
    )
    assert writer.calls == []
    assert report.writer_status == "stale_plan"
    assert not report.writer_applied
    assert not report.unexpected_changes


def test_environment_alone_cannot_enable_canary(monkeypatch):
    import app.cli as cli

    monkeypatch.setenv("PAYROLL_REVIEW_JOURNAL_ENABLED", "true")
    monkeypatch.setattr(cli, "load_production_canary_preview", lambda **_kwargs: (
        pytest.fail("disabled CLI reached production preview")
    ))
    monkeypatch.setattr(sys, "argv", [
        "app.cli", "payroll-production-canary",
        "--review-journal-file", "journal.json",
        "--review-hmac-key-file", "key.hex",
        "--review-source-content-hash", CONTENT_HASH,
        "--employer-id", "employer-1",
        "--statement-type", "salary",
    ])
    with pytest.raises(SystemExit, match="2"):
        cli.main()


def test_apply_flag_alone_cannot_bypass_plan_hash_confirmation(monkeypatch):
    import app.cli as cli

    monkeypatch.setattr(cli, "load_production_canary_preview", lambda **_kwargs: (
        pytest.fail("unconfirmed CLI reached production preview")
    ))
    monkeypatch.setattr(sys, "argv", [
        "app.cli", "payroll-production-canary", "--enable-review-journal",
        "--review-journal-file", "journal.json",
        "--review-hmac-key-file", "key.hex",
        "--review-source-content-hash", CONTENT_HASH,
        "--employer-id", "employer-1", "--statement-type", "salary",
        "--apply",
    ])
    with pytest.raises(SystemExit, match="2"):
        cli.main()
