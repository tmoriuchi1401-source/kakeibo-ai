"""Opt-in memory composition for the UNCHANGED ReceiptConfirmation writer.

No production factory, network client, authority issuer or new CAS engine.
The v2 completion remains synthetic evidence, never a financial permission.
"""
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict
import inspect
from pathlib import Path

from app.drive_run_state import StateError
from app.iphone_accounting_gate import AccountingProbeRequest, SCHEMA as PROBE_SCHEMA
from app.iphone_accounting_ownership import relation
from app.receipt_confirmation import ReceiptConfirmation, same_row
from app.receipt_confirmation_contract import (IsolatedConfirmationContract, PROVIDER, SCHEMA,
    encoded, memory_only, observe_ledger, planned_after, validate_request)
from app.unified_receipt_purposes import ACCOUNTING_POST
from app.unified_receipt_review import digest
from app.unified_receipt_write import ConditionalRequests

_WRITER_CODE = ReceiptConfirmation._write_accounting_plan.__code__


def native_caller(frame):
    if frame.f_code is not _WRITER_CODE:
        fail('native_body_caller_required')


def fail(reason):
    raise StateError('synthetic_legacy_' + reason)


class _MemoryLedger:
    """Only native raw append/update/read, in exact native plan order.

    No SheetsDB inheritance/handle, Google client, filesystem or network API.
    The injected versioned IO is the same disposable memory transport used by
    the existing synthetic CAS tests, not a second financial coordinator.
    """
    synthetic_memory_only = True

    def __init__(self, adapter, candidate):
        self.adapter, self.c = adapter, deepcopy(candidate)
        self.io = adapter.contract.readers.ledger
        memory_only(self.io)
        self.events = []
        self.uncertain = False
        self.expected = []
        current = deepcopy(candidate['before'][0])
        for title, row in candidate['plan']:
            found = [(i, old) for i, old in enumerate(current[title]) if old[0] == row[0]]
            if not found:
                self.expected.append(('append', title, None, deepcopy(row)))
                current[title].append(deepcopy(row))
            elif not same_row(title, found[0][1], row):
                self.expected.append(('update', title, found[0][0]+2, deepcopy(row)))
                current[title][found[0][0]] = deepcopy(row)

    def _guard(self):
        active = self.adapter._active.get()
        if active is None or active[0] is not self or not self.adapter._dispatched:
            fail('dispatch_lease_required')
        frame = inspect.currentframe()
        try:
            bodies = []
            while frame is not None:
                if frame.f_code is _WRITER_CODE:
                    bodies.append(frame.f_locals.get('self'))
                frame = frame.f_back
            if bodies != [active[1]]:
                fail('native_body_reentry_forbidden')
        finally:
            del frame

    def get(self, region):
        self._guard()
        title, separator, cells = region.partition('!')
        title = title.strip("'")
        if separator != '!' or cells != 'A2:T' or title not in {'レシート', '支出明細', '取込データ'}:
            fail('read_scope')
        return deepcopy(observe_ledger(self.io)[0][0][title])

    def _write(self, mode, title, number, row):
        if inspect.currentframe().f_back.f_code not in {
                _MemoryLedger.append_raw.__code__, _MemoryLedger.update_row_raw.__code__}:
            fail('native_db_entry_required')
        self._guard()
        if self.uncertain:
            fail('ledger_UNKNOWN_read_only')
        index = len(self.events)
        operation = (mode, title, number, deepcopy(row))
        if index >= len(self.expected) or operation != self.expected[index]:
            fail('write_scope_or_order')
        self.adapter._point('before_ledger_'+str(index+1))
        # Source/input/authority/claim are rechecked on EVERY native DB call.
        self.adapter._current(self.c)
        before, observation = observe_ledger(self.io)
        prefix = planned_after(self.c['before'], [[t, r] for _, t, _, r in self.events])
        if before != prefix or observation != self.adapter._ledger_observation:
            fail('ledger_before_changed')
        after = planned_after(before, [[title, row]])
        raw, tag = self.io.read_versioned()
        if observe_ledger(self.io) != (before, observation):
            fail('ledger_changed_before_replace')
        try:
            self.io.replace_versioned(raw, tag, encoded(dict(complete=True, snapshot=after)))
            actual, new_observation = observe_ledger(self.io)
            if actual != after or new_observation['tag'] == tag:
                fail('ledger_readback_unknown')
        except Exception:
            self.uncertain = True  # No retry or subsequent accounting write.
            raise
        self.events.append(operation)
        self.adapter._ledger_observation = new_observation
        try:
            self.adapter._point('after_ledger_'+str(index+1))
        except BaseException:
            self.uncertain = True
            raise

    def append_raw(self, title, rows):
        native_caller(inspect.currentframe().f_back)
        if type(rows) is not list or len(rows) != 1:
            fail('single_planned_row_required')
        self._write('append', title, None, rows[0])

    def update_row_raw(self, title, number, row):
        native_caller(inspect.currentframe().f_back)
        self._write('update', title, number, row)


class _GuardedConfirmation(ReceiptConfirmation):
    """A pending projection, never the old v1 save/resume entry point."""
    def __init__(self, adapter, ledger):
        # No production constructor or store is supplied to this writer view.
        self.adapter, self.db = adapter, ledger

    def apply_confirmations(self, *args, **kwargs):
        fail('v1_entry_disabled')

    def _write_accounting_plan(self, *args, **kwargs):
        fail('direct_writer_disabled')

    def save_item(self, key, item):
        native_caller(inspect.currentframe().f_back)
        self.db._guard()
        self.adapter._native_completed(self, key, item)

    def _delegate(self, capability, key, item):
        a = self.adapter
        if capability is not a._capability or a._lease.get() is not capability or a._dispatched:
            fail('dispatch_lease_required')
        a._lease.set(None)  # Burn BEFORE entering the original writer, even on crash.
        a._dispatched = True
        a.dispatches += 1
        token = a._active.set((self.db, self))
        try:
            a._point('after_dispatch_lease')
            # Audited delegate: original validator/plan/writer source is unchanged.
            ReceiptConfirmation._write_accounting_plan(self, key, item)
        finally:
            a._active.reset(token)


class SyntheticLegacyConfirmation:
    """Only fresh v2 synthetic fixtures may enter this closed composition.

    Shared acquisition uses the existing ConditionalRequests and #133 relation
    fence. The native preflight binding is #139's complete request fingerprint;
    the older inert SyntheticAccountingGate's NativePreflight digest is NOT
    substituted for this native contract. No shared schema is changed.
    """
    def __init__(self, contract, *, enabled=False, isolated_synthetic=False, fresh_attempt=False, checkpoint=None):
        if (enabled is not True or isolated_synthetic is not True
                or type(contract) is not IsolatedConfirmationContract
                or type(contract.shared) is not ConditionalRequests or not contract.shared.synthetic_accounting):
            fail('isolation_required')
        for io in (contract.store.io, contract.shared.io, contract.readers.ledger):
            memory_only(io)
            if any(hasattr(io, name) for name in ('session', 'service', 'credentials', 'client', 'drive', 'sheets')):
                fail('external_transport_capability')
        native, _, _ = contract.readers.native()
        for handle in (native, native.db, native.store):
            memory_only(handle)
            if any(hasattr(handle, name) for name in ('session', 'service', 'credentials', 'client', 'drive', 'sheets')):
                fail('external_native_capability')
        if type(native) is not ReceiptConfirmation:
            fail('existing_native_service_required')
        if len({id(io) for io in (contract.store.io, contract.shared.io, contract.readers.ledger)}) != 3:
            fail('independent_stores_required')
        self.contract, self.checkpoint = contract, checkpoint
        from .native_accounting_sandbox import verify_isolated_sink_census
        verify_isolated_sink_census(Path(__file__).resolve().parents[1])
        self._fresh_attempt = fresh_attempt is True
        self._capability = object()
        self._lease = ContextVar('synthetic_legacy_dispatch', default=None)
        self._active = ContextVar('synthetic_legacy_native_body', default=None)
        self._dispatched = self._attempted = self._completed = self.uncertain = False
        self.dispatches = 0
        self.db = self.writer = None
        self._c = self._context = self._ledger_observation = None

    def _point(self, name):
        if self.checkpoint is not None:
            self.checkpoint(name)

    def _current(self, c, *, eligibility=False):
        k = self.contract
        k._fresh(c, eligibility=eligibility)
        k._auth(c, self._context)
        r = k.store.read()[0]['operations'][c['operation']]
        if r['phase'] not in {'pending', 'writer_started', 'complete_fixture'} or k._shared(c) != r['claim']:
            fail('native_claim_changed')
        return r

    def _claim(self, c, expected_revision):
        k = self.contract
        k._fresh(c, eligibility=True); k._auth(c, self._context)
        request = AccountingProbeRequest.parse(dict(schema=PROBE_SCHEMA, synthetic_only=True, owner=c['owner'],
            request_id=c['shared_request_id'], expected_state_revision=expected_revision,
            operation_identity=c['operation'], ownership=c['ownership'], native_preflight_digest=c['fingerprint']))
        before, raw, tag = k.shared.read()
        if before['revision'] != expected_revision:
            fail('shared_stale_revision')
        if c['operation'] in before['operations']:
            fail('shared_scope_already_owned')
        for old in before['operations'].values():
            previous = k.shared.parse_request(old['request'])
            if previous.request_id in {c['request_id'], c['shared_request_id']}:
                fail('shared_request_reuse')
            if getattr(previous, 'purpose', ACCOUNTING_POST) != ACCOUNTING_POST:
                continue
            if (not isinstance(previous, AccountingProbeRequest) or previous.owner != c['owner']
                    or relation(previous.ownership, c['ownership']) != 'independent'):
                fail('shared_scope_already_owned')
        after = deepcopy(before); after['revision'] += 1
        after['operations'][c['operation']] = dict(request=asdict(request), fingerprint=request.fingerprint(),
            state='accepted', events=[dict(state='accepted', at=k.clock())])
        self._point('before_shared_claim')
        k._fresh(c, eligibility=True); k._auth(c, self._context)
        k.shared.replace(before, raw, tag, after)
        # Also require stable full versioned read-back and a changed strong tag.
        from app.receipt_confirmation_contract import token
        observed = k.shared.read()
        token(observed[1], observed[2])
        if observed[0] != after or observed[2] == tag or k.shared.read() != observed:
            fail('shared_readback_unknown')
        self._point('after_shared_claim')
        k.shared.transition(request, 'executing', k.clock())
        return request

    def submit(self, candidate, context=None, *, expected_shared_revision=0):
        c = validate_request(deepcopy(candidate))
        k = self.contract
        k._auth(c, context)
        # Exact replay is READ-ONLY, including after re-creation. No claim,
        # authority consume, writer dispatch or completion repair is attempted.
        native = k.store.read()[0]
        if native.get('schema') != SCHEMA:
            fail('v1_read_only')
        old = native.get('operations', {}).get(c['operation'])
        if old is not None:
            if old['request'] != c:
                fail('native_request_reuse')
            return self.inspect(c, context)
        if not self._fresh_attempt or self._attempted or self.uncertain or k.store.uncertain:
            fail('attempt_read_only')
        self._attempted = True
        self._c, self._context = c, context
        try:
            # Qualification validates the existing native dry-run/plan and
            # persists only the new v2 operation; legacy remains immutable.
            k.accept_fixture(c, context, expected_revision=native['revision'])
            request = self._claim(c, expected_shared_revision)
            k.consume(c, context, expected_revision=k.store.read()[0]['revision'])
            k.start(c, context, expected_revision=k.store.read()[0]['revision'])
            self._point('before_dispatch')
            r = self._current(c, eligibility=True)
            if r['phase'] != 'writer_started' or k._start_ticket != c['fingerprint']:
                fail('acknowledged_start_required')
            before, self._ledger_observation = observe_ledger(k.readers.ledger)
            if before != c['before'] or self._ledger_observation != r['unstarted']['ledger']:
                fail('before_changed_after_start')
            self.db = _MemoryLedger(self, c)
            self.writer = _GuardedConfirmation(self, self.db)
            self._lease.set(self._capability)
            self.writer._delegate(self._capability, c['review_id'], deepcopy(r['pending']))
            if not self._completed:
                fail('native_completion_missing')
            self._point('before_shared_completion')
            if k.inspect(c)['classification'] != 'COMPLETE':
                fail('native_readback_incomplete')
            k.shared.transition(request, 'awaiting_readback', k.clock())
            k.shared.transition(request, 'complete', k.clock())
            self._point('after_shared_completion')
            return self.inspect(c, context)
        except BaseException:
            self.uncertain = True
            # Keep native pending/consume/start and shared ownership forever.
            # No retry, gap fill, v1 save, pending clear, rollback or fence release.
            raise
        finally:
            self._lease.set(None)

    def _native_completed(self, writer, key, item):
        if inspect.currentframe().f_back.f_code is not _GuardedConfirmation.save_item.__code__:
            fail('native_result_caller_required')
        self.db._guard()
        c, k = self._c, self.contract
        if writer is not self.writer or self._completed or self.db.uncertain or key != c['review_id']:
            fail('completion_scope')
        r = self._current(c)
        expected = deepcopy(r['pending']); expected['status'] = 'applied'
        if item != expected or r['phase'] != 'writer_started' or self.db.events != self.db.expected:
            fail('native_applied_required')
        observed, observation = observe_ledger(k.readers.ledger)
        if observed != planned_after(c['before'], c['plan']) or observation != self._ledger_observation:
            fail('exact_full_readback_required')
        self._point('after_native_applied')
        # Same #139 schema/provider, still explicitly SYNTHETIC. The certificate
        # is now derived ONLY from the original writer's validated save_item
        # result + independent complete read-back, never supplied by the caller.
        certificate = dict(mode=PROVIDER, fingerprint=c['fingerprint'], start_digest=digest(r['start']),
            pending_digest=digest(r['pending']), snapshot_digest=digest(observed), observation=observation, at=k.clock())
        k.complete_fixture(c, certificate, expected_revision=k.store.read()[0]['revision'])
        self._completed = True
        self._point('after_native_completion')

    def inspect(self, candidate, context=None):
        """Read-only across process reconstruction. Never repair shared state."""
        c = validate_request(deepcopy(candidate))
        k = self.contract
        k.auth._read(context)
        if k.auth.owner != c['owner']:
            fail('read_owner_mismatch')
        shared = k.shared.read()
        result = k.inspect(c)
        outer = shared[0]['operations'].get(c['operation'])
        if result['classification'] == 'COMPLETE' and (not outer or outer['state'] != 'complete'):
            result['classification'] = 'UNKNOWN'
        if k.shared.read() != shared or self.uncertain or (self.db and self.db.uncertain):
            result['classification'] = 'UNKNOWN'
        # This is an in-process diagnostic count, not reconstructed authority.
        result.update(writer_dispatches=self.dispatches, retry_allowed=False,
            native_completion_provider=PROVIDER, real_native_complete=False, accounting_permission=False)
        return result
