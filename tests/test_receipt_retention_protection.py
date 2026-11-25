from datetime import datetime, timezone

import pytest

from app.maintenance import cleanup_processed_receipts, receipt_completion_check
from test_maintenance import FakeDrive, FOLDER_ID


def original():
    return {'id': 'synthetic-original', 'mimeType': 'image/png', 'appProperties': {
        'kakeiboReceiptClass': 'normal', 'kakeiboProcessedAt': '2020-01-01T00:00:00Z'}}


@pytest.mark.parametrize('proof', [None, lambda _: False, lambda _: None])
def test_legacy_processed_without_accounting_proof_never_deleted(proof):
    drive = FakeDrive([{'files': [original()]}])
    result = cleanup_processed_receipts(FOLDER_ID, apply=True, service=drive, completion_check=proof)
    assert result['expired'] == result['deleted'] == 0 and drive.resource.deletes == []


def test_proof_revoked_between_listing_and_delete_retains_original():
    checks = iter([True, False])
    drive = FakeDrive([{'files': [original()]}])
    result = cleanup_processed_receipts(FOLDER_ID, apply=True, service=drive,
                                       completion_check=lambda _: next(checks))
    assert result['expired'] == 1 and result['deleted'] == 0 and drive.resource.deletes == []


def test_proof_unknown_retains_original():
    def unknown(_):
        raise TimeoutError()
    drive = FakeDrive([{'files': [original()]}])
    assert cleanup_processed_receipts(FOLDER_ID, apply=True, service=drive,
        completion_check=unknown)['deleted'] == 0
    assert drive.resource.deletes == []


def test_completed_normal_can_be_deleted_only_after_second_fresh_proof():
    calls = []
    drive = FakeDrive([{'files': [original()]}])
    result = cleanup_processed_receipts(FOLDER_ID, apply=True, service=drive,
        completion_check=lambda item: calls.append(item['id']) or True,
        now=datetime(2026, 11, 26, tzinfo=timezone.utc))
    assert result['deleted'] == 1 and len(calls) == 2 and len(drive.resource.deletes) == 1


def test_pdf_requires_all_member_proof_and_missing_google_state_is_closed():
    check = receipt_completion_check({})
    assert check(dict(original(), mimeType='application/pdf')) is False
    assert check(original()) is False
