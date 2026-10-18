"""Synthetic accounting boundary probes using the EXISTING ConditionalRequests.

No native execute, writer, authority factory, HTTP registration or live client.
An accepted probe never becomes a financial/native permission.
"""
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict, dataclass
from uuid import UUID

from .drive_run_state import StateError
from .iphone_accounting_ownership import hashed, parse_ownership, relation, unknown
from .unified_receipt_purposes import ACCOUNTING_POST, PurposeBoundary
from .unified_receipt_review import digest
from .unified_receipt_write import ConditionalRequests, OperationRequest

SCHEMA = 'synthetic-accounting-probe-v1'
ENTRIES = frozenset({'normal_auto', 'buyback_income', 'legacy_confirmation', 'pdf_auto', 'pdf_manual',
                     'grouped_unit', 'fixed_canary', 'explicit_unit', 'future_one_tab'})


@dataclass(frozen=True)
class AccountingProbeRequest:
    schema: str
    synthetic_only: bool
    owner: str
    request_id: str
    expected_state_revision: int
    operation_identity: str
    ownership: dict
    native_preflight_digest: str

    @property
    def purpose(self):
        return ACCOUNTING_POST

    @classmethod
    def parse(cls, value):
        try:
            r = cls(**deepcopy(value)); o = parse_ownership(r.ownership)
            if (r.schema != SCHEMA or r.synthetic_only is not True or not hashed(r.owner)
                    or str(UUID(r.request_id)) != r.request_id
                    or type(r.expected_state_revision) is not int or r.expected_state_revision < 0
                    or not hashed(r.native_preflight_digest) or r.operation_identity != o.operation_identity):
                raise ValueError()
            return r
        except (KeyError, TypeError, ValueError, AttributeError):
            raise StateError('synthetic_accounting_request_invalid') from None

    def fingerprint(self):
        # Entry is absent: an exact request may be re-inspected via another
        # ingress. Expected revision IS part of this stricter request contract.
        return digest({k: v for k, v in asdict(self).items() if k != 'request_id'})


@dataclass(frozen=True)
class NativePreflight:
    """In-process result from a native validator/authority/CAS read adapter."""
    ownership_digest: str
    validator_digest: str
    native_state_digest: str
    native_store_revision: str


@dataclass(frozen=True)
class _Lease:
    request: AccountingProbeRequest
    observation: object
    preflight: NativePreflight
    entry: str
    key: str
    nonce: object


class _GuardedProbe:
    def __init__(self, gate, observer):
        self._gate, self._observer = gate, observer

    def __call__(self, lease=None):
        if not isinstance(lease, _Lease) or self._gate._lease.get() is not lease:
            raise StateError('synthetic_shared_claim_required')
        self._gate._lease.set(None)  # Consume before observer, including re-entry.
        self._observer(lease.entry, lease.key, lease.request.operation_identity)


class SyntheticAccountingGate:
    def __init__(self, requests, reader, auth, preflight, probe, *, enabled=False,
                 isolated_synthetic=False, clock):
        if (enabled is not True or isolated_synthetic is not True
                or type(requests) is not ConditionalRequests or not requests.synthetic_accounting
                or getattr(requests.io, 'synthetic_memory_only', False) is not True
                or getattr(reader, 'synthetic_memory_only', False) is not True
                or not isinstance(auth, PurposeBoundary)):
            raise StateError('synthetic_accounting_gate_disabled')
        self.requests, self.reader, self.auth = requests, reader, auth
        self.preflight, self.clock = preflight, clock
        self._lease = ContextVar('synthetic_accounting_probe_lease', default=None)
        self._probe = _GuardedProbe(self, probe)

    def _native(self, entry, key, observation):
        checked = self.preflight(entry, key, observation)
        if (not isinstance(checked, NativePreflight)
                or checked.ownership_digest != digest(observation.value())
                or not all(hashed(x) for x in (checked.validator_digest, checked.native_state_digest))
                or not isinstance(checked.native_store_revision, str) or not checked.native_store_revision):
            raise StateError('synthetic_native_preflight_required')
        return checked

    def submit(self, entry, key, request_id, expected_revision, context):
        if entry not in ENTRIES:
            raise StateError('synthetic_accounting_entry_invalid')
        # Authentication precedes source/native reads. The existing host verifier
        # remains responsible for issuer/sub, owner, ticket, Origin, CSRF and TTL.
        self.auth._post(context, ACCOUNTING_POST, request_id, None, expected_revision)
        observation = self.reader.observe(entry, key)
        if observation.owner != self.auth.owner:
            raise StateError('synthetic_accounting_owner_mismatch')
        self.auth._post(context, ACCOUNTING_POST, request_id, observation.input_snapshot, expected_revision)
        checked = self._native(entry, key, observation)
        request = AccountingProbeRequest.parse(dict(schema=SCHEMA, synthetic_only=True, owner=self.auth.owner,
            request_id=request_id, expected_state_revision=expected_revision,
            operation_identity=observation.operation_identity, ownership=observation.value(),
            native_preflight_digest=digest(asdict(checked))))
        before, raw, tag = self.requests.read()
        for key_id, old in before['operations'].items():
            previous = self.requests.parse_request(old['request'])
            if previous.request_id == request_id:
                if key_id != request.operation_identity or old['fingerprint'] != request.fingerprint():
                    raise StateError('synthetic_accounting_request_id_conflict')
                return self._result(old['state'], replay=True)
        if before['revision'] != expected_revision:
            raise StateError('synthetic_accounting_stale_revision')
        for old in before['operations'].values():
            previous = self.requests.parse_request(old['request'])
            if isinstance(previous, AccountingProbeRequest):
                if previous.owner != request.owner:
                    unknown()
                if relation(previous.ownership, request.ownership) != 'independent':
                    raise StateError('synthetic_accounting_scope_already_owned')
            elif isinstance(previous, OperationRequest):
                unknown()  # Unscoped old financial history requires explicit recovery/migration.
            # Metadata/reconciliation records do not become financial authority;
            # their request IDs are still globally unique above.
        after = deepcopy(before); after['revision'] += 1
        after['operations'][request.operation_identity] = dict(request=asdict(request),
            fingerprint=request.fingerprint(), state='accepted', events=[dict(state='accepted', at=self.clock())])
        self.requests.replace(before, raw, tag, after)  # Existing strong CAS + exact read-back, no retry.
        self.requests.transition(request, 'executing', self.clock())
        lease = _Lease(request, observation, checked, entry, key, object())
        token = self._lease.set(lease)
        try:
            self.auth._post(context, ACCOUNTING_POST, request_id, observation.input_snapshot, expected_revision)
            self._boundary(lease)
            self.requests.transition(request, 'awaiting_readback', self.clock())
            self.requests.transition(request, 'complete', self.clock())
            return self._result('complete')
        except Exception:
            latest, _, _ = self.requests.read()
            state = latest['operations'][request.operation_identity]['state']
            if state in {'executing', 'awaiting_readback'}:
                self.requests.transition(request, 'partial_unknown', self.clock())
            raise StateError('synthetic_accounting_partial_unknown_readback_only') from None
        finally:
            self._lease.reset(token)

    def _boundary(self, lease):
        if not isinstance(lease, _Lease) or self._lease.get() is not lease:
            raise StateError('synthetic_shared_claim_required')
        current = self.reader.observe(lease.entry, lease.key)
        if current != lease.observation or self._native(lease.entry, lease.key, current) != lease.preflight:
            raise StateError('synthetic_accounting_changed_before_boundary')
        current_store, _, _ = self.requests.read()
        op = current_store['operations'].get(lease.request.operation_identity)
        if not op or op['state'] != 'executing' or op['fingerprint'] != lease.request.fingerprint():
            raise StateError('synthetic_shared_claim_required')
        self._probe(lease)

    def inspect_result(self, request_id, context=None):
        """Read-only uncertain-result lookup; no dispatch or native recovery."""
        self.auth._read(context)
        state, _, _ = self.requests.read()
        rows = [op for op in state['operations'].values() if op['request']['request_id'] == request_id]
        if len(rows) != 1 or rows[0]['request'].get('owner') != self.auth.owner:
            raise StateError('synthetic_accounting_result_unknown')
        return self._result(rows[0]['state'], replay=True)

    @staticmethod
    def _result(state, replay=False):
        return dict(state=state, replay=replay, synthetic_only=True, native_authority=False,
                    accounting_permission=False, accounting_writes=0)
