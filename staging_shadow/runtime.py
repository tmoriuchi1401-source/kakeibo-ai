"""Memory-only composition around unchanged native Google/session validators.

No web login/bootstrap bypass: the server cannot initiate or exchange OAuth.
Only private in-process synthetic smoke can exercise signed fixture identity.
"""
from copy import deepcopy
from hashlib import sha256
import ipaddress
import os
import socket
import time
from uuid import uuid4

from cryptography.fernet import Fernet
from flask import request, jsonify

from app.drive_run_state import StateError
from app.human_general_auth_transport import (AuthRequestStore, AuthenticatedGeneralConfirmation,
    GoogleIdentityVerifier, ISSUER, empty_auth_state)
from app.human_general_authority import review_identity
from app.page_receipt_model import SourceRef, PageUnit, stable_page, digest
from app.unified_receipt_purposes import PurposeBoundary
from app.unified_receipt_shadow import make_isolated_shadow_application, ShadowConfig, PREFIX
from services.human_general import web, runtime as host_runtime
from services.human_general.backend import FirestoreConditionalState, EncryptedTickets, canonical, timestamp
from services.human_general.shared_login import SharedLogins, Lifetime
from .memory_cas import Database
from .config import Config

RAW = b'kakeibo-isolated-staging-synthetic-v1'
SOURCE_ID = 'synthetic-staging-source'
CLIENT = 'synthetic.apps.googleusercontent.com'
OWNER = '101'
BINDING = digest(['staging-memory-auth-v1'])


def deny_external(*_args, **_kwargs): raise StateError('staging_external_transport_forbidden')


def install_network_guard():
    """Supplement --network none. Only literal loopback TCP/UDP may be used."""
    if getattr(socket, '_staging_guard', False): return
    connect, connect_ex, sendto, lookup = socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto, socket.getaddrinfo
    def local(sock, address):
        if sock.family == socket.AF_UNIX: return
        try: valid = ipaddress.ip_address(address[0]).is_loopback
        except (ValueError, TypeError, IndexError): valid = False
        if not valid: raise OSError('staging_egress_denied')
    def guarded_connect(sock, address): local(sock, address); return connect(sock, address)
    def guarded_ex(sock, address): local(sock, address); return connect_ex(sock, address)
    def guarded_sendto(sock, data, *args): local(sock, args[-1]); return sendto(sock, data, *args)
    def guarded_lookup(host, *args, **kwargs):
        try: valid = ipaddress.ip_address(host).is_loopback
        except (ValueError, TypeError): valid = False
        if not valid: raise OSError('staging_dns_denied')
        return lookup(host, *args, **kwargs)
    socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto = guarded_connect, guarded_ex, guarded_sendto
    socket.getaddrinfo = guarded_lookup
    socket._staging_guard = True


class MemoryRuntime:
    mode = 'isolated_synthetic_memory'
    def __init__(self, config, *, identity=None, clock=time.time):
        self.origin, self.clock = config.origin, clock
        self.client, self.key = Database(), Fernet.generate_key()
        self.settings = dict(mode='synthetic_only', session_canary_enabled=True)
        self.identity = identity or GoogleIdentityVerifier(CLIENT, OWNER, request=deny_external, clock=clock)
        self.tickets = EncryptedTickets(self.client, self.key, clock=clock)
        self.logins = SharedLogins(self.client, self.key, self.identity, lifetime=Lifetime(600, 600), clock=clock)
        self.browser_login_cookie = None
    def state(self, rid, kind): return FirestoreConditionalState(self.client.collection(kind).document(rid), clock=self.clock)
    def source(self, source_id):
        if source_id != SOURCE_ID: raise StateError('synthetic_source_only')
        return RAW
    def current_page(self, rid, source_id, number):
        if source_id != SOURCE_ID or number != 1: raise StateError('synthetic_source_only')
        data = self.client.collection('pages').document(rid).get(retry=None, timeout=10)
        if not data.exists or data.get('expires_at').timestamp() <= self.clock(): raise StateError('synthetic_page_expired')
        return PageUnit.model_validate(data.get('page'))
    def gateway(self, rid):
        store = AuthRequestStore(self.state(rid, 'requests'), BINDING, preflight=lambda: None)
        g = AuthenticatedGeneralConfirmation(store, self.identity,
            lambda sid, number: self.current_page(rid, sid, number), self.source,
            redirect_uri=self.origin+'/oauth/callback', exchange_code=deny_external, clock=self.clock)
        g.verify_login = lambda record: self.logins.verify_bound(record, self.browser_login_cookie)
        return g
    def begin(self, _rid): raise StateError('staging_interactive_oauth_forbidden')
    def seed(self):
        """Private memory fixture only. No HTTP seeding route, authority or log."""
        rid = str(uuid4()); expires = timestamp(int(self.clock())+600)
        source = SourceRef(source_file_id=SOURCE_ID, source_content_hash=sha256(RAW).hexdigest(), page_count=1, source_kind='image')
        page = PageUnit(source=source, page_number=1, stable_page_identity=stable_page(source, 1),
            automatic_classification='sensitive_unknown', automatic_reason='insufficient_evidence',
            observation_complete=True, extraction_status='extracted', observation_render_hash='0'*64,
            review_identity='0'*64, authority_revision=1, clearly_sensitive=False)
        page = page.model_copy(update={'review_identity': review_identity(page)})
        self.client.collection('requests').document(rid).create({'payload':canonical(empty_auth_state(BINDING)), 'expires_at':expires}, retry=None, timeout=10)
        self.client.collection('pages').document(rid).create({'page':page.model_dump(), 'expires_at':expires}, retry=None, timeout=10)
        self.gateway(rid).prepare(page, request_id=rid)
        return rid


def cases():
    rows = []
    for index, count in enumerate((1, 2, 0), 1):
        rows.append(dict(case_id='synthetic-staging-'+str(index), state='held',
            disposition='LEGACY_UNMAPPED_HOLD' if count == 0 else '',
            display=dict(source_name='合成原本 '+str(index), page_range='p1', unit_count=count,
                state_label='読取り専用・処理禁止', next_action='隔離stagingの表示を確認',
                units=[dict(unit_id='synthetic-unit-'+str(index)+'-'+str(n), receipt_index=n,
                    page_range='p1', state_label='合成Unit・操作無効', next_action='記帳しません') for n in range(1, count+1)])))
    return rows


def create_application(config, *, identity=None, clock=time.time):
    rt = MemoryRuntime(config, identity=identity, clock=clock)
    boundary = PurposeBoundary(digest([ISSUER, OWNER]), deny_external, deny_external, cases, clock=clock)
    # Explicit isolated composition only. The native production source is untouched.
    host_runtime.configured_runtime = lambda: rt
    app = make_isolated_shadow_application(web.make_application, lambda: rt, boundary,
        config=ShadowConfig(True, 'isolated_synthetic'), cookies={'login':web.LOGIN_COOKIE, 'ticket':web.COOKIE})
    @app.before_request
    def staging_route_boundary():
        path = request.path
        allowed = path == '/health' or path == '/logout' or path == PREFIX or path.startswith(PREFIX+'/cases/')
        if path.startswith(PREFIX+'/') and path.endswith('/prepare'): allowed = True
        if not allowed: return jsonify(error='staging_route_disabled'), 403
    return app, rt


def make_application():
    config = Config.read(os.environ)
    install_network_guard()
    return create_application(config)[0]
