"""Offline real JS receiver + Python core + strict HTTP stand-ins; no credentials."""
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

from app.drive_run_state import StateError
from app.receipt_sheets_authority import (HEADERS, UNKNOWN, SheetAuthority, canonical,
    check_row, display_record, execute, result_formula, validate_frozen)
from app.receipt_sheets_context import expected_context
from app.receipt_sheets_decoder import decode_receipt_row
from app.receipt_sheets_source import checked_source
from app.receipt_sheets_worker import GooglePort, make_binding
from app.receipt_sheet_context import expected_context as legacy_context
from app.receipt_sheet_decoder import decode_receipt_row as legacy_decode
from test_receipt_private_adapter import fixture
from test_receipt_sheets_authority import Port, STAMP
from test_receipt_sheet_validation import RID


@pytest.fixture(autouse=True)
def deny_legacy_and_financial_ports(monkeypatch):
    from app.receipt_private_adapter import ReceiptPrivateAdapter
    from app.sheets import SheetsDB
    def deny(*a, **kw): raise AssertionError('forbidden legacy/financial IO')
    monkeypatch.setattr(ReceiptPrivateAdapter, '__init__', deny)
    monkeypatch.setattr(SheetsDB, '__init__', deny)


def bundle_module():
    spec = importlib.util.spec_from_file_location('receipt_bundle', 'scripts/build_receipt_sheets_bundle.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def received(version='missing', bundle_dir=None, damage=None, setup=False):
    w, row, catalog, _ = fixture()
    entry = catalog['cases'][w.case_id]
    source = entry['anchor']['source']
    if version == 'missing': source.pop('version')
    else: source['version'] = version
    entry['anchor']['total_confirmation']['source'] = deepcopy(source)
    entry['decoder']['machine'][2] = source.get('version', '')
    row[38] = source.get('version', '')
    w.source = deepcopy(source)
    c = make_binding(catalog, spreadsheet_id='synthetic-sheet', folder_id='synthetic-folder',
        owner_email='owner@synthetic.invalid', service_email='service@synthetic.invalid',
        ref='review/receipt-sheets-authority-20261116')
    c['enabled'] = True
    row[8] = 'register:' + RID
    if damage: damage(c, row)
    result = subprocess.run(['node', 'apps-script/receipt-validation/Sheets.test.cjs'],
        input=json.dumps(dict(config=c, table=[HEADERS], ui=[row], request_id=RID,
                             bundle_dir=str(bundle_dir) if bundle_dir else None, setup=setup)),
        text=True, encoding='utf-8', capture_output=True, check=True)
    return w, c, row, json.loads(result.stdout)


class Response:
    status_code = 200
    def __init__(self, data=None, content=b''): self.data, self.content = data, content
    def json(self): return deepcopy(self.data)


class OriginalSession:
    """Only exact original GETs; a PUT or any other endpoint cannot be issued."""
    def __init__(self, w):
        self.fid = w.source['source_id']
        self.meta = deepcopy(w.metadata[self.fid])
        self.raw = w.raw
        self.trace, self.metadata_reads, self.media_reads = [], 0, 0
        self.change_metadata = None
    def get(self, url, **kw):
        assert url == 'https://www.googleapis.com/drive/v3/files/' + self.fid
        assert kw['timeout'] == 20 and kw['allow_redirects'] is False
        if kw['params'] == {'alt': 'media'}:
            self.trace.append('bytes'); self.media_reads += 1
            return Response(content=self.raw)
        assert 'fields' in kw['params'] and 'version' not in kw['params']['fields']
        self.trace.append('metadata'); self.metadata_reads += 1
        data = deepcopy(self.meta)
        if self.change_metadata: self.change_metadata(data, self.metadata_reads)
        return Response(data)


def run_case(w, c, js, session, change=None):
    p = Port(js['rows'])
    g = GooglePort(session, c)
    def validate(snapshot):
        session.trace.append('validate')
        if change: change(session)
        return validate_frozen(snapshot)
    result = execute(RID, authority=SheetAuthority(p, clock=lambda: STAMP),
        context_check=g.context_check, source_check=g.source_check, validate=validate)
    return result, p


@pytest.mark.parametrize('version', ['missing', '', 'audit-only-123'])
def test_optional_version_js_snapshot_row_worker_display_and_dedicated_bundle(version, tmp_path):
    dest = tmp_path / 'bundle'
    proof = bundle_module().build(dest)
    w, c, row, js = received(version, dest)
    assert js['errors'] == [] and js['writes'] == 1 and len(js['dispatches']) == 1
    snap = check_row(js['rows'][1])
    assert snap['source'] == w.source
    assert js['rows'][1][3] == w.source.get('version', '')
    assert sha256(canonical(snap).encode()).hexdigest() == js['rows'][1][6]
    entry = json.loads(c['cases'][w.case_id]['context_json'])
    assert decode_receipt_row(row, expected=expected_context(w.case_id, entry))['source'] == w.source
    session = OriginalSession(w)
    # Missing version in actual metadata is accepted even with a historical audit value.
    session.meta.pop('version', None)
    result, p = run_case(w, c, js, session)
    assert result['state'] == '検証済み・未記帳'
    assert result['financial_write_count'] == 0 and result['posting_authority'] is False
    assert session.trace == ['metadata', 'bytes', 'metadata', 'validate', 'metadata', 'bytes', 'metadata']
    assert session.media_reads == 2 and len(p.writes) == 2
    assert display_record(RID, p.rows, receipt_id=w.case_id, source=w.source) == '検証済み・未記帳'
    assert len(proof['oauth_scopes']) == 4


@pytest.mark.parametrize('field', ['version', 'name', 'modifiedTime', 'description'])
def test_metadata_only_changes_during_download_and_validation_are_accepted(field):
    w, c, _, js = received()
    session = OriginalSession(w)
    def mutate(data, n): data[field] = 'metadata-only-' + str(n)
    session.change_metadata = mutate
    result, p = run_case(w, c, js, session)
    assert result['state'] == '検証済み・未記帳' and len(p.writes) == 2
    assert session.media_reads == 2


def damage(session, kind):
    m = session.meta
    if kind == 'content': session.raw += b'content-changed'
    elif kind == 'id': m['id'] = 'another-original'
    elif kind == 'mime': m['mimeType'] = 'image/jpeg'
    elif kind == 'owner': m['owners'] = [dict(emailAddress='other@synthetic.invalid')]
    elif kind == 'acl_added': m['permissions'].append(dict(type='anyone', role='reader'))
    elif kind == 'acl_removed': m['permissions'].pop()
    elif kind == 'acl_role': m['permissions'][1]['role'] = 'reader'
    elif kind == 'acl_expiry': m['permissions'][1]['expirationTime'] = '2099-01-01T00:00:00Z'
    elif kind == 'parent': m['parents'] = ['another-parent']
    elif kind == 'trashed': m['trashed'] = True
    else: raise AssertionError(kind)


@pytest.mark.parametrize('kind', ['content', 'id', 'mime', 'owner', 'acl_added', 'acl_removed',
                                  'acl_role', 'acl_expiry', 'parent', 'trashed'])
@pytest.mark.parametrize('phase', ['before', 'after'])
def test_security_or_content_mismatch_stops_without_terminal_result(kind, phase):
    w, c, _, js = received()
    session = OriginalSession(w)
    if phase == 'before': damage(session, kind)
    result, p = run_case(w, c, js, session,
                        change=(lambda s: damage(s, kind)) if phase == 'after' else None)
    assert result['state'] == UNKNOWN and len(p.writes) == 1
    assert p.rows[1][7:9] == ['検証中', '']
    assert result['financial_write_count'] == 0 and result['posting_authority'] is False
    if phase == 'before': assert 'validate' not in session.trace


@pytest.mark.parametrize('kind', ['acl_added', 'parent', 'mime'])
def test_access_change_while_reading_original_stops_at_metadata_readback(kind):
    w, c, _, js = received()
    session = OriginalSession(w)
    def mutate(data, n):
        if n == 2:
            damage(session, kind)
            data.clear(); data.update(deepcopy(session.meta))
    session.change_metadata = mutate
    result, p = run_case(w, c, js, session)
    assert result['state'] == UNKNOWN and len(p.writes) == 1
    assert session.media_reads == 1 and 'validate' not in session.trace


def test_metadata_order_is_not_a_security_change():
    w, c, _, js = received()
    s = OriginalSession(w)
    def reorder(m, n):
        if n % 2 == 0: m['permissions'].reverse()
    s.change_metadata = reorder
    assert run_case(w, c, js, s)[0]['state'] == '検証済み・未記帳'


def test_legacy_decoder_and_context_still_require_version_and_keep_old_behavior():
    w, row, catalog, _ = fixture()
    entry = catalog['cases'][w.case_id]
    assert expected_context(w.case_id, entry) == legacy_context(w.case_id, entry)
    assert decode_receipt_row(row, expected=expected_context(w.case_id, entry)) == legacy_decode(row, expected=legacy_context(w.case_id, entry))
    source = entry['anchor']['source']; source.pop('version')
    entry['anchor']['total_confirmation']['source'] = deepcopy(source)
    entry['decoder']['machine'][2] = ''; row[38] = ''
    assert expected_context(w.case_id, entry)['source'] == source
    with pytest.raises(ValueError, match='original_identity_or_purpose_invalid'):
        legacy_context(w.case_id, entry)


@pytest.mark.parametrize('field', ['source_id', 'sha256', 'mime_type', 'classification', 'transaction_kind'])
def test_nonversion_source_fields_remain_mandatory_in_python_and_js(field):
    w, c, _, _ = received()
    bad = deepcopy(w.source); bad.pop(field)
    with pytest.raises(ValueError): checked_source(bad)
    def alter(config, row):
        item = config['cases'][w.case_id]
        e = json.loads(item['expected_json']); e['source'].pop(field)
        item['expected_json'] = canonical(e)
    _, _, _, js = received(damage=alter)
    assert js['writes'] == 0 and js['dispatches'] == [] and js['errors']


def test_g2_ignores_observational_version_but_keeps_content_and_result_bindings():
    formula = result_formula(2)
    assert '$AM2' not in formula and 'INDEX(rs,1,4)' not in formula
    for part in ['$AL2', '$AN2', 'n=1', 'snapshot_digest', 'posting_authority', 'financial_write_count']:
        assert part in formula


def test_display_requires_content_identity_and_ignores_only_audit_version():
    w, c, _, js = received('historical-version')
    result, p = run_case(w, c, js, OriginalSession(w))
    assert result['state'] == '検証済み・未記帳'
    assert display_record(RID, p.rows, receipt_id=w.case_id, source=dict(w.source, version='changed-audit')) == '検証済み・未記帳'
    for change in [dict(mime_type='image/jpeg'), dict(sha256='a'*64), dict(source_id='other-original')]:
        assert display_record(RID, p.rows, receipt_id=w.case_id, source=dict(w.source, **change)) == UNKNOWN


def test_dedicated_lifecycle_only_uses_existing_trigger_and_stops_before_io(tmp_path):
    dest = tmp_path / 'bundle'; bundle_module().build(dest)
    _, _, _, js = received(bundle_dir=dest, setup=True)
    assert js['output']['state'] == '受付済み'
    assert js['setup'] == dict(enabled=False, mode='sheets-authority-v1', trigger_count=1,
                               trigger_mutations=0, token_copy=False)
    assert js['errors'] == ['receipt_sheets_configuration_invalid']
    assert not any('drive' in call['url'] for call in js['calls'])


def test_dedicated_package_contains_no_drive_write_or_legacy_journal_adapter():
    files = bundle_module().bundle_files()
    code = '\n'.join(b.decode() for name, b in files.items() if name.endswith('.gs'))
    assert set(files) == {'Code.gs', 'Connection.gs', 'Sheets.gs', 'appsscript.json'}
    for forbidden in ['receiptConnectionPorts_', 'receiptPrepareDraft', 'receiptValidationEdited',
                      'drive/v2', 'drive/v3', 'DriveApp', 'If-Match', 'newTrigger', 'deleteTrigger']:
        assert forbidden not in code
    manifest = json.loads(files['appsscript.json'])
    assert set(manifest['oauthScopes']) == {'https://www.googleapis.com/auth/' + s for s in
        ['spreadsheets', 'script.scriptapp', 'script.external_request', 'userinfo.email']}
    result = subprocess.run(['node', '-e', 'new (require("node:vm").Script)(require("node:fs").readFileSync(0,"utf8"));'],
                            input=code, text=True, encoding='utf-8', capture_output=True)
    assert result.returncode == 0, result.stderr
