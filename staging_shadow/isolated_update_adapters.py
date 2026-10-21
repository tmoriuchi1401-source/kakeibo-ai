"""R2-4 memory-only update composition; NO production registration.

Existing native documents, validators and writer bodies remain unchanged. The
extra journal is synthetic execution evidence, never a financial authority.
ConditionalRequests.replace is the only conditional-document engine. Separate
stores are deliberately separate commits; any ambiguous prefix remains fenced.
"""
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib
import inspect
import json
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace
from uuid import UUID, uuid4

from app.drive_run_state import StateError, StateBinding
from app.receipt_confirmation_contract import encoded, snapshot
from app.unified_receipt_review import digest
from app.unified_receipt_write import ConditionalRequests
from .auto_native_evidence import load
from .ledger_execution_boundary import LedgerExecutionBoundary, _ledger, _memory

ENTRIES = {'A01': ('review_pipeline', 'ReviewApprovalPipeline'),
           'A02': ('reconciliation', 'ReconciliationPipeline'),
           'A03': ('auto_expense', 'AutoExpensePipeline'),
           'A03-bank': ('bank_finalization', 'BankFinalizationPipeline'),
           'A10': ('daily_corrections', 'DailyCorrections')}
STAGES = {'A01': 'review_apply', 'A02': 'reconcile', 'A03': 'auto_expense', 'A03-bank': 'auto_expense'}
BASE = 'd25d3823310c832fe955a2b3d51eb1df539c7184'
PINS = {'app/review_pipeline.py': '35a33aff93a5c9af37ee3ea7ea9fac59fa7061fafd7fb603a462d5af1fdf6a77', 'app/reconciliation.py': '9ff496478f5dc1117d8d100b4976211ae6c38f5d7b19b4be891a529d62345723', 'app/auto_expense.py': 'bc43eed657eba8fedb0b71b0381ddab59391b22480ba29d33bb5c0012d8e0b06', 'app/bank_finalization.py': 'f618b9b3c7175d3efc25fa7b059be1983932562adec24d116750aca72f727bc5', 'app/daily_corrections.py': '10894ab77cb441a6cbb1cc7b3f917f2d31338a3b0cb923abaa0220b0aa4343d0', 'app/production_ledger.py': 'acfe417b47ebb2c2447517e1b907a6fe633385a0f8e489193989c8bf360672b0', 'app/unified_receipt_write.py': 'ded31e5c9ecda57b777040f49abc75bb0f4c4f334ae6018c2d47698298898f36'}
STAMP = '2026-10-21T00:00:00+00:00'
PHASES = ('claimed', 'native_pending', 'writer_started', 'native_complete', 'complete')


def deny(reason='gate_required'):
    raise StateError('isolated_update_' + reason)


class Envelope(ConditionalRequests):
    """Synthetic storage envelope only; preserves the native payload verbatim."""
    def __init__(self, io):
        _memory(io)
        if getattr(io, 'supports_strong_cas', False) is not True:
            deny('strong_CAS_required')
        super().__init__(io, 'synthetic-update-envelope')

    def read(self):
        raw, tag = self.io.read_versioned()
        value = load(raw)
        from app.pdf_intake_registry import strong_etag
        if (set(value) != {'revision', 'payload'} or type(value['revision']) is not int
                or value['revision'] < 0 or not strong_etag(tag)):
            deny('envelope_UNKNOWN')
        return value, raw, tag

    def save(self, frame, payload):
        value, raw, tag = frame
        after = dict(revision=value['revision'] + 1, payload=deepcopy(payload))
        self.replace(value, raw, tag, after)
        return self.read()


@dataclass(frozen=True)
class Candidate:
    entry: str
    operation: str
    request_id: str
    arguments: tuple
    options: tuple
    ledger: tuple
    source: tuple
    native: tuple
    journal: tuple
    shared: tuple
    plan: tuple
    after: object
    source_after: object
    result: object
    native_pending: object
    fingerprint: str


class UpdateLedgerBoundary(LedgerExecutionBoundary):
    """Adds inventoried update evidence to the existing full-ledger admission.

The shared request schema is NOT changed or mislabelled as receipt authority.
Updates reserve its existing revision by CAS while their native journal owns
the full-ledger fence. Receipt admission observes that durable fence too.
"""
    def __init__(self, *, evidence_io, native_io, source_io, **kwargs):
        super().__init__(**kwargs)
        if len({id(x) for x in (evidence_io, native_io, source_io, self._shared, self._ledger)}) != 5:
            deny('independent_stores_required')
        self.evidence, self.native, self.source = (Envelope(x) for x in (evidence_io, native_io, source_io))
        self._update = ContextVar('synthetic_update_execution', default=None)

    def records(self):
        value = self.evidence.read()[0]['payload']
        if set(value) != {'synthetic_only', 'operations'} or value['synthetic_only'] is not True:
            deny('journal_UNKNOWN')
        records = value['operations']
        if not isinstance(records, list): deny('journal_UNKNOWN')
        ids = set()
        for record in records:
            if (record.get('phase') not in PHASES or record.get('request_id') in ids
                    or record.get('binding') != digest(record.get('candidate'))):
                deny('journal_UNKNOWN')
            ids.add(record['request_id'])
            candidate = record['candidate']
            if (candidate.get('ledger_id') != self.ledger_id or candidate.get('entry') not in ENTRIES
                    or record.get('events') != list(PHASES[:PHASES.index(record['phase'])+1])
                    or candidate.get('fingerprint') != digest({k:candidate[k] for k in
                        ('entry','arguments','options','before','source','native','plan','after','source_after')})):
                deny('journal_binding_UNKNOWN')
            if record['phase'] == 'complete':
                if (record.get('dispatches') != 1 or record.get('completion') !=
                        digest([record['candidate']['after'], record['candidate']['source_after'], record.get('native_after')])):
                    deny('completion_UNKNOWN')
        return records

    def _retained_observation(self):
        prior = super()._retained_observation()
        if any(r['phase'] != 'complete' for r in self.records()):
            deny('pending_UNKNOWN_readonly')
        payload = self.native.read()[0]['payload']
        from app.production_ledger import validate_ledger
        binding = StateBinding('production_run', self.ledger_id, 'synthetic-state-folder', 'synthetic-run-ledger')
        validate_ledger(encoded(payload['production']), binding)
        if (any(v['phase'] != 'ready' for v in payload['production']['sources'].values()) or
                any(v.get('state') not in {'queued', 'applied', 'failed'} for v in payload['corrections']['requests'].values())):
            deny('old_pending_UNKNOWN')
        return prior + (self.evidence.io.read_versioned(), self.native.io.read_versioned())

    def _completed_native(self, previous, run):
        # Reverse ONLY exact, completed update deltas for the old native proof.
        # This does not write/repair a row or alter the original receipt request.
        current, _, _ = _ledger(self._ledger)
        virtual = deepcopy(current)
        for record in reversed(self.records()):
            if record['phase'] != 'complete': deny('pending_UNKNOWN_readonly')
            candidate = record['candidate']
            for part in range(2):
                for table in virtual[part]:
                    old = {r[0]: r for r in candidate['before'][part][table]}
                    new = {r[0]: r for r in candidate['after'][part][table]}
                    for identity in old.keys() | new.keys():
                        if old.get(identity) == new.get(identity): continue
                        rows = virtual[part][table]
                        matches = [r for r in rows if r[0] == identity]
                        if matches != ([new[identity]] if identity in new else []):
                            deny('completed_update_target_changed')
                        if identity in old:
                            if matches: rows[rows.index(matches[0])] = deepcopy(old[identity])
                            else: deny('deleted_target_unsupported')
                        else: rows.remove(matches[0])
        class ReadOnly:
            def read_versioned(self): return encoded(dict(complete=True, snapshot=virtual)), '"virtual-proof"'
        LedgerExecutionBoundary._completed_native(SimpleNamespace(_ledger=ReadOnly()), previous, run)

    def compose_updates(self):
        return IsolatedUpdates(self)


class _DB:
    """Memory snapshot/plan port, stable-ID binding for EVERY update cell."""
    synthetic_memory_only = True
    @staticmethod
    def check_sink(sink):
        if sink is not None:
            allowed = next(v for v in IsolatedUpdates.execute.__code__.co_consts
                if inspect.iscode(v) and v.co_name == 'sink')
            if getattr(sink, '__code__', None) is not allowed: deny('direct_sink_refused')
    def __init__(self, rows, source, sink=None):
        self.check_sink(sink)
        self.rows, self.source = deepcopy(rows), deepcopy(source)
        self.plan, self.native_writes, self.sink = [], [], sink

    def get(self, region):
        title = region.split('!')[0].strip("'")
        if title in self.rows[0]: return deepcopy(self.rows[0][title])
        if title in self.source['tables']: return deepcopy(self.source['tables'][title])
        deny('uninventoried_table')

    def categories(self): return [tuple(x) for x in self.source['categories']]
    def category_rules(self): return self.get('カテゴリ自動分類ルール!A2:R')
    def expense_index(self): return {r[0]: n for n, r in enumerate(self.rows[0]['支出明細'], 2)}
    def expense_records(self): return {r[0]: (n, deepcopy(r)) for n, r in enumerate(self.rows[0]['支出明細'], 2)}
    def expense_rows_for_import(self, identity):
        return [(n, deepcopy(r)) for n, r in enumerate(self.rows[0]['支出明細'], 2) if r[10] == identity]
    def ensure_expense_status_column(self):
        if any(len(r) != 13 for r in self.rows[0]['支出明細']): deny('native_schema_migration_forbidden')

    def _op(self, title, mode, rows, columns=None):
        self.check_sink(self.sink)
        if not rows: return
        rows = deepcopy(rows)
        target = self.rows[0].get(title, self.source['tables'].get(title))
        if target is None: deny('uninventoried_table')
        bound = []
        for number, row in (enumerate(rows, len(target) + 2) if mode == 'append' else rows):
            if not row or not isinstance(row[0], str) or not row[0]: deny('fixed_ID_required')
            if any(isinstance(v, str) and v.startswith(('=', '+', '@')) for v in row):
                deny('formula_interpretation_risk')
            if mode == 'append':
                if any(r[0] == row[0] for r in target): deny('duplicate_ID')
                before = None
            else:
                if number < 2 or number > len(target) + 1 or target[number-2][0] != row[0]:
                    deny('row_ID_changed')
                before = deepcopy(target[number-2])
            bound.append(dict(id=row[0], row=number, before=before, after=row))
        operation = dict(table=title, mode=mode, targets=bound, columns=columns)
        if self.sink: self.sink(operation)
        self.plan.append(operation)
        parts = self.rows if title in self.rows[0] else [self.source['tables']]
        for part in parts:
            for item in bound:
                if mode == 'append': part[title].append(deepcopy(item['after']))
                elif columns is None: part[title][item['row']-2] = deepcopy(item['after'])
                else:
                    for column in columns: part[title][item['row']-2][column] = deepcopy(item['after'][column])

    def append(self, title, rows): self._op(title, 'append', rows)
    def update_rows(self, title, rows): self._op(title, 'update', rows)
    def get_raw(self, region):
        number = int(re.search(r'!A(\d+):', region)[1])
        return deepcopy(self.rows[0]['支出明細'][number-2:number-1])
    def update_expense_fields(self, number, cells):
        row = deepcopy(self.rows[0]['支出明細'][number-2])
        for column, value in cells: row[column] = value
        self._op('支出明細', 'update', [(number, row)], columns=[c for c, _ in cells])


class _CorrectionsStore:
    def __init__(self, payload, db, sink=None): self.payload, self.db, self.sink = deepcopy(payload), db, sink
    def read(self, key):
        return deepcopy(self.payload['corrections'] if key == 'corrections' else self.db.source['projection'].get(key))
    def write(self, key, value):
        if key != 'corrections': deny('native_scope_exceeded')
        if self.sink: self.sink(value)
        self.payload['corrections'] = deepcopy(value)
        self.db.native_writes.append(deepcopy(value))


class _StageTransport:
    def __init__(self, native, sink): self.raw, self.sink = encoded(native), sink
    def read(self): return self.raw
    def write(self, raw): self.sink(load(raw)); self.raw = raw


class IsolatedUpdates:
    def __init__(self, boundary):
        if type(boundary) is not UpdateLedgerBoundary: deny('composition_required')
        import hashlib
        root = Path(__file__).resolve().parents[1]
        for path, wanted in PINS.items():
            if hashlib.sha256((root/path).read_bytes().replace(b'\r\n',b'\n')).hexdigest() != wanted:
                deny('native_source_pin_changed')
        self.boundary, self.fault = boundary, None
        self.dispatches = []
        self.package = '_kakeibo_updates_' + uuid4().hex
        package = ModuleType(self.package)
        package.__path__ = [str(Path(__file__).resolve().parents[1] / 'app')]
        sys.modules[self.package] = package
        sys.modules[self.package + '.drive_run_state'] = importlib.import_module('app.drive_run_state')
        self._native_source = ContextVar('synthetic_update_native_source', default=None)
        def guarded(body):
            def invoke(native_self, *args, **kwargs):
                if (inspect.currentframe().f_back.f_code is not IsolatedUpdates._invoke.__code__
                        or type(getattr(native_self, 'db', None)) is not _DB):
                    deny('direct_sink_refused')
                token = self._native_source.set(native_self.db.source)
                try: return body(native_self, *args, **kwargs)
                finally: self._native_source.reset(token)
            return invoke
        for entry, (module, cls) in ENTRIES.items():
            native = self.module(module)
            kind = getattr(native, cls)
            kind.apply = guarded(kind.apply)
            if entry == 'A10':
                kind.apply_pending = lambda *args, **kwargs: deny('use_bounded_apply_pending')
                native.stamp = lambda: STAMP
        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None): return datetime.fromisoformat(STAMP).astimezone(tz or timezone.utc)
        self.module('auto_expense').datetime = Frozen
        self.module('amazon_money_runtime').money_enabled = lambda *args: self._native_source.get()['money_mode']
        clients = self.module('google_clients')
        for name in ('credentials', 'drive_service', 'sheets_service', 'read_only_drive_service',
                     'read_only_sheets_service', 'download_drive_file'):
            if hasattr(clients, name): setattr(clients, name, lambda *a, **k: deny('live_client_forbidden'))

    def module(self, name): return importlib.import_module(self.package + '.' + name)
    def point(self, name):
        if self.fault: self.fault(name)

    def _invoke(self, entry, db, payload, arguments, options, native_sink=None):
        if type(db) is not _DB: deny('memory_DB_required')
        db.check_sink(db.sink)
        if native_sink is not None and (self.boundary._update.get() is None or db.sink is None
                or inspect.currentframe().f_back.f_code is not IsolatedUpdates.execute.__code__):
            deny('native_dispatch_context_required')
        module, cls = ENTRIES[entry]
        kind = getattr(self.module(module), cls)
        if entry == 'A10':
            store = _CorrectionsStore(payload, db, native_sink)
            return kind(store, db).apply(*arguments)
        if entry == 'A03-bank':
            self.module(module).validate_bank_finalization_canary(arguments[0],
                target_spreadsheet_id=self.boundary.ledger_id, approved_target=self.boundary.ledger_id,
                current_head=options['head'], expected_head=options['expected_head'])
        kwargs = dict(category_rule_auto_apply_enabled=options.get('category_rules', False)) if entry == 'A03' else {}
        if entry == 'A03-bank':
            kwargs['confirmed_internal_transfers'] = frozenset(tuple(v) for v in db.source.get('confirmed_internal_transfers', []))
        return kind(db, **kwargs).apply(*arguments)

    def _native_valid(self, entry, payload, arguments):
        from app.production_ledger import validate_ledger
        if set(payload) != {'production', 'corrections'}: deny('native_inventory_UNKNOWN')
        validate_ledger(encoded(payload['production']), self.binding())
        if any(v['phase'] != 'ready' for v in payload['production']['sources'].values()):
            deny('old_pending_UNKNOWN')
        requests = payload['corrections']['requests']
        if any(v.get('state') not in {'queued', 'applied', 'failed'} for v in requests.values()):
            deny('old_correction_pending_UNKNOWN')
        if entry == 'A10' and (len(arguments) != 1 or requests.get(arguments[0], {}).get('state') != 'queued'):
            deny('new_queued_request_required')

    def binding(self):
        return StateBinding('production_run', self.boundary.ledger_id, 'synthetic-state-folder', 'synthetic-run-ledger')

    def prepare(self, entry, request_id, *, arguments=(), options=None):
        if entry not in ENTRIES: deny('unconnected_entry')
        try:
            if str(UUID(request_id)) != request_id: raise ValueError()
        except (ValueError, TypeError): deny('request_ID_invalid')
        options = deepcopy(options or {})
        allowed = {'head', 'expected_head'} if entry == 'A03-bank' else {'category_rules'} if entry == 'A03' else set()
        if set(options) - allowed: deny('unknown_options')
        b = self.boundary
        b._retained_observation()
        ledger = b._ledger.read_versioned(); source = b.source.read(); native = b.native.read()
        journal = b.evidence.read(); shared = ConditionalRequests(b._shared, 'synthetic-shared', synthetic_accounting=True).read()
        self._native_valid(entry, native[0]['payload'], arguments)
        if any(r['request_id'] == request_id for r in b.records()): deny('replay_readonly')
        for record in shared[0]['operations'].values():
            if record['state'] != 'complete': deny('shared_pending_UNKNOWN')
            if not b._runtimes: deny('native_receipt_inventory_required')
            b._completed_native(record, SimpleNamespace(runtime=b._runtimes[-1]))
        before = _ledger(b._ledger)[0]
        if type(source[0]['payload'].get('money_mode')) is not bool: deny('native_mode_observation_required')
        if len(before) != 2: deny('all_ledger_required')
        for part in before:
            for rows in part.values():
                if any(not r or not r[0] for r in rows) or len({r[0] for r in rows}) != len(rows): deny('ambiguous_ID')
        db = _DB(before, source[0]['payload'])
        # A01's Amazon-money path belongs to A04/A05 and stays disconnected.
        if entry == 'A01' and any(len(r) > 11 and r[11] == 'Amazon注文と照合' for r in db.get('要確認!A2:V')):
            deny('Amazon_out_of_scope')
        result = self._invoke(entry, db, native[0]['payload'], arguments, options)
        if result.get('errors', 0) or entry == 'A10' and result.get('state') != 'applied': deny('native_validator_failed')
        pending = db.native_writes[0] if entry == 'A10' and db.native_writes else None
        state = dict(entry=entry, arguments=arguments, options=options, before=before,
            source=source[0]['payload'], native=native[0]['payload'], plan=db.plan,
            after=db.rows, source_after=db.source)
        operation = digest([entry, arguments, native[0]['revision'], digest(before), digest(source[0]['payload'])])
        c = Candidate(entry, operation, request_id, tuple(arguments), tuple(sorted(options.items())), ledger,
            source, native, journal, shared, tuple(db.plan), db.rows, db.source, result, pending, digest(state))
        if (b._ledger.read_versioned() != ledger or b.source.read() != source or b.native.read() != native
                or b.evidence.read() != journal or ConditionalRequests(b._shared, 's', synthetic_accounting=True).read() != shared):
            deny('observation_changed')
        return c

    def apply_pending(self, *, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100: deny('limit_invalid')
        native = self.boundary.native.read()[0]['payload']
        self._native_valid('A01', native, ())
        output = []
        for key, item in native['corrections']['requests'].items():
            if item['state'] != 'queued': continue
            if len(output) >= limit: break
            output.append(self.execute(self.prepare('A10', str(uuid4()), arguments=(key,))))
        return output

    def apply_core(self, *, category_rules=False):
        """Same review -> reconciliation -> automatic-expense native order.

        Each stage completes both stores before the next is prepared. No intake,
        refresh, maintenance or bank/PDF parser is pulled into this composition.
        """
        results = []
        for entry in ('A01', 'A02', 'A03'):
            c = self.prepare(entry, str(uuid4()), options={'category_rules':category_rules} if entry=='A03' else None)
            results.append(self.execute(c))
        return results

    def execute(self, c):
        if type(c) is not Candidate: deny('candidate_required')
        fresh = self.prepare(c.entry, c.request_id, arguments=c.arguments, options=dict(c.options))
        if fresh != c: deny('candidate_changed')
        b = self.boundary
        self.point('before_claim')
        accepted = dict(entry=c.entry, arguments=c.arguments, options=dict(c.options),
            before=load(c.ledger[0])['snapshot'], source=c.source[0]['payload'], native=c.native[0]['payload'],
            plan=list(c.plan), after=c.after, source_after=c.source_after,
            fingerprint=c.fingerprint, ledger_id=b.ledger_id,
            observations={name:dict(tag=frame[-1],digest=digest(frame[-2].hex())) for name,frame in
                (('ledger',c.ledger),('source',c.source),('native',c.native),('shared',c.shared))})
        accepted = load(encoded(accepted))  # Persist JSON types; tuple != list on exact CAS read-back.
        record = dict(request_id=c.request_id, operation=c.operation, binding=digest(accepted), candidate=accepted,
            phase='claimed', dispatches=0, events=['claimed'])
        value = deepcopy(c.journal[0]['payload']); value['operations'].append(record)
        journal = b.evidence.save(c.journal, value)  # Durable ownership of whole ledger.
        frames = dict(ledger=c.ledger, source=c.source, native=c.native, journal=journal, shared=c.shared)
        token = b._update.set((c.operation, frames))
        def fresh_frames():
            if b._update.get() != (c.operation, frames): deny('dispatch_context_required')
            if (b._ledger.read_versioned() != frames['ledger'] or b.source.read() != frames['source']
                    or b.native.read() != frames['native'] or b.evidence.read() != frames['journal']
                    or ConditionalRequests(b._shared, 's', synthetic_accounting=True).read() != frames['shared']):
                deny('independent_revalidation_failed')
        def reserve():
            fresh_frames()
            store = ConditionalRequests(b._shared, 'synthetic-shared', synthetic_accounting=True)
            old, raw, tag = frames['shared']; after = deepcopy(old); after['revision'] += 1
            store.replace(old, raw, tag, after); frames['shared'] = store.read()
        def phase(name, **extra):
            fresh_frames(); value = deepcopy(frames['journal'][0]['payload'])
            r = value['operations'][-1]
            if r['request_id'] != c.request_id or PHASES.index(name) != PHASES.index(r['phase']) + 1:
                deny('phase_transition_invalid')
            r.update(phase=name, **extra); r['events'].append(name)
            frames['journal'] = b.evidence.save(frames['journal'], value)
        def native_save(payload):
            fresh_frames(); frames['native'] = b.native.save(frames['native'], payload)
        offset = 0
        def sink(operation):
            nonlocal offset
            fresh_frames()
            current = frames['journal'][0]['payload']['operations'][-1]
            if current['phase'] != 'writer_started' or current['dispatches'] != 1: deny('writer_start_required')
            if offset >= len(c.plan) or operation != c.plan[offset]: deny('frozen_plan_changed')
            table = operation['table']
            if table in load(frames['ledger'][0])['snapshot'][0]:
                value = load(frames['ledger'][0]); rows = value['snapshot']
                for part in rows:
                    for target in operation['targets']:
                        if operation['mode'] == 'append': part[table].append(deepcopy(target['after']))
                        else:
                            n = target['row'] - 2
                            # FORMULA observation can differ from effective value.
                            if part[table][n][0] != target['id']: deny('fixed_ID_changed')
                            if operation['columns'] is None: part[table][n] = deepcopy(target['after'])
                            else:
                                for column in operation['columns']: part[table][n][column] = deepcopy(target['after'][column])
                raw, tag = frames['ledger']
                b._ledger.replace_versioned(raw, tag, encoded(value))
                actual = b._ledger.read_versioned()
                if actual[0] != encoded(value) or actual[1] == tag: deny('ledger_readback_UNKNOWN')
                frames['ledger'] = actual
            else:
                value = deepcopy(frames['source'][0]['payload'])
                for target in operation['targets']:
                    value['tables'][table][target['row']-2] = deepcopy(target['after'])
                frames['source'] = b.source.save(frames['source'], value)
            offset += 1
            self.point('after_write_' + str(offset))
        try:
            reserve(); self.point('after_shared_claim')
            payload = deepcopy(c.native[0]['payload'])
            if c.entry == 'A10':
                if c.native_pending is None: deny('native_pending_evidence_required')
                payload['corrections'] = deepcopy(c.native_pending); native_save(payload)
                stage = None
            else:
                native = self.module('production_ledger')
                transport = _StageTransport(payload['production'], lambda p: native_save(dict(payload, production=p)))
                stage = native.ProductionLedger(transport, self.binding())
                stage.begin(STAGES[c.entry], UUID(c.request_id).hex)
                payload = deepcopy(frames['native'][0]['payload'])
            phase('native_pending'); self.point('after_native_pending')
            # Re-run native validators from frozen source and original before;
            # no untrusted caller-supplied authority can bypass this step.
            check = _DB(load(c.ledger[0])['snapshot'], c.source[0]['payload'])
            check_result = self._invoke(c.entry, check, c.native[0]['payload'], c.arguments, dict(c.options))
            if tuple(check.plan) != c.plan or check_result != c.result: deny('native_revalidation_failed')
            phase('writer_started', dispatches=1); self.point('after_writer_start')
            fresh_frames(); self.dispatches.append(c.operation)
            db = _DB(load(c.ledger[0])['snapshot'], c.source[0]['payload'], sink)
            native_payload = deepcopy(frames['native'][0]['payload'])
            result = self._invoke(c.entry, db, native_payload, c.arguments, dict(c.options),
                lambda v: native_save(dict(frames['native'][0]['payload'], corrections=v)))
            self.point('after_all_rows')
            fresh_frames()
            if (offset != len(c.plan) or _ledger(b._ledger)[0] != c.after
                    or frames['source'][0]['payload'] != c.source_after): deny('all_ledger_readback_UNKNOWN')
            if stage is not None: stage.complete(STAGES[c.entry], result, 0.0)
            else:
                item = frames['native'][0]['payload']['corrections']['requests'][c.arguments[0]]
                if item['state'] != 'applied': deny('native_completion_UNKNOWN')
            phase('native_complete', native_after=deepcopy(frames['native'][0]['payload']))
            self.point('after_native_complete')
            reserve()  # Separate shared completion; never a cross-store transaction.
            phase('complete', completion=digest([c.after, c.source_after, frames['native'][0]['payload']]))
            fresh_frames(); self.point('after_shared_complete')
            return dict(state='complete', synthetic_only=True, production_connected=False, result=result)
        finally:
            # NO retry, compensation, fence release or pending recovery here.
            b._update.reset(token)

    def inspect(self, request_id):
        records = [r for r in self.boundary.records() if r['request_id'] == request_id]
        if len(records) != 1: deny('result_unknown')
        r = records[0]
        current = _ledger(self.boundary._ledger)[0]
        return dict(phase=r['phase'], dispatches=r['dispatches'], readonly=True,
            ledger_matches=current == r['candidate']['after'],
            classification='COMPLETE' if r['phase']=='complete' else
                'PARTIAL' if current not in (r['candidate']['before'],r['candidate']['after']) else 'UNKNOWN',
            retry_allowed=False, production_connected=False)
