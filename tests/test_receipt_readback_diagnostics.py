"""Synthetic claim read-back faults through the real dedicated HTTP transport.

exercise() is also used by a local differential check against the pinned
pre-change classes. No credentials, live UUIDs, sockets or financial writer.
"""
from copy import deepcopy
from hashlib import sha256
import json
import logging
import socket
from types import SimpleNamespace

import pytest

from app.drive_run_state import StateError
from app.page_receipt_model import digest
from app.receipt_private_adapter import ReceiptPrivateAdapter
from app.receipt_sheet_worker import ExistingReceiptDriveIO, REGISTRY_MAX_BYTES
from app.receipt_sheet_validation import ValidationJournal
from app.receipt_get_diagnostics import CODES as GET_DIAGNOSTIC_CODES
from test_receipt_private_adapter import fixture, capture, encode
from test_receipt_sheet_validation import RID
from test_receipt_worker_stages import PRIVATE_CODES, WORKER_CODES

PRIVATE = 'private-exception-original-input-token-must-not-be-logged'
CASES = [
    (None, None),
    ('binding', 'receipt_private_read_binding_failed'),
    ('metadata_get', 'receipt_private_read_metadata_get_failed'),
    ('metadata_decode', 'receipt_private_read_metadata_get_failed'),
    ('metadata_invalid', 'receipt_private_read_metadata_validation_failed'),
    ('transport_metadata_get', 'receipt_private_transport_metadata_get_failed'),
    ('transport_metadata_decode', 'receipt_private_transport_metadata_get_failed'),
    ('transport_acl_invalid', 'receipt_private_transport_metadata_validation_failed'),
    ('transport_meta_invalid', 'receipt_private_transport_metadata_validation_failed'),
    ('body_get', 'receipt_private_transport_body_get_failed'),
    ('body_http', 'receipt_private_transport_body_get_failed'),
    ('body_size', 'receipt_private_transport_body_size_failed'),
    ('transport_metadata_race', 'receipt_private_transport_metadata_consistency_failed'),
    ('etag', 'receipt_private_read_etag_failed'),
    ('metadata_race', 'receipt_private_read_metadata_consistency_failed'),
    ('json', 'receipt_private_read_json_failed'),
    ('utf8', 'receipt_private_read_json_failed'),
    ('duplicate_key', 'receipt_private_read_json_failed'),
    ('catalog', 'receipt_private_read_catalog_failed'),
    ('journal', 'receipt_private_read_journal_failed'),
    ('journal_catalog', 'receipt_private_read_journal_catalog_failed'),
    ('body_mismatch', 'receipt_private_readback_body_mismatch'),
    ('etag_unchanged', 'receipt_private_readback_etag_unchanged'),
    ('body_and_etag', 'receipt_private_readback_body_mismatch'),
]
NEW_CODES = {
    'receipt_private_metadata_folder_version_changed',
    'receipt_private_read_binding_failed', 'receipt_private_read_metadata_get_failed',
    'receipt_private_read_metadata_validation_failed', 'receipt_private_read_body_failed',
    'receipt_private_read_etag_failed', 'receipt_private_read_metadata_consistency_failed',
    'receipt_private_read_json_failed', 'receipt_private_read_catalog_failed',
    'receipt_private_read_journal_failed', 'receipt_private_read_journal_catalog_failed',
    'receipt_private_readback_body_mismatch', 'receipt_private_readback_etag_unchanged',
    'receipt_private_transport_metadata_get_failed', 'receipt_private_transport_metadata_validation_failed',
    'receipt_private_transport_body_get_failed', 'receipt_private_transport_body_size_failed',
    'receipt_private_transport_metadata_consistency_failed',
}
READ_SHAPE = [
    ('synthetic-folder', 'metadata'), ('synthetic-private', 'metadata'),
    ('synthetic-folder', 'acl'), ('synthetic-private', 'acl'),
    ('synthetic-private', 'meta'), ('synthetic-private', 'media'),
    ('synthetic-private', 'meta'), ('synthetic-folder', 'metadata'), ('synthetic-private', 'metadata'),
]
POST_CAS_GETS = {
    None:18, 'binding':0, 'metadata_get':1, 'metadata_decode':1, 'metadata_invalid':2,
    'transport_metadata_get':3, 'transport_metadata_decode':3, 'transport_acl_invalid':3,
    'transport_meta_invalid':5, 'body_get':6, 'body_http':6, 'body_size':6,
    'transport_metadata_race':7, 'etag':7,
}


def api_shape(trace):
    result = []
    for call in trace:
        fid = call['url'].rsplit('/', 1)[1]
        params = call['params']
        if call['method'] == 'PUT':
            assert params == {'uploadType':'media', 'fields':'id,etag'}
            assert call['headers'] == {'If-Match':'"2"', 'Content-Type':'application/json'}
            result.append((fid, 'PUT'))
        elif params == {'alt':'media'}:
            result.append((fid, 'media'))
        else:
            fields = params['fields']
            kind = 'acl' if fields == 'id,owners(emailAddress),permissions(type,role,emailAddress,deleted)' else (
                'meta' if fields == 'id,etag,parents(id),labels(trashed),mimeType' else 'metadata')
            result.append((fid, kind))
    return result


@pytest.fixture(autouse=True)
def no_live_or_financial_io(monkeypatch):
    calls = []
    def denied(*args, **kwargs):
        calls.append(True)
        raise AssertionError('external or financial IO forbidden')
    for method in ('connect', 'connect_ex', 'sendto'):
        monkeypatch.setattr(socket.socket, method, denied)
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)
    from app.sheets import SheetsDB
    monkeypatch.setattr(SheetsDB, '__init__', denied)
    yield
    assert calls == []


def exercise(fault, adapter_type=ReceiptPrivateAdapter, transport_type=ExistingReceiptDriveIO, on_claim=None):
    _, row, catalog, memory = fixture()
    config = dict(private_id='synthetic-private', folder_id='synthetic-folder',
                  owner_email='owner@synthetic.invalid', service_email='service@synthetic.invalid')
    def adapter(port):
        return adapter_type(port, file_id=config['private_id'], folder_id=config['folder_id'],
            owner_email=config['owner_email'], service_email=config['service_email'], catalog_digest=digest(catalog))
    prep = adapter(memory)
    request, tag = capture(prep, row)
    journal = ValidationJournal(prep, preflight=lambda: None)
    journal.prepare(request, expected_tag=tag)
    journal.accept_prepared(RID, request['snapshot']['digest'])
    assert memory.writes == 2
    api_trace = []
    post_put_gets = []
    armed = [False]

    class Session:
        def request(self, method, url, **kwargs):
            assert kwargs['allow_redirects'] is False and 0 < kwargs['timeout'] <= 20
            api_trace.append(dict(method=method, url=url, params=deepcopy(kwargs.get('params')),
                headers=deepcopy(kwargs.get('headers')), body_sha256=sha256(kwargs['data']).hexdigest() if 'data' in kwargs else None))
            if method == 'PUT':
                assert kwargs['headers']['If-Match'] == f'"{memory.generation}"'
                if fault in ('put_before', 'put_response'):
                    memory.fault = 'before' if fault == 'put_before' else 'after'
                memory.replace_versioned(memory.raw, kwargs['headers']['If-Match'], kwargs['data'])
                armed[0] = True
                if fault in ('etag_unchanged', 'body_and_etag'):
                    memory.generation -= 1
                if fault == 'binding':
                    port.config['file_id'] = 'synthetic-other-private'
                return SimpleNamespace(status_code=200, content=b'{}')
            assert method == 'GET' and url.startswith('https://www.googleapis.com/drive/v2/files/')
            fid = url.rsplit('/', 1)[1]
            point = 0
            if armed[0]:
                post_put_gets.append(True)
                point = len(post_put_gets)
            if fault == 'external_update' and point == 3:
                changed = json.loads(memory.raw)
                changed['journal']['generation'] += 1
                memory.replace_versioned(memory.raw, f'"{memory.generation}"', encode(changed))
            media = kwargs['params'].get('alt') == 'media'
            if (fault == 'metadata_get' and point == 1
                    or fault == 'transport_metadata_get' and point == 3
                    or fault == 'body_get' and point == 6):
                raise TimeoutError(PRIVATE)
            if fault == 'body_http' and point == 6:
                return SimpleNamespace(status_code=503, content=PRIVATE.encode())
            if media:
                raw, _ = memory.read_versioned()
                if point == 6:
                    if fault == 'body_size': raw = b' ' * (REGISTRY_MAX_BYTES + 1)
                    elif fault == 'json': raw = b'{'
                    elif fault == 'utf8': raw = b'\xff'
                    elif fault == 'duplicate_key': raw = b'{"a":1,"a":2}'
                    elif fault in ('catalog', 'journal', 'journal_catalog'):
                        value = json.loads(raw)
                        if fault == 'catalog': value['catalog']['cases'] = {}
                        elif fault == 'journal': value['journal']['generation'] = True
                        else:
                            snap = value['journal']['requests'][RID]['snapshot']
                            snap['context_digest'] = 'f' * 64
                            snap['digest'] = digest({k:v for k,v in snap.items() if k != 'digest'})
                        raw = encode(value)
                    elif fault in ('body_mismatch', 'body_and_etag'):
                        raw = json.dumps(json.loads(raw), ensure_ascii=True, indent=2).encode()
                return SimpleNamespace(status_code=200, content=raw)
            value = memory.metadata(fid, kwargs['params']['fields'])
            fields = kwargs['params']['fields']
            if fields == 'id,owners(emailAddress),permissions(type,role,emailAddress,deleted)':
                value = {k:value[k] for k in ('id', 'owners', 'permissions')}
            elif fields == 'id,etag,parents(id),labels(trashed),mimeType':
                value = {k:value[k] for k in ('id', 'etag', 'parents', 'labels', 'mimeType')}
            if fault == 'metadata_invalid' and point == 2 or fault == 'transport_acl_invalid' and point == 3:
                value['owners'] = []
            if fault == 'transport_meta_invalid' and point == 5:
                value['parents'] = []
            if fault == 'transport_metadata_race' and point == 7:
                value['etag'] = '"synthetic-race"'
            if fault == 'etag' and point in (5, 7):
                value['etag'] = '"synthetic-consistent-but-different"'
            if fault == 'metadata_race' and point == 8:
                value['version'] = '999'
            if fault == 'cas_version_lag' and point == 2:
                value['version'] = str(int(value['version']) - 1)
            if fault in ('post_version_up', 'post_version_down', 'post_both') and point == 9:
                value['version'] = str(int(value['version']) + (-1 if fault == 'post_version_down' else 1))
            if fault in ('post_etag', 'post_both') and point == 9:
                value['etag'] = '"synthetic-later-etag"'
            def decoded():
                if fault == 'metadata_decode' and point == 1 or fault == 'transport_metadata_decode' and point == 3:
                    raise ValueError(PRIVATE)
                return value
            return SimpleNamespace(status_code=200, content=encode(value), json=decoded)

    port = transport_type(config, Session())
    private = adapter(port)
    journal = ValidationJournal(private, preflight=lambda: None)
    error = None
    if on_claim is not None:
        on_claim()  # Keep preparation/acceptance logs outside this observation.
    try:
        journal.transition(RID, '検証中')
    except StateError as exc:
        error = str(exc)
    claim_trace = deepcopy(api_trace)
    stopped = private._stopped
    before_reconcile = memory.attempts
    # Clearing the injected fault allows a GET, never a new CAS after ambiguity.
    armed[0] = False
    memory.fault = None
    port.config['file_id'] = 'synthetic-private'
    reconciled, current_tag = private.read_versioned()
    replay_error = None
    if fault:
        try:
            private.replace_versioned(reconciled, current_tag)
        except StateError as exc:
            replay_error = str(exc)
        assert replay_error == 'receipt_private_write_stopped'
    expected_attempts = 4 if fault == 'external_update' else 3
    assert memory.attempts == before_reconcile == expected_attempts
    assert memory.writes == (2 if fault == 'put_before' else expected_attempts)
    return dict(error=error, stopped=stopped, stopped_after_reconcile=private._stopped,
        replay_error=replay_error, api_trace=claim_trace, reconcile_trace=api_trace[len(claim_trace):],
        cas_attempts=memory.attempts, cas_writes=memory.writes, persistent_bytes_sha256=sha256(memory.raw).hexdigest(),
        persistent_etag=f'"{memory.generation}"', persistent_journal=json.loads(memory.raw)['journal'],
        ack_pending=private._ack is not None, cached_tag=private._cached[1])


@pytest.mark.parametrize('fault,code', CASES, ids=[fault or 'success' for fault,_ in CASES])
def test_claim_readback_faults_are_classified_without_data_or_retries(caplog, fault, code):
    caplog.set_level(logging.WARNING)
    result = exercise(fault, on_claim=caplog.clear)
    messages = [r.getMessage() for r in caplog.records]
    assert set(messages) <= PRIVATE_CODES | WORKER_CODES | NEW_CODES | GET_DIAGNOSTIC_CODES
    assert all(PRIVATE not in message and RID not in message for message in messages)
    assert result['persistent_journal']['generation'] == 3
    assert result['persistent_journal']['requests'][RID]['state'] == '検証中'
    assert result['persistent_journal']['requests'][RID]['result'] is None
    assert sum(c['method'] == 'PUT' for c in result['api_trace']) == 1
    # Exact pre-change HTTP sequence, including the inherited CAS pre-read.
    # A mismatched ETag must still short-circuit the final two metadata GETs.
    post = POST_CAS_GETS.get(fault, 9)
    expected = READ_SHAPE * 2 + READ_SHAPE[2:7] + [('synthetic-private', 'PUT')]
    expected += (READ_SHAPE * 2)[:post]
    assert api_shape(result['api_trace']) == expected
    assert api_shape(result['reconcile_trace']) == READ_SHAPE
    if fault:
        assert result['error'] == 'receipt_private_write_unknown'
        assert result['stopped'] and result['stopped_after_reconcile']
        assert code in messages and 'receipt_private_adapter_readback_failed' in messages
        assert 'receipt_private_ack_readback_begin' not in messages
    else:
        assert result['error'] is None and not result['stopped'] and not result['stopped_after_reconcile']
        assert not set(messages) & NEW_CODES
        assert 'receipt_private_ack_readback_ok' in messages
    if fault == 'body_and_etag':
        assert 'receipt_private_readback_etag_unchanged' not in messages
