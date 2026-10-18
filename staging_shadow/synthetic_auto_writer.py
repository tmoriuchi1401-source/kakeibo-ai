"""Narrow synthetic-only connection to ReceiptPipeline's existing materializer.

This is a test composition, not a production constructor. It accepts only the
memory-only ledger backend used by the 5B-2B-2A evidence contract and has no
Google/Drive/Firestore/AI client or transport capability.
"""
from copy import deepcopy

from app.bank_income import INCOME_HEADERS, INCOME_SHEET
from app.drive_run_state import StateError
from app.models import ReceiptResult
from app.receipt_pipeline import ReceiptPipeline
from app.receipt_plan.planning import PlanningDB
from app.sheets import HEADERS
from app.unified_receipt_review import digest

from .auto_native_evidence import ledger_outcome, ledger_view


TABLES = ('レシート', '支出明細', INCOME_SHEET, '取込データ')


class _ExistingPlannerDB(PlanningDB):
    """Calls the existing materializer against a write-capturing plan DB."""
    def __init__(self, categories, before):
        super().__init__(categories)
        self.before = deepcopy(before)

    def sheet_titles(self):
        return list(self.before[0])

    def get(self, region):
        title = region.split('!')[0].strip("'")
        values = deepcopy(self.before[0][title])
        if region.endswith('A1:J1'):
            return values[:1]
        if region.endswith('A2:J'):
            return values[1:]
        return values

    def append(self, title, rows):
        if title not in TABLES or not rows:
            raise StateError('synthetic_receipt_plan_table_invalid')
        self.plan.extend((title, deepcopy(row)) for row in rows)
        for part in self.before:
            part[title].extend(deepcopy(rows))

    append_raw = append


def existing_plan(req):
    """Build plan by executing only the existing planner/materializer on a sink."""
    parsed = ReceiptResult.model_validate(req['inputs']['parsed'])
    categories = [tuple(pair) for pair in req['inputs']['categories']]
    receipt = req['plan']['operations'][0]['rows'][0]
    sink = _ExistingPlannerDB(categories, req['before'])
    result = ReceiptPipeline(sink, None, clock=lambda: receipt[7])._materialize_result(
        parsed, req['ownership']['source']['source_id'], receipt[5], [])
    if result.get('status') != 'imported':
        raise StateError('synthetic_existing_materializer_plan_not_imported')
    operations = []
    for title in req['plan']['operations']:
        rows = [deepcopy(row) for table, row in sink.plan if table == title['table']]
        operations.append({'table': title['table'], 'rows': rows})
    # A receipt plan has the native order and excludes the income table for
    # normal expenses. Buyback's exact native order includes income before import.
    if req['kind'] == 'normal_auto':
        titles = ('レシート', '支出明細', '取込データ')
    else:
        titles = ('レシート', INCOME_SHEET, '取込データ')
    if tuple(op['table'] for op in operations) != titles:
        raise StateError('synthetic_existing_plan_order_changed')
    planned = {'operations': operations}
    if digest(planned) != req['plan_digest']:
        raise StateError('synthetic_existing_plan_golden_mismatch')
    return parsed, categories, planned


class _SyntheticSheetsDB:
    """Strictly append-only view over the evidence contract's fake ledger IO."""
    synthetic_memory_only = True

    def __init__(self, io, req, expected):
        if getattr(io, 'synthetic_memory_only', False) is not True:
            raise StateError('synthetic_writer_backend_required')
        self.io, self.req, self.expected = io, req, expected
        self.offset = 0
        self.append_events = []
        self.after_append = None
        view, _, _ = ledger_view(io)
        if view != req['before']:
            raise StateError('synthetic_writer_before_snapshot_changed')

    def _snapshot(self):
        return ledger_view(self.io)[0]

    def categories(self):
        return [tuple(pair) for pair in self.req['inputs']['categories']]

    def sheet_titles(self):
        return list(self._snapshot()[0])

    def import_ids(self):
        return {r[0] for r in self._snapshot()[0]['取込データ'][1:] if r and r[0]}

    def receipt_ids(self):
        return {r[0] for r in self._snapshot()[0]['レシート'][1:] if r and r[0]}

    def expense_index(self):
        return {r[0]: i for i, r in enumerate(self._snapshot()[0]['支出明細'][1:], 2) if r and r[0]}

    def ensure_expense_status_column(self):
        current = self._snapshot()[0]['支出明細'][0]
        if current != HEADERS['支出明細']:
            raise StateError('synthetic_writer_expense_header_changed')

    def get(self, region):
        title = region.split('!')[0].strip("'")
        snapshot = self._snapshot()
        if title not in snapshot[0]:
            raise StateError('synthetic_writer_unknown_table')
        rows = deepcopy(snapshot[0][title])
        if title == INCOME_SHEET and region.endswith('A1:J1'):
            return rows[:1]
        if title == INCOME_SHEET and region.endswith('A2:J'):
            return rows[1:]
        return rows

    def _append(self, title, rows):
        if title not in TABLES or self.offset >= len(self.expected['operations']):
            raise StateError('synthetic_writer_scope_exceeded')
        rows = deepcopy(rows)
        operation = {'table': title, 'rows': rows}
        if operation != self.expected['operations'][self.offset]:
            raise StateError('synthetic_writer_plan_mismatch')
        if any(isinstance(cell, str) and cell.startswith('=') for row in rows for cell in row):
            # SheetsDB.append uses USER_ENTERED. Never let a synthetic writer
            # test silently treat formula-like text as a literal cell value.
            raise StateError('synthetic_writer_formula_interpretation_risk')
        raw, tag = self.io.read_versioned()
        before, _, _ = ledger_view(self.io)
        prefix = deepcopy(self.req['before'])
        for prior in self.expected['operations'][:self.offset]:
            for part in prefix:
                part[prior['table']].extend(deepcopy(prior['rows']))
        if before != prefix:
            raise StateError('synthetic_writer_unexpected_ledger_change')
        after = deepcopy(before)
        header = INCOME_HEADERS if title == INCOME_SHEET else HEADERS[title]
        if after[0][title][0] != header or after[1][title][0] != header:
            raise StateError('synthetic_writer_header_changed')
        for part in after:
            known = {str(row[0]) for row in part[title][1:] if row and row[0]}
            if any(str(row[0]) in known for row in rows):
                raise StateError('synthetic_writer_duplicate_target')
            part[title].extend(deepcopy(rows))
        state = {'schema': 'synthetic-auto-ledger-observation-v1',
                 'generation': self._generation(raw) + 1, 'complete': True, 'snapshot': after}
        import json
        encoded = json.dumps(state, sort_keys=True, separators=(',', ':')).encode()
        self.io.replace_versioned(raw, tag, encoded)
        actual, _, _ = ledger_view(self.io)
        if actual != after:
            raise StateError('synthetic_writer_append_readback_unknown')
        self.offset += 1
        self.append_events.append((title, deepcopy(rows)))
        if self.after_append:
            self.after_append(title, deepcopy(rows))

    @staticmethod
    def _generation(raw):
        import json
        return json.loads(raw)['generation']

    def append(self, title, rows):
        self._append(title, rows)

    def append_raw(self, title, rows):
        self._append(title, rows)


class SyntheticReceiptWriter:
    """Guarded target used only after the evidence adapter persists start marker."""
    synthetic_memory_only = True

    def __init__(self, io):
        if getattr(io, 'synthetic_memory_only', False) is not True:
            raise StateError('synthetic_writer_disabled')
        self.io = io
        self.dispatches = []
        self.last_db = None
        self.fault = None
        self._gate = None
        self._capability = None

    def _bind_gate(self, gate, capability):
        if self._gate is not None or capability is None:
            raise StateError('synthetic_writer_gate_binding_invalid')
        self._gate, self._capability = gate, capability

    def execute(self, req, *, capability=None, marker=None):
        from .auto_native_evidence import NativeEvidenceJournal
        if (self._gate is None or capability is not self._capability
                or self._gate._attempt.get() is None
                or self._gate._ready.get() is not None
                or marker != NativeEvidenceJournal.marker(req,
                    self._gate.journal.read()[0]['operations'].get(req['ownership']['operation_identity'], {}).get('shared_claim'))
                or self._gate.journal.read()[0]['operations'].get(req['ownership']['operation_identity'], {}).get('writer_start') != marker):
            raise StateError('synthetic_writer_guarded_lease_required')
        # Only source-whole normal/buyback operations are supported by this
        # connection. Other entry types retain their native authorities and
        # remain disconnected from this writer adapter.
        if req['kind'] not in {'normal_auto', 'buyback_income'}:
            raise StateError('synthetic_existing_writer_entry_unsupported')
        parsed, categories, planned = existing_plan(req)
        snapshot, _, _ = ledger_view(self.io)
        if snapshot != req['before'] or self.dispatches.count(req['ownership']['operation_identity']):
            raise StateError('synthetic_writer_dispatch_or_snapshot_conflict')
        db = _SyntheticSheetsDB(self.io, req, planned)
        self.last_db = db

        def after_append(title, rows):
            if self.fault:
                self.fault(title, len(db.append_events))

        db.after_append = after_append
        source_id = req['ownership']['source']['source_id']
        receipt = planned['operations'][0]['rows'][0]
        self.dispatches.append(req['ownership']['operation_identity'])
        result = ReceiptPipeline(db, None, clock=lambda: receipt[7])._materialize_result(
            parsed, source_id, receipt[5], [])
        if (result.get('status') != 'imported'
                or db.offset != len(planned['operations'])
                or ledger_outcome(req, ledger_view(self.io)[0]) != 'complete'):
            raise StateError('synthetic_existing_writer_readback_required')
        return result
