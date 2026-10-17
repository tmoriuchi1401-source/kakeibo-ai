"""Bounded, read-only one-tab adapter. No client factory, writer or authority.

The host supplies a trusted contract and independently attested source reader.
Sheet notes, hidden cells and successful reads never establish authority.
"""
from copy import deepcopy
from dataclasses import dataclass
import math

from .drive_run_state import StateError
from .unified_receipt_review import digest

CELL_FIELDS = 'userEnteredValue,note,dataValidation'
GET_FIELDS = ('spreadsheetId,sheets(properties,protectedRanges,tables,'
              'data(startRow,startColumn,rowData(values(' + CELL_FIELDS + ')))))')
INPUT_COLUMNS = {'一般': (3, 4, 5, 6, 7, 12),
                 '医療': (3, 4, 5, 6, 7, 8, 12), 'PDF': (3, 10, 11, 12), '保留': ()}
HELD = {'SYN-G103', 'SYN-H301'}
CASE_IDS = {'SYN-G101', 'SYN-G102', 'SYN-G103', 'SYN-H301',
            'SYN-M101', 'SYN-M102', 'SYN-P201', 'SYN-P202'}


def deny(reason):
    raise StateError('iphone_sheet_' + reason)


def raw_value(cell):
    value = cell.get('userEnteredValue', {})
    if not value:
        return ''
    if len(value) != 1:
        deny('raw_value_invalid')
    key, result = next(iter(value.items()))
    if key in {'stringValue', 'formulaValue'} and isinstance(result, str) and len(result) <= 8000:
        return result
    if key == 'numberValue' and type(result) in (int, float) and math.isfinite(result):
        return result
    deny('raw_value_invalid')


def cell_content(cell):
    return {k: deepcopy(cell[k]) for k in ('userEnteredValue', 'note', 'dataValidation') if k in cell}


def normalized_protections(ranges):
    result = []
    for p in ranges:
        q = {k: deepcopy(v) for k, v in p.items() if k != 'requestingUserCanEdit'}
        q['unprotectedRanges'] = sorted(q.get('unprotectedRanges', []), key=digest)
        result.append(q)
    return sorted(result, key=digest)


def unpack(snapshot, workbook_id, sheet_id):
    if snapshot.get('spreadsheetId') != workbook_id:
        deny('workbook_changed')
    sheets = snapshot.get('sheets', [])
    if len(sheets) != 1 or sheets[0].get('properties', {}).get('sheetId') != sheet_id:
        deny('sheet_set_changed')
    sheet = sheets[0]
    blocks = sheet.get('data', [])
    if len(blocks) != 1 or blocks[0].get('startRow', 0) != 0 or blocks[0].get('startColumn', 0) != 0:
        deny('grid_origin_or_coverage_invalid')
    rows = []
    for r in blocks[0].get('rowData', []):
        values = r.get('values', [])
        if len(values) > 26 or any(cell_content(c) for c in values[17:]):
            deny('unexpected_columns')
        rows.append([cell_content(c) for c in values[:17]] + [{} for _ in range(max(0, 17-len(values)))])
    while rows and not any(rows[-1]):
        rows.pop()
    return sheet, rows


def source_identity(source):
    keys = ('source_id', 'sha256', 'source_version', 'version_scheme')
    if (not isinstance(source, dict) or any(not isinstance(source.get(k), str) or not source[k] for k in keys)
            or source['version_scheme'] != 'drive_revision_id'
            or len(source['sha256']) != 64 or any(c not in '0123456789abcdef' for c in source['sha256'])):
        deny('source_attestation_invalid')
    return {k: source[k] for k in keys}


def issue_contract(trusted_snapshot, attested_sources):
    """Explicit offline issuance after checking approved baseline + source gate.

    Never call on an arbitrary current workbook to repair or adopt its layout.
    The orchestration must compare the original approved rows before issuance.
    """
    wid = trusted_snapshot['spreadsheetId']
    sid = trusted_snapshot['sheets'][0]['properties']['sheetId']
    sheet, rows = unpack(trusted_snapshot, wid, sid)
    if len(rows) != 9:
        deny('eight_case_baseline_required')
    sources = {s['case_id']: source_identity(s) for s in attested_sources}
    if len(sources) != 7 or len(attested_sources) != 7:
        deny('source_set_invalid')
    cases = {}; opened = []
    for n, cells in enumerate(rows[1:], 1):
        cid, kind = raw_value(cells[0]), raw_value(cells[1])
        if cid in cases or kind not in INPUT_COLUMNS:
            deny('baseline_case_invalid')
        held = cid in HELD or raw_value(cells[3]) == '保留'
        editable = () if held else INPUT_COLUMNS[kind]
        if cid == 'SYN-H301':
            if kind != '保留' or raw_value(cells[15]) or raw_value(cells[16]):
                deny('legacy_mapping_forbidden')
            identity = None
        else:
            identity = sources.get(cid)
            if not identity or (raw_value(cells[15]), raw_value(cells[16])) != (identity['sha256'], identity['source_id']):
                deny('baseline_source_mismatch')
        for c in editable:
            opened.append(dict(sheetId=sid, startRowIndex=n, endRowIndex=n+1, startColumnIndex=c, endColumnIndex=c+1))
        cases[cid] = dict(row_index=n, kind=kind, held=held, identity=identity,
                          editable=list(editable), cells=deepcopy(cells))
    protections = normalized_protections(sheet.get('protectedRanges', []))
    if (len(protections) != 1 or protections[0].get('range') != {'sheetId': sid}
            or protections[0].get('warningOnly', False) is not False
            or sorted(protections[0]['unprotectedRanges'], key=digest) != sorted(opened, key=digest)):
        deny('baseline_protection_invalid')
    if set(cases) != CASE_IDS or set(sources) != set(cases) - {'SYN-H301'}:
        deny('source_set_invalid')
    return dict(schema='iphone-sheet-contract-v1', workbook_id=wid, sheet_id=sid,
                properties=deepcopy(sheet['properties']), protections=protections,
                tables=deepcopy(sheet.get('tables', [])), header=rows[0], cases=cases)


@dataclass(frozen=True)
class DraftBatch:
    contract_digest: str
    snapshot_digest: str
    drafts: dict


def validate_snapshot(contract, snapshot, observed_sources):
    sheet, rows = unpack(snapshot, contract['workbook_id'], contract['sheet_id'])
    if sheet['properties'] != contract['properties']:
        deny('structure_changed')
    if normalized_protections(sheet.get('protectedRanges', [])) != contract['protections']:
        deny('protection_changed')
    if sheet.get('tables', []) != contract['tables']:
        deny('table_structure_changed')
    if len(rows) != 9 or rows[0] != contract['header']:
        deny('header_or_case_count_changed')
    ids = [raw_value(r[0]) for r in rows[1:]]
    if len(set(ids)) != 8 or ids != list(contract['cases']):
        deny('case_order_or_identity_changed')
    expected_sources = {c['identity']['source_id']: c['identity'] for c in contract['cases'].values() if c['identity']}
    if {k: source_identity(v) for k, v in observed_sources.items()} != expected_sources:
        deny('original_revision_or_hash_changed')
    drafts = {}
    for cid, expected in contract['cases'].items():
        cells = rows[expected['row_index']]
        for c, (current, original) in enumerate(zip(cells, expected['cells'])):
            if c not in expected['editable'] and current != original:
                deny('immutable_or_held_cell_changed')
            if current.get('note') != original.get('note') or current.get('dataValidation') != original.get('dataValidation'):
                deny('note_or_validation_changed')
            if c in expected['editable']:
                if 'formulaValue' in current.get('userEnteredValue', {}):
                    deny('input_formula_forbidden')
                v = raw_value(current)
                rule = current.get('dataValidation', {}).get('condition', {})
                if rule.get('type') == 'ONE_OF_LIST' and v != '' and v not in [x.get('userEnteredValue') for x in rule.get('values', [])]:
                    deny('dropdown_value_invalid')
                if c == 6 and v != '' and (type(v) not in (int, float) or v != int(v) or not 1 <= v <= 99999999):
                    deny('amount_invalid')
        bound = digest(dict(case_id=cid, kind=expected['kind'], identity=expected['identity']))
        drafts[bound] = dict(case_id=cid, kind=expected['kind'], identity=deepcopy(expected['identity']),
                            held=expected['held'], cells=deepcopy(cells), input_snapshot_digest=digest(cells),
                            authorized=False, operation_targets=[])
    return DraftBatch(digest(contract), digest(snapshot), drafts)


class GoogleRawTransport:
    """Injected existing credential/client, get-only. No OAuth/config discovery."""
    def __init__(self, service, contract):
        self.service, self.contract = service, deepcopy(contract)

    def read(self):
        # Extra bounded sentinel columns/rows detect additions; full grid shape
        # is checked independently. No whole-grid scan or mutable endpoint.
        return self.service.spreadsheets().get(
            spreadsheetId=self.contract['workbook_id'], ranges=["'確認'!A1:Z12"],
            fields=GET_FIELDS).execute()


class RawReader:
    def __init__(self, contract, read_raw, read_sources):
        self.contract, self.read_raw, self.read_sources = deepcopy(contract), read_raw, read_sources

    def read(self):
        first_sources = self.read_sources()
        first = self.read_raw()
        second = self.read_raw()
        second_sources = self.read_sources()
        if first != second or first_sources != second_sources:
            deny('concurrent_or_unknown_read')
        return validate_snapshot(self.contract, second, second_sources)


def dry_run(batch, validators):
    """Only injected existing pure/native draft validators; no execution route."""
    results = {}
    for key, draft in batch.drafts.items():
        if draft['held'] or raw_value(draft['cells'][3]) == '保留':
            checked = {'status': 'held'}
        elif draft['kind'] not in validators:
            checked = {'status': 'native_snapshot_required'}
        else:
            checked = validators[draft['kind']](deepcopy(draft))
        results[key] = dict(validation=deepcopy(checked), authorized=False, operation_targets=[])
    return results


def regeneration_preview(batch, saved_batch):
    if (batch.contract_digest != saved_batch['contract_digest']
            or batch.drafts != saved_batch['drafts']):
        deny('unsent_inputs_preserved_stop_regeneration')
    # No remote write capability. A future host still needs edit exclusion and
    # a fresh readback; equal reads do not establish a Sheets transaction.
    return dict(status='preview_only', snapshot_digest=batch.snapshot_digest,
                drafts=deepcopy(batch.drafts), authorized=False, operation_targets=[])
