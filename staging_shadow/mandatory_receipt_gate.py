"""R2-2 memory-only composition. No production constructor is registered here.

The existing CAS engine, native validators and writer bodies are reused. The
views below only translate the two existing synthetic ledger representations.
Python object privacy is not an adversarial sandbox; callers own no live client.
"""
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
import inspect

from app.bank_income import INCOME_HEADERS, INCOME_SHEET
from app.drive_run_state import StateError
from app.iphone_accounting_gate import ENTRIES, SyntheticAccountingGate
from app.iphone_accounting_ownership import TrustedOwnershipReader, parse_ownership, relation, hashed
from app.receipt_confirmation_contract import encoded, memory_only, snapshot
from app.sheets import HEADERS
from app.unified_receipt_purposes import PurposeBoundary
from app.unified_receipt_review import digest
from app.unified_receipt_write import ConditionalRequests
from .auto_native_evidence import (LEDGER_SCHEMA, NativeEvidenceJournal,
    SyntheticAutoEvidenceAdapter, load, ledger_view, observation_token)
from .native_accounting_sandbox import NativeAccountingSandbox, NativeInvocation
from .receipt_draft_contract import IsolatedDraftContract, IsolatedDraftStore, LINEAGE
from .synthetic_auto_writer import SyntheticReceiptWriter, _SyntheticSheetsDB
from .synthetic_draft_writer import SyntheticDraftConfirmation
from .synthetic_legacy_writer import _MemoryLedger


def deny(reason='guarded_handle_required'):
    raise StateError('mandatory_' + reason)


def _legacy_clear(value):
    """Conservative read-only check; never manufacture a fresh v3 baseline."""
    legacy = value.get('legacy', value)
    items = legacy.get('confirmation_items', {})
    for item in items.values():
        source = item.get('source', {})
        if (item.get('status') != 'waiting' or item.get('require_reconfirm') or
                not isinstance(source,dict) or not source.get('source_id') or
                not source.get('version') or not hashed(source.get('sha256'))):
            deny('retained_legacy_fence')
    waiting = {i['source']['source_id'] for i in items.values() if isinstance(i.get('source'), dict)}
    for sid, record in legacy.get('records', {}).items():
        # Existing analysis-only scaffold may be complete while the separate
        # confirmation is still waiting. Every ambiguous financial state holds.
        if record.get('phase') != 'new' and not (record.get('phase') == 'complete' and sid in waiting):
            deny('retained_legacy_record_UNKNOWN')


class RetainedHistoryReader:
    """Observe inventoried old documents plus independent ownership evidence.

    Nonempty historical operations outside the registered R1 store cannot be
    silently adopted: they hold the route until separately approved recovery.
    Empty ports is explicit fixture provisioning, never production discovery.
    """
    def __init__(self, ownership_reader, *, legacy_ports):
        if not callable(ownership_reader) or type(legacy_ports) is not tuple:
            deny('history_inventory_required')
        for port in legacy_ports:
            memory_only(port)
        self._reader, self._ports = ownership_reader, legacy_ports

    def __call__(self):
        return self.observe()[0]

    def observe(self):
        observed = []
        for port in self._ports:
            raw, tag = port.read_versioned()
            value = load(raw)
            if value.get('schema') not in {'receipt-reimport-v1','receipt-reimport-v2','receipt-reimport-v3'}:
                deny('legacy_schema_UNKNOWN')
            if value['schema'] != 'receipt-reimport-v1':
                deny('registered_native_parent_pin_required')
            _legacy_clear(value)
            if value.get('operations'):
                deny('retained_operation_requires_readonly_reconciliation')
            observed.append((port, raw, tag))
        frame = self._reader()
        if any(p.read_versioned() != (raw, tag) for p, raw, tag in observed):
            deny('legacy_history_unstable')
        return frame, tuple((digest(raw.hex()),tag) for _,raw,tag in observed)


@dataclass
class _Attempt:
    kind: str
    candidate: dict
    history: object
    uncertain: bool = False


def _frames():
    frame = inspect.currentframe().f_back
    try:
        out = []
        while frame:
            out.append(frame.f_code)
            frame = frame.f_back
        return out
    finally:
        del frame


class _Port:
    synthetic_memory_only = True

    def __init__(self, composition, backend, role):
        memory_only(backend)
        if any(hasattr(backend, k) for k in ('session','service','credentials','client','drive','sheets')):
            deny('live_transport_forbidden')
        self._composition, self._backend, self._role = composition, backend, role

    def read_versioned(self):
        return self._backend.read_versioned()

    def replace_versioned(self, before, tag, after):
        c = self._composition
        active = c._active.get()
        if active is None or active.uncertain:
            deny()
        frames = _frames()
        if self._role == 'ledger':
            if not ((_SyntheticSheetsDB._append.__code__ in frames and
                     SyntheticReceiptWriter.execute.__code__ in frames and active.kind == 'auto') or
                    (_MemoryLedger._write.__code__ in frames and
                     SyntheticDraftConfirmation._dispatch_once.__code__ in frames and active.kind == 'r1')):
                deny('ledger_caller_required')
            c._fresh(active, financial=True)
        elif ConditionalRequests.replace.__code__ not in frames:
            deny('existing_cas_required')
        try:
            value = load(after)
            if self._role == 'shared':
                # Error clean-up must not change a fence after an uncertain
                # dispatch. Retain executing/start evidence and inspect only.
                if any(r['state'] == 'partial_unknown' for r in value['operations'].values()):
                    active.uncertain = True
                    deny('UNKNOWN_read_only')
                c._admit(load(before), value, active)
            elif self._role == 'auto_native':
                if any(r['phase'] in {'partial', 'unknown'} for r in value['operations'].values()):
                    active.uncertain = True
                    deny('UNKNOWN_read_only')
            self._backend.replace_versioned(before, tag, after)
            actual, new_tag = self._backend.read_versioned()
            if actual != after or new_tag == tag or self._backend.read_versioned() != (actual, new_tag):
                deny('CAS_readback_UNKNOWN')
        except BaseException:
            active.uncertain = True
            raise


class _AutoLedger:
    """Four complete tables, including income, over the SAME R1 ledger IO."""
    synthetic_memory_only = True

    def __init__(self, port):
        self._port = port

    @staticmethod
    def _view(raw):
        value = load(raw)
        if set(value) != {'complete', 'snapshot'} or value['complete'] is not True:
            deny('all_ledger_observation_required')
        rows = snapshot(value['snapshot'])
        tables = {'レシート', '支出明細', '取込データ', INCOME_SHEET}
        if not tables <= set(rows[0]):
            deny('income_observation_required')
        rows = [{t: part[t] for t in tables} for part in rows]
        for part in rows:
            for title in part:
                part[title].insert(0, deepcopy(INCOME_HEADERS if title == INCOME_SHEET else HEADERS[title]))
        # A stable projection revision; actual optimistic concurrency uses the
        # backend's opaque strong ETag, never this synthetic generation value.
        return encoded(dict(schema=LEDGER_SCHEMA, generation=0, complete=True, snapshot=rows))

    def read_versioned(self):
        raw, tag = self._port.read_versioned()
        return self._view(raw), tag

    def replace_versioned(self, before, tag, after):
        raw, current_tag = self._port.read_versioned()
        if (self._view(raw), current_tag) != (before, tag):
            deny('ledger_412')
        value = load(after)
        from .auto_native_evidence import validate_snapshot
        validate_snapshot(value['snapshot'])
        if (set(value) != {'schema', 'generation', 'complete', 'snapshot'} or
                value['schema'] != LEDGER_SCHEMA or value['generation'] != 1 or value['complete'] is not True):
            deny('ledger_projection_invalid')
        rows = deepcopy(value['snapshot'])
        for part in rows:
            for title in part:
                part[title] = part[title][1:]
        complete = snapshot(load(raw)['snapshot'])
        for old, new in zip(complete, rows):
            old.update(new)
        self._port.replace_versioned(raw, tag, encoded(dict(complete=True, snapshot=complete)))


class _PipelineHandle:
    def __init__(self, composition, entry, key):
        self._composition, self._entry, self._key = composition, entry, key

    def process_bytes(self, data, mime_type, *, native_id, shared_id, revision, context=None):
        c = self._composition
        source = parse_ownership(c._candidate(self._entry, self._key)['ownership']).source
        from hashlib import sha256
        expected_mime = 'application/pdf' if source['source_kind'] == 'pdf' else 'image/png'
        if type(data) is not bytes or sha256(data).hexdigest() != source['sha256'] or mime_type != expected_mime:
            deny('source_bytes_or_mime_changed')
        return c.submit_auto(self._entry, self._key, native_id, shared_id, revision, context)

    def _materialize_result(self, *args, **kwargs):
        deny('bare_materializer_closed')


class MandatoryReceiptComposition:
    """Explicit frozen server-side routes; no live CLI/run/schedule registration.

    history_reader returns an independently observed complete ownership frame
    ({complete, records}) for retained v1/v2/v3/PDF history. Missing/opaque
    mappings must set complete=False. It is an evidence port, not a UI field.
    Native automatic preflight remains synthetic validation, NOT real authority.
    """
    def __init__(self, *, shared_io, ledger_io, auto_native_io, auth, reader,
                 preflight, candidate_reader, binding_reader, history_reader,
                 enabled=False, isolated_synthetic=False, allow_new_attempts=False, clock):
        if enabled is not True or isolated_synthetic is not True:
            deny('isolation_required')
        if type(reader) is not TrustedOwnershipReader or type(auth) is not PurposeBoundary:
            deny('trusted_validator_required')
        if type(history_reader) is not RetainedHistoryReader:
            deny('history_inventory_required')
        if not all(callable(p) for p in (preflight, candidate_reader, binding_reader, history_reader, clock)):
            deny('gate_ports_required')
        ports = (shared_io, ledger_io, auto_native_io)
        for p in ports:
            memory_only(p)
            if any(hasattr(p, k) for k in ('session', 'service', 'credentials', 'client', 'drive', 'sheets')):
                deny('live_transport_forbidden')
        if len({id(p) for p in ports}) != 3:
            deny('independent_stores_required')
        self._active = ContextVar('mandatory_receipt_attempt', default=None)
        self._new_attempts = allow_new_attempts is True
        self.reader, self.auth = reader, auth
        self._preflight_port = preflight
        self._candidate, self._binding, self._history = candidate_reader, binding_reader, history_reader
        self._r1_ports = []
        self._r1_stores = {}
        self.shared = ConditionalRequests(_Port(self, shared_io, 'shared'),
            'synthetic-mandatory-receipt-shared', synthetic_accounting=True)
        self.ledger = _Port(self, ledger_io, 'ledger')
        self.auto_ledger = _AutoLedger(self.ledger)
        self.journal = NativeEvidenceJournal(_Port(self, auto_native_io, 'auto_native'), enabled=True)
        self.writer = SyntheticReceiptWriter(self.auto_ledger)
        self.adapter = SyntheticAutoEvidenceAdapter(self.journal, self.shared, auth,
            candidate_reader, binding_reader, self.auto_ledger, enabled=True, writer=self.writer)
        self.gate = SyntheticAccountingGate(self.shared, reader, auth, self._preflight,
            self.adapter.observe_boundary, enabled=True, isolated_synthetic=True,
            clock=clock, checkpoint=self.adapter.checkpoint)
        self.sandbox = NativeAccountingSandbox(self.gate,
            [NativeInvocation(e, 'isolated', (), {}) for e in ENTRIES], enabled=True, isolated_synthetic=True)
        # Private copies only; the actual app constructors remain untouched.
        cli = self.sandbox.module('cli')
        cli.make = cli.make_receipt_pipeline = self.make_receipt_pipeline
        self.sandbox.module('production_source').receipts = self.receipts
        self.sandbox.module('receipt_confirmation_production').execute = self.legacy_confirmation
        old = self.sandbox.module('receipt_confirmation').ReceiptConfirmation
        old.apply_confirmations = lambda *a, **k: self.legacy_confirmation()
        old.save_item = lambda *a, **k: deny('v1_financial_save_closed')
        self.sandbox.module('receipt_reimport_production').ReimportStore.save = lambda *a, **k: deny('v1_save_closed')

    def _preflight(self, entry, key, ownership):
        active = self._active.get()
        if active is None or active.kind != 'auto':
            deny('native_attempt_required')
        c = active.candidate
        if ('privacy_provenance' not in c['binding'] or
                c['binding']['privacy_provenance'] not in {None,'normal'}):
            deny('sensitive_source_before_OCR_or_AI')
        native = self.journal.read()
        rows, raw, tag = ledger_view(self.auto_ledger)
        if (ownership.value() != c['ownership'] or rows != c['before'] or
                observation_token(raw,tag) != c['before_observation']):
            deny('independent_before_claim_observation_changed')
        self._fresh(active)
        checked = self._preflight_port(entry,key,ownership)
        if self.journal.read() != native or ledger_view(self.auto_ledger) != (rows,raw,tag):
            deny('native_ledger_changed_during_preflight')
        return checked

    def make_receipt_pipeline(self, entry, key):
        if entry not in {'normal_auto', 'buyback_income'}:
            deny('runner_not_connected')
        # Resolve only a frozen server route here. Fresh source/ownership is
        # observed inside the gate; exact old requests can still inspect fences.
        parse_ownership(self._candidate(entry, key)['ownership'])
        return _PipelineHandle(self, entry, key)

    def receipts(self, entry, key, data, mime_type, **request):
        return self.make_receipt_pipeline(entry, key).process_bytes(data, mime_type, **request)

    @staticmethod
    def legacy_confirmation(*args, **kwargs):
        return dict(classification='HELD', reason='旧確認の金融操作は隔離gateで停止。R1の独立検証が必要。',
            synthetic_only=True, retry_allowed=False, writer_dispatches=0)

    def r1_native_port(self, backend, *, expected_digest, spreadsheet_id, parent_digest):
        # Existing saved v3 lineage is required by IsolatedDraftStore. This
        # method neither seeds nor migrates a document.
        if backend in [p._backend for p in self._r1_ports] or backend in (
                self.shared.io._backend, self.ledger._backend, self.journal.io._backend):
            deny('native_store_reused')
        p = _Port(self, backend, 'r1_native')
        store = IsolatedDraftStore(p, expected_digest, spreadsheet_id, parent_digest=parent_digest,
            enabled=True, isolated_synthetic=True)
        if store.read()[0].get('schema') != 'receipt-reimport-v3':
            deny('native_v3_history_required')
        self._r1_ports.append(p)
        self._r1_stores[p] = store
        return p

    def _history_records(self, active, *, financial=False):
        frame = self._history()
        if type(frame) is not dict or set(frame) != {'complete', 'records'} or frame['complete'] is not True:
            deny('legacy_history_UNKNOWN')
        records = list(frame['records'])
        current = self.adapter._attempt.get() if active.kind == 'auto' else None
        for r in self.journal.read()[0]['operations'].values():
            if current is not None and r['request'] == current[2]:
                continue
            records.append(r['request']['ownership'])
        for port in self._r1_ports:
            value = self._r1_stores[port].read()[0]
            if value.get('schema') != 'receipt-reimport-v3':
                deny('native_v3_history_required')
            _legacy_clear(value)
            for op, r in value['operations'].items():
                if op == LINEAGE and r.get('schema') == 'synthetic-v2-inheritance-v1':
                    continue
                # Only this exact qualified R1 request may claim. Retained
                # pending/applied/UNKNOWN and a different request never skip.
                if (active.kind == 'r1' and r.get('request') == active.candidate and
                        (r.get('phase') == 'qualified' or financial and r.get('phase') == 'writer_started')):
                    continue
                if not isinstance(r.get('request'), dict) or 'ownership' not in r['request']:
                    deny('native_history_unmapped')
                records.append(r['request']['ownership'])
        return records

    def _admit(self, before, after, active):
        added = set(after['operations']) - set(before['operations'])
        if not added:
            return
        if len(added) != 1:
            deny('one_claim_required')
        new = after['operations'][added.pop()]['request']
        wanted = active.candidate['ownership']
        if new.get('ownership') != wanted:
            deny('candidate_claim_mismatch')
        self._fresh(active)
        previous = [r['request'].get('ownership') for r in before['operations'].values()]
        previous += self._history_records(active)
        for old in previous:
            a, b = parse_ownership(old), parse_ownership(wanted)
            # Copies do not acquire independent whole-source ownership merely
            # by changing Drive ID, request ID or native operation identity.
            if a.source['sha256'] == b.source['sha256'] or relation(a.value(), b.value()) != 'independent':
                deny('source_already_owned')

    def _fresh(self, active, *, financial=False):
        if self._observe_history() != active.history:
            deny('retained_history_changed')
        if active.kind == 'auto':
            c = active.candidate
            binding = c['binding']
            if self._binding(binding['entry'], binding['key']) != binding:
                deny('source_native_history_changed')
            if financial:
                attempt = self.adapter._attempt.get()
                if attempt is None:
                    deny('native_attempt_required')
                req = attempt[2]; op = c['ownership']['operation_identity']
                native = self.journal.read()[0]['operations'].get(op)
                shared = self.shared.read()[0]['operations'].get(op)
                if (not native or not shared or native['request'] != req or native['phase'] != 'writer_started' or
                        shared['state'] != 'executing' or shared['request']['request_id'] != req['shared_request_id'] or
                        native['shared_claim']['fingerprint'] != shared['fingerprint'] or
                        native['writer_start'] != NativeEvidenceJournal.marker(req,native['shared_claim'])):
                    deny('native_shared_start_changed')
        if financial:
            for old in self._history_records(active, financial=True):
                a, b = parse_ownership(old), parse_ownership(active.candidate['ownership'])
                if a.source['sha256'] == b.source['sha256'] or relation(a.value(),b.value()) != 'independent':
                    deny('retained_source_fence_changed')
        # R1's existing _MemoryLedger revalidates the complete native/input/
        # source/authority snapshots before every append/update.

    def submit_auto(self, entry, key, native_id, shared_id, revision, context=None):
        if entry not in {'normal_auto', 'buyback_income'} or self._active.get() is not None:
            deny('auto_route_required')
        candidate = deepcopy(self._candidate(entry, key))
        active = _Attempt('auto', candidate, self._observe_history())
        t = self._active.set(active)
        try:
            with self.adapter.attempt(entry, key, native_id, shared_id, revision, context) as replay:
                if replay is not None:
                    return dict(replay=True, report=self.report(native_id, context))
                if not self._new_attempts:
                    deny('reconstructed_read_only')
                self.gate.submit(entry, key, shared_id, revision, context or {})
                return self.report(native_id, context)
        except BaseException:
            self._new_attempts = False
            active.uncertain = True
            raise
        finally:
            self._active.reset(t)

    def submit_r1(self, contract, candidate, context=None, *, revision=0, checkpoint=None):
        if (type(contract) is not IsolatedDraftContract or contract.shared is not self.shared or
                contract.readers.ledger is not self.ledger or contract.store.io not in self._r1_ports or
                self._active.get() is not None):
            deny('R1_shared_composition_required')
        active = _Attempt('r1', deepcopy(candidate), self._observe_history())
        t = self._active.set(active)
        try:
            writer = SyntheticDraftConfirmation(contract, enabled=True, isolated_synthetic=True,
                fresh_attempt=self._new_attempts, checkpoint=checkpoint)
            self.last_r1_writer = writer
            return writer.submit(candidate, context, expected_shared_revision=revision)
        except BaseException:
            self._new_attempts = False
            active.uncertain = True
            raise
        finally:
            self._active.reset(t)

    def report(self, native_id, context=None):
        result = self.adapter.report(native_id, context)
        if result['classification'] == 'COMPLETE' and result['review'].get('shared_state') != 'complete':
            return dict(result, classification='UNKNOWN', quarantined=True, reason='shared_completion_missing')
        return result

    def _observe_history(self):
        frame, observation = self._history.observe()
        if (type(frame) is not dict or set(frame) != {'complete', 'records'} or
                frame['complete'] is not True or type(frame['records']) is not list):
            deny('legacy_history_UNKNOWN')
        for record in frame['records']:
            parse_ownership(record)
        if self._history.observe() != (frame, observation):
            deny('legacy_history_unstable')
        return deepcopy((frame, observation))
