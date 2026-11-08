"""Offline diagnosis of baseline gaps, not a live transport or recovery host.

The restart tests deliberately demonstrate permitted baseline writes after a
lost response. They must not be read as acceptance of that live behavior.
"""
import socket
from copy import deepcopy

import pytest
import requests
from google.auth.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession
from requests.adapters import BaseAdapter

from app.drive_run_state import StateError
from app.pdf_intake_registry import DriveRegistryIO
from app.receipt_sheet_validation import UNKNOWN, execute_accepted_request
from test_receipt_private_adapter import adapter, capture, fixture, journal
from test_receipt_sheet_validation import RID


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("live/financial IO forbidden")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    from app.sheets import SheetsDB
    monkeypatch.setattr(SheetsDB, "__init__", denied)


class FakeCredentials(Credentials):
    def __init__(self):
        super().__init__()
        self.token = "synthetic-token"
        self.refreshes = 0

    def refresh(self, request):
        self.refreshes += 1
        self.token = "synthetic-refreshed-token"


class Wire(BaseAdapter):
    """Count PreparedRequest sends below AuthorizedSession, without sockets."""
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append((request, kwargs))
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        response = requests.Response()
        response.status_code = outcome
        response._content = b'{}'
        response.request = request
        response.url = request.url
        response.headers['Location'] = 'https://redirect.synthetic.invalid/other'
        return response

    def close(self):
        pass


def transport(outcomes, *, no_auth_retry=False):
    credentials = FakeCredentials()
    options = dict(max_refresh_attempts=0, refresh_status_codes=()) if no_auth_retry else {}
    session = AuthorizedSession(credentials, **options)
    session.trust_env = False
    wire = Wire(outcomes)
    session.mount('https://', wire)
    config = dict(file_id='synthetic-file-0001', folder_id='synthetic-folder-0001',
                  inbox_id='synthetic-inbox-0001', processed_id='synthetic-processed-0001',
                  owner_digest='a'*64)
    io = DriveRegistryIO(config, {'client_email': 'service@synthetic.invalid'},
                         session=session, writable=True)
    io.read_versioned = lambda: (b'before', '"old"')
    return io, credentials, wire


def test_v2_if_match_put_shape_and_no_implicit_readback():
    io, credentials, wire = transport([200])
    io.replace_versioned(b'before', '"old"', b'after')
    assert len(wire.sent) == 1
    request, options = wire.sent[0]
    assert request.method == 'PUT'
    assert request.url == ('https://www.googleapis.com/upload/drive/v2/files/'
                           'synthetic-file-0001?uploadType=media&fields=id%2Cetag')
    assert request.headers['If-Match'] == '"old"'
    assert request.headers['Content-Type'] == 'application/json'
    assert request.body == b'after'
    assert options['timeout'] == 20
    assert credentials.refreshes == 0
    # This IO method itself does no post-PUT GET; its caller must do it.


@pytest.mark.parametrize('statuses, sends, refreshes, succeeds', [
    ([401, 200], 2, 1, True),
    ([401, 401, 200], 3, 2, True),
    ([401, 401, 401], 3, 2, False),
    ([401, 412], 2, 1, False),
])
def test_default_authorized_session_repeats_same_put(statuses, sends, refreshes, succeeds):
    io, credentials, wire = transport(statuses)
    if succeeds:
        io.replace_versioned(b'before', '"old"', b'after')
    else:
        with pytest.raises(StateError):
            io.replace_versioned(b'before', '"old"', b'after')
    assert len(wire.sent) == sends
    assert credentials.refreshes == refreshes
    assert all(r.method == 'PUT' and r.body == b'after' and r.headers['If-Match'] == '"old"'
               for r, _ in wire.sent)


@pytest.mark.parametrize('outcome', [401, 412, 429, 500, 503, 301, 302, 303, 307, 308,
                                  requests.Timeout('synthetic'), requests.ConnectionError('synthetic')])
def test_injected_zero_refresh_session_sends_once_on_failure(outcome):
    io, credentials, wire = transport([outcome], no_auth_retry=True)
    with pytest.raises(StateError):
        io.replace_versioned(b'before', '"old"', b'after')
    assert len(wire.sent) == 1
    assert credentials.refreshes == 0


def test_server_412_and_local_stale_share_error_but_different_send_counts():
    io, _, wire = transport([412])
    with pytest.raises(StateError, match='^HTTP_412$'):
        io.replace_versioned(b'before', '"old"', b'after')
    assert len(wire.sent) == 1
    with pytest.raises(StateError, match='^HTTP_412$'):
        io.replace_versioned(b'not-current', '"old"', b'after')
    assert len(wire.sent) == 1  # Local rejection sent zero additional PUTs.


def test_default_http_adapter_retry_is_zero_but_not_auth_retry():
    with AuthorizedSession(FakeCredentials()) as session:
        retry = session.get_adapter('https://').max_retries
        assert retry.total == 0 and retry.read is False
        assert session._max_refresh_attempts == 2


def test_expired_deadline_prevents_send(monkeypatch):
    io, _, wire = transport([])
    io.deadline = 100
    monkeypatch.setattr('app.pdf_intake_registry.monotonic', lambda: 101)
    with pytest.raises(StateError, match='drive_deadline'):
        io.replace_versioned(b'before', '"old"', b'after')
    assert wire.sent == []


@pytest.mark.parametrize('tag', ['W/"weak"', '', 'unquoted'])
def test_weak_or_missing_etag_never_sends_put(tag):
    io, _, wire = transport([])
    with pytest.raises(StateError, match='conditional_write_forbidden'):
        io.replace_versioned(b'before', tag, b'after')
    assert wire.sent == []


@pytest.mark.parametrize('phase', ['prepare', 'accept', 'claim'])
def test_restart_loses_stop_latch_when_failed_write_did_not_commit(phase):
    _, row, catalog, memory = fixture()
    first = adapter(memory, catalog)
    request, tag = capture(first, row)
    if phase != 'prepare':
        journal(first).prepare(request, expected_tag=tag)
    if phase == 'claim':
        journal(first).accept_prepared(RID, request['snapshot']['digest'])
    memory.fault = 'before'

    def operation(target):
        j = journal(target)
        if phase == 'prepare':
            j.prepare(request, expected_tag=tag)
        elif phase == 'accept':
            j.accept_prepared(RID, request['snapshot']['digest'])
        else:
            j.transition(RID, '検証中')

    with pytest.raises(StateError, match='write_unknown'):
        operation(first)
    attempts = memory.attempts
    memory.fault = None
    first.read_versioned()
    with pytest.raises(StateError, match='write_stopped'):
        operation(first)
    assert memory.attempts == attempts
    # Diagnostic: durable journal is unchanged, so a new instance can resend.
    restarted = adapter(memory, catalog)
    restarted.read_versioned()
    operation(restarted)
    assert memory.attempts == attempts + 1


def test_lost_claim_response_and_restart_can_invoke_validator_if_claim_not_saved():
    _, row, catalog, memory = fixture()
    first = adapter(memory, catalog)
    request, tag = capture(first, row)
    journal(first).prepare(request, expected_tag=tag)
    journal(first).accept_prepared(RID, request['snapshot']['digest'])
    calls = []
    def reader(case):
        calls.append(case)
        raise StateError('synthetic-hold')
    memory.fault = 'before'
    assert execute_accepted_request(RID, reader=reader, journal=journal(first))['state'] == UNKNOWN
    assert calls == []
    memory.fault = None
    restarted = adapter(memory, catalog)
    result = execute_accepted_request(RID, reader=reader, journal=journal(restarted))
    assert calls == [row[0]] and result['state'] == '保留'


@pytest.mark.parametrize('phase', ['prepare', 'accept', 'claim'])
def test_committed_lost_response_is_found_by_get_and_same_phase_replay_rejected(phase):
    _, row, catalog, memory = fixture()
    first = adapter(memory, catalog)
    request, tag = capture(first, row)
    if phase != 'prepare':
        journal(first).prepare(request, expected_tag=tag)
    if phase == 'claim':
        journal(first).accept_prepared(RID, request['snapshot']['digest'])
    memory.fault = 'after'
    with pytest.raises(StateError):
        if phase == 'prepare':
            journal(first).prepare(request, expected_tag=tag)
        elif phase == 'accept':
            journal(first).accept_prepared(RID, request['snapshot']['digest'])
        else:
            journal(first).transition(RID, '検証中')
    memory.fault = None
    restarted = adapter(memory, catalog)
    observed = journal(restarted).query(RID)
    assert observed['state'] == {'prepare': '下書き準備済み', 'accept': '受付済み', 'claim': '検証中'}[phase]
    attempts = memory.attempts
    with pytest.raises(StateError):
        if phase == 'prepare':
            journal(restarted).prepare(request, expected_tag=restarted.read_versioned()[1])
        elif phase == 'accept':
            journal(restarted).accept_prepared(RID, request['snapshot']['digest'])
        else:
            journal(restarted).transition(RID, '検証中')
    assert memory.attempts == attempts


def test_persisted_unknown_cannot_resume_even_in_new_instance():
    _, row, catalog, memory = fixture()
    first = adapter(memory, catalog)
    request, tag = capture(first, row)
    journal(first).prepare(request, expected_tag=tag)
    journal(first).accept_prepared(RID, request['snapshot']['digest'])
    journal(first).transition(RID, UNKNOWN)
    restarted = adapter(memory, catalog)
    raw = deepcopy(memory.raw)
    with pytest.raises(StateError, match='transition_forbidden'):
        journal(restarted).transition(RID, '検証中')
    assert journal(restarted).query(RID)['state'] == UNKNOWN
    assert memory.raw == raw
