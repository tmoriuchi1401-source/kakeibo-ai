"""Synthetic-only runtime. Configuration is mounted from Secret Manager.

No endpoint can prepare arbitrary pages or load a real PDF. Operator bootstrap
is a separate authenticated administrative command, never a public HTTP route.
"""
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from itsdangerous import URLSafeTimedSerializer
from google.cloud import firestore

from app.human_general_auth_transport import (AuthRequestStore, AuthenticatedGeneralConfirmation,
    GoogleCodeExchange, GoogleIdentityVerifier, ISSUER, TTL, empty_auth_state)
from app.human_general_authority import (HumanGeneralConfirmation, HumanGeneralAuthorityStore,
    empty_state, review_identity)
from app.page_receipt_model import PageUnit, SourceRef, stable_page, digest
from app.drive_run_state import StateError
from .backend import FirestoreConditionalState, EncryptedTickets, canonical, timestamp

RAW = b'kakeibo-hga-isolated-synthetic-source-v1'
BINDING = digest(['hga-cloud-run-synthetic-v1'])
SOURCE_ID = 'synthetic-hga-source'


class ExpectedTagAuthorityStore(HumanGeneralAuthorityStore):
    def __init__(self, *args, expected_tag=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.expected_tag = expected_tag

    def load(self):
        value = super().load()
        if self.expected_tag is not None:
            if self.expected_tag != self.tag or self.expected_tag == '*':
                raise StateError('HTTP_412')
            self.expected_tag = None
        return value


def synthetic_page(kind='sensitive_unknown', clearly_sensitive=False):
    source = SourceRef(source_file_id=SOURCE_ID, source_content_hash=sha256(RAW).hexdigest(),
                       page_count=1, source_kind='image')
    page = PageUnit(source=source, page_number=1, stable_page_identity=stable_page(source, 1),
        automatic_classification=kind, automatic_reason='insufficient_evidence',
        observation_complete=True, extraction_status='extracted', observation_render_hash='0'*64,
        review_identity='0'*64, authority_revision=1, clearly_sensitive=clearly_sensitive)
    return page.model_copy(update={'review_identity': review_identity(page)})


class SyntheticRuntime:
    def __init__(self, client, settings, key, *, clock=time.time, identity=None, exchange=None):
        if (settings.get('mode') != 'synthetic_only' or settings.get('owner_issuer') != ISSUER
                or not settings.get('database', '').startswith('hga-synthetic-')
                or settings['origin'] != 'https://' + urlsplit(settings['origin']).netloc):
            raise StateError('synthetic_configuration_invalid')
        self.client, self.settings, self.clock, self.key = client, settings, clock, key
        self.origin = settings['origin']
        self.identity = identity or GoogleIdentityVerifier(settings['client_id'], settings['owner_sub'], clock=clock)
        self.exchange = exchange or GoogleCodeExchange(settings['client_id'], settings['client_secret'],
                                      self.origin+'/oauth/callback', requests.Session())
        self.tickets = EncryptedTickets(client, key, clock=clock)

    def state(self, rid, kind):
        return FirestoreConditionalState(self.client.collection(kind).document(rid), clock=self.clock)

    def current_page(self, rid, source_id, number):
        if source_id != SOURCE_ID or number != 1:
            raise StateError('synthetic_source_only')
        data = self.client.collection('pages').document(rid).get(retry=None, timeout=10)
        if not data.exists or data.get('expires_at').timestamp() <= self.clock():
            raise StateError('synthetic_page_expired')
        page = PageUnit.model_validate(data.get('page'))
        if page.source != synthetic_page().source:
            raise StateError('synthetic_source_only')
        return page

    @staticmethod
    def source(source_id):
        if source_id != SOURCE_ID:
            raise StateError('synthetic_source_only')
        return RAW

    def gateway(self, rid):
        store = AuthRequestStore(self.state(rid, 'requests'), BINDING, preflight=lambda: None)
        return AuthenticatedGeneralConfirmation(store, self.identity,
            lambda source_id, number: self.current_page(rid, source_id, number), self.source,
            redirect_uri=self.origin+'/oauth/callback', exchange_code=self.exchange, clock=self.clock)

    def factory(self, rid, expected_tag=None):
        def build(actor):
            return HumanGeneralConfirmation(
                ExpectedTagAuthorityStore(self.state(rid, 'authorities'), BINDING,
                    preflight=lambda: None, expected_tag=expected_tag),
                lambda source_id, number: self.current_page(rid, source_id, number), actor,
                load_source=self.source, clock=lambda: datetime.fromtimestamp(self.clock(), timezone.utc).isoformat())
        return build

    def begin(self, rid):
        gateway = self.gateway(rid)
        ticket = gateway.begin(rid)
        # Google sends the code in POST body, never callback query/log URL.
        ticket = replace(ticket, authorization_url=ticket.authorization_url+'&response_mode=form_post')
        record = gateway.store.load()['requests'][rid]
        self.tickets.save(ticket, record['expires_at'])
        return ticket

    def seed(self):
        """CLI administrator only. Not reachable from public Flask routes."""
        rid, now = str(uuid4()), int(self.clock())
        expires = timestamp(now+TTL)
        batch = self.client.batch()
        for kind, data in (
            ('requests', {'payload': canonical(empty_auth_state(BINDING))}),
            ('authorities', {'payload': canonical(empty_state(BINDING))}),
            ('pages', {'page': synthetic_page().model_dump()})):
            batch.create(self.client.collection(kind).document(rid), {**data, 'expires_at': expires})
        batch.commit(retry=None, timeout=10)
        self.gateway(rid).prepare(synthetic_page(), request_id=rid)
        proof = URLSafeTimedSerializer(self.key, salt='hga-link-v1').dumps({'request': rid})
        return self.origin+'/start?request='+rid+'&proof='+proof


def configured_runtime():
    # Files are Secret Manager volume mounts. Never fall back to a local JSON.
    oauth = json.loads(Path(os.environ['HGA_CONFIG_PATH']).read_text())
    owner = json.loads(Path(os.environ['HGA_OWNER_PATH']).read_text())
    settings = {**oauth, 'owner_issuer': owner['issuer'], 'owner_sub': owner['sub'],
        'origin': os.environ['HGA_ORIGIN'], 'database': os.environ['HGA_DATABASE'],
        'mode': 'synthetic_only'}
    key = Path(os.environ['HGA_SESSION_KEY_PATH']).read_bytes().strip()
    client = firestore.Client(project=os.environ['GOOGLE_CLOUD_PROJECT'], database=settings['database'])
    if os.environ.get('HGA_REAL_PAGE_CONFIG_PATH'):
        # Operator-only pinned config and separate private HGA state. No browser
        # input can enable this capability or pick a different source/page.
        from .real_runtime import RealPageRuntime
        config=json.loads(Path(os.environ['HGA_REAL_PAGE_CONFIG_PATH']).read_text())
        info=json.loads(Path(os.environ['HGA_DRIVE_CREDENTIAL_PATH']).read_text())
        return RealPageRuntime(client,settings,key,config,info)
    return SyntheticRuntime(client, settings, key)
