import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from app.coverage_confirmation import (
    COVERAGE_CONFIRMATION_HEADERS,
    COVERAGE_REASON_OPERATIONAL_ONLY,
    COVERAGE_STATUS_USER_CONFIRMED,
    ConfirmationIdentity,
    ConfirmationValidationError,
    CoverageConfirmationRecord,
    content_sha256,
    coverage_confirmation_id,
    coverage_confirmation_to_sheet_row,
    evaluate_confirmation_records,
    lookup_coverage_confirmation_rows,
    preview_coverage_confirmation_append,
    resolve_coverage_confirmation_identity,
)


HASH = "a" * 64
UTC_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
CREATED_AT = datetime(2026, 8, 30, 12, 5, tzinfo=timezone.utc)


def record(**overrides):
    data = {
        "schema_version": "1",
        "provider": "paypay",
        "content_sha256": HASH,
        "confirmed_start": "2025-08-20",
        "confirmed_end": "2026-08-20",
        "range_source": "user_confirmed",
        "confirmed_at": UTC_NOW,
        "confirmation_version": "1",
        "source_filename": "Transactions_20250820-20260820.csv",
        "drive_file_id": None,
    }
    data.update(overrides)
    return CoverageConfirmationRecord(**data)


def test_valid_record_is_immutable_and_normalized():
    item = record(content_sha256=HASH.upper())
    assert item.content_sha256 == HASH
    assert item.identity == ConfirmationIdentity("paypay", HASH)


@pytest.mark.parametrize("overrides,reason", [
    ({"provider": " "}, "invalid_provider"),
    ({"content_sha256": "not-a-hash"}, "invalid_content_sha256"),
    ({"confirmed_start": "not-a-date"}, "invalid_confirmed_start"),
    ({"confirmation_version": ""}, "invalid_confirmation_version"),
    ({"source_filename": ""}, "invalid_source_filename"),
])
def test_invalid_required_fields_are_rejected(overrides, reason):
    with pytest.raises(ConfirmationValidationError, match=reason):
        record(**overrides)


def test_start_after_end_is_rejected():
    with pytest.raises(ConfirmationValidationError, match="invalid_confirmed_range"):
        record(confirmed_start="2026-08-21")


def test_only_user_confirmed_range_source_is_accepted():
    with pytest.raises(ConfirmationValidationError, match="invalid_range_source"):
        record(range_source="filename_candidate")


def test_naive_confirmation_time_is_rejected():
    with pytest.raises(ConfirmationValidationError, match="timezone_aware"):
        record(confirmed_at=datetime(2026, 8, 30, 12, 0))


def test_non_utc_confirmation_time_is_rejected():
    jst = timezone(timedelta(hours=9))
    with pytest.raises(ConfirmationValidationError, match="must_be_utc"):
        record(confirmed_at=datetime(2026, 8, 30, 21, 0, tzinfo=jst))


def test_optional_drive_file_id_is_not_identity():
    first = record(drive_file_id="drive-file-a")
    second = record(drive_file_id="drive-file-b")
    assert first.identity == second.identity


def test_identity_ignores_filename():
    assert record(source_filename="first.csv").identity == record(
        source_filename="second.csv",
    ).identity


def test_identity_changes_for_provider_or_content():
    item = record()
    assert item.identity != ConfirmationIdentity("au_pay_card", HASH)
    assert item.identity != ConfirmationIdentity("paypay", "b" * 64)


def test_no_records_is_missing():
    result = evaluate_confirmation_records(ConfirmationIdentity("paypay", HASH), [])
    assert result.status == "missing"
    assert result.record is None


def test_one_record_is_confirmed():
    item = record()
    result = evaluate_confirmation_records(item.identity, [item])
    assert result.status == "confirmed"
    assert result.record == item
    assert result.duplicate_same_range is False


def test_same_range_duplicates_are_confirmed_with_diagnostic():
    first = record()
    second = record(confirmed_at=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc))
    result = evaluate_confirmation_records(first.identity, [first, second])
    assert result.status == "confirmed"
    assert result.duplicate_same_range is True


def test_different_ranges_are_conflict():
    first = record()
    second = record(confirmed_start="2025-08-21")
    result = evaluate_confirmation_records(first.identity, [first, second])
    assert result.status == "conflict"
    assert result.record is None


def test_invalid_or_mismatched_record_is_not_usable():
    item = record()
    invalid = evaluate_confirmation_records(item.identity, [item, object()])
    mismatched = evaluate_confirmation_records(
        item.identity, [record(provider="au_pay_card")],
    )
    assert invalid.status == "invalid"
    assert mismatched.status == "invalid"


def test_content_hash_depends_only_on_bytes():
    assert content_sha256(b"same csv") == content_sha256(bytearray(b"same csv"))
    assert content_sha256(b"same csv") != content_sha256(b"same Csv")


def test_sheet_schema_has_fixed_minimal_column_order():
    assert COVERAGE_CONFIRMATION_HEADERS == [
        "Confirmation ID",
        "Schema Version",
        "Provider",
        "Content SHA-256",
        "Confirmed Start",
        "Confirmed End",
        "Range Source",
        "Coverage Status",
        "Coverage Reason",
        "Confirmed At",
        "Confirmation Version",
        "Source Filename",
        "Drive File ID",
        "Created At",
    ]


def test_phase_one_record_converts_to_fixed_sheet_row():
    stored = coverage_confirmation_to_sheet_row(record(), created_at=CREATED_AT)
    row = stored.to_sheet_row()
    mapped = dict(zip(COVERAGE_CONFIRMATION_HEADERS, row, strict=True))

    assert len(row) == len(COVERAGE_CONFIRMATION_HEADERS)
    assert mapped["Confirmation ID"] == coverage_confirmation_id(record().identity)
    assert mapped["Provider"] == "paypay"
    assert mapped["Content SHA-256"] == HASH
    assert mapped["Confirmed Start"] == "2025-08-20"
    assert mapped["Confirmed End"] == "2026-08-20"
    assert mapped["Coverage Status"] == COVERAGE_STATUS_USER_CONFIRMED
    assert mapped["Coverage Reason"] == COVERAGE_REASON_OPERATIONAL_ONLY
    assert mapped["Confirmed At"] == "2026-08-30T12:00:00+00:00"
    assert mapped["Created At"] == "2026-08-30T12:05:00+00:00"


def test_optional_none_is_saved_as_empty_cell():
    row = coverage_confirmation_to_sheet_row(
        record(drive_file_id=None), created_at=CREATED_AT,
    ).to_sheet_row()
    mapped = dict(zip(COVERAGE_CONFIRMATION_HEADERS, row, strict=True))

    assert mapped["Drive File ID"] == ""
    assert all(value is not None for value in row)


def test_confirmation_id_is_stable_and_uses_phase_one_identity_only():
    expected_digest = hashlib.sha256(f"paypay\0{HASH}".encode()).hexdigest()[:24]
    first = record(source_filename="first.csv", drive_file_id="drive-a")
    second = record(source_filename="second.csv", drive_file_id="drive-b")

    assert coverage_confirmation_id(first.identity) == f"CC-{expected_digest}"
    assert coverage_confirmation_id(first.identity) == coverage_confirmation_id(
        second.identity,
    )


def test_created_at_must_follow_phase_one_utc_timestamp_rule():
    with pytest.raises(ConfirmationValidationError, match="created_at_must_be_utc"):
        coverage_confirmation_to_sheet_row(
            record(),
            created_at=datetime(
                2026, 8, 30, 21, 5, tzinfo=timezone(timedelta(hours=9)),
            ),
        )


def test_pure_lookup_distinguishes_not_found_and_exact_duplicate():
    item = record()
    stored = coverage_confirmation_to_sheet_row(
        item, created_at=CREATED_AT,
    ).to_sheet_row()

    missing = lookup_coverage_confirmation_rows(item, [])
    duplicate = lookup_coverage_confirmation_rows(item, [stored])

    assert (missing.status, missing.diagnostic) == (
        "not_found", "confirmation_not_found",
    )
    assert duplicate.status == "exact_duplicate"
    assert duplicate.matching_row_number == 2


def test_exact_duplicate_ignores_non_identity_confirmation_and_creation_times():
    item = record()
    existing = coverage_confirmation_to_sheet_row(
        item, created_at=CREATED_AT,
    ).to_sheet_row()
    later = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)
    repeated = record(confirmed_at=later)

    preview = preview_coverage_confirmation_append(
        repeated, [existing], created_at=later,
    )

    assert preview.action == "skip"
    assert preview.append_planned is False
    assert preview.duplicate_skip_planned is True
    assert preview.append_row is None
    assert preview.lookup_status == "exact_duplicate"


def test_empty_sheet_rows_are_ignored_consistently():
    item = record()
    existing = coverage_confirmation_to_sheet_row(
        item, created_at=CREATED_AT,
    ).to_sheet_row()

    lookup = lookup_coverage_confirmation_rows(
        item, [[], [None] * len(COVERAGE_CONFIRMATION_HEADERS), existing],
    )

    assert lookup.status == "exact_duplicate"
    assert lookup.matching_row_number == 4


def test_not_found_produces_append_preview_with_target_row():
    item = record()

    preview = preview_coverage_confirmation_append(
        item, [], created_at=CREATED_AT,
    )

    assert preview.action == "append"
    assert preview.append_planned is True
    assert preview.duplicate_skip_planned is False
    assert preview.append_row == coverage_confirmation_to_sheet_row(
        item, created_at=CREATED_AT,
    ).to_sheet_row()
    assert preview.diagnostic == "append_new_confirmation"


def test_similar_dates_and_filename_are_not_an_approximate_duplicate():
    existing = coverage_confirmation_to_sheet_row(
        record(), created_at=CREATED_AT,
    ).to_sheet_row()
    candidate = record(content_sha256="b" * 64)

    preview = preview_coverage_confirmation_append(
        candidate, [existing], created_at=CREATED_AT,
    )

    assert preview.action == "append"
    assert preview.lookup_status == "not_found"


def test_same_identity_with_different_range_is_conflict_not_duplicate_or_append():
    existing = coverage_confirmation_to_sheet_row(
        record(), created_at=CREATED_AT,
    ).to_sheet_row()
    candidate = record(confirmed_start="2025-08-21")

    preview = preview_coverage_confirmation_append(
        candidate, [existing], created_at=CREATED_AT,
    )

    assert preview.action == "skip"
    assert preview.append_planned is False
    assert preview.duplicate_skip_planned is False
    assert preview.lookup_status == "identity_conflict"
    assert preview.diagnostic == "identity_range_conflict"


def test_malformed_existing_row_blocks_append_preview_safely():
    preview = preview_coverage_confirmation_append(
        record(), [["unexpected"]], created_at=CREATED_AT,
    )

    assert preview.action == "skip"
    assert preview.append_planned is False
    assert preview.duplicate_skip_planned is False
    assert preview.lookup_status == "invalid"
    assert preview.diagnostic == "invalid_existing_row"


def test_append_preview_only_iterates_rows_and_cannot_call_a_write_method():
    class WriteGuardRows(list):
        def append(self, value):
            raise AssertionError(f"write attempted: {value}")

        def update(self, value):
            raise AssertionError(f"write attempted: {value}")

        def clear(self):
            raise AssertionError("write attempted")

    rows = WriteGuardRows()

    preview = preview_coverage_confirmation_append(
        record(), rows, created_at=CREATED_AT,
    )

    assert preview.append_planned is True
    assert rows == []


def test_identity_resolver_restores_exact_single_full_record():
    item = record()
    row = coverage_confirmation_to_sheet_row(item, created_at=CREATED_AT).to_sheet_row()

    result = resolve_coverage_confirmation_identity(item.identity, [row])

    assert result.status == "exact_match"
    assert result.diagnostic == "exact_identity_match"
    assert result.matching_row_number == 2
    assert result.record == item
    stored = result.stored_confirmation
    assert stored is not None
    assert stored.confirmation_id == coverage_confirmation_id(item.identity)
    assert stored.schema_version == "1"
    assert stored.provider == "paypay"
    assert stored.content_sha256 == HASH
    assert stored.confirmed_start == "2025-08-20"
    assert stored.confirmed_end == "2026-08-20"
    assert stored.range_source == "user_confirmed"
    assert stored.coverage_status == COVERAGE_STATUS_USER_CONFIRMED
    assert stored.coverage_reason == COVERAGE_REASON_OPERATIONAL_ONLY
    assert stored.confirmed_at == UTC_NOW
    assert stored.confirmation_version == "1"
    assert stored.source_filename == "Transactions_20250820-20260820.csv"
    assert stored.drive_file_id is None
    assert stored.created_at == CREATED_AT


def test_identity_resolver_distinguishes_valid_not_found_store():
    unrelated = record(content_sha256="b" * 64)
    row = coverage_confirmation_to_sheet_row(
        unrelated, created_at=CREATED_AT,
    ).to_sheet_row()

    result = resolve_coverage_confirmation_identity(record().identity, [row])

    assert result.status == "not_found"
    assert result.diagnostic == "confirmation_not_found"
    assert result.record is None
    assert result.stored_confirmation is None


def test_identity_resolver_restores_empty_drive_file_id_as_none():
    item = record(drive_file_id=None)
    row = coverage_confirmation_to_sheet_row(item, created_at=CREATED_AT).to_sheet_row()
    assert row[12] == ""

    result = resolve_coverage_confirmation_identity(item.identity, [row])

    assert result.status == "exact_match"
    assert result.record is not None
    assert result.record.drive_file_id is None


def test_identity_resolver_fails_closed_for_any_malformed_row():
    valid = coverage_confirmation_to_sheet_row(
        record(), created_at=CREATED_AT,
    ).to_sheet_row()

    result = resolve_coverage_confirmation_identity(
        record().identity, [valid, ["malformed"]],
    )

    assert result.status == "invalid_store"
    assert result.diagnostic == "invalid_existing_row"
    assert result.record is None


def test_identity_resolver_fails_closed_for_duplicate_identity_rows():
    first = coverage_confirmation_to_sheet_row(
        record(), created_at=CREATED_AT,
    ).to_sheet_row()
    second = coverage_confirmation_to_sheet_row(
        record(confirmed_at=datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)),
        created_at=datetime(2026, 8, 31, 9, 5, tzinfo=timezone.utc),
    ).to_sheet_row()

    result = resolve_coverage_confirmation_identity(
        record().identity, [first, second],
    )

    assert result.status == "invalid_store"
    assert result.diagnostic == "duplicate_identity"
    assert result.record is None


def test_identity_resolver_fails_closed_for_identity_range_conflict():
    first = coverage_confirmation_to_sheet_row(
        record(), created_at=CREATED_AT,
    ).to_sheet_row()
    second = coverage_confirmation_to_sheet_row(
        record(confirmed_start="2025-08-21"), created_at=CREATED_AT,
    ).to_sheet_row()

    result = resolve_coverage_confirmation_identity(
        record().identity, [first, second],
    )

    assert result.status == "invalid_store"
    assert result.diagnostic == "identity_range_conflict"
    assert result.record is None


def test_identity_resolver_allows_unrelated_distinct_identities():
    target = record()
    rows = [
        coverage_confirmation_to_sheet_row(
            record(content_sha256="b" * 64), created_at=CREATED_AT,
        ).to_sheet_row(),
        coverage_confirmation_to_sheet_row(
            target, created_at=CREATED_AT,
        ).to_sheet_row(),
        coverage_confirmation_to_sheet_row(
            record(provider="au_pay_card", content_sha256="c" * 64),
            created_at=CREATED_AT,
        ).to_sheet_row(),
    ]

    result = resolve_coverage_confirmation_identity(target.identity, rows)

    assert result.status == "exact_match"
    assert result.record == target
    assert result.matching_row_number == 3


@pytest.mark.parametrize("identity", [
    ConfirmationIdentity("au_pay_card", HASH),
    ConfirmationIdentity("paypay", "b" * 64),
])
def test_identity_resolver_wrong_provider_or_sha_is_not_found(identity):
    row = coverage_confirmation_to_sheet_row(
        record(), created_at=CREATED_AT,
    ).to_sheet_row()

    result = resolve_coverage_confirmation_identity(identity, [row])

    assert result.status == "not_found"
    assert result.record is None


def test_identity_resolver_revalidates_deterministic_confirmation_id():
    row = coverage_confirmation_to_sheet_row(
        record(), created_at=CREATED_AT,
    ).to_sheet_row()
    row[0] = "CC-invalid"

    result = resolve_coverage_confirmation_identity(record().identity, [row])

    assert result.status == "invalid_store"
    assert result.diagnostic == "invalid_existing_row"


@pytest.mark.parametrize("cell,value", [
    (4, "not-a-date"),
    (4, "2026-08-21"),
])
def test_identity_resolver_fails_closed_for_invalid_date_or_range(cell, value):
    row = coverage_confirmation_to_sheet_row(
        record(), created_at=CREATED_AT,
    ).to_sheet_row()
    row[cell] = value

    result = resolve_coverage_confirmation_identity(record().identity, [row])

    assert result.status == "invalid_store"
    assert result.record is None


@pytest.mark.parametrize("cell", [9, 13])
def test_identity_resolver_fails_closed_for_invalid_timestamp(cell):
    row = coverage_confirmation_to_sheet_row(
        record(), created_at=CREATED_AT,
    ).to_sheet_row()
    row[cell] = "not-a-timestamp"

    result = resolve_coverage_confirmation_identity(record().identity, [row])

    assert result.status == "invalid_store"
    assert result.record is None


@pytest.mark.parametrize("cell,value", [
    (1, "2"),
    (6, "filename_candidate"),
    (7, "complete"),
    (8, "provider_completeness_proven"),
    (10, ""),
    (11, ""),
])
def test_identity_resolver_reuses_all_stored_record_validation(cell, value):
    row = coverage_confirmation_to_sheet_row(
        record(), created_at=CREATED_AT,
    ).to_sheet_row()
    row[cell] = value

    result = resolve_coverage_confirmation_identity(record().identity, [row])

    assert result.status == "invalid_store"
    assert result.record is None
