"""Explicit owner input -> existing general manual writer, no OCR/AI.

The host must validate Drive grouping/page-kind/source and the current owner's
explicit request snapshot. It shares the existing production mutex and private
projection journal. This service is not a workflow, UI or permission shortcut.
"""
from copy import deepcopy
from datetime import date
import json

from .drive_run_state import StateError
from . import manual_entry
from .pdf_unit_processing import DriveUnitProcessingStore, digest
from .receipt_confirmation import same_row
from .receipt_reimport import _date, _money
from .sheets import HEADERS


def _rows(db, title):
    width = {'支出明細': 'M', 'レシート': 'I'}[title]
    return db.get_raw(f"'{title}'!A2:{width}")


def _exact(db, expected):
    rows = [r for r in _rows(db, '支出明細') if r and r[0] == expected[0]]
    return len(rows) == 1 and same_row('支出明細', rows[0], expected)


def _duplicate_barrier(db, expected):
    """Candidate is a hold, never an amount-only duplicate decision."""
    day, amount = date.fromisoformat(expected[1]), expected[4]
    for title in ('レシート', '支出明細'):
        for raw in _rows(db, title):
            row = list(raw) + [''] * (13 - len(raw))
            if not raw or row[0] == expected[0]: continue
            if title == '支出明細':
                # Compare standalone non-receipt/manual totals. Receipt item
                # amounts are not transaction totals and are checked above.
                if row[9] or row[12] not in ('', 'active'): continue
                value = _money(row[4])
            else:
                value = _money(row[3])
            existing_day = _date(row[1])
            if value == amount and existing_day and abs((date.fromisoformat(existing_day) - day).days) <= 7:
                raise StateError('pdf_manual_duplicate_candidate')


class _WriterFence:
    """Limit the established writer to its one planned append + private queue."""
    def __init__(self, db, expected):
        self.db, self.expected, self.appended = db, expected, False

    def __getattr__(self, name):
        if name in {'get_raw', 'categories', 'expense_records'}:
            return getattr(self.db, name)
        raise StateError('pdf_manual_writer_operation_forbidden')

    def ensure_expense_status_column(self):
        # Already provisioned schema only. Never change an unrelated header.
        if self.db.get_raw("'支出明細'!A1:M1") != [HEADERS['支出明細']]:
            raise StateError('pdf_manual_accounting_schema_changed')

    def set_raw_range(self, range_, rows):
        # Queue projection can fail after append. Canonical rows cannot update.
        import re
        match=re.fullmatch(r"'_手入力受付'!A([1-9][0-9]*)", range_)
        if not match or int(match.group(1)) < 2:
            raise StateError('pdf_manual_writer_operation_forbidden')
        return self.db.set_raw_range(range_, rows)

    def append_raw(self, title, rows):
        if title != '支出明細' or rows != [self.expected] or self.appended:
            raise StateError('pdf_manual_writer_operation_forbidden')
        self.appended = True
        return self.db.append_raw(title, rows)


def confirm(db, store, spec, request_id, *, verify_current, verify_owner_request,
            project=lambda *_: None):
    """Owner verification must bind UUID, explicit action and exact input fields.

    Pending records reconcile by exact accounting read-back only. They never
    re-enter the writer after ambiguous delivery. A failed projection is repaired
    on replay without applying the durable intent or accounting a second time.
    """
    if not isinstance(store,DriveUnitProcessingStore):
        raise StateError('pdf_manual_durable_store_required')
    if not manual_entry.UUID.fullmatch(request_id):
        raise StateError('manual_request_id_invalid')
    requests = manual_entry._requests(db)
    if request_id not in requests: raise StateError('manual_request_missing')
    _, queue_row = requests[request_id]
    try:
        payload = json.loads(queue_row[2])
        if (set(payload) != {'date', 'amount', 'merchant', 'note', 'major', 'minor', 'payment', 'pdf_unit'}
                or payload['pdf_unit'] != digest(spec)
                or (payload['major'], payload['minor']) not in set(db.categories())):
            raise ValueError()
        day, amount, merchant, note, major, minor, payment = manual_entry._payload(
            queue_row[2], set(db.categories()))
    except Exception:
        raise StateError('pdf_manual_owner_input_invalid') from None
    expected = [manual_entry.expense_id(request_id), day, merchant, '手入力', amount,
        major, minor, payment, 'manual', '', 'manual:' + request_id, note, 'active']
    raw_snapshot = queue_row[2]

    def barrier(_request=None, _expected=None):
        if (verify_current(deepcopy(spec)) is not True or
                verify_owner_request(request_id, deepcopy(payload)) is not True):
            raise StateError('pdf_manual_source_or_owner_changed')
        current = manual_entry._requests(db).get(request_id)
        if not current or current[1][2] != raw_snapshot:
            raise StateError('pdf_manual_owner_snapshot_changed')

    barrier()
    already = store.load()['records'].get(spec['unit_id'])
    if already is None:
        if queue_row[1]!='dispatching':raise StateError('pdf_manual_request_not_dispatching')
        _duplicate_barrier(db, expected)
    record, created = store.reserve(spec, 'general_manual', digest(payload),
        {'支出明細': [expected]}, digest(['existing-general-manual-v1', request_id]),
        verify_current=verify_current)
    appends = 0
    if created:
        # A pre-existing request state is never reset to make the writer run.
        if queue_row[1] != 'dispatching':
            raise StateError('pdf_manual_request_not_dispatching')
        def before_append(request, row):
            barrier(request,row)
            store.verify_pending(spec['unit_id'],record['intent_digest'])
            _duplicate_barrier(db, expected)
        writer = _WriterFence(db, expected)
        manual_entry.execute(writer, request_id, before_append=before_append)
        appends = int(writer.appended)
    elif record['phase'] == 'pending' and not _exact(db, expected):
        raise StateError('pdf_manual_pending_requires_reconciliation')
    done = store.complete(spec['unit_id'], record['intent_digest'],
        verify_current=verify_current,
        verify_readback=lambda _: _exact(db, expected))
    # Recheck the current owner snapshot across completion before projecting.
    # If it changed, accounting is already durable; do not relabel it as failed.
    barrier()
    project(request_id, deepcopy(done))
    return {'status': 'manual_imported', 'unit_id': spec['unit_id'],
            'accounting_appends': appends, 'replayed': not created}
