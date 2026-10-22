from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.drive_run_state import StateError
from staging_shadow.google_runner_preflight import (SCHEMA,DRIVE_FILE,ReadOnlyPreflight,
    validate_manifest,firestore_document_resource)

SHA='a'*40
CLIENT='123-probe.apps.googleusercontent.com'


def manifest():
    return dict(schema=SCHEMA,candidate_sha=SHA,owner_email='owner@example.test',owner_permission_id='owner-id',oauth_client_id=CLIENT,
        book=dict(file_id='book',parent_id='inputs',sheet_id=42),
        sources=[dict(case_id='SYN-G101',file_id='png',parent_id='inputs',mime_type='image/png',sha256='b'*64,version='9')],
        storage=dict(folder_id='state',slot_ids=['slot'+str(n) for n in range(8)]),
        firestore=dict(project_id='synthetic-project',database_id='synthetic-state',
            document_path='synthetic_runner_control/one',runtime_principal='worker@synthetic-project.iam.gserviceaccount.com'))


class Request:
    def __init__(self,fn):self.fn=fn
    def execute(self,*,num_retries):assert num_retries==0;return self.fn()


class ReadOnlyServices:
    def __init__(self):self.calls=[];self.fault=None
    def files(self):return self
    def permissions(self):return self
    def about(self):return self
    def get(self, *, fields, fileId=None, supportsAllDrives=None):
        def read():
            self.calls.append(('get',fileId))
            if self.fault=='revoked':raise PermissionError('private SDK error must not escape')
            if fileId is None:
                return dict(user=dict(permissionId='owner-id',emailAddress='wrong@example.test' if self.fault=='identity' else 'owner@example.test'),
                    storageQuota=dict(limit='15000000000',usage='1000'))
            value=dict(id=fileId,parents=['inputs' if fileId in {'book','png'} else 'state'],
                mimeType='application/vnd.google-apps.spreadsheet' if fileId=='book' else 'image/png' if fileId=='png'
                    else 'application/vnd.google-apps.folder' if fileId=='state' else 'application/json',
                trashed=False,shared=False,size='0',owners=[dict(permissionId='owner-id',emailAddress='owner@example.test')],
                capabilities=dict(canEdit=True,canDownload=True),version='9',sha256Checksum='b'*64)
            if 'owners(permissionId)' in fields:value['owners']=[dict(permissionId='owner-id')]
            if fileId=='png':
                if self.fault=='pin':value['version']='10'
                if self.fault=='normalized':value.pop('sha256Checksum')
                if self.fault=='moved':value['parents']=['unapproved']
            return value
        return Request(read)
    def list(self, *, fileId, fields, supportsAllDrives):
        def read():
            self.calls.append(('permissions',fileId))
            acl=[dict(id='owner-id',type='user',role='owner')]
            if self.fault=='shared':acl.append(dict(id='public',type='anyone',role='reader'))
            return dict(permissions=acl)
        return Request(read)
    def spreadsheets(self):
        def get(**kwargs):
            assert kwargs==dict(spreadsheetId='book',includeGridData=False,fields='spreadsheetId,sheets.properties(sheetId,title)')
            def read():
                self.calls.append(('sheet-metadata','book'))
                return dict(spreadsheetId='book',sheets=[dict(properties=dict(sheetId=41 if self.fault=='sheet' else 42,title='確認'))])
            return Request(read)
        return SimpleNamespace(get=get)
    def __getattr__(self,name):raise AssertionError('unexpected service operation: '+name)


def run(s,m=None,**kwargs):
    return ReadOnlyPreflight(s,s).run(m or manifest(),expected_sha=SHA,granted_scopes=[DRIVE_FILE],
        actual_client_id=CLIENT,enabled=True,**kwargs)


def test_default_off_does_not_inspect_credentials_or_services():
    assert ReadOnlyPreflight(None,None).run({},expected_sha=None,granted_scopes=None,actual_client_id=None)==dict(status='DISABLED',runner_write_allowed=False)


def test_metadata_success_cannot_enable_worker_or_assert_iam_isolation():
    s=ReadOnlyServices();result=run(s)
    assert result['status']=='METADATA_VERIFIED_NO_GO' and result['runner_write_allowed'] is False
    assert result['blockers']==['FIRESTORE_EFFECTIVE_ISOLATION_UNPROVEN','PERSISTENCE_WRITE_TRIAL_REQUIRED']
    assert all(op in {'get','permissions','sheet-metadata'} for op,_ in s.calls)
    assert result['firestore_resource']=='projects/synthetic-project/databases/synthetic-state/documents/synthetic_runner_control/one'


@pytest.mark.parametrize('change',['sha','default_database','project_fallback','old_collection','client_missing','slot_overlap','few_slots','storage_parent','extra_flag','duplicate_source','owner_id','path'])
def test_unsafe_configuration_rejected_before_any_io(change):
    m=manifest();s=ReadOnlyServices()
    if change=='sha':m['candidate_sha']='c'*40
    elif change=='default_database':m['firestore']['database_id']='(default)'
    elif change=='project_fallback':m['firestore']['project_id']=''
    elif change=='old_collection':m['firestore']['document_path']='login_sessions/one'
    elif change=='client_missing':m['oauth_client_id']=None
    elif change=='slot_overlap':m['storage']['slot_ids'][0]='png'
    elif change=='few_slots':m['storage']['slot_ids']=m['storage']['slot_ids'][:2]
    elif change=='storage_parent':m['storage']['folder_id']='inputs'
    elif change=='extra_flag':m['write_enabled']=True
    elif change=='duplicate_source':m['sources']*=2
    elif change=='owner_id':m['owner_permission_id']='../owner'
    else:m['book']['file_id']='../book'
    with pytest.raises(StateError):run(s,m)
    assert s.calls==[]


@pytest.mark.parametrize('fault',['revoked','identity','pin','shared','moved','sheet'])
def test_fresh_identity_revision_acl_and_revocation_fail_closed(fault):
    s=ReadOnlyServices();s.fault=fault
    with pytest.raises(StateError) as error:run(s)
    assert 'private SDK' not in str(error.value)
    assert not any(x in str(error.value) for x in ('owner@example.test','unapproved'))


def test_unprovisioned_storage_and_missing_pins_are_explicit_blockers():
    m=manifest();m['storage']=None;m['sources'][0].update(sha256=None,version=None)
    s=ReadOnlyServices();s.fault='normalized'
    r=run(s,m)
    assert {'STORAGE_NOT_PROVISIONED','SOURCE_PIN_REVIEW_REQUIRED','RAW_SOURCE_METADATA_REQUIRED'}<=set(r['blockers'])
    assert r['runner_write_allowed'] is False


@pytest.mark.parametrize('scopes',[[DRIVE_FILE,'openid'],['https://www.googleapis.com/auth/drive']])
def test_onepick_scope_widening_or_oidc_combination_is_rejected(scopes):
    s=ReadOnlyServices()
    with pytest.raises(StateError):ReadOnlyPreflight(s,s).run(manifest(),expected_sha=SHA,granted_scopes=scopes,actual_client_id=CLIENT,enabled=True)
    assert s.calls==[]


def test_different_oauth_client_is_not_silently_reused():
    s=ReadOnlyServices()
    with pytest.raises(StateError):ReadOnlyPreflight(s,s).run(manifest(),expected_sha=SHA,granted_scopes=[DRIVE_FILE],actual_client_id='different',enabled=True)
    assert s.calls==[]


def test_first_owner_observation_requires_review_before_pinning():
    m=manifest();m['owner_permission_id']=None
    r=run(ReadOnlyServices(),m)
    assert r['owner_permission_id']=='owner-id'
    assert 'OWNER_PERMISSION_ID_REVIEW_REQUIRED' in r['blockers']
    assert r['runner_write_allowed'] is False


def test_same_email_cannot_hide_changed_owner_permission_id():
    m=manifest();m['owner_permission_id']='previous-owner'
    s=ReadOnlyServices()
    with pytest.raises(StateError,match='connected_owner_changed'):run(s,m)
    assert s.calls==[('get',None)]
