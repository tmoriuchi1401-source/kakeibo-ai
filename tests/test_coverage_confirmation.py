from datetime import datetime, timedelta, timezone

import pytest

from app.coverage_confirmation import (
    ConfirmationIdentity,
    ConfirmationValidationError,
    CoverageConfirmationRecord,
    content_sha256,
    evaluate_confirmation_records,
)


HASH = "a" * 64
UTC_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


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
