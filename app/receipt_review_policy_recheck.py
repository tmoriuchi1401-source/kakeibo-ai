"""One append-only policy recheck of a historical tax-only hold.

No intake, AI, registration, tax-mode amendment, or financial writer.
"""
from hashlib import sha256
import sqlite3
from urllib.parse import quote

from .page_receipt_model import digest
from .receipt_review_cases import VALIDATOR_REVISION, validate_snapshot
from .receipt_review_worker import checked_record
from .receipt_sheets_authority import UNKNOWN, parse, request_id

TAB = '_receipt_policy_reviews'
HEADERS = ['request_id', 'snapshot_digest', 'validator_revision',
           'original_result_digest', 'result_json', 'result_digest', 'created_at']


def recheck_plan(record):
    row, entry, snapshot = checked_record(record, record['row'][0])
    old = parse(row[8], ensure_ascii=False)
    if (row[7] != '保留' or old.get('state') != row[7]
            or old.get('issues') != ['tax_treatment_unknown']
            or old.get('request_id') != row[0] or old.get('snapshot_digest') != row[6]
            or old.get('financial_write_count') != 0 or old.get('posting_authority') is not False
            or snapshot['inputs']['tax_mode'] != '不明'):
        raise ValueError('policy_original_not_eligible')
    return dict(schema='receipt-policy-recheck-v1', request_id=row[0],
                snapshot_digest=row[6], original_result_digest=digest(old),
                original_record_digest=digest(row), entry_digest=digest(entry),
                validator_revision=VALIDATOR_REVISION)


class RecheckAttemptFence:
    def __init__(self, path):
        if str(path) == ':memory:':
            raise ValueError('persistent_recheck_fence_required')
        self.path = path

    def consume(self, rid, plan_digest):
        request_id(rid)
        with sqlite3.connect(self.path.resolve().as_uri() + '?mode=rw', uri=True) as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM attempts WHERE request_id=?', (rid,)).fetchone() is None:
                raise ValueError('original_registration_fence_missing')
            db.execute('CREATE TABLE IF NOT EXISTS policy_recheck_attempts '
                       '(request_id TEXT PRIMARY KEY, plan_digest TEXT NOT NULL)')
            db.execute('INSERT INTO policy_recheck_attempts VALUES (?,?)', (rid, plan_digest))


class RestRecheckReader:
    def __init__(self, session, spreadsheet_id, original_reader):
        self.session, self.spreadsheet_id, self.original_reader = session, spreadsheet_id, original_reader

    def original(self, rid):
        return self.original_reader.read(rid)

    def results(self, rid):
        request_id(rid)
        reply = self.session.get('https://sheets.googleapis.com/v4/spreadsheets/' +
            self.spreadsheet_id + '/values/' + quote("'"+TAB+"'!A1:G1000", safe=''),
            timeout=30, allow_redirects=False)
        if reply.status_code != 200:
            raise RuntimeError('policy_history_read_unknown')
        rows = reply.json().get('values', [])
        if not rows or rows[0] != HEADERS or len(rows) > 1000:
            raise ValueError('policy_history_header_invalid')
        seen, matches = set(), []
        for row in rows[1:]:
            if (len(row) != 7 or any(type(x) is not str for x in row)
                    or row[0] in seen or row[2] != VALIDATOR_REVISION
                    or sha256(row[4].encode()).hexdigest() != row[5]):
                raise ValueError('policy_history_invalid')
            seen.add(request_id(row[0]))
            result = parse(row[4], ensure_ascii=False)
            if (result.get('request_id') != row[0] or result.get('snapshot_digest') != row[1]
                    or result.get('validator_revision') != row[2]
                    or result.get('original_result_digest') != row[3]
                    or result.get('financial_write_count') != 0 or result.get('posting_authority') is not False):
                raise ValueError('policy_history_binding_invalid')
            if row[0] == rid:
                matches.append(result)
        return matches


def execute_recheck(rid, *, reader, port, fence, duplicate_check):
    """One attempt; every uncertain read/write stops without a retry."""
    request_id(rid)
    try:
        original = reader.original(rid)
        row, entry, snapshot = checked_record(original, rid)
        plan = recheck_plan(original)
        if reader.results(rid):
            raise ValueError('policy_recheck_already_attempted')
        fence.consume(rid, digest(plan))
        port.source_check(entry['source'])
        outcome = validate_snapshot(snapshot, entry)
        if duplicate_check(entry, snapshot):
            outcome = dict(state='保留', issues=['existing_receipt_duplicate'],
                           warnings=outcome['warnings'], validator_revision=VALIDATOR_REVISION,
                           financial_write_count=0, posting_authority=False)
        port.source_check(entry['source'])
        if reader.original(rid) != original or reader.results(rid):
            raise ValueError('policy_context_changed')
        result = dict(outcome, request_id=rid, snapshot_digest=row[6],
                      original_result_digest=plan['original_result_digest'])
        port.rpc('policy_recheck', plan=plan, result=result)
        if reader.original(rid) != original or reader.results(rid) != [result]:
            raise ValueError('policy_recheck_unknown')
        return result
    except Exception:
        return dict(request_id=rid, state=UNKNOWN, posting_authority=False, financial_write_count=0)
