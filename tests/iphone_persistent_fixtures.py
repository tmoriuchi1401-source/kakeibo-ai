"""Synthetic atomic document server. No Google client, credentials or network."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from threading import Lock

from google.api_core.exceptions import FailedPrecondition


class Snapshot:
    exists = True
    def __init__(self, payload, version):
        self.payload = payload
        self.update_time = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=version)
    def get(self, name):
        return self.payload if name == 'payload' else datetime(2100, 1, 1, tzinfo=timezone.utc)


class AtomicDocument:
    """Models update_time precondition, atomic payload, lost response and faults.
    Server object outlives repository/IO instances; intentionally not evidence
    of Firestore durability, IAM, TTL, billing or disaster recovery.
    """
    def __init__(self, payload=b''):
        self.payload, self.version, self.writes, self.reads = payload, 1, 0, 0
        self.lock, self.fault = Lock(), None

    def get(self, *, retry, timeout):
        assert retry is None and timeout == 10
        with self.lock:
            self.reads += 1
            if self.fault == 'read':
                raise OSError('synthetic disconnect')
            return Snapshot(self.payload, self.version)

    def update(self, values, *, option, retry, timeout):
        assert retry is None and timeout == 10 and set(values) == {'payload'}
        with self.lock:
            if option._last_update_time != Snapshot(self.payload, self.version).update_time:
                raise FailedPrecondition('synthetic conflict')
            if self.fault == 'before':
                raise OSError('synthetic unknown before commit')
            self.payload = values['payload']; self.version += 1; self.writes += 1
            if self.fault == 'partial':
                self.payload = self.payload[:30]
            if self.fault == 'after':
                raise OSError('synthetic lost response after commit')


def repository(document, key, batch, *, owner=None, generation=1):
    # Actual, hash-pinned existing backend, not an accept-all CAS replacement.
    import unified_purpose_fixtures as fixtures
    import importlib
    import sys
    from pathlib import Path
    from types import ModuleType
    from app.iphone_persistent_drafts import PersistentJournal
    fixtures.native_modules()
    path = Path(sys.modules['app._unified_pinned'].__path__[0]).parent
    if '_draft_existing_backend' not in sys.modules:
        package = ModuleType('_draft_existing_backend')
        package.__path__ = [str(path/'services/human_general')]
        sys.modules[package.__name__] = package
    if '_draft_existing_backend.backend' in sys.modules:
        backend = sys.modules['_draft_existing_backend.backend']
    else:
        alias = 'app.human_general_auth_transport'
        prior = sys.modules.get(alias)
        sys.modules[alias] = importlib.import_module('app._unified_pinned.human_general_auth_transport')
        try:
            backend = importlib.import_module('_draft_existing_backend.backend')
        finally:
            if prior is None: sys.modules.pop(alias, None)
            else: sys.modules[alias] = prior
    return PersistentJournal(backend.FirestoreConditionalState(document, clock=lambda:1792278000), key,
        owner=owner or fixtures.OWNER, contract_digest=batch.contract_digest,
        generation=generation, initial_snapshot=batch.snapshot_digest)


def provision(tmp_path):
    from cryptography.fernet import Fernet
    from app.iphone_draft_journal import LocalJournal
    from iphone_readonly_fixtures import saved_bundle
    import uuid
    bundle, batch = saved_bundle(coobserved=True)
    local = LocalJournal(tmp_path/'legacy.json', batch.contract_digest)
    _, tag = local.initialize()
    local.save(batch, tag, str(uuid.uuid4()))
    before, tag = local.read_versioned()
    key = Fernet.generate_key(); document = AtomicDocument()
    repo = repository(document, key, batch)
    details = {cid:{k:deepcopy(m[k]) for k in ('source_observation', 'item_draft') if k in m}
               for cid,m in bundle['mappings'].items()}
    document.payload = repo.migration_payload(before, tag, details=details)  # explicit fake provisioning
    return repo, document, key, bundle, batch, local
