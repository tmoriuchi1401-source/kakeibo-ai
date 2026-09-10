from dataclasses import replace
import json

import pytest

from app.payroll_models import PayrollItem, PayrollPreview
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
    deserialize_payroll_review_decision_journal,
    load_local_payroll_review_hmac_key,
    load_payroll_review_decision_journal,
    preview_payroll_review_journal_write,
    replay_payroll_review_decision_journal,
    serialize_payroll_review_decision_journal,
    write_payroll_review_journal,
)
from app.payroll_sheets import PayrollSheetsSnapshot, validate_sheet_schema
from app.payroll_storage import (
    PAYROLL_SCHEMAS,
    PayrollEmployerRecord,
    PayrollStandardItemRecord,
    phase_a_to_storage_candidate,
)
from app.payroll_storage_preview import build_write_plan


KEY = b"persisted-payroll-review-key-001"
NOW = "2026-09-10T12:00:00+09:00"


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


def candidate():
    preview = PayrollPreview(
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
            PayrollItem(
                raw_item_name="出勤日数", section="attendance", raw_value=None,
                value=None, needs_review=True,
                review_reason_code="pairing_not_found",
            ),
        ],
    )
    return phase_a_to_storage_candidate(
        preview, employer_id="employer-1", statement_type="salary",
        source_type="drive", source_file_id="file-1", content_hash="hash-1",
        standard_items=snapshot().standard_items,
    )


def source_binding(value_candidate, index, source_value):
    item = value_candidate.items[index]
    values = {
        "contract_version": SOURCE_VALUE_BINDING_VERSION,
        "content_hash": value_candidate.statement.content_hash,
        "parser_mode": value_candidate.parse_method,
        "page_count": 1,
        "item_occurrence": index,
        "raw_label_digest": _mac(KEY, ("raw_label", item.raw_item_name)),
        "source_value": source_value,
        "source_value_digest": _mac(KEY, ("source_value", source_value)),
        "label_token_id": _mac(KEY, ("label", index)),
        "value_token_id": _mac(KEY, ("value", index)),
        "page": 1,
        "relation": "exact_same_row_right",
        "rerun_digest": _mac(KEY, ("rerun", index)),
    }
    unsigned = PayrollReviewSourceValueBinding(**values, signature="")
    return PayrollReviewSourceValueBinding(
        **values, signature=_mac(KEY, _source_value_binding_payload(unsigned)),
    )


def record(value_candidate, sheets, index, source_value):
    binding = source_binding(value_candidate, index, source_value)
    evidence = capture_payroll_review_evidence(
        value_candidate, sheets, index, local_key=KEY,
        source_value_binding=binding,
    )
    assertion = create_payroll_review_assertion(
        evidence, decision="exclude_non_item", operator_id="reviewer-1",
        local_key=KEY,
    )
    persisted = capture_persisted_payroll_review_decision(
        value_candidate, sheets, evidence, assertion,
        raw_label=value_candidate.items[index].raw_item_name,
        parser_raw_value=value_candidate.items[index].raw_value,
        local_key=KEY, created_at_utc=NOW, applied_at_utc=NOW,
    )
    applied = replay_payroll_review_decision_journal(
        value_candidate, sheets,
        build_payroll_review_decision_journal(
            [persisted], local_key=KEY, created_at_utc=NOW,
        ),
        local_key=KEY,
    )
    assert applied.accepted
    return persisted, applied.candidate


def journal_fixture():
    sheets = snapshot()
    original = candidate()
    first, after_first = record(original, sheets, 1, "0")
    second, _after_second = record(after_first, sheets, 2, "21")
    journal = build_payroll_review_decision_journal(
        [first, second], local_key=KEY, created_at_utc=NOW,
    )
    return original, sheets, journal


def test_external_journal_round_trip_fresh_candidate_and_no_secret(tmp_path):
    original, sheets, journal = journal_fixture()
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    target = tmp_path / "state" / "review-decisions.json"
    preview = preview_payroll_review_journal_write(
        journal, target, repository_root=repository_root, local_key=KEY,
    )
    unconfirmed = write_payroll_review_journal(
        preview, repository_root=repository_root, local_key=KEY,
    )
    assert preview.status == "ready"
    assert not unconfirmed.written
    assert not target.exists()

    written = write_payroll_review_journal(
        preview, repository_root=repository_root, local_key=KEY, confirmed=True,
    )
    assert written.written
    loaded = load_payroll_review_decision_journal(
        target, local_key=KEY, repository_root=repository_root,
    )
    replay = replay_payroll_review_decision_journal(
        original.model_copy(deep=True), sheets, loaded, local_key=KEY,
    )
    assert replay.accepted
    assert replay.applied_count == 2
    assert sum(item.needs_review for item in replay.candidate.items) == 0
    plan = build_write_plan([replay.candidate], sheets)[0]
    assert plan.status == "ready"
    assert len(plan.planned_header_rows) == 1
    assert len(plan.planned_item_rows) == 1
    assert KEY.hex() not in target.read_text(encoding="utf-8")


@pytest.mark.parametrize("change", [
    "content", "parser", "employer", "type", "period", "occurrence",
    "raw_label", "raw_value", "schema",
])
def test_reload_drift_is_atomic_and_fail_closed(change):
    original, sheets, journal = journal_fixture()
    changed = original.model_copy(deep=True)
    changed_sheets = sheets.model_copy(deep=True)
    if change == "content":
        changed.statement.content_hash = "changed"
    elif change == "parser":
        changed.statement.parser_version = "changed"
    elif change == "employer":
        changed.statement.employer_id = "changed"
    elif change == "type":
        changed.statement.statement_type = "bonus"
    elif change == "period":
        changed.statement.pay_period = "2026-09"
    elif change == "occurrence":
        changed.items[1], changed.items[2] = changed.items[2], changed.items[1]
    elif change == "raw_label":
        changed.items[1].raw_item_name = "changed"
    elif change == "raw_value":
        changed.items[1].raw_value = "0"
    elif change == "schema":
        changed_sheets.standard_items[0].standard_name = "changed"
    before = changed.model_dump(mode="json")
    replay = replay_payroll_review_decision_journal(
        changed, changed_sheets, journal, local_key=KEY,
    )
    assert not replay.accepted
    assert replay.applied_count == 0
    assert replay.candidate.model_dump(mode="json") == before
    assert sum(item.needs_review for item in replay.candidate.items) == 2


def test_tamper_unknown_decision_wrong_key_duplicate_and_replay_fail_closed():
    original, sheets, journal = journal_fixture()
    payload = serialize_payroll_review_decision_journal(journal)
    values = json.loads(payload)
    values["records"][0]["raw_label"] = "tampered"
    with pytest.raises(ValueError, match="review_journal_.*invalid"):
        deserialize_payroll_review_decision_journal(
            json.dumps(values), local_key=KEY,
        )

    values = json.loads(payload)
    values["records"][0]["assertion"]["decision"] = "invent_value"
    with pytest.raises(ValueError, match="unknown_decision"):
        deserialize_payroll_review_decision_journal(
            json.dumps(values), local_key=KEY,
        )
    with pytest.raises(ValueError):
        deserialize_payroll_review_decision_journal(
            payload, local_key=b"different-persisted-review-key01",
        )
    with pytest.raises(ValueError, match="duplicate_assertion"):
        build_payroll_review_decision_journal(
            [journal.records[0], journal.records[0]],
            local_key=KEY, created_at_utc=NOW,
        )

    first = replay_payroll_review_decision_journal(
        original, sheets, journal, local_key=KEY,
    )
    replay = replay_payroll_review_decision_journal(
        first.candidate, sheets, journal, local_key=KEY,
    )
    assert first.accepted
    assert not replay.accepted
    assert replay.applied_count == 0


def test_stale_revision_and_repo_local_secret_or_journal_are_rejected(tmp_path):
    original, sheets, journal = journal_fixture()
    stale_assertion = replace(journal.records[0].assertion, assertion_revision=2)
    with pytest.raises(ValueError, match="assertion_revision_stale"):
        capture_persisted_payroll_review_decision(
            original, sheets, journal.records[0].evidence, stale_assertion,
            raw_label=original.items[1].raw_item_name,
            parser_raw_value=original.items[1].raw_value,
            local_key=KEY, created_at_utc=NOW, applied_at_utc=NOW,
        )
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError, match="outside_repository"):
        preview_payroll_review_journal_write(
            journal, repo / "journal.json",
            repository_root=repo, local_key=KEY,
        )
    key_path = repo / "key"
    key_path.write_text(KEY.hex(), encoding="ascii")
    with pytest.raises(ValueError, match="outside_repository"):
        load_local_payroll_review_hmac_key(key_path, repository_root=repo)


def test_write_revalidates_forged_or_stale_preview(tmp_path):
    _original, _sheets, journal = journal_fixture()
    repo = tmp_path / "repo"
    repo.mkdir()
    target = tmp_path / "state" / "review-decisions.json"
    preview = preview_payroll_review_journal_write(
        journal, target, repository_root=repo, local_key=KEY,
    )
    forged = replace(preview, target_path=repo / "forged.json")
    with pytest.raises(ValueError, match="outside_repository"):
        write_payroll_review_journal(
            forged, repository_root=repo, local_key=KEY, confirmed=True,
        )
    target.parent.mkdir()
    target.write_text("conflict", encoding="utf-8")
    with pytest.raises(ValueError, match="preview_stale_or_invalid"):
        write_payroll_review_journal(
            preview, repository_root=repo, local_key=KEY, confirmed=True,
        )
