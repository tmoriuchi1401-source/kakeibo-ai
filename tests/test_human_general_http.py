"""Offline shared-store/HTTP tests; no Google or production resources."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet
from google.api_core.exceptions import FailedPrecondition
from google.auth import jwt
import pytest

from app.drive_run_state import StateError
from app.human_general_auth_transport import GoogleIdentityVerifier
from services.human_general.backend import FirestoreConditionalState, EncryptedTickets, timestamp
from services.human_general.runtime import SyntheticRuntime
from services.human_general.web import create_app, COOKIE, START_COOKIE
from test_human_general_auth_transport import keys, CLIENT, OWNER, REDIRECT

ORIGIN = 'https://confirmation.example.test'


class Snapshot:
    def __init__(self, data, version):
        self.exists = data is not None
        self.data = deepcopy(data)
        self.update_time = datetime(2026, 1, 1, tzinfo=timezone.utc)+timedelta(microseconds=version)

    def get(self, key):
        return self.data[key]


class Document:
    def __init__(self, db, path):
        self.db, self.path = db, path

    def get(self, *, retry, timeout):
        assert retry is None and timeout == 10
        return Snapshot(self.db.data.get(self.path), self.db.versions.get(self.path, 0))

    def create(self, data, *, retry, timeout):
        assert retry is None and timeout == 10 and self.path not in self.db.data
        self.db.data[self.path] = deepcopy(data)
        self.db.versions[self.path] = 1
        self.db.writes += 1

    def update(self, data, *, option, retry, timeout):
        assert retry is None and timeout == 10
        self.db.attempts += 1
        actual = self.get(retry=None, timeout=10)
        if self.db.race or option._last_update_time != actual.update_time:
            raise FailedPrecondition('synthetic')
        self.db.data[self.path].update(deepcopy(data))
        self.db.versions[self.path] += 1
        self.db.writes += 1


class Database:
    def __init__(self):
        self.data, self.versions = {}, {}
        self.writes = self.attempts = 0
        self.race = False

    def collection(self, name):
        return SimpleNamespace(document=lambda rid: Document(self, name+'/'+rid))

    def batch(self):
        pending = []
        return SimpleNamespace(create=lambda ref, data: pending.append((ref, data)),
            commit=lambda **kw: [ref.create(data, **kw) for ref, data in pending])


class HttpRig:
    def __init__(self, keys):
        self.signer, self.public = keys
        self.now = int(time.time())
        self.db, self.key = Database(), Fernet.generate_key()
        self.claim_changes = {}
        self.exchanges = 0
        self.settings = dict(mode='synthetic_only', database='hga-synthetic-auth', origin=ORIGIN,
                            client_id=CLIENT, client_secret='synthetic-only-secret',
                            owner_issuer='https://accounts.google.com', owner_sub=OWNER)
        identity = GoogleIdentityVerifier(CLIENT, OWNER, request=self.certificates, clock=lambda: self.now)
        self.runtime = SyntheticRuntime(self.db, self.settings, self.key, clock=lambda: self.now,
                                       identity=identity, exchange=self.exchange)
        self.app = create_app(lambda: self.runtime)
        self.app.testing = True
        self.client = self.app.test_client()
        self.link = self.runtime.seed()
        self.start_path = urlsplit(self.link).path+'?'+urlsplit(self.link).query
        self.rid = parse_qs(urlsplit(self.link).query)['request'][0]

    def certificates(self, url, **kwargs):
        return SimpleNamespace(status=200, data=json.dumps({'synthetic-key': self.public}).encode())

    def exchange(self, code, verifier):
        self.exchanges += 1
        ticket = self.runtime.tickets.load(self.client.get_cookie(COOKIE, domain='confirmation.example.test').value)
        assert code == 'synthetic-code' and verifier == ticket.pkce_verifier
        claims = dict(iss='https://accounts.google.com', aud=CLIENT, sub=OWNER, iat=self.now,
                      exp=self.now+300, nonce=ticket.nonce, email='owner@example.test', email_verified=True)
        claims.update(self.claim_changes)
        return jwt.encode(self.signer, claims).decode()

    def get(self, path):
        return self.client.get(path, base_url=ORIGIN)

    def post(self, path, data, origin=ORIGIN):
        return self.client.post(path, data=data, base_url=ORIGIN, headers={'Origin': origin})

    def start(self):
        from html.parser import HTMLParser
        fields = {}
        class Form(HTMLParser):
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'input': fields[attrs['name']] = attrs['value']
        response = self.get(self.start_path)
        assert response.status_code == 200
        Form().feed(response.get_data(as_text=True))
        response = self.post('/start', fields)
        assert response.status_code == 303
        cookie = self.client.get_cookie(COOKIE, domain='confirmation.example.test').value
        self.ticket = self.runtime.tickets.load(cookie)
        return response

    def callback(self, **changes):
        data = dict(code='synthetic-code', state=self.ticket.state)
        data.update(changes)
        return self.post('/oauth/callback', data, origin='https://accounts.google.com')

    def confirm(self, **changes):
        _, tag = self.runtime.state(self.rid, 'authorities').read_versioned()
        data = dict(action='confirm', csrf=self.ticket.csrf, etag=tag)
        data.update(changes)
        return self.post('/confirm', data)

    def grants(self):
        return json.loads(self.db.data['authorities/'+self.rid]['payload'])


@pytest.fixture
def http(keys, monkeypatch):
    import requests
    monkeypatch.setattr(requests.sessions.Session, 'request', lambda *_a, **_k: pytest.fail('live network'))
    return HttpRig(keys)


def test_http_signed_owner_explicit_post_exact_readback_and_replay(http):
    before = http.db.writes
    assert http.get(http.start_path).status_code == 200
    assert http.db.writes == before  # no durable GET mutation
    response = http.start()
    # A form POST redirects to Google. Browsers enforce form-action on that
    # redirect too; allow precisely the pinned Google authentication origin.
    assert "form-action 'self' https://accounts.google.com;" in response.headers['Content-Security-Policy']
    params = parse_qs(urlsplit(response.location).query)
    assert params['response_mode'] == ['form_post'] and params['scope'] == ['openid email']
    assert 'SameSite=None' in response.headers.getlist('Set-Cookie')[0]
    assert 'Secure' in response.headers.getlist('Set-Cookie')[0]
    assert 'HttpOnly' in response.headers.getlist('Set-Cookie')[0]
    assert http.callback().status_code == 303
    assert not http.grants()['grants']  # OAuth success alone grants nothing
    before = http.db.writes
    confirm_screen=http.get('/confirm')
    assert confirm_screen.status_code == 200
    assert 'このページだけをGeminiへ送信' in confirm_screen.get_data(as_text=True)
    assert http.db.writes == before
    assert http.confirm().status_code == 303
    grant_state = http.grants()
    assert len(grant_state['grants']) == len(grant_state['audit']) == 1
    assert http.get('/result').status_code == 200
    before = http.db.writes
    assert http.confirm().status_code == 409
    assert http.callback().status_code == 409
    assert http.get(http.start_path).status_code == 409
    assert http.db.writes == before and http.grants() == grant_state


def test_form_referrer_policy_keeps_origin_without_url_path_or_query(http):
    # Browser-level comparison is recorded separately. The header contract must
    # never regress to no-referrer (Origin:null) or expose a path/query via unsafe-url.
    assert http.get(http.start_path).headers['Referrer-Policy'] == 'strict-origin'
    assert http.start().headers['Referrer-Policy'] == 'strict-origin'
    assert http.callback().status_code == 303
    assert http.get('/confirm').headers['Referrer-Policy'] == 'strict-origin'
    assert http.confirm().status_code == 303
    assert http.get('/result').headers['Referrer-Policy'] == 'strict-origin'


@pytest.mark.parametrize('origin', [None, 'null', '*', 'https://arbitrary.run.app',
    ORIGIN+'.evil.example', ORIGIN+'/', 'https://sub.confirmation.example.test'])
@pytest.mark.parametrize('path', ['/start', '/confirm'])
def test_same_origin_posts_reject_missing_null_and_lookalike_origins(http, origin, path):
    from html.parser import HTMLParser
    if path == '/start':
        fields = {}
        class Form(HTMLParser):
            def handle_starttag(self, tag, attrs):
                value = dict(attrs)
                if tag == 'input':
                    fields[value['name']] = value['value']
        Form().feed(http.get(http.start_path).get_data(as_text=True))
    else:
        http.start()
        assert http.callback().status_code == 303
        _, tag = http.runtime.state(http.rid, 'authorities').read_versioned()
        fields = dict(action='confirm', csrf=http.ticket.csrf, etag=tag)
    before, exchanges = http.db.writes, http.exchanges
    state_before = deepcopy(http.db.data)
    # A legitimate Referer never substitutes for the required Origin header.
    headers = {'Referer': ORIGIN+'/start'}
    if origin is not None:
        headers['Origin'] = origin
    response = http.client.post(path, data=fields, base_url=ORIGIN, headers=headers)
    assert response.status_code == 400
    assert http.db.writes == before and http.exchanges == exchanges
    assert http.db.data == state_before and not http.grants()['grants']


def test_unsigned_or_tampered_link_refused_before_state_change(http):
    before = http.db.writes
    assert http.get('/start?request='+http.rid).status_code == 400
    assert http.get(http.start_path[:-1]+'!').status_code == 400
    assert http.db.writes == before


def test_new_runtime_instance_recovers_encrypted_ticket(http):
    http.start()
    second = EncryptedTickets(http.db, http.key, clock=lambda: http.now)
    assert second.load(http.ticket.cookie) == http.ticket
    stored = http.db.data['sessions/'+__import__('hashlib').sha256(http.ticket.cookie.encode()).hexdigest()]
    assert set(stored) == {'ciphertext', 'expires_at'}
    for secret in (http.ticket.cookie, http.ticket.state, http.ticket.pkce_verifier, http.ticket.nonce):
        assert secret.encode() not in stored['ciphertext']
    http.now += 600
    with pytest.raises(StateError, match='expired'): second.load(http.ticket.cookie)


@pytest.mark.parametrize('failure', ['state', 'nonce', 'actor', 'csrf', 'origin', 'expired', 'page', 'revision', 'medical', 'payroll', 'pii'])
def test_rejection_never_issues_authority(http, failure):
    http.start()
    if failure == 'state':
        assert http.callback(state='wrong').status_code == 400
    elif failure in {'nonce', 'actor'}:
        http.claim_changes = {'nonce': 'wrong'} if failure == 'nonce' else {'sub': '999999'}
        assert http.callback().status_code == 403
    elif failure == 'expired':
        http.now += 600
        assert http.callback().status_code == 410
    else:
        assert http.callback().status_code == 303
        if failure == 'csrf': assert http.confirm(csrf='wrong').status_code == 400
        elif failure == 'origin':
            assert http.post('/confirm', dict(action='confirm', csrf=http.ticket.csrf), origin='https://other.test').status_code == 400
        else:
            data = http.db.data['pages/'+http.rid]['page']
            if failure in {'medical', 'payroll'}: data['automatic_classification'] = failure
            elif failure == 'pii': data['clearly_sensitive'] = True
            elif failure == 'revision': data['authority_revision'] += 1
            elif failure == 'page': data['stable_page_identity'] = 'a'*64
            assert http.confirm().status_code in {400, 409, 503}
    assert not http.grants()['grants'] and not http.grants()['audit']


def test_firestone_conditional_no_unconditional_fallback_or_retry(http):
    state = http.runtime.state(http.rid, 'authorities')
    before, tag = state.read_versioned()
    state.replace_versioned(before, tag, before+b' ')
    attempts = http.db.attempts
    with pytest.raises(StateError, match='HTTP_412'): state.replace_versioned(before, tag, before)
    assert http.db.attempts == attempts
    before, tag = state.read_versioned()
    http.db.race = True
    with pytest.raises(StateError, match='HTTP_412'): state.replace_versioned(before, tag, before)
    assert http.db.attempts == attempts+1
    with pytest.raises(StateError, match='HTTP_412'): state.replace_versioned(before, '*', before)


def test_stale_cas_is_http_412_with_no_authority(http):
    http.start()
    assert http.callback().status_code == 303
    http.db.race = True
    assert http.confirm().status_code == 412
    assert not http.grants()['grants']


def test_http_security_get_callback_rejected_no_writes_no_raw_errors(http):
    before = http.db.writes
    assert http.get('/oauth/callback?code=secret').status_code == 405
    assert http.db.writes == before
    response = http.get('/health')
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    assert 'frame-ancestors' in response.headers['Content-Security-Policy']
    assert 'secret' not in http.get('/oauth/callback?code=secret').get_data(as_text=True)


def test_stale_form_etag_is_actual_http_412_no_authority(http):
    http.start()
    assert http.callback().status_code == 303
    state = http.runtime.state(http.rid, 'authorities')
    before, tag = state.read_versioned()
    state.replace_versioned(before, tag, before+b' ')
    assert http.confirm(etag=tag).status_code == 412
    assert not http.grants()['grants']


def test_no_real_source_or_production_mode(http):
    with pytest.raises(StateError): http.runtime.source('1fcmMMGj86DLq54G0inD8LI_XSyQSPfTY')
    bad = dict(http.settings, mode='production')
    with pytest.raises(StateError): SyntheticRuntime(http.db, bad, http.key)
    assert not http.grants()['grants']
