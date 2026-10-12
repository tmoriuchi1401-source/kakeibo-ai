"""Actual pinned Flask factory and Google RSA login; external memory IO only."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
import importlib
import json
from pathlib import Path
import sys
import time
from types import ModuleType, SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from google.auth import crypt, jwt
from google.api_core.exceptions import FailedPrecondition

import unified_purpose_fixtures as fixtures
from app.unified_receipt_shadow import ShadowConfig, make_isolated_shadow_application, PREFIX
from app.unified_receipt_purposes import ACCOUNTING_POST

ORIGIN = 'https://confirmation.example.test'
DOMAIN = 'confirmation.example.test'


class Snapshot:
    def __init__(self, data, version):
        self.exists, self.data = data is not None, deepcopy(data)
        self.update_time = datetime(2026, 1, 1, tzinfo=timezone.utc)+timedelta(microseconds=version)
    def get(self, key): return self.data[key]


class Document:
    def __init__(self, db, path): self.db, self.path = db, path
    def get(self, *, retry, timeout):
        assert retry is None and timeout == 10
        return Snapshot(self.db.data.get(self.path), self.db.versions.get(self.path, 0))
    def create(self, data, *, retry, timeout):
        assert retry is None and timeout == 10 and self.path not in self.db.data
        self.db.data[self.path] = deepcopy(data); self.db.versions[self.path] = 1; self.db.writes += 1
    def update(self, data, *, option, retry, timeout):
        assert retry is None and timeout == 10
        actual = self.get(retry=None, timeout=10)
        if self.db.race or option._last_update_time != actual.update_time: raise FailedPrecondition('isolated CAS')
        self.db.data[self.path].update(deepcopy(data)); self.db.versions[self.path] += 1; self.db.writes += 1


class Database:
    # Same conditional transport shape as existing test_human_general_http.py.
    shadow_memory_only = True
    def __init__(self):
        self.data, self.versions = {}, {}; self.writes = 0; self.race = False
    def collection(self, name): return SimpleNamespace(document=lambda rid: Document(self, name+'/'+rid))
    def batch(self):
        pending = []
        return SimpleNamespace(create=lambda ref, data: pending.append((ref, data)),
            commit=lambda **kw: [ref.create(data, **kw) for ref, data in pending])


def host_modules(monkeypatch):
    n = fixtures.native_modules()
    path = Path(sys.modules['app._unified_pinned'].__path__[0]).parent
    # Import UNMODIFIED host sources with their exact pinned app dependencies.
    # Temporary import aliases only in tests; no source rewrite or auth bypass.
    package = ModuleType('_unified_shadow_host')
    package.__path__ = [str(path/'services/human_general')]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    for name in list(sys.modules):
        if name.startswith('_unified_shadow_host.'):
            monkeypatch.delitem(sys.modules, name)
    with monkeypatch.context() as aliases:
        for name in ('human_general_auth_transport', 'human_general_authority', 'page_receipt_model', 'drive_run_state'):
            aliases.setitem(sys.modules, 'app.'+name, importlib.import_module('app._unified_pinned.'+name))
        runtime = importlib.import_module('_unified_shadow_host.runtime')
        web = importlib.import_module('_unified_shadow_host.web')
    return n, runtime, web


def inputs(response):
    fields = {}
    class Parser(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag == 'input':
                value = dict(attrs); fields[value['name']] = value.get('value', '')
    Parser().feed(response.get_data(as_text=True))
    return fields


class HostRig:
    def __init__(self, monkeypatch, action='confirm_grouping', decision='同一レシート', *, enabled=True, accounting=False):
        self.n, self.runtime_module, self.web = host_modules(monkeypatch)
        self.now = int(time.time()); self.db = Database(); self.key = Fernet.generate_key()
        monkeypatch.setattr(fixtures, 'NOW', self.now)
        self.f = fixtures.existing_review_fixture(action, decision)
        self.claim_changes = {}; self.exchange_count = 0; self.factory_calls = 0
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.signer = crypt.RSASigner.from_string(private.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), key_id='synthetic-key')
        self.public = private.public_key().public_bytes(serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        identity = self.n.human_general_auth_transport.GoogleIdentityVerifier(
            'synthetic.apps.googleusercontent.com', '101', request=self.certificates, clock=lambda: self.now)
        self.rt = self.runtime_module.SyntheticRuntime(self.db, dict(mode='synthetic_only', database='hga-synthetic-auth',
            origin=ORIGIN, client_id='synthetic.apps.googleusercontent.com', client_secret='synthetic-only-secret',
            owner_issuer='https://accounts.google.com', owner_sub='101'), self.key, identity=identity,
            exchange=self.exchange, clock=lambda: self.now)
        self.rt.browser_login_cookie = None
        self.snapshot = self.cards()
        self.f.boundary.snapshot_reader = self.read_snapshot
        self.f.boundary.clock = lambda: self.now
        self.financial = None
        if accounting:
            from test_unified_receipt_purposes import accounting_fixture
            self.boundary, self.case, self.bridge, self.financial_port, self.financial_request, self.financial_db = accounting_fixture()
            self.boundary.snapshot_reader = lambda: deepcopy(self.snapshot)
            self.financial = self.financial_request
        else:
            self.boundary = self.f.boundary
        monkeypatch.setattr(self.runtime_module, 'configured_runtime', self.configured)
        self.app = make_isolated_shadow_application(self.web.make_application, self.configured, self.boundary,
            config=ShadowConfig(enabled, 'isolated_synthetic'),
            cookies={'login': self.web.LOGIN_COOKIE, 'ticket': self.web.COOKIE})
        self.app.testing = True; self.client = self.app.test_client()
        self.link = self.rt.seed()
        self.start_path = urlsplit(self.link).path+'?'+urlsplit(self.link).query
        self.login_request_id = parse_qs(urlsplit(self.link).query)['request'][0]
        # Native privacy/reconciliation receive the existing session actor,
        # not a Sheet actor or a separate login mechanism.
        original = self.f.port.reader
        def observed(*args):
            o = original(*args)
            from flask import request
            cookie = request.cookies.get(self.web.LOGIN_COOKIE)
            if action == 'link_existing':
                record = dict(request_id=args[-1], digest=o.native_binding_digest,
                              created_at=self.now, expires_at=self.now+600)
                self.f.actor = self.rt.logins.actor(cookie, record)
            elif action == 'confirm_privacy':
                def actor(_rid):
                    login = self.rt.logins.current(cookie)
                    return {'provider': 'google', 'subject': login['email']}
                self.f.privacy.verified_actor = actor
            return o
        self.f.port.reader = observed

    def configured(self): self.factory_calls += 1; return self.rt
    def read_snapshot(self):
        rows = deepcopy(self.snapshot)
        state = self.f.store.read()[0]
        labels = {'accepted':'要求受付済み', 'executing':'実行中', 'awaiting_readback':'read-back待ち',
                  'complete':'確認操作完了', 'partial_unknown':'partial／unknown停止'}
        for row in rows:
            matches = [o for o in state['operations'].values() if o['request']['case_id'] == row['case_id']]
            if len(matches) == 1:
                row['state'] = matches[0]['state']
                row['display']['state_label'] = labels.get(row['state'], row['state'])
        return rows
    def certificates(self, url, **kwargs):
        assert url == 'https://www.googleapis.com/oauth2/v1/certs'
        return SimpleNamespace(status=200, data=json.dumps({'synthetic-key':self.public}).encode())
    def exchange(self, code, verifier):
        self.exchange_count += 1
        ticket = self.rt.tickets.load(self.client.get_cookie(self.web.COOKIE, domain=DOMAIN).value)
        assert code == 'synthetic-code' and verifier == ticket.pkce_verifier
        claims = dict(iss='https://accounts.google.com', aud=self.rt.identity.client_id, sub='101',
            iat=self.now, exp=self.now+300, nonce=ticket.nonce, email='owner@example.test', email_verified=True)
        claims.update(self.claim_changes)
        return jwt.encode(self.signer, claims).decode()
    def get(self, path): return self.client.get(path, base_url=ORIGIN)
    def post_form(self, path, data, origin=ORIGIN):
        return self.client.post(path, data=data, base_url=ORIGIN, headers={'Origin':origin})
    def login(self):
        fields = inputs(self.get(self.start_path))
        started = self.post_form('/start', fields)
        assert started.status_code == 303
        self.ticket = self.rt.tickets.load(self.client.get_cookie(self.web.COOKIE, domain=DOMAIN).value)
        return self.post_form('/oauth/callback', {'code':'synthetic-code', 'state':self.ticket.state}, 'https://accounts.google.com')
    def post(self, path, payload, *, csrf=True, origin=ORIGIN, client=None):
        headers = {'Origin':origin}
        if csrf: headers['X-CSRF-Token'] = self.ticket.csrf if csrf is True else csrf
        return (client or self.client).post(path, json=payload, base_url=ORIGIN, headers=headers)
    def prepare(self, **changes):
        v = dict(case_id=self.f.case_id, requested_action=self.f.action, request_id=self.f.rid, expected_revision=0)
        v.update(changes)
        return self.post(PREFIX+'/'+self.f.purpose+'/prepare', v)
    def submit(self, payload, **kwargs):
        return self.post(PREFIX+'/'+self.f.purpose+'/submit', payload, **kwargs)
    def cards(self):
        # No placeholders: metadata cards have zero finalized accounting Units.
        return [dict(case_id=self.f.case_id, purpose=self.f.purpose, state='ready', display=dict(
            source_name='分割確認の原本名が長い場合でも案件を見失わないための確認用PDF.pdf', page_range='p1–p2',
            unit_count=0, state_label='構造確認が必要', next_action='ページの分割・結合を確認してください。'*5,
            unsent_inputs=[dict(label='本人の未送信メモ', value='入力途中：消さない <script>unsafe()</script>')], units=[])),
            dict(case_id='isolated-legacy-hold', purpose='RECONCILIATION_REVIEW', state='held',
                disposition='LEGACY_UNMAPPED_HOLD', display=dict(source_name='旧案件履歴', page_range='p1', unit_count=0,
                next_action='対応を証明できないため保留。操作不可。', units=[])),
            dict(case_id='isolated-completed', purpose='METADATA_REVIEW', state='complete', display=dict(
                source_name='確認済み原本', page_range='p3', unit_count=0, state_label='完了', next_action='確認操作完了', units=[]))]


def add_native_unit_cards(h):
    """Display actual locally generated Units, never invented IDs/authorities."""
    from test_unified_receipt_write import pdf_native
    for count in (1, 2):
        case, bridge, port, request, db, runner = pdf_native(count=count)
        assert db.writes == []
        unit = runner.store.load()['pages'][port.key]['units'][port.uid]
        manifest = unit['candidate']['manifest']
        h.snapshot.append(dict(case_id='isolated-native-units-'+str(count), purpose=ACCOUNTING_POST,
            state='ready', display=dict(source_name='synthetic確定Unit確認用原本-'+str(count)+'.pdf',
                page_range='p1', unit_count=count, state_label='本人確認が必要',
                next_action='対象レシートごとに明細を確認。shadowでは記帳不可。',
                units=[dict(unit_id=u['receipt_unit_id'], receipt_index=u['receipt_index'],
                    page_range='p1', state_label='本人確認待ち', next_action='原本と明細を比較') for u in manifest['units']])))
