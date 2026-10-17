"""Offline one-tab draft contract. No Sheets client, writer, grant or trigger.

Rows are untrusted input. Issued projections and journals belong to a trusted
repository, never to hidden cells. Coordinates are temporary projection slots.
This module deliberately does not implement concurrent live Sheet mutation.
"""
from copy import deepcopy
from dataclasses import dataclass
import re

from .drive_run_state import StateError
from .unified_receipt_review import digest, LEGACY_UNMAPPED_HOLD

FIELDS = ('date', 'merchant', 'amount', 'category', 'payment', 'partition', 'memo')
KIND_FIELDS = {'normal': FIELDS[:5] + ('memo',),
               'medical': FIELDS[:5] + ('memo',), 'grouping': ('partition', 'memo')}
PURPOSE = {'normal': 'ACCOUNTING_POST', 'medical': 'ACCOUNTING_POST',
           'grouping': 'STRUCTURE_REVIEW'}
IDENTITY = {'source_file_id', 'source_content_hash', 'source_version',
            'page_numbers', 'page_identities', 'receipt_unit_id', 'receipt_index'}
ROW_KEYS = {'case_id', 'identity', 'kind', 'purpose', 'hold', 'original_url', 'inputs', 'notes'}


def fail(reason):
    raise StateError('iphone_draft_' + reason)


def check_row(row):
    try:
        i = row['identity']
        if (set(row) != ROW_KEYS or row['kind'] not in KIND_FIELDS
                or row['purpose'] != PURPOSE[row['kind']]
                or not isinstance(row['case_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', row['case_id'])
                or set(i) != IDENTITY
                or not isinstance(i['source_file_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}', i['source_file_id'])
                or not isinstance(i['source_content_hash'], str) or not re.fullmatch('[0-9a-f]{64}', i['source_content_hash'])
                or not isinstance(i['source_version'], str) or not i['source_version']
                or not isinstance(i['page_numbers'], list) or not i['page_numbers']
                or any(type(n) is not int or n < 1 for n in i['page_numbers'])
                or i['page_numbers'] != sorted(set(i['page_numbers']))
                or not isinstance(i['page_identities'], list) or len(i['page_identities']) != len(i['page_numbers'])
                or any(not isinstance(p, str) or not re.fullmatch('[0-9a-f]{64}', p) for p in i['page_identities'])
                or len(set(i['page_identities'])) != len(i['page_identities'])
                or not isinstance(i['receipt_unit_id'], str)
                or type(i['receipt_index']) is not int or i['receipt_index'] < 0
                or row['kind'] == 'grouping' and (i['receipt_unit_id'] or i['receipt_index'] != 0)
                or row['kind'] != 'grouping' and row['hold'] != LEGACY_UNMAPPED_HOLD
                and (not i['receipt_unit_id'] or i['receipt_index'] < 1)
                or not isinstance(row['hold'], str)
                or row['original_url'] != 'https://drive.google.com/file/d/' + i['source_file_id'] + '/view'
                or set(row['inputs']) != set(FIELDS)
                or any(type(v) not in (str, int) or isinstance(v, str) and (len(v) > 4000 or v.startswith('='))
                       for v in row['inputs'].values())
                or not isinstance(row['notes'], dict)
                or any(k not in FIELDS or not isinstance(v, str) or len(v) > 4000 for k, v in row['notes'].items())):
            raise ValueError()
        if any(row['inputs'][f] != '' for f in FIELDS if f not in KIND_FIELDS[row['kind']]):
            raise ValueError()
        # H301 is a synthetic display alias, never a mutable medical mapping.
        if row['case_id'] == 'H301' and row['hold'] != LEGACY_UNMAPPED_HOLD:
            raise ValueError()
        return row
    except (KeyError, TypeError, ValueError):
        fail('row_invalid')


def binding(row):
    check_row(row)
    return digest({k: row[k] for k in ('case_id', 'identity', 'kind', 'purpose')})


def index_rows(rows):
    if not isinstance(rows, list):
        fail('rows_invalid')
    out = {}
    for row in rows:
        check_row(row)
        if row['case_id'] in out:
            fail('duplicate_case_id')
        out[row['case_id']] = row
    return out


def policy(rows):
    """Identity-derived protection/validation plan, NOT remote API success."""
    index_rows(rows)
    return [dict(case_id=r['case_id'], binding=binding(r),
                 editable=[] if r['hold'] else list(KIND_FIELDS[r['kind']]),
                 validations={f: ('positive_integer_or_blank' if f == 'amount' else 'literal_draft')
                              for f in KIND_FIELDS[r['kind']] if not r['hold']}) for r in rows]


def snapshot(row):
    return digest(dict(binding=binding(row), inputs=row['inputs'], notes=row['notes']))


@dataclass(frozen=True)
class IssuedProjection:
    generation: int
    rows: list
    controls: list


def regenerate(canonical_rows, journal=None, *, generation):
    """Pure regeneration from trusted canonical rows and accepted drafts only.

    A missing/changed identity gets NO draft from another identity. Older drafts
    stay in the journal, including deleted and superseded cases, for recovery.
    Caller must capture a valid live projection first or quarantine it intact.
    """
    if type(generation) is not int or generation < 1:
        fail('generation_invalid')
    rows = deepcopy(canonical_rows)
    index_rows(rows)
    for row in rows:
        saved = (journal or {}).get(binding(row))
        if saved is not None:
            if binding(saved) != binding(row):
                fail('journal_identity_changed')
            row['inputs'], row['notes'] = deepcopy((saved['inputs'], saved['notes']))
            check_row(row)
    return IssuedProjection(generation, rows, policy(rows))


def capture(issued, first, second):
    """Return draft journal updates only after two identical bounded reads.

    Live reads must include raw input/formula/note data and all controls. The
    equal-read check detects observed races, not arbitrary later Sheet edits.
    Therefore this result cannot authorize posting or live regeneration.
    """
    if not isinstance(issued, IssuedProjection):
        fail('trusted_projection_required')
    if first != second:
        fail('concurrent_or_unknown_read')
    if not isinstance(first, dict) or set(first) != {'generation', 'rows', 'controls'}:
        fail('projection_invalid')
    if first['generation'] != issued.generation:
        fail('stale_generation')
    expected = index_rows(issued.rows)
    actual = index_rows(first['rows'])
    if list(actual) != list(expected):
        fail('layout_changed_search_only')
    if first['controls'] != issued.controls or issued.controls != policy(issued.rows):
        fail('protection_or_validation_changed')
    changes = {}
    for cid, row in actual.items():
        old = expected[cid]
        if any(row[k] != old[k] for k in ROW_KEYS - {'inputs', 'notes'}):
            fail('identity_kind_or_hold_changed')
        if row['hold'] and row != old:
            fail('held_input_changed')
        changes[binding(row)] = deepcopy(row)
    return changes


def validate_handoff(row, canonical, expected_snapshot):
    """Independent downstream binding check; returns an UNAUTHORIZED draft."""
    if binding(row) != binding(canonical):
        fail('downstream_identity_changed')
    if row['hold'] or canonical['hold']:
        fail('held_case')
    if snapshot(row) != expected_snapshot:
        fail('input_snapshot_changed')
    return dict(schema='iphone-draft-v1', case_id=row['case_id'], identity=deepcopy(row['identity']),
                kind=row['kind'], purpose=row['purpose'], inputs=deepcopy(row['inputs']),
                notes=deepcopy(row['notes']), input_snapshot_digest=snapshot(row),
                operation_targets=[], authorized=False)


def protection_request(issued, sheet_id):
    """Preview full-sheet protection with only eligible input-cell exceptions.

    Offline illustrative column contract: A case ID; B:H immutable display;
    I:O FIELDS. No apply function exists. Actual adopted column mapping must be
    bound by the future reader; never apply this plan to an existing workbook.
    """
    if type(sheet_id) is not int or sheet_id < 0 or issued.controls != policy(issued.rows):
        fail('policy_invalid')
    opened = []
    for n, control in enumerate(issued.controls, 1):
        for f in control['editable']:
            c = 8 + FIELDS.index(f)
            opened.append(dict(sheetId=sheet_id, startRowIndex=n, endRowIndex=n+1,
                               startColumnIndex=c, endColumnIndex=c+1))
    return dict(addProtectedRange=dict(protectedRange=dict(
        range=dict(sheetId=sheet_id), warningOnly=False,
        description='iphone-draft-search-only-generation-' + str(issued.generation),
        unprotectedRanges=opened)))
