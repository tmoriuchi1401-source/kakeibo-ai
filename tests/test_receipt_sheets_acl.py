"""Owner-side new-mode minimum ACL: exact owner+writer Sheet / owner+reader original."""
from copy import deepcopy

import pytest

from app.drive_run_state import StateError
from app.receipt_sheets_worker import GooglePort, connect
from app.receipt_sheets_owner_check import check_owner_metadata
from test_receipt_sheets_source_identity import received, OriginalSession, Response


@pytest.fixture(autouse=True)
def no_financial_or_legacy_io(monkeypatch):
    from app.sheets import SheetsDB
    from app.receipt_private_adapter import ReceiptPrivateAdapter
    def deny(*a, **kw): raise AssertionError('financial/legacy IO forbidden')
    monkeypatch.setattr(SheetsDB, '__init__', deny)
    monkeypatch.setattr(ReceiptPrivateAdapter, '__init__', deny)


class MetadataSession:
    def __init__(self, w, c):
        self.source_id = w.source['source_id']
        self.raw, self.calls = w.raw, []
        original = OriginalSession(w).meta
        sheet = deepcopy(original)
        sheet.update(id=c['spreadsheet_id'], mimeType='application/vnd.google-apps.spreadsheet')
        sheet['permissions'][1]['role'] = 'writer'
        self.meta = {self.source_id: original, c['spreadsheet_id']: sheet}
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def get(self, url, **kw):
        fid = url.rsplit('/', 1)[1]
        assert url == 'https://www.googleapis.com/drive/v3/files/' + fid and fid in self.meta
        assert kw['timeout'] == 20 and kw['allow_redirects'] is False
        self.calls.append((fid, deepcopy(kw['params'])))
        if kw['params'] == {'alt': 'media'}:
            assert fid == self.source_id
            return Response(content=self.raw)
        return Response(self.meta[fid])
    # No mutation methods exist on this transport.


def resource(asset):
    w, c, _, _ = received()
    session = MetadataSession(w, c)
    fid = c['spreadsheet_id'] if asset == 'sheet' else w.source['source_id']
    role = 'writer' if asset == 'sheet' else 'reader'
    return w, c, session, fid, role


def owner_metadata(session, c, fid, role):
    mime = 'application/vnd.google-apps.spreadsheet' if fid == c['spreadsheet_id'] else 'image/png'
    if role not in {'reader', 'writer'}:
        return check_owner_metadata(fid, {}, c, worker_role=role, mime_type=mime)
    m = session.get('https://www.googleapis.com/drive/v3/files/' + fid,
        params={'fields': 'id,mimeType,parents,permissions'}, timeout=20, allow_redirects=False).json()
    return check_owner_metadata(fid, m, c, worker_role=role, mime_type=mime)


@pytest.mark.parametrize('asset', ['sheet', 'png'])
def test_exact_minimum_acl_is_accepted(asset):
    _, c, session, fid, role = resource(asset)
    m = owner_metadata(session, c, fid, role)
    assert m['permissions'] == sorted([('user', 'owner', c['owner_email']),
                                        ('user', role, c['service_email'])])
    assert len(session.calls) == 1


@pytest.mark.parametrize('asset', ['sheet', 'png'])
@pytest.mark.parametrize('wrong', ['opposite', 'commenter', 'owner'])
def test_insufficient_or_excess_worker_role_is_rejected(asset, wrong):
    _, c, session, fid, role = resource(asset)
    session.meta[fid]['permissions'][1]['role'] = (
        'reader' if role == 'writer' else 'writer') if wrong == 'opposite' else wrong
    with pytest.raises(StateError, match='acl_or_identity_changed'):
        owner_metadata(session, c, fid, role)


@pytest.mark.parametrize('asset', ['sheet', 'png'])
@pytest.mark.parametrize('extra', [dict(type='anyone', role='reader'),
    dict(type='domain', role='reader', domain='synthetic.invalid'),
    dict(type='user', role='reader', emailAddress='extra@synthetic.invalid')])
def test_any_extra_active_sharing_recipient_is_rejected(asset, extra):
    _, c, session, fid, role = resource(asset)
    session.meta[fid]['permissions'].append(deepcopy(extra))
    with pytest.raises(StateError, match='acl_or_identity_changed'):
        owner_metadata(session, c, fid, role)


@pytest.mark.parametrize('asset', ['sheet', 'png'])
@pytest.mark.parametrize('damage', ['owner_only', 'duplicate', 'wrong_principal', 'pending_owner'])
def test_missing_duplicate_wrong_or_transferring_worker_is_rejected(asset, damage):
    _, c, session, fid, role = resource(asset)
    permissions = session.meta[fid]['permissions']
    if damage == 'owner_only': permissions.pop()
    elif damage == 'duplicate': permissions.append(deepcopy(permissions[1]))
    elif damage == 'wrong_principal': permissions[1]['emailAddress'] = 'other@synthetic.invalid'
    elif damage == 'pending_owner': permissions[1]['pendingOwner'] = True
    with pytest.raises(StateError, match='acl_or_identity_changed'):
        owner_metadata(session, c, fid, role)


def test_png_promotion_is_not_seen_by_worker_but_is_rejected_by_owner_postcheck():
    w, c, session, fid, _ = resource('png')
    original = session.get
    def changed(url, **kw):
        response = original(url, **kw)
        if kw['params'] == {'alt': 'media'}: session.meta[fid]['permissions'][1]['role'] = 'writer'
        return response
    session.get = changed
    GooglePort(session, c).source_check(w.source)
    assert len(session.calls) == 3  # Worker cannot inspect ACL changes.
    with pytest.raises(StateError, match='acl_or_identity_changed'):
        owner_metadata(session, c, fid, 'reader')


def test_credential_binding_mismatch_stops_before_token_or_resource_io(monkeypatch):
    from google.oauth2 import service_account
    _, c, _, _, _ = resource('sheet')
    def deny(*a, **kw): raise AssertionError('must stop before credential setup')
    monkeypatch.setattr(service_account.Credentials, 'from_service_account_info', deny)
    with pytest.raises(StateError, match='service_account_mismatch'):
        with connect(c, dict(client_email='wrong@synthetic.invalid')): raise AssertionError('yielded')


@pytest.mark.parametrize('sheet_role', ['writer', 'reader'])
def test_real_connect_checks_identity_while_owner_checks_roles_with_unchanged_scopes(monkeypatch, sheet_role):
    import requests
    from google.auth.transport import requests as transport
    from google.oauth2 import service_account, _client
    w, c, session, _, _ = resource('sheet')
    session.meta[c['spreadsheet_id']]['permissions'][1]['role'] = sheet_role
    observed = {}
    class Credentials:
        def _make_authorization_grant_assertion(self): return 'SYNTHETIC_ASSERTION'
    def from_info(info, scopes):
        assert info['client_email'] == c['service_email']
        observed['scopes'] = scopes
        return Credentials()
    class Auth:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def close(self): pass
        def request(self, *a, **kw): raise AssertionError('real auth forbidden')
    def jwt(*a, **kw):
        assert a[1] == 'https://oauth2.googleapis.com/token' and kw == {'can_retry': False}
        return 'SYNTHETIC_TOKEN', None, None
    monkeypatch.setattr(service_account.Credentials, 'from_service_account_info', from_info)
    monkeypatch.setattr(requests, 'Session', Auth)
    monkeypatch.setattr(_client, 'jwt_grant', jwt)
    monkeypatch.setattr(transport, 'AuthorizedSession', lambda *a, **kw: session)
    if sheet_role == 'reader':
        with pytest.raises(StateError, match='acl_or_identity_changed'):
            check_owner_metadata(c['spreadsheet_id'], session.meta[c['spreadsheet_id']], c,
                worker_role='writer', mime_type='application/vnd.google-apps.spreadsheet')
    with connect(c, dict(client_email=c['service_email'])) as port:
        port.source_check(w.source)
    assert len(session.calls) == 4
    assert observed['scopes'] == ['https://www.googleapis.com/auth/drive.readonly',
                                 'https://www.googleapis.com/auth/spreadsheets']


@pytest.mark.parametrize('role', ['commenter', 'owner'])
def test_unexpected_acl_policy_role_stops_before_get(role):
    _, c, session, fid, _ = resource('sheet')
    with pytest.raises(StateError, match='metadata_role_invalid'):
        owner_metadata(session, c, fid, role)
    assert session.calls == []


@pytest.mark.parametrize('asset', ['sheet', 'png'])
def test_unavailable_full_permission_list_is_not_treated_as_minimum_acl(asset):
    # Drive files.permissions is only populated for users who can share.
    # A reader response with omitted ACL must not become an implicit success.
    _, c, session, fid, role = resource(asset)
    session.meta[fid].pop('permissions')
    with pytest.raises(StateError, match='acl_or_identity_changed'):
        owner_metadata(session, c, fid, role)
