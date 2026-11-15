"""Offline proposal: permission order is not a grant; all values stay guarded.

Use the existing dedicated HTTP transport and synthetic claim-CAS fixture.
These tests do not reproduce or diagnose the historical live metadata pair.
"""
from copy import deepcopy
import logging

import pytest

from app.receipt_private_adapter import FIELDS
from app.receipt_sheet_worker import ExistingReceiptDriveIO
from test_receipt_readback_diagnostics import (
    READ_SHAPE, api_shape, exercise, no_live_or_financial_io,
)
from test_receipt_sheet_validation import RID


def changed_transport(targets, change):
    """Alter just the second full metadata response after the fake CAS."""
    class Transport(ExistingReceiptDriveIO):
        def __init__(self, *args):
            super().__init__(*args)
            self.after_cas = False
            self.counts = {}

        def replace_versioned(self, *args):
            super().replace_versioned(*args)
            self.after_cas = True

        def metadata(self, fid, fields):
            value = super().metadata(fid, fields)
            if self.after_cas and fields == FIELDS and fid in targets:
                self.counts[fid] = self.counts.get(fid, 0) + 1
                if self.counts[fid] == 2:
                    change(value)
            return value
    return Transport


@pytest.mark.parametrize('targets', [
    ('synthetic-folder',), ('synthetic-private',),
    ('synthetic-folder', 'synthetic-private'),
], ids=['folder', 'private', 'both'])
def test_permission_reordering_preserves_success_bytes_and_exact_http_trace(caplog, targets):
    stable = exercise(None)
    caplog.set_level(logging.WARNING)
    reordered = exercise(None, transport_type=changed_transport(
        targets, lambda m: m['permissions'].reverse()), on_claim=caplog.clear)
    assert reordered['error'] is None
    # Same GET order/count, single claim CAS, body, ETag, ACK and stop state.
    assert reordered == stable
    assert sum(c['method'] == 'PUT' for c in reordered['api_trace']) == 1
    assert api_shape(reordered['api_trace']) == (
        READ_SHAPE * 2 + READ_SHAPE[2:7] + [('synthetic-private', 'PUT')] + READ_SHAPE * 2)
    assert 'receipt_private_ack_readback_ok' in caplog.messages
    assert 'receipt_private_read_metadata_consistency_failed' not in caplog.messages
    assert all(RID not in message for message in caplog.messages)


CHANGES = [
    'id', 'mimeType', 'trashed', 'owner', 'acl_role', 'acl_principal',
    'acl_type', 'acl_deleted', 'parents', 'version', 'etag',
    'permission_duplicate', 'permission_deleted_extra',
    'permission_field_presence', 'permission_extra_field',
    'empty_version', 'weak_etag',
]


def alter(field, m):
    if field == 'id':
        m['id'] = 'synthetic-other'
    elif field == 'mimeType':
        m['mimeType'] = 'synthetic/other'
    elif field == 'trashed':
        m['labels']['trashed'] = True
    elif field == 'owner':
        m['owners'][0]['emailAddress'] = 'other@synthetic.invalid'
    elif field == 'acl_role':
        m['permissions'][1]['role'] = 'reader'
    elif field == 'acl_principal':
        m['permissions'][1]['emailAddress'] = 'other@synthetic.invalid'
    elif field == 'acl_type':
        m['permissions'][1]['type'] = 'group'
    elif field == 'acl_deleted':
        m['permissions'][1]['deleted'] = True
    elif field == 'parents':
        m['parents'] = [{'id': 'synthetic-other-folder'}]
    elif field == 'version':
        m['version'] = '999'
    elif field == 'etag':
        m['etag'] = '"synthetic-other-etag"'
    elif field == 'permission_duplicate':
        m['permissions'].append(deepcopy(m['permissions'][0]))
    elif field == 'permission_deleted_extra':
        m['permissions'].append(dict(type='user', role='reader',
            emailAddress='deleted@synthetic.invalid', deleted=True))
    elif field == 'permission_field_presence':
        m['permissions'][0]['deleted'] = False
    elif field == 'permission_extra_field':
        m['permissions'][0]['synthetic_extra'] = True
    elif field == 'empty_version':
        m['version'] = ''
    elif field == 'weak_etag':
        m['etag'] = 'W/"synthetic-weak"'
    else:
        raise AssertionError('unknown synthetic fault')
    m['permissions'].reverse()  # Reordering never masks a content change.


@pytest.mark.parametrize('target', ['synthetic-folder', 'synthetic-private'],
                         ids=['folder', 'private'])
@pytest.mark.parametrize('field', CHANGES)
def test_metadata_content_changes_still_stop_after_one_fake_cas(caplog, target, field):
    caplog.set_level(logging.WARNING)
    result = exercise('synthetic_metadata_fault', transport_type=changed_transport(
        (target,), lambda m: alter(field, m)), on_claim=caplog.clear)
    assert result['error'] == 'receipt_private_write_unknown'
    assert result['stopped'] and result['stopped_after_reconcile']
    assert result['replay_error'] == 'receipt_private_write_stopped'
    assert result['cas_attempts'] == result['cas_writes'] == 3  # prepare, accept, claim
    assert result['persistent_journal']['generation'] == 3
    assert result['persistent_journal']['requests'][RID]['state'] == '検証中'
    assert result['persistent_journal']['requests'][RID]['result'] is None
    assert sum(c['method'] == 'PUT' for c in result['api_trace']) == 1
    base = READ_SHAPE * 2 + READ_SHAPE[2:7] + [('synthetic-private', 'PUT')]
    post = api_shape(result['api_trace'])[len(base):]
    assert api_shape(result['api_trace'])[:len(base)] == base
    assert post == READ_SHAPE[:len(post)] and len(post) in (8, 9)
    assert api_shape(result['reconcile_trace']) == READ_SHAPE
    assert 'receipt_private_adapter_readback_failed' in caplog.messages
    assert 'receipt_private_ack_readback_begin' not in caplog.messages
    assert all(RID not in message for message in caplog.messages)
