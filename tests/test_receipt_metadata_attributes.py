"""Fixed attribute labels only; synthetic snapshots and HTTP/CAS stand-ins.

Post-validation snapshot faults exercise all labels without weakening the real
metadata guard. Invalid id/owner/etc from Drive still stop at the earlier guard.
"""
from copy import deepcopy
import logging

import pytest

from app.receipt_private_adapter import ReceiptPrivateAdapter, _log_metadata_changes
from test_receipt_metadata_order import changed_transport
from test_receipt_private_adapter import fixture
from test_receipt_readback_diagnostics import (
    READ_SHAPE, api_shape, exercise, no_live_or_financial_io,
)
from test_receipt_sheet_validation import RID


PRIVATE = 'secret-metadata-id-etag-owner-input-must-not-appear'
FIELDS = ('id', 'mimeType', 'trashed', 'owner', 'permissions', 'parents', 'version', 'etag', 'other')
CODES = (
    {
        'id': 'receipt_private_metadata_folder_id_changed',
        'mimeType': 'receipt_private_metadata_folder_mime_type_changed',
        'trashed': 'receipt_private_metadata_folder_trashed_changed',
        'owner': 'receipt_private_metadata_folder_owner_changed',
        'permissions': 'receipt_private_metadata_folder_permissions_changed',
        'parents': 'receipt_private_metadata_folder_parents_changed',
        'version': 'receipt_private_metadata_folder_version_changed',
        'etag': 'receipt_private_metadata_folder_etag_changed',
        'other': 'receipt_private_metadata_folder_other_changed',
    },
    {
        'id': 'receipt_private_metadata_json_id_changed',
        'mimeType': 'receipt_private_metadata_json_mime_type_changed',
        'trashed': 'receipt_private_metadata_json_trashed_changed',
        'owner': 'receipt_private_metadata_json_owner_changed',
        'permissions': 'receipt_private_metadata_json_permissions_changed',
        'parents': 'receipt_private_metadata_json_parents_changed',
        'version': 'receipt_private_metadata_json_version_changed',
        'etag': 'receipt_private_metadata_json_etag_changed',
        'other': 'receipt_private_metadata_json_other_changed',
    },
)


def alter(m, field):
    if field in ('id', 'mimeType', 'version'):
        m[field] = PRIVATE
    elif field == 'trashed':
        m['labels']['trashed'] = True
    elif field == 'owner':
        m['owners'] = [{'emailAddress': PRIVATE}]
    elif field == 'permissions':
        m['permissions'].append({'type': 'user', 'role': 'reader', 'emailAddress': PRIVATE})
    elif field == 'parents':
        m['parents'] = [{'id': PRIVATE}]
    elif field == 'etag':
        m['etag'] = '"' + PRIVATE + '"'
    elif field == 'other':
        m[PRIVATE] = {'value': PRIVATE}
    else:
        raise AssertionError('unknown synthetic field')


def snapshot_fault_adapter(base, changes):
    """Inject only the outer after-snapshot, after actual validators ran."""
    class Adapter(base):
        def __init__(self, io, **kwargs):
            super().__init__(io, **kwargs)
            self.after_cas = False
            self.snapshots = 0
            if hasattr(io, 'http'):  # Leave preparation's byte-only fake untouched.
                replace = io.replace_versioned

                def marked_replace(*args):
                    result = replace(*args)
                    self.after_cas = True
                    return result

                io.replace_versioned = marked_replace

        def _metadata(self):
            value = super()._metadata()
            if self.after_cas:
                self.snapshots += 1
                if self.snapshots == 2:
                    for index, fields in changes:
                        for field in fields:
                            alter(value[index], field)
            return value
    return Adapter


def observed_fault(changes, base=ReceiptPrivateAdapter, on_claim=None):
    return exercise('synthetic_attribute_fault',
        adapter_type=snapshot_fault_adapter(base, changes), on_claim=on_claim)


def attribute_messages(caplog):
    records = [r for r in caplog.records if r.getMessage().startswith('receipt_private_metadata_')]
    allowed = set(CODES[0].values()) | set(CODES[1].values()) | {'receipt_private_metadata_other_changed'}
    assert all(r.msg in allowed and not r.args and not r.exc_info for r in records)
    assert all(PRIVATE not in r.getMessage() and RID not in r.getMessage() for r in caplog.records)
    return [r.getMessage() for r in records]


def assert_same_strict_stop(result):
    assert result['error'] == 'receipt_private_write_unknown'
    assert result['stopped'] and result['stopped_after_reconcile']
    assert result['replay_error'] == 'receipt_private_write_stopped'
    assert result['cas_attempts'] == result['cas_writes'] == 3
    assert result['persistent_journal']['generation'] == 3
    assert result['persistent_journal']['requests'][RID]['state'] == '検証中'
    assert result['persistent_journal']['requests'][RID]['result'] is None
    expected = READ_SHAPE * 2 + READ_SHAPE[2:7] + [('synthetic-private', 'PUT')] + READ_SHAPE
    assert api_shape(result['api_trace']) == expected
    assert api_shape(result['reconcile_trace']) == READ_SHAPE


@pytest.mark.parametrize('index', [0, 1], ids=['folder', 'json'])
@pytest.mark.parametrize('field', FIELDS)
def test_single_attribute_has_only_the_fixed_file_kind_and_field_code(caplog, index, field):
    caplog.set_level(logging.WARNING)
    result = observed_fault(((index, (field,)),), on_claim=caplog.clear)
    assert_same_strict_stop(result)
    assert attribute_messages(caplog) == [CODES[index][field]]
    assert 'receipt_private_read_metadata_consistency_failed' in caplog.messages
    assert 'receipt_private_ack_readback_begin' not in caplog.messages


@pytest.mark.parametrize('changes', [
    ((0, FIELDS), (1, FIELDS)),
    ((0, ('version', 'permissions')), (1, ('etag', 'parents', 'other'))),
], ids=['all-fields-both-files', 'mixed-fields-both-files'])
def test_simultaneous_changes_emit_every_matching_code_once(caplog, changes):
    caplog.set_level(logging.WARNING)
    result = observed_fault(changes, on_claim=caplog.clear)
    assert_same_strict_stop(result)
    assert attribute_messages(caplog) == [CODES[i][f] for i, fields in changes for f in FIELDS if f in fields]


@pytest.mark.parametrize('index', [0, 1], ids=['folder', 'json'])
@pytest.mark.parametrize('change', ['unknown-label', 'unknown-null-key', 'absent-vs-null'])
def test_other_fields_and_presence_changes_never_emit_untrusted_keys(caplog, index, change):
    _, _, _, io = fixture()
    before = [deepcopy(io.meta['synthetic-folder']), deepcopy(io.meta['synthetic-private'])]
    if change == 'absent-vs-null':
        before[index]['parents'] = None
    after = deepcopy(before)
    if change == 'unknown-label':
        after[index]['labels'][PRIVATE] = PRIVATE
    elif change == 'unknown-null-key':
        after[index][PRIVATE] = None
    else:
        after[index].pop('parents')
    untouched = deepcopy((before, after))
    caplog.set_level(logging.WARNING)
    _log_metadata_changes(before, after)
    assert attribute_messages(caplog) == [CODES[index]['parents' if change == 'absent-vs-null' else 'other']]
    assert (before, after) == untouched


@pytest.mark.parametrize('targets', [
    ('synthetic-folder',), ('synthetic-private',), ('synthetic-folder', 'synthetic-private'),
], ids=['folder', 'json', 'both'])
def test_permission_order_fix_remains_successful_and_has_no_attribute_logs(caplog, targets):
    caplog.set_level(logging.WARNING)
    result = exercise(None, transport_type=changed_transport(
        targets, lambda m: m['permissions'].reverse()), on_claim=caplog.clear)
    assert result['error'] is None and not result['stopped']
    assert attribute_messages(caplog) == []


def test_stable_read_does_not_emit_attribute_logs(caplog):
    caplog.set_level(logging.WARNING)
    result = exercise(None, on_claim=caplog.clear)
    assert result['error'] is None
    assert attribute_messages(caplog) == []


@pytest.mark.parametrize('before,after', [([], []), ({}, []), ([{}, None], [{}, {}])])
def test_unexpected_snapshot_structure_uses_only_a_fixed_other_code(caplog, before, after):
    caplog.set_level(logging.WARNING)
    _log_metadata_changes(before, after)
    assert attribute_messages(caplog) == ['receipt_private_metadata_other_changed']
