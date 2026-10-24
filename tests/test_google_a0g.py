"""Synthetic credentials and metadata only; conftest denies all live sockets."""
from copy import deepcopy
from dataclasses import replace
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from app.drive_run_state import StateError
from staging_shadow import google_a0g as a0
from test_google_runner_preflight import manifest, SHA, CLIENT


def contract():
    m = manifest(); m['storage'] = None
    return dict(schema=a0.SCHEMA, preflight=m,
        oauth=dict(client_type='Desktop', audience='External', publishing_status='Testing', scopes=[a0.DRIVE_FILE]),
        book_version='9', limits=deepcopy(a0.LIMITS), approved=False)


def approval(c=None, **changes):
    value = a0.ApprovedAttempt(SHA, a0.contract_digest(c or contract(), expected_sha=SHA), 2000,
        oauth_authorization=True, token_exchange=True, refresh_token_issuance=True, metadata_get=True)
    return replace(value, **changes)


def grant(**changes):
    return replace(a0.AccessGrant('SYNTHETIC_ACCESS_TOKEN', CLIENT, (a0.DRIVE_FILE,), 1500), **changes)


def payloads():
    owner = dict(permissionId='owner-id', emailAddress='owner@example.test')
    def file(fid, mime):
        return dict(id=fid, parents=['inputs'], mimeType=mime, trashed=False, shared=False,
            owners=[owner], capabilities=dict(canEdit=True, canDownload=True), version='9', sha256Checksum='b'*64)
    acl = dict(permissions=[dict(id='owner-id', type='user', role='owner', deleted=False)])
    return [dict(user=owner, storageQuota=dict(limit='100000', usage='0')),
        file('book', a0.BOOK), acl,
        dict(spreadsheetId='book', sheets=[dict(properties=dict(sheetId=42, title='確認'))]),
        file('png', 'image/png'), deepcopy(acl)]


class Sender:
    def __init__(self, values=None):
        self.values = payloads() if values is None else values
        self.calls = []

    def __call__(self, url, headers, **kwargs):
        self.calls.append((url, headers, kwargs))
        value = self.values[len(self.calls)-1]
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, a0.Response) else a0.Response(200, json.dumps(value).encode())


def transport(*, c=None, ap=None, g=None, sender=None, wall=None, mono=None, enabled=True):
    c = c or contract()
    return a0.GuardedMetadataHTTP(c, expected_sha=SHA, approval=ap or approval(c),
        grant=g or grant(), enabled=enabled, sender=sender or Sender(),
        wall_clock=wall or (lambda: 1000), monotonic=mono or (lambda: 0))


def run(*, c=None, sender=None, **kwargs):
    c = c or contract()
    return a0.run_metadata_audit(c, expected_sha=SHA, approval=approval(c), grant=grant(),
        enabled=True, sender=sender or Sender(), wall_clock=lambda: 1000, monotonic=lambda: 0, **kwargs)


def test_existing_preflight_is_composed_with_exact_six_wire_requests_and_never_go():
    sender = Sender(); result = run(sender=sender)
    assert result['status'] == 'METADATA_VERIFIED_NO_GO'
    assert result['runner_write_allowed'] is False
    assert result['blockers'] == ['FIRESTORE_EFFECTIVE_ISOLATION_UNPROVEN', 'PERSISTENCE_WRITE_TRIAL_REQUIRED', 'STORAGE_NOT_PROVISIONED']
    plan = a0.request_plan(contract(), expected_sha=SHA)
    assert len(sender.calls) == 6
    for (url, headers, options), (expected_url, expected_query) in zip(sender.calls, plan):
        assert url.split('?')[0] == expected_url
        assert set(parse_qs(urlsplit(url).query)) == set(expected_query)
        assert headers == {'Authorization': 'Bearer SYNTHETIC_ACCESS_TOKEN', 'Accept': 'application/json'}
        assert options == dict(timeout=10, max_bytes=1_048_576)
    assert 'includeGridData=false' in sender.calls[3][0]
    assert all('SYNTHETIC_ACCESS_TOKEN' not in url for url, _, _ in sender.calls)


def test_default_off_neither_reads_contract_nor_creates_sender():
    assert a0.run_metadata_audit(None, expected_sha=None) == dict(status='DISABLED', runner_write_allowed=False)


@pytest.mark.parametrize('change', ['sha', 'client', 'type', 'scope', 'oidc', 'scope_duplicate', 'audience',
    'publishing', 'approved', 'storage', 'extra', 'sources', 'case', 'mime', 'timeout', 'retry', 'redirect', 'write', 'bool_limit'])
def test_contract_drift_rejected_without_io(change):
    c = contract()
    if change == 'sha': c['preflight']['candidate_sha'] = 'c'*40
    elif change == 'client': c['preflight']['oauth_client_id'] = None
    elif change == 'type': c['oauth']['client_type'] = 'Web'
    elif change == 'scope': c['oauth']['scopes'] = ['https://www.googleapis.com/auth/drive']
    elif change == 'oidc': c['oauth']['scopes'].append('openid')
    elif change == 'scope_duplicate': c['oauth']['scopes'] *= 2
    elif change == 'audience': c['oauth']['audience'] = 'Internal'
    elif change == 'publishing': c['oauth']['publishing_status'] = 'In production'
    elif change == 'approved': c['approved'] = True
    elif change == 'storage': c['preflight']['storage'] = manifest()['storage']
    elif change == 'extra': c['access_token'] = 'SYNTHETIC'
    elif change == 'sources': c['preflight']['sources'] *= 2
    elif change == 'case': c['preflight']['sources'][0]['case_id'] = 'SYN-M101'
    elif change == 'mime': c['preflight']['sources'][0]['mime_type'] = 'application/pdf'
    elif change == 'timeout': c['limits']['http_seconds'] = 11
    elif change == 'retry': c['limits']['retries'] = 1
    elif change == 'redirect': c['limits']['redirects'] = True
    elif change == 'write': c['limits']['runner_write_allowed'] = True
    else: c['limits']['retries'] = False
    with pytest.raises(StateError): a0.validate_contract(c, expected_sha=SHA)


@pytest.mark.parametrize('method', ['POST', 'PATCH', 'DELETE', 'PUT', 'HEAD', 'get', 'OPTIONS'])
def test_http_methods_rejected_before_sender_and_failure_latches(method):
    sender = Sender(); http = transport(sender=sender); url, q = a0.request_plan(contract(), expected_sha=SHA)[0]
    with pytest.raises(StateError, match='request_not_allowlisted'): http.request(method, url, q)
    with pytest.raises(StateError, match='session_stopped'): http.request('GET', url, q)
    assert sender.calls == []


@pytest.mark.parametrize('change', ['host', 'http', 'userinfo', 'port', 'fragment', 'query_url', 'other_file',
    'encoded_path', 'batch', 'discovery', 'media', 'fields', 'extra_query', 'body', 'headers', 'token_query'])
def test_wire_target_and_payload_allowlist_reject_before_send(change):
    sender = Sender(); http = transport(sender=sender)
    url, q = a0.request_plan(contract(), expected_sha=SHA)[0]; q = deepcopy(q); kwargs = {}
    if change == 'host': url = url.replace('www.googleapis.com', 'metadata.google.internal')
    elif change == 'http': url = url.replace('https:', 'http:')
    elif change == 'userinfo': url = url.replace('https://', 'https://bad@')
    elif change == 'port': url = url.replace('.com/', '.com:443/')
    elif change == 'fragment': url += '#fragment'
    elif change == 'query_url': url += '?fields=*'
    elif change == 'other_file': url = 'https://www.googleapis.com/drive/v3/files/unapproved'
    elif change == 'encoded_path': url = url.replace('/about', '/%61bout')
    elif change == 'batch': url = 'https://www.googleapis.com/batch/drive/v3'
    elif change == 'discovery': url = 'https://www.googleapis.com/discovery/v1/apis/drive/v3/rest'
    elif change == 'media': q['alt'] = 'media'
    elif change == 'fields': q['fields'] = '*'
    elif change == 'extra_query': q['pageToken'] = 'next'
    elif change == 'body': kwargs['body'] = b''
    elif change == 'headers': kwargs['headers'] = {'X-HTTP-Method-Override': 'PATCH'}
    else: q['access_token'] = 'SYNTHETIC'
    with pytest.raises(StateError): http.request('GET', url, q, **kwargs)
    assert sender.calls == []


@pytest.mark.parametrize('change', ['client', 'scope', 'duplicate_scope', 'expired', 'nan_expiry', 'newline_token',
    'approval_digest', 'approval_sha', 'approval_expiry', 'auth_approval', 'exchange_approval', 'refresh_approval', 'get_approval', 'off'])
def test_credentials_and_approval_binding_fail_before_io(change):
    sender = Sender(); g = grant(); ap = approval(); enabled = True
    if change == 'client': g = grant(client_id='456-other.apps.googleusercontent.com')
    elif change == 'scope': g = grant(scopes=(a0.DRIVE_FILE, 'openid'))
    elif change == 'duplicate_scope': g = grant(scopes=(a0.DRIVE_FILE, a0.DRIVE_FILE))
    elif change == 'expired': g = grant(expires_at=1000)
    elif change == 'nan_expiry': g = grant(expires_at=float('nan'))
    elif change == 'newline_token': g = grant(access_token='synthetic\r\nheader')
    elif change == 'approval_digest': ap = approval(contract_digest='c'*64)
    elif change == 'approval_sha': ap = approval(candidate_sha='c'*40)
    elif change == 'approval_expiry': ap = approval(expires_at=1000)
    elif change == 'auth_approval': ap = approval(oauth_authorization=False)
    elif change == 'exchange_approval': ap = approval(token_exchange=False)
    elif change == 'refresh_approval': ap = approval(refresh_token_issuance=False)
    elif change == 'get_approval': ap = approval(metadata_get=False)
    else: enabled = False
    with pytest.raises(StateError): transport(sender=sender, g=g, ap=ap, enabled=enabled)
    assert sender.calls == []


@pytest.mark.parametrize('status', [301, 302, 307, 308, 400, 401, 403, 404, 429, 500])
def test_denials_redirects_and_errors_stop_without_retry_refresh_or_follow(status):
    sender = Sender([a0.Response(status, b'private response must not escape')]); http = transport(sender=sender)
    url, q = a0.request_plan(contract(), expected_sha=SHA)[0]
    with pytest.raises(StateError) as err: http.request('GET', url, q)
    assert 'private response' not in str(err.value)
    with pytest.raises(StateError, match='session_stopped'): http.request('GET', url, q)
    assert len(sender.calls) == 1


@pytest.mark.parametrize('raw', [b'not-json', b'[]', b'{"error":"invalid_grant"}', b'{"a":1,"a":2}', b'{"a":NaN}', b' '*1_048_577],
    ids=['malformed', 'array', 'oauth-error', 'duplicate-keys', 'nonfinite', 'over-limit'])
def test_invalid_or_oversized_responses_stop(raw):
    sender = Sender([a0.Response(200, raw)]); http = transport(sender=sender)
    url, q = a0.request_plan(contract(), expected_sha=SHA)[0]
    with pytest.raises(StateError): http.request('GET', url, q)
    assert len(sender.calls) == 1


def test_timeout_exception_never_exposes_secret_and_never_retries():
    sender = Sender([TimeoutError('SYNTHETIC_ACCESS_TOKEN private-url')]); http = transport(sender=sender)
    url, q = a0.request_plan(contract(), expected_sha=SHA)[0]
    with pytest.raises(StateError) as error: http.request('GET', url, q)
    assert 'SYNTHETIC' not in str(error.value)
    assert len(sender.calls) == 1


def test_owner_mismatch_stops_after_about_only():
    values = payloads(); values[0]['user']['emailAddress'] = 'other@example.test'
    sender = Sender(values)
    with pytest.raises(StateError): run(sender=sender)
    assert len(sender.calls) == 1


def test_pinned_book_change_stops_before_other_metadata():
    values = payloads(); values[1]['version'] = '10'; sender = Sender(values)
    with pytest.raises(StateError, match='book_revision_changed'): run(sender=sender)
    assert len(sender.calls) == 2


@pytest.mark.parametrize('fault', ['source_version', 'parent', 'sharing', 'pagination', 'sheet'])
def test_existing_preflight_metadata_safety_checks_are_not_bypassed(fault):
    values = payloads()
    if fault == 'source_version': values[4]['version'] = '10'
    elif fault == 'parent': values[1]['parents'] = ['other']
    elif fault == 'sharing': values[1]['shared'] = True
    elif fault == 'pagination': values[2]['nextPageToken'] = 'more'
    else: values[3]['sheets'][0]['properties']['sheetId'] = 41
    with pytest.raises(StateError): run(sender=Sender(values))


def test_first_owner_and_revision_observations_remain_unpinned_no_go():
    c = contract(); c['book_version'] = None; c['preflight']['owner_permission_id'] = None
    c['preflight']['sources'][0].update(sha256=None, version=None)
    original = deepcopy(c); result = run(c=c)
    assert c == original
    assert {'OWNER_PERMISSION_ID_REVIEW_REQUIRED', 'SOURCE_PIN_REVIEW_REQUIRED', 'BOOK_VERSION_REVIEW_REQUIRED', 'STORAGE_NOT_PROVISIONED'} <= set(result['blockers'])
    assert result['runner_write_allowed'] is False


def test_request_count_and_replay_order_are_bounded():
    sender = Sender(); http = transport(sender=sender); plan = a0.request_plan(contract(), expected_sha=SHA)
    for url, q in plan: http.request('GET', url, q)
    with pytest.raises(StateError, match='request_limit'): http.request('GET', *plan[0])
    assert len(sender.calls) == 6
    sender = Sender(); http = transport(sender=sender)
    with pytest.raises(StateError): http.request('GET', *plan[1])
    assert sender.calls == []


def test_wrong_file_and_boolean_query_at_correct_sequence_position_are_rejected():
    plan = a0.request_plan(contract(), expected_sha=SHA)
    sender = Sender(); http = transport(sender=sender)
    http.request('GET', *plan[0])
    with pytest.raises(StateError):
        http.request('GET', plan[1][0].replace('/book', '/other'), plan[1][1])
    assert len(sender.calls) == 1
    sender = Sender(); http = transport(sender=sender)
    for url, q in plan[:3]: http.request('GET', url, q)
    q = deepcopy(plan[3][1]); q['includeGridData'] = 0
    with pytest.raises(StateError): http.request('GET', plan[3][0], q)
    assert len(sender.calls) == 3


def test_deadlines_rechecked_between_requests_and_contract_is_frozen():
    now = [1000]; elapsed = [0]; sender = Sender(); c = contract()
    http = transport(c=c, sender=sender, wall=lambda:now[0], mono=lambda:elapsed[0])
    c['preflight']['book']['file_id'] = 'tampered'
    plan = a0.request_plan(contract(), expected_sha=SHA)
    http.request('GET', *plan[0]); now[0] = 1500
    with pytest.raises(StateError): http.request('GET', *plan[1])
    assert len(sender.calls) == 1
    http = transport(sender=Sender(), mono=lambda:elapsed[0]); elapsed[0] = 180
    with pytest.raises(StateError, match='process_deadline'): http.request('GET', *plan[0])


def test_response_after_expiry_is_not_accepted():
    now = [1000]; source = Sender()
    def sender(*args, **kwargs):
        result = source(*args, **kwargs); now[0] = 1500; return result
    http = transport(sender=sender, wall=lambda:now[0])
    with pytest.raises(StateError): http.request('GET', *a0.request_plan(contract(), expected_sha=SHA)[0])
    assert len(source.calls) == 1


def test_callback_and_pure_pkce_plan_have_exact_two_files_single_scope():
    c = contract()
    params = a0.authorization_parameters(c, expected_sha=SHA, approval=approval(), state='s'*32,
        code_verifier='v'*43, redirect_uri='http://127.0.0.1:8765/oauth2callback', now=1000)
    assert params['code_challenge_method'] == 'S256' and params['scope'] == a0.DRIVE_FILE
    assert params['file_ids'] == 'book,png' and params['allow_folder_selection'] == 'false'
    assert params['include_granted_scopes'] == 'false' and params['access_type'] == 'offline'
    assert a0.validate_callback(c, expected_sha=SHA, expected_state='s'*32, returned_state='s'*32,
        picked_file_ids=['png','book'], scopes=[a0.DRIVE_FILE]) == 'CALLBACK_BOUND_NO_TOKEN_EXCHANGE'


@pytest.mark.parametrize('change', ['state', 'extra', 'missing', 'duplicate', 'oidc', 'denied'])
def test_bad_callback_never_becomes_an_exchange(change):
    args = dict(expected_sha=SHA, expected_state='s'*32, returned_state='s'*32,
        picked_file_ids=['book','png'], scopes=[a0.DRIVE_FILE])
    if change == 'state': args['returned_state'] = 'x'*32
    elif change == 'extra': args['picked_file_ids'].append('other')
    elif change == 'missing': args['picked_file_ids'] = ['book']
    elif change == 'duplicate': args['picked_file_ids'] = ['book','book']
    elif change == 'oidc': args['scopes'].append('openid')
    else: args['error'] = 'access_denied private details'
    with pytest.raises(StateError): a0.validate_callback(contract(), **args)


@pytest.mark.parametrize('redirect', ['https://example.test/oauth2callback', 'http://localhost:8765/oauth2callback',
    'http://127.0.0.1:8765/other', 'http://127.0.0.1:8765/oauth2callback?target=other'])
def test_external_or_changed_redirect_plan_is_rejected(redirect):
    with pytest.raises(StateError): a0.authorization_parameters(contract(), expected_sha=SHA, approval=approval(),
        state='s'*32, code_verifier='v'*43, redirect_uri=redirect, now=1000)


def test_exchange_result_uses_actual_expiry_and_never_hands_refresh_to_data_transport():
    response = dict(access_token='SYNTHETIC_ACCESS_TOKEN', refresh_token='SYNTHETIC_REFRESH_TOKEN',
        token_type='Bearer', scope=a0.DRIVE_FILE, expires_in=123)
    value = a0.accept_exchange_result(contract(), expected_sha=SHA, approval=approval(),
        actual_client_id=CLIENT, response=response, now=1000)
    assert value.expires_at == 1123
    assert 'SYNTHETIC' not in repr(value)
    assert not hasattr(value, 'refresh_token')
    with pytest.raises(StateError): a0.accept_exchange_result(contract(), expected_sha=SHA,
        approval=approval(refresh_token_issuance=False), actual_client_id=CLIENT, response=response, now=1000)


@pytest.mark.parametrize('response', [dict(error='invalid_grant'), dict(error='access_denied'),
    dict(access_token='SYNTHETIC', token_type='Bearer', scope=a0.DRIVE_FILE, expires_in=0),
    dict(access_token='SYNTHETIC', token_type='Bearer', scope=a0.DRIVE_FILE+' openid', expires_in=100)])
def test_expired_revoked_or_widened_exchange_result_stops(response):
    with pytest.raises(StateError): a0.accept_exchange_result(contract(), expected_sha=SHA,
        approval=approval(), actual_client_id=CLIENT, response=response, now=1000)


def test_real_wire_builder_uses_literal_get_and_closes_connection(monkeypatch):
    seen = []
    class Connection:
        def __init__(self, host, timeout): seen.append((host, timeout))
        def request(self, method, path, *, headers): seen.append((method, path, headers))
        def getresponse(self): return self
        status = 302
        def read(self, limit): return b'{}'
        def close(self): seen.append('closed')
    monkeypatch.setattr(a0, 'HTTPSConnection', Connection)
    response = a0._https_get_direct('https://www.googleapis.com/drive/v3/about?fields=user',
        {'Authorization':'Bearer SYNTHETIC'}, timeout=10, max_bytes=100)
    assert seen[1][0] == 'GET' and seen[-1] == 'closed' and response.status == 302
    assert len(seen) == 3  # No redirect follow or retry.


def _stall_worker(pipe, *_args):
    time.sleep(2)
    pipe.close()


def _success_worker(pipe, *_args):
    pipe.send((200, b'{}'))
    pipe.close()


def test_parent_watchdog_accepts_bounded_synthetic_worker_response():
    response = a0._bounded_wire(_success_worker, 'https://www.googleapis.com/unused', {}, timeout=5, max_bytes=100)
    assert response == a0.Response(200, b'{}')


def test_parent_watchdog_terminates_stalled_dns_or_wire_worker_without_network():
    start = time.monotonic()
    with pytest.raises(StateError, match='http_deadline'):
        a0._bounded_wire(_stall_worker, 'https://www.googleapis.com/unused', {}, timeout=0.1, max_bytes=100)
    assert time.monotonic() - start < 3
