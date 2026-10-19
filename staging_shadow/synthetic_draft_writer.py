"""Explicit v3 composition of the existing, unchanged guarded legacy writer.

No new financial sink, request schema, authority or CAS engine. All writer DB
calls still pass #140's call-frame/lease/scope/order/read-back guards.
"""
from contextvars import ContextVar
from copy import deepcopy
import inspect
from pathlib import Path
from threading import Lock

from app.receipt_confirmation_contract import encoded, memory_only, observe_ledger, planned_after, token
from app.receipt_confirmation_draft_request import validate_shape
from app.unified_receipt_review import digest
from app.unified_receipt_write import ConditionalRequests
from .receipt_draft_contract import IsolatedDraftContract, SCHEMA, PROVIDER
from .synthetic_legacy_writer import SyntheticLegacyConfirmation, _MemoryLedger, _GuardedConfirmation, fail


class SyntheticDraftConfirmation(SyntheticLegacyConfirmation):
    """Opt-in v3 only. The existing v2 adapter is not modified or selected."""
    def __init__(self, contract, *, enabled=False, isolated_synthetic=False, fresh_attempt=False, checkpoint=None):
        if (enabled is not True or isolated_synthetic is not True or type(contract) is not IsolatedDraftContract
                or type(contract.shared) is not ConditionalRequests or not contract.shared.synthetic_accounting):
            fail('v3_isolation_required')
        ports = (contract.store.io, contract.shared.io, contract.readers.ledger,
            contract.readers.draft, contract.readers.source, contract.readers.categories,
            contract.readers.authority, contract.readers.source_bytes)
        for io in ports:
            memory_only(io)
            if any(hasattr(io, key) for key in ('session', 'service', 'credentials', 'client', 'drive', 'sheets')):
                fail('external_transport_capability')
        if len({id(p) for p in ports[:3]}) != 3: fail('independent_stores_required')
        from .native_accounting_sandbox import verify_isolated_sink_census
        verify_isolated_sink_census(Path(__file__).resolve().parents[1])
        self.contract, self.checkpoint = contract, checkpoint
        self._fresh_attempt = fresh_attempt is True
        self._capability = object()
        self._lease = ContextVar('synthetic_draft_dispatch', default=None)
        self._active = ContextVar('synthetic_draft_native_body', default=None)
        self._submit_lock = Lock()
        self._dispatched = self._attempted = self._completed = self.uncertain = False
        self.dispatches = 0
        self.db = self.writer = None
        self._c = self._context = self._ledger_observation = self._applied = None
        contract._bind_writer(self)

    def _current(self, c, *, eligibility=False):
        k = self.contract
        if self.uncertain or k.uncertain or k.store.uncertain: fail('v3_UNKNOWN_read_only')
        if self._dispatched and k._writer_context.get() is not self: fail('v3_writer_context_required')
        k._fresh(c, eligibility=eligibility); k._auth(c, self._context)
        v, raw, tag = k.store.read()
        r = v['operations'][c['operation']]
        if (r['request'] != c or r['phase'] not in {'pending', 'writer_started', 'complete_fixture'}
                or k._shared(c, executing=True) != r['claim']
                or token(raw, tag) != k._native_observation): fail('v3_native_claim_changed')
        return r

    def _claim(self, c, expected_revision):
        self._point('before_shared_claim')
        self.contract.claim_fixture(c, self._context, expected_shared_revision=expected_revision)
        self._point('after_shared_claim')
        return self.contract.shared.parse_request(self.contract.shared.read()[0]['operations'][c['operation']]['request'])

    def _dispatch_once(self, c):
        k = self.contract
        if (inspect.currentframe().f_back.f_code is not IsolatedDraftContract.dispatch.__code__
                or k._writer_context.get() is not self or self._dispatched): fail('v3_dispatch_context_required')
        r = self._current(c, eligibility=True)
        if r['phase'] != 'writer_started': fail('v3_acknowledged_start_required')
        before, self._ledger_observation = observe_ledger(k.readers.ledger)
        if before != c['before'] or self._ledger_observation != r['unstarted']['ledger']: fail('v3_before_changed_after_start')
        self.db = _MemoryLedger(self, c)
        self.writer = _GuardedConfirmation(self, self.db)
        self._lease.set(self._capability)
        try:
            self.writer._delegate(self._capability, c['review_id'], deepcopy(r['pending']))
        finally:
            self._lease.set(None)
        if not self._completed: fail('v3_native_completion_missing')

    def submit(self, candidate, context=None, *, expected_shared_revision=0):
        if not self._submit_lock.acquire(blocking=False): fail('v3_in_flight_read_only')
        try:
            c = validate_shape(deepcopy(candidate)); k = self.contract
            native = k.store.read()[0]
            if native.get('schema') != SCHEMA: fail('v3_explicit_root_required')
            old = native['operations'].get(c['operation'])
            if old is not None:
                if old['request'] != c: fail('native_request_reuse')
                return self.inspect(c, context)  # Owner read only, no expired financial replay.
            k._auth(c, context)
            if not self._fresh_attempt or self._attempted or self.uncertain or k.store.uncertain:
                fail('attempt_read_only')
            self._attempted = True; self._c, self._context = c, context
            try:
                k.accept_fixture(c, context, expected_revision=native['revision'])
                request = self._claim(c, expected_shared_revision)
                k.consume(c, context, expected_revision=k.store.read()[0]['revision'])
                k.start(c, context, expected_revision=k.store.read()[0]['revision'])
                self._point('before_dispatch')
                k.dispatch(c, composition=self)
                self._point('before_shared_completion')
                if k.inspect(c, context)['classification'] != 'COMPLETE': fail('v3_native_readback_incomplete')
                self._finish_shared(request, 'awaiting_readback')
                self._finish_shared(request, 'complete')
                self._point('after_shared_completion')
                return self.inspect(c, context)
            except BaseException:
                self.uncertain = True
                raise  # Retain all three stores; no retry, repair or rollback.
        finally:
            self._submit_lock.release()

    def _native_completed(self, writer, key, item):
        if inspect.currentframe().f_back.f_code is not _GuardedConfirmation.save_item.__code__: fail('native_result_caller_required')
        self.db._guard(); c, k = self._c, self.contract
        if writer is not self.writer or self._completed or self.db.uncertain or key != c['review_id']: fail('completion_scope')
        r = self._current(c)
        expected = deepcopy(r['pending']); expected['status'] = 'applied'
        if item != expected or r['phase'] != 'writer_started' or self.db.events != self.db.expected: fail('native_applied_required')
        observed, observation = observe_ledger(k.readers.ledger)
        if observed != planned_after(c['before'], c['plan']) or observation != self._ledger_observation: fail('exact_full_readback_required')
        self._applied = deepcopy(item)
        self._point('after_native_applied')
        certificate = dict(mode=PROVIDER, fingerprint=c['fingerprint'], start_digest=digest(r['start']),
            pending_digest=digest(r['pending']), snapshot_digest=digest(observed), observation=observation, at=k.clock())
        k.complete_fixture(c, certificate, expected_revision=k.store.read()[0]['revision'], composition=self)
        self._completed = True; self._point('after_native_completion')

    def _finish_shared(self, request, state):
        k, c = self.contract, self._c
        k._fresh(c, eligibility=False); k._auth(c, self._context)
        before, raw, tag = k.shared.read()
        if token(raw, tag) != k._shared_observation or k.inspect(c, self._context)['classification'] != 'COMPLETE':
            fail('v3_completion_changed')
        expected = deepcopy(before); expected['revision'] += 1
        r = expected['operations'][c['operation']]
        r['state'] = state; r['events'].append(dict(state=state, at=k.clock()))
        self._point('before_shared_'+state)
        k._fresh(c, eligibility=False); k._auth(c, self._context)
        if k.shared.read() != (before, raw, tag) or k.inspect(c, self._context)['classification'] != 'COMPLETE':
            fail('v3_completion_changed_before_shared_cas')
        k.shared.transition(request, state, now=k.clock())
        actual = k.shared.read()
        if actual[0] != expected or actual[1] != encoded(expected) or actual[2] == tag or k.shared.read() != actual:
            fail('v3_shared_completion_UNKNOWN')
        k._shared_observation = token(actual[1], actual[2])
        self._point('after_shared_'+state)

    def inspect(self, candidate, context=None):
        c = validate_shape(deepcopy(candidate)); k = self.contract
        k.auth._read(context)
        if k.auth.owner != c['owner']: fail('read_owner_mismatch')
        shared = k.shared.read(); result = k.inspect(c, context)
        outer = shared[0]['operations'].get(c['operation'])
        if result['classification'] == 'COMPLETE' and (not outer or outer['state'] != 'complete'):
            result['classification'] = 'UNKNOWN'
        if k.shared.read() != shared or self.uncertain or (self.db and self.db.uncertain): result['classification'] = 'UNKNOWN'
        result.update(writer_dispatches=self.dispatches, retry_allowed=False, native_completion_provider=PROVIDER)
        return result
