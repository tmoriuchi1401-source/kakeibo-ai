"""Offline finish plan + separately approved, one-shot finish of a claimed row.

No claim, live validate_snapshot, credential provisioning, gate/STOP changes,
financial writer, or AI. Never invoke execute_finish_only during preparation.
"""
from copy import deepcopy
import hmac
from pathlib import Path
import re
import sqlite3

from .page_receipt_model import digest
from .receipt_review_cases import validate_snapshot
from .receipt_review_worker import checked_record
from .receipt_sheets_authority import HEADERS, UNKNOWN, parse, request_id

VALIDATOR_REVISION = 'ec355eba1eba774032cac6f146d7d58328e314da'
SCHEMA = 'receipt-finish-only-v1'
CASE_HEADERS = ['case_id', 'source_id', 'source_version', 'source_sha256',
                'case_json', 'case_digest', 'request_id']


def _target(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{10,150}', value):
        raise ValueError('finish_target_invalid')
    return value


def prepare_finish_plan(record, rid, *, spreadsheet_id, deployment_id):
    """Pure offline PR205 validation; the returned private artifact is not authority."""
    before, entry, snapshot = checked_record(record, rid)
    if entry['source']['mime_type'] == 'application/pdf':
        raise ValueError('finish_baseline_validator_does_not_support_pdf')
    if before[7:9] != ['検証中', '']:
        raise ValueError('finish_requires_claimed_empty_result')
    validated = validate_snapshot(snapshot, entry)
    result = dict(request_id=rid, snapshot_digest=before[6], state=validated['state'],
                  issues=validated['issues'], financial_write_count=0, posting_authority=False)
    for key in ('receipt_total', 'item_total', 'difference'):
        if key in validated:
            result[key] = validated[key]
    plan = dict(schema=SCHEMA, validator_revision=VALIDATOR_REVISION,
                spreadsheet_id=_target(spreadsheet_id), deployment_id=_target(deployment_id),
                request_id=rid, before=before, entry=entry,
                before_digest=digest(before), entry_digest=digest(entry),
                snapshot_digest=before[6], result=result, result_digest=digest(result))
    return dict(plan, plan_digest=digest(plan))


def _checked_plan(plan, approved_digest):
    plan = deepcopy(plan)
    keys = {'schema', 'validator_revision', 'spreadsheet_id', 'deployment_id', 'request_id',
            'before', 'entry', 'before_digest', 'entry_digest', 'snapshot_digest',
            'result', 'result_digest', 'plan_digest'}
    if (type(plan) is not dict or set(plan) != keys or plan['schema'] != SCHEMA
            or plan['validator_revision'] != VALIDATOR_REVISION
            or type(approved_digest) is not str or not re.fullmatch('[a-f0-9]{64}', approved_digest)):
        raise ValueError('finish_plan_invalid')
    actual_digest = digest({k: v for k, v in plan.items() if k != 'plan_digest'})
    if not hmac.compare_digest(actual_digest, approved_digest) or plan['plan_digest'] != actual_digest:
        raise ValueError('finish_plan_not_approved')
    _target(plan['spreadsheet_id']); _target(plan['deployment_id'])
    row, entry, _ = checked_record({'row': plan['before'], 'entry': plan['entry']}, plan['request_id'])
    if entry['source']['mime_type'] == 'application/pdf':
        raise ValueError('finish_baseline_validator_does_not_support_pdf')
    result = plan['result']
    if (row[7:9] != ['検証中', ''] or digest(row) != plan['before_digest']
            or digest(entry) != plan['entry_digest'] or row[6] != plan['snapshot_digest']
            or digest(result) != plan['result_digest'] or type(result) is not dict
            or result.get('request_id') != row[0] or result.get('snapshot_digest') != row[6]
            or result.get('financial_write_count') != 0 or type(result.get('financial_write_count')) is not int
            or result.get('posting_authority') is not False
            or result.get('state') not in {'検証済み・未記帳', '修正が必要', '保留'}
            or type(result.get('issues')) is not list
            or any(type(x) is not str or not re.fullmatch('[a-z0-9_:.-]+', x) for x in result['issues'])
            or not {'request_id', 'snapshot_digest', 'state', 'issues', 'financial_write_count', 'posting_authority'} <= set(result)
            or set(result) - {'request_id', 'snapshot_digest', 'state', 'issues', 'financial_write_count', 'posting_authority',
                             'receipt_total', 'item_total', 'difference'}):
        raise ValueError('finish_plan_binding_invalid')
    return plan


class FinishAttemptFence:
    """Separate permanent finish fence; the original consumed claim fence stays intact."""
    def __init__(self, path):
        if str(path) == ':memory:':
            raise ValueError('persistent_finish_fence_required')
        self.path = Path(path)

    def consume_finish(self, rid, plan_digest):
        request_id(rid)
        if not re.fullmatch('[a-f0-9]{64}', plan_digest):
            raise ValueError('finish_plan_digest_invalid')
        # Never create/replace the original attempts DB, reset a UUID, or re-claim.
        with sqlite3.connect(self.path.resolve().as_uri() + '?mode=rw', uri=True) as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM attempts WHERE request_id=?', (rid,)).fetchone() is None:
                raise ValueError('consumed_claim_fence_required')
            db.execute('CREATE TABLE IF NOT EXISTS finish_attempts (request_id TEXT PRIMARY KEY, plan_digest TEXT NOT NULL)')
            db.execute('INSERT INTO finish_attempts VALUES (?,?)', (rid, plan_digest))


class RestReviewReader:
    """Independent bounded Sheets REST GET; no retries, writes, logs, or auth loading."""
    def __init__(self, session, spreadsheet_id):
        self.session, self.spreadsheet_id = session, _target(spreadsheet_id)

    def read(self, rid):
        request_id(rid)
        names = {'_receipt_requests': HEADERS, '_receipt_cases': CASE_HEADERS}
        response = self.session.get('https://sheets.googleapis.com/v4/spreadsheets/' + self.spreadsheet_id,
            params={'ranges': ["'_receipt_requests'!A1:J1000", "'_receipt_cases'!A1:G1000"],
                    'fields': 'spreadsheetId,sheets(properties(title,gridProperties(rowCount,columnCount)),data(startRow,startColumn,rowData(values(effectiveValue))))'},
            timeout=30, allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError('finish_independent_read_unknown')
        body = response.json(); tables = {}
        if body.get('spreadsheetId') != self.spreadsheet_id or len(body.get('sheets', [])) != 2:
            raise ValueError('finish_independent_binding_invalid')
        for sheet in body['sheets']:
            props = sheet['properties']; name = props['title']; grid = props['gridProperties']
            if name not in names or name in tables or not 1 <= grid['rowCount'] <= 1000 or grid['columnCount'] < len(names[name]):
                raise ValueError('finish_independent_bounds_invalid')
            data = sheet['data']
            if len(data) != 1 or data[0].get('startRow', 0) or data[0].get('startColumn', 0):
                raise ValueError('finish_independent_range_invalid')
            raw = data[0].get('rowData', [])
            if len(raw) > grid['rowCount']:
                raise ValueError('finish_independent_bounds_invalid')
            rows = []
            for raw_row in raw:
                cells = raw_row.get('values', [])
                if len(cells) > len(names[name]):
                    raise ValueError('finish_independent_width_invalid')
                row = []
                for i in range(len(names[name])):
                    value = cells[i].get('effectiveValue', {}) if i < len(cells) else {}
                    if value and (set(value) != {'stringValue'} or type(value['stringValue']) is not str):
                        raise ValueError('finish_independent_cell_invalid')
                    row.append(value.get('stringValue', ''))
                rows.append(row)
            while rows and not any(rows[-1]): rows.pop()
            if not rows or rows[0] != names[name]:
                raise ValueError('finish_independent_header_invalid')
            tables[name] = rows[1:]
        matches = [row for row in tables['_receipt_requests'] if row[0] == rid]
        if len(matches) != 1:
            raise ValueError('finish_independent_request_ambiguous')
        row = matches[0]
        cases = [r for r in tables['_receipt_cases'] if r[0] == row[1] or r[6] == rid]
        if len(cases) != 1:
            raise ValueError('finish_independent_case_ambiguous')
        case = cases[0]; entry = parse(case[4])
        if case[:4] != [entry['case_id'], entry['source']['source_id'], entry['source']['version'], entry['source']['sha256']] or case[5:] != [digest(entry), rid]:
            raise ValueError('finish_independent_catalog_invalid')
        checked_record({'row': row, 'entry': entry}, rid)
        return {'row': row, 'entry': entry}


def execute_finish_only(plan, *, approved_plan_digest, port, reader, fence):
    """One explicitly approved finish, no claim or validation, independent REST readback.

    Caller must stop both gates/delete the Script PAT copy in finally; this
    function never changes gate/STOP settings. UNKNOWN permits read-only inquiry.
    """
    rid = plan.get('request_id') if type(plan) is dict else None
    try:
        p = _checked_plan(plan, approved_plan_digest); rid = p['request_id']
        if port.script_id != p['deployment_id'] or reader.spreadsheet_id != p['spreadsheet_id']:
            raise ValueError('finish_execution_target_invalid')
        def current(read):
            row, entry, snap = checked_record(read(rid), rid)
            if row != p['before'] or entry != p['entry']:
                raise ValueError('finish_before_changed')
            return row, entry, snap
        current(reader.read); current(port.read)
        fence.consume_finish(rid, p['plan_digest']) # Commit before first finish/source check; never retry.
        port.source_check(deepcopy(p['entry']['source']))
        current(reader.read)
        try:
            finished = port.finish(rid, deepcopy(p['before']), deepcopy(p['result']))
        finally:
            # Both read-only checks still run after a lost finish response.
            try:
                port.source_check(deepcopy(p['entry']['source']))
            finally:
                final_record = reader.read(rid)
        actual, entry, _ = checked_record(final_record, rid)
        if (actual != finished or actual[:7] != p['before'][:7] or entry != p['entry']
                or actual[7] != p['result']['state'] or parse(actual[8], ensure_ascii=False) != p['result']):
            raise ValueError('finish_readback_unknown')
        return deepcopy(p['result'])
    except Exception:
        return dict(request_id=rid, state=UNKNOWN, financial_write_count=0, posting_authority=False)
