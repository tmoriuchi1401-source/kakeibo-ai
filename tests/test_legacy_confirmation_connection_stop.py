"""Characterize why 5B-2B-2C cannot dispatch with the existing native contract.

These are stopping evidence, not a replacement authority or a writer bridge.
Every accounting writer is a failing spy. Ledger observations below are seeded
directly into the existing disposable test DB, never posted by a writer.
"""
from copy import deepcopy
from hashlib import sha256
from unittest.mock import Mock

import pytest

from app.drive_run_state import StateError
from app.models import ReceiptItem, ReceiptResult
from app.receipt_confirmation import ReceiptConfirmation, TITLE
from app.receipt_confirmation_production import verify_receipt_source
from app.receipt_reimport_production import ReimportStore, target_snapshot
from app.unified_receipt_native import ReceiptConfirmationPort, confirmation_operation_identity
from app.unified_receipt_review import Case
from test_receipt_confirmation import Store, DB
from test_receipt_reimport_production import fixtures as reimport_fixture


class StoppedBeforeWriter(BaseException):
    pass


def native_fixture(kind):
    store, db = Store(), DB()
    payload = b'fully-synthetic-legacy-confirmation-source'
    source = dict(source_id='synthetic-legacy-stop', version='1',
                  sha256=sha256(payload).hexdigest(), mime_type='image/png')
    metadata = dict(id=source['source_id'], version='1', mimeType='image/png',
                    parents=['synthetic-folder'], trashed=False)

    def verify(src, folder):
        return verify_receipt_source(src, folder, folder, '',
                                     lambda _: deepcopy(metadata), lambda _: payload)

    service = ReceiptConfirmation(store, db, verify)
    if kind == 'medical':
        service.observe_medical(source, 'synthetic-folder')
    else:
        parsed = ReceiptResult(date='2026-01-02', merchant='Synthetic shop', total=120,
            payment_method='現金', items=[
                ReceiptItem(name='Synthetic A', amount=70, major_category='食費', minor_category='食品'),
                ReceiptItem(name='Synthetic B', amount=50, major_category='食費', minor_category='食品')])
        db.rows['レシート'] = [['R-'+source['source_id'], parsed.date, parsed.merchant,
            120, '現金', '', '要確認', 'synthetic-time', '']]
        store.value['manifest'] = dict(sources=[source], folder_id='synthetic-folder')
        store.value['records'] = {source['source_id']: dict(phase='complete',
            parsed=parsed.model_dump(), before=target_snapshot(service.tables(), source['source_id']))}
        service.prepare_general()
    service.render()
    db.rows[TITLE][0][7:15] = (
        ['2026-01-02', 'Synthetic clinic', 120, '医療・保険｜病院', '現金', '医療費を確定', '', '']
        if kind == 'medical' else ['', '', '', '', '', '候補明細で確定', '', ''])
    service.capture_inputs()
    key = next(iter(service.items))
    service._write_accounting_plan = Mock(side_effect=StoppedBeforeWriter)
    db.writes = 0
    return service, store, db, metadata, key


def case_for(service, key):
    item = service.items[key]
    src = item['source']
    case = Case(key, 'receipt-confirmation-v1', dict(review_id=key,
        source_file_id=src['source_id'], source_content_hash=src['sha256'],
        source_version=src['version']), [], TITLE, item['kind'], authority_status='waiting')
    markers = ('date', 'merchant', 'amount', 'category', 'payment', 'action', 'duplicate_target', 'memo')
    case.fields = [[k, k, v] for k, v in zip(markers, item['inputs'])]
    return case


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_real_native_draft_validation_is_not_an_authority(kind):
    service, store, db, _, key = native_fixture(kind)
    before = deepcopy((store.value, db.rows))
    case = case_for(service, key)
    port = ReceiptConfirmationPort(service, key, lambda *_: None)
    assert port.validate_draft(case, case)['status'] == 'validated_not_authorized'
    assert service.apply_confirmations(dry_run=True, keys=[key]) == 1
    # No existing persisted inspection was supplied. Do not substitute a fake
    # NativeInspection, #131 acknowledgement, or #135 auto journal here.
    with pytest.raises(StateError, match='unified_native_original_or_snapshot_changed'):
        port.inspect(case, 'post')
    assert (store.value, db.rows) == before and db.writes == 0
    service._write_accounting_plan.assert_not_called()


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('observed', ['before', 'prefix', 'full'])
def test_native_pending_recovery_does_not_prove_writer_start(kind, observed):
    service, store, db, metadata, key = native_fixture(kind)
    before = deepcopy(db.rows)
    with pytest.raises(StoppedBeforeWriter):
        service.apply_confirmations(keys=[key])
    pending = deepcopy(service.items[key])
    assert pending['status'] == 'pending' and db.rows == before and db.writes == 0
    service._write_accounting_plan.assert_called_once()
    # The only generated intent is the unchanged existing native shape.
    assert pending['confirmation_hash']
    assert not {'native_request_id', 'authority_revision', 'writer_start'} & pending.keys()
    plan = pending['plan']
    for title, row in (plan if observed == 'full' else plan[:1] if observed == 'prefix' else []):
        matches = [i for i, old in enumerate(db.rows[title]) if old and old[0] == row[0]]
        if matches:
            db.rows[title][matches[0]] = deepcopy(row)
        else:
            db.rows[title].append(deepcopy(row))
    ledger = deepcopy(db.rows)
    restarted = ReceiptConfirmation(store, db, service.verify_source)
    restarted._write_accounting_plan = Mock(side_effect=AssertionError('recovery dispatched writer'))
    if observed == 'full':
        # Native recovery marks applied from matching ledger despite our known
        # stop BEFORE writer. That status is insufficient for the requested
        # complete proof; this test does not classify it as shared COMPLETE.
        assert restarted.apply_confirmations(keys=[key]) == 0
        assert restarted.items[key]['status'] == 'applied'
    else:
        with pytest.raises(StateError, match='confirmation_write_reconciliation_required'):
            restarted.apply_confirmations(keys=[key])
        assert restarted.items[key] == pending
    restarted._write_accounting_plan.assert_not_called()
    assert db.rows == ledger and db.writes == 0 and metadata['version'] == '1'


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_native_pending_completion_does_not_observe_independent_version(kind):
    service, store, db, metadata, key = native_fixture(kind)
    with pytest.raises(StoppedBeforeWriter):
        service.apply_confirmations(keys=[key])
    pending = deepcopy(service.items[key])
    for title, row in pending['plan']:
        matches = [i for i, old in enumerate(db.rows[title]) if old and old[0] == row[0]]
        if matches:
            db.rows[title][matches[0]] = deepcopy(row)
        else:
            db.rows[title].append(deepcopy(row))
    metadata['version'] = '2'  # Same bytes; existing source verifier permits it.
    # The port requires the exact version, whereas native pending recovery uses
    # verify_source and _complete directly. Keep both existing contracts intact.
    with pytest.raises(StateError, match='unified_confirmation_exact_version_required'):
        ReceiptConfirmationPort(service, key, lambda *_: None)._current(case_for(service, key), 'post')
    assert service.apply_confirmations(keys=[key]) == 0
    assert service.items[key]['status'] == 'applied' and db.writes == 0


def test_native_store_read_then_write_is_not_a_conditional_claim():
    first, transport, _ = reimport_fixture()
    second = ReimportStore(transport, first.expected_digest, first.spreadsheet_id)
    a, b = deepcopy(first.value), deepcopy(second.value)
    a['records']['synthetic-0']['phase'] = 'held'
    b['records']['synthetic-1']['phase'] = 'held'
    write = transport.write
    completed = []

    def interleaved(payload):
        transport.write = write
        # Another process saves after first's pre-read but before its write.
        second.save(b)
        completed.append(deepcopy(second.value))
        write(payload)

    transport.write = interleaved
    first.save(a)
    assert completed == [b] and first.value == a and second.value == b
    fresh = ReimportStore(transport, first.expected_digest, first.spreadsheet_id)
    assert fresh.value == a and fresh.value['records']['synthetic-1']['phase'] == 'new'
    assert transport.writes == 2  # Both saved/read back; one update was lost.


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_native_operation_identity_does_not_supply_request_or_authority(kind):
    service, _, db, _, key = native_fixture(kind)
    old = deepcopy(service.items[key])
    edited = deepcopy(old)
    edited['inputs'][7] = 'different fully synthetic confirmation input'
    assert confirmation_operation_identity(edited) == confirmation_operation_identity(old)
    # Correct stable financial identity cannot replace the missing request/input
    # and authority binding. Native hash is only generated when pending saves.
    assert 'confirmation_hash' not in old and 'plan' not in old and db.writes == 0
    service._write_accounting_plan.assert_not_called()
