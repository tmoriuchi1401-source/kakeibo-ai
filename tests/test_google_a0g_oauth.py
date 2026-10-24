"""Only synthetic code/secret/token. No OAuth authorization or network."""
from copy import deepcopy
import json

import pytest

from app.drive_run_state import StateError
from staging_shadow import google_a0g as a0
from staging_shadow import google_a0g_oauth as oauth
from test_google_a0g import contract, approval, SHA, Sender


def callback():
    return dict(state='s'*32, picked_file_ids=['book','png'], scopes=[a0.DRIVE_FILE], code='SYNTHETIC_CODE')


def exchange(sender, **kwargs):
    return oauth.ApprovedCodeExchange(contract(), expected_sha=SHA, approval=approval(), enabled=True,
        sender=sender, wall_clock=lambda:1000, monotonic=lambda:0, **kwargs)


def call(adapter, cb=None, **kwargs):
    return adapter.exchange(callback=callback() if cb is None else cb, expected_state='s'*32,
        code_verifier='v'*43, redirect_uri='http://127.0.0.1:8765/oauth2callback',
        client_secret='SYNTHETIC_CLIENT_SECRET', **kwargs)


def test_separate_approved_exchange_hands_only_access_token_to_six_get_composition():
    calls = []
    def sender(params, **options):
        calls.append((params, options))
        return a0.Response(200, json.dumps(dict(access_token='SYNTHETIC_ACCESS_TOKEN',
            refresh_token='SYNTHETIC_REFRESH_TOKEN', scope=a0.DRIVE_FILE,
            expires_in=90, token_type='Bearer')).encode())
    adapter = exchange(sender); grant = call(adapter)
    assert grant.expires_at == 1090 and not hasattr(grant, 'refresh_token')
    assert 'SYNTHETIC' not in repr(grant)
    assert len(calls) == 1 and calls[0][0]['grant_type'] == 'authorization_code'
    assert 'refresh_token' not in calls[0][0] and 'scope' not in calls[0][0]
    data = Sender()
    result = a0.run_metadata_audit(contract(), expected_sha=SHA, approval=approval(), grant=grant,
        enabled=True, sender=data, wall_clock=lambda:1000, monotonic=lambda:0)
    assert result['runner_write_allowed'] is False and len(data.calls) == 6
    with pytest.raises(StateError, match='exchange_attempt_consumed'): call(adapter)
    assert len(calls) == 1


@pytest.mark.parametrize('change', ['state', 'files', 'scope', 'denied', 'code'])
def test_unbound_callback_cannot_reach_token_sender(change):
    calls = []; cb = callback()
    if change == 'state': cb['state'] = 'x'*32
    elif change == 'files': cb['picked_file_ids'].append('other')
    elif change == 'scope': cb['scopes'].append('openid')
    elif change == 'denied': cb['error'] = 'access_denied'
    else: cb['code'] = ''
    adapter = exchange(lambda *args, **kwargs:calls.append(args))
    with pytest.raises(StateError): call(adapter, cb)
    assert calls == []


@pytest.mark.parametrize('status', [302, 400, 401, 403, 429, 500])
def test_exchange_failure_is_not_retried_or_followed(status):
    calls = []
    def sender(*args, **kwargs): calls.append(args); return a0.Response(status, b'private error')
    adapter = exchange(sender)
    with pytest.raises(StateError) as err: call(adapter)
    assert 'private error' not in str(err.value)
    with pytest.raises(StateError): call(adapter)
    assert len(calls) == 1


def test_exchange_network_error_never_leaks_inputs_and_attempt_stays_consumed():
    def sender(*args, **kwargs): raise TimeoutError('SYNTHETIC_CLIENT_SECRET SYNTHETIC_CODE')
    adapter = exchange(sender)
    with pytest.raises(StateError) as err: call(adapter)
    assert 'SYNTHETIC' not in str(err.value)
    with pytest.raises(StateError, match='attempt_consumed'): call(adapter)


def test_exchange_is_off_by_default_and_refresh_issuance_needs_approval():
    with pytest.raises(StateError, match='disabled'):
        oauth.ApprovedCodeExchange({}, expected_sha=None, approval=None)
    with pytest.raises(StateError, match='separate_approval'):
        oauth.ApprovedCodeExchange(contract(), expected_sha=SHA,
            approval=approval(refresh_token_issuance=False), enabled=True, wall_clock=lambda:1000)


def test_auth_wire_endpoint_and_fields_are_separate_from_data_transport(monkeypatch):
    seen = []
    class Connection:
        def __init__(self, host, timeout): seen.append((host, timeout))
        def request(self, method, path, *, body, headers): seen.append((method, path, body, headers))
        def getresponse(self): return self
        status = 200
        def read(self, limit): return b'{}'
        def close(self): seen.append('closed')
    monkeypatch.setattr(oauth, 'HTTPSConnection', Connection)
    params = dict(client_id='123-probe.apps.googleusercontent.com', client_secret='SYNTHETIC_SECRET',
        code='SYNTHETIC_CODE', code_verifier='v'*43,
        redirect_uri='http://127.0.0.1:8765/oauth2callback', grant_type='authorization_code')
    oauth._token_post_direct(params, timeout=10, max_bytes=100)
    assert seen[0] == ('oauth2.googleapis.com', 10)
    assert seen[1][0:2] == ('POST','/token') and seen[-1] == 'closed'
    assert b'grant_type=authorization_code' in seen[1][2]
    bad = deepcopy(params); bad['grant_type'] = 'refresh_token'
    with pytest.raises(StateError, match='refresh_forbidden'): oauth._token_post_direct(bad, timeout=10, max_bytes=100)
    assert len(seen) == 3


@pytest.mark.parametrize('method', ['GET','POST'])
def test_data_transport_cannot_call_token_or_revoke_endpoints(method):
    from test_google_a0g import transport
    data = Sender(); http = transport(sender=data)
    with pytest.raises(StateError):
        http.request(method, 'https://oauth2.googleapis.com/token', {})
    assert data.calls == []
