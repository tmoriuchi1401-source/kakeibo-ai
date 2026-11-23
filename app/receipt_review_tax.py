"""One tax-mode correction of a final held registration; no AI/intake/finance.

The original request is immutable. Intent and result are separate append-only
events. A consumed correction or uncertain event must never be resent.
"""
from copy import deepcopy
from hashlib import sha256
import sqlite3
from urllib.parse import quote

from .page_receipt_model import digest
from .receipt_review_cases import validate_snapshot
from .receipt_review_worker import checked_record
from .receipt_sheets_authority import UNKNOWN, parse, request_id

TAB = '_receipt_tax_corrections'
HEADERS = ['correction_id', 'request_id', 'snapshot_digest', 'correction_no',
           'event', 'payload_json', 'payload_digest', 'created_at']


def correction_plan(record, tax_mode, evidence):
    row, entry, snapshot = checked_record(record, record['row'][0])
    original = parse(row[8], ensure_ascii=False)
    if (row[7] != '保留' or original['state'] != row[7]
            or original['request_id'] != row[0] or original['snapshot_digest'] != row[6]
            or original['issues'] != ['tax_treatment_unknown']
            or original['financial_write_count'] != 0 or original['posting_authority'] is not False
            or snapshot['inputs']['tax_mode'] != '不明'):
        raise ValueError('tax_original_not_eligible')
    if evidence == 'insufficient' and tax_mode == '不明':
        return None  # No correction, no new attempt, keep the original hold.
    if evidence != 'original_print_confirmed' or tax_mode not in {'税込', '税別'}:
        raise ValueError('tax_printed_evidence_required')
    amended = deepcopy(snapshot)
    amended['inputs']['tax_mode'] = tax_mode
    amended['input_version'] = digest(amended['inputs'])
    return dict(schema='receipt-tax-correction-v1', correction_id=row[0]+':tax:1',
        request_id=row[0], snapshot_digest=row[6], correction_no=1,
        original_record_digest=digest(row), entry_digest=digest(entry),
        before_mode='不明', after_mode=tax_mode, evidence=evidence,
        amended_snapshot_digest=digest(amended))


def amended_snapshot(record, plan):
    expected = correction_plan(record, plan['after_mode'], plan['evidence'])
    if expected != plan:
        raise ValueError('tax_correction_binding_invalid')
    snapshot = deepcopy(checked_record(record, plan['request_id'])[2])
    snapshot['inputs']['tax_mode'] = plan['after_mode']
    snapshot['input_version'] = digest(snapshot['inputs'])
    return snapshot


class TaxAttemptFence:
    def __init__(self, path):
        if str(path) == ':memory:':
            raise ValueError('persistent_tax_fence_required')
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS tax_attempts (correction_id TEXT PRIMARY KEY)')

    def consume(self, cid):
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO tax_attempts VALUES (?)', (cid,))


class RestTaxReader:
    """Independent read-only REST receipt ledger plus the original reader."""
    def __init__(self, session, spreadsheet_id, original_reader):
        self.session, self.spreadsheet_id, self.original_reader = session, spreadsheet_id, original_reader

    def original(self, rid):
        return self.original_reader.read(rid)

    def events(self, rid):
        request_id(rid)
        reply = self.session.get('https://sheets.googleapis.com/v4/spreadsheets/' +
            self.spreadsheet_id + '/values/' + quote("'"+TAB+"'!A1:H1000", safe=''),
            timeout=30, allow_redirects=False)
        if reply.status_code != 200:
            raise RuntimeError('tax_independent_read_unknown')
        rows = reply.json().get('values', [])
        if not rows or rows[0] != HEADERS or len(rows) > 1000:
            raise ValueError('tax_history_header_invalid')
        events = []
        for row in rows[1:]:
            if (len(row) != 8 or not all(type(x) is str for x in row)
                    or row[0] != request_id(row[1])+':tax:1' or row[3] != '1'
                    or row[4] not in {'intent', 'result'}
                    or sha256(row[5].encode()).hexdigest() != row[6]):
                raise ValueError('tax_history_invalid')
            payload = parse(row[5], ensure_ascii=row[4] == 'intent')
            if (payload['correction_id'] != row[0] or payload['request_id'] != row[1]
                    or payload['snapshot_digest'] != row[2]):
                raise ValueError('tax_history_binding_invalid')
            if row[1] == rid:
                events.append(dict(event=row[4], payload=payload))
        if [e['event'] for e in events] not in ([], ['intent'], ['intent', 'result']):
            raise ValueError('tax_history_duplicate')
        return events


def execute_tax_correction(rid, tax_mode, evidence, *, reader, port, fence, duplicate_check):
    """The caller owns OFF/STOP cleanup. Never call during offline preparation."""
    request_id(rid)
    try:
        original = reader.original(rid)
        row, entry, _ = checked_record(original, rid)
        plan = correction_plan(original, tax_mode, evidence)
        if plan is None:
            return dict(request_id=rid, state='保留', issues=['tax_treatment_unknown'],
                        correction_applied=False, financial_write_count=0, posting_authority=False)
        if reader.events(rid):
            raise ValueError('tax_correction_already_attempted')
        fence.consume(plan['correction_id'])
        port.rpc('tax_begin', plan=plan)
        if (reader.original(rid) != original or reader.events(rid) != [dict(event='intent', payload=plan)]):
            raise ValueError('tax_begin_unknown')
        port.source_check(entry['source'])
        amended = amended_snapshot(original, plan)
        outcome = validate_snapshot(amended, entry)
        if duplicate_check(entry, amended):
            outcome = dict(state='保留', issues=['existing_receipt_duplicate'],
                           financial_write_count=0, posting_authority=False)
        port.source_check(entry['source'])
        result = dict(outcome, request_id=rid, snapshot_digest=row[6],
            correction_id=plan['correction_id'], correction_digest=digest(plan),
            amended_snapshot_digest=plan['amended_snapshot_digest'], tax_mode=tax_mode)
        port.rpc('tax_finish', correction_id=plan['correction_id'], result=result)
        if (reader.original(rid) != original or reader.events(rid) !=
                [dict(event='intent', payload=plan), dict(event='result', payload=result)]):
            raise ValueError('tax_finish_unknown')
        return result
    except Exception:
        return dict(request_id=rid, state=UNKNOWN, posting_authority=False, financial_write_count=0)
