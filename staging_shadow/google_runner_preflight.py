"""Read-only preparation for #161; no auth, token, CAS or write factory.

Accept an already authorized, bounded SDK service after separate approval.
Only fixed-ID metadata GETs are issued. Successful diagnostics NEVER enable
the runner: Firestore effective isolation and write custody need other gates.
"""
from copy import deepcopy
import re

from app.drive_run_state import StateError
from .persistent_registration import DriveCheckpointSlots, Slot

SCHEMA = 'synthetic-google-runner-preflight-v1'
DRIVE_FILE = 'https://www.googleapis.com/auth/drive.file'
FOLDER = 'application/vnd.google-apps.folder'
BOOK = 'application/vnd.google-apps.spreadsheet'
MIN_SLOTS = 8  # initial + up to five changed checkpoints, with two spare slots
MAX_SLOTS = 128
FIELDS = ('id,name,mimeType,parents,trashed,shared,driveId,size,version,sha256Checksum,'
          'owners(permissionId,emailAddress),capabilities(canEdit,canDownload)')


def fail(code):raise StateError('google_runner_preflight_'+code)


def identity(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',value):
        fail('fixed_id_required')
    return value


def validate_manifest(value, *, expected_sha):
    """Pure validation. Private manifests cannot contain credentials or flags
    that turn a metadata audit into permission to dispatch a worker.
    """
    m = deepcopy(value)
    try:
        if set(m) != {'schema','candidate_sha','owner_email','oauth_client_id','book','sources','storage','firestore'}:
            fail('manifest_fields')
        if (m['schema'] != SCHEMA or not re.fullmatch('[a-f0-9]{40}',expected_sha)
                or m['candidate_sha'] != expected_sha):fail('candidate_sha_mismatch')
        if not isinstance(m['owner_email'],str) or not re.fullmatch(r'[^\s@]+@[^\s@]+',m['owner_email']):
            fail('owner_required')
        if not isinstance(m['oauth_client_id'],str) or not re.fullmatch(r'[0-9]+-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com',m['oauth_client_id']):
            fail('dedicated_oauth_client_required')
        b = m['book']
        if set(b) != {'file_id','parent_id','sheet_id'} or type(b['sheet_id']) is not int or b['sheet_id'] < 0:
            fail('book_binding')
        files = [identity(b['file_id'])]; parents = [identity(b['parent_id'])]
        cases = []
        if not isinstance(m['sources'],list) or not 1 <= len(m['sources']) <= 8:fail('source_allowlist')
        for s in m['sources']:
            if (set(s) != {'case_id','file_id','parent_id','mime_type','sha256','version'}
                    or not re.fullmatch(r'SYN-[GMP][0-9]{3}',s['case_id'])
                    or s['mime_type'] not in {'image/png','application/pdf'}):fail('source_binding')
            for name, pattern in (('sha256','[a-f0-9]{64}'),('version','[0-9]+')):
                if s[name] is not None and (not isinstance(s[name],str) or not re.fullmatch(pattern,s[name])):
                    fail('source_pin')
            files.append(identity(s['file_id']));parents.append(identity(s['parent_id']));cases.append(s['case_id'])
        if len(cases) != len(set(cases)):fail('duplicate_case')
        storage = m['storage']
        if storage is not None:
            if (set(storage) != {'folder_id','slot_ids'} or type(storage['slot_ids']) is not list
                    or not MIN_SLOTS <= len(storage['slot_ids']) <= MAX_SLOTS):
                fail('slot_capacity')
            folder = identity(storage['folder_id'])
            if folder in files or folder in parents:fail('dedicated_storage_required')
            files.extend(identity(i) for i in storage['slot_ids']);parents.append(folder)
        if len(files) != len(set(files)) or set(files)&set(parents):fail('overlapping_allowlist')
        f = m['firestore']
        if (set(f) != {'project_id','database_id','document_path','runtime_principal'}
                or not re.fullmatch(r'[a-z][a-z0-9-]{4,61}[a-z0-9]',f['project_id'])
                or not re.fullmatch(r'[a-z][a-z0-9-]{2,61}[a-z0-9]',f['database_id'])
                or not re.fullmatch(r'synthetic_runner_control/[A-Za-z0-9_-]{1,100}',f['document_path'])
                or not re.fullmatch(r'[A-Za-z0-9_-]+@'+re.escape(f['project_id'])+r'\.iam\.gserviceaccount\.com',f['runtime_principal'])):
            fail('explicit_synthetic_firestore_binding')
    except (KeyError,TypeError,ValueError):fail('manifest_invalid')
    return m


def firestore_document_resource(manifest, *, expected_sha):
    f = validate_manifest(manifest,expected_sha=expected_sha)['firestore']
    # Never fall back to ADC's project or the SDK's (default) database.
    return 'projects/'+f['project_id']+'/databases/'+f['database_id']+'/documents/'+f['document_path']


class ReadOnlyPreflight:
    def __init__(self, drive, sheets):self.drive,self.sheets = drive,sheets

    def _file(self, file_id, parent, mime, owner, *, editable=False):
        meta = self.drive.files().get(fileId=file_id,supportsAllDrives=True,fields=FIELDS).execute(num_retries=0)
        acl = self.drive.permissions().list(fileId=file_id,supportsAllDrives=True,
            fields='nextPageToken,permissions(id,type,role,deleted)').execute(num_retries=0)
        if (meta.get('id') != file_id or meta.get('parents') != [parent]
                or meta.get('mimeType') != mime or meta.get('trashed') is not False
                or meta.get('shared') is not False or meta.get('driveId')
                or len(meta.get('owners',[])) != 1
                or meta['owners'][0].get('permissionId') != owner['permissionId']
                or meta['owners'][0].get('emailAddress') != owner['emailAddress']
                or acl.get('nextPageToken') or len(acl.get('permissions',[])) != 1):
            fail('private_target_mismatch')
        p = acl['permissions'][0]
        if (p.get('id') != owner['permissionId'] or p.get('type') != 'user'
                or p.get('role') != 'owner' or p.get('deleted',False) is not False
                or editable and meta.get('capabilities',{}).get('canEdit') is not True):
            fail('private_target_mismatch')
        return meta

    def run(self, manifest, *, expected_sha, granted_scopes, actual_client_id, enabled=False):
        if enabled is not True:return dict(status='DISABLED',runner_write_allowed=False)
        m = validate_manifest(manifest,expected_sha=expected_sha)
        if actual_client_id != m['oauth_client_id']:fail('oauth_client_changed')
        # Desktop/mobile onepick cannot combine drive.file with OIDC scopes.
        # Identity comes from Drive about.user; never from Sheet cell values.
        if set(granted_scopes) != {DRIVE_FILE}:fail('unexpected_oauth_scopes')
        try:
            about = self.drive.about().get(fields='user(permissionId,emailAddress),storageQuota(limit,usage)').execute(num_retries=0)
            owner = about['user']
            if not owner.get('permissionId') or owner.get('emailAddress') != m['owner_email']:
                fail('connected_owner_changed')
            b = m['book']
            self._file(b['file_id'],b['parent_id'],BOOK,owner,editable=True)
            sheet = self.sheets.spreadsheets().get(spreadsheetId=b['file_id'],includeGridData=False,
                fields='spreadsheetId,sheets.properties(sheetId,title)').execute(num_retries=0)
            if (sheet.get('spreadsheetId') != b['file_id'] or len(sheet.get('sheets',[])) != 1
                    or sheet['sheets'][0]['properties'] != dict(sheetId=b['sheet_id'],title='確認')):
                fail('sheet_structure_changed')
            observations = []; blockers = ['FIRESTORE_EFFECTIVE_ISOLATION_UNPROVEN','PERSISTENCE_WRITE_TRIAL_REQUIRED']
            for s in m['sources']:
                meta = self._file(s['file_id'],s['parent_id'],s['mime_type'],owner)
                current = dict(case_id=s['case_id'],file_id=s['file_id'],sha256=meta.get('sha256Checksum'),version=meta.get('version'))
                observations.append(current)
                if s['sha256'] is None or s['version'] is None:
                    blockers.append('SOURCE_PIN_REVIEW_REQUIRED')
                elif (s['sha256'],s['version']) != (current['sha256'],current['version']):
                    fail('source_revision_changed')
                if not current['sha256'] or not current['version']:blockers.append('RAW_SOURCE_METADATA_REQUIRED')
            if m['storage'] is None:blockers.append('STORAGE_NOT_PROVISIONED')
            else:
                storage = m['storage']
                slots = DriveCheckpointSlots(self.drive,
                    [Slot(i,storage['folder_id']) for i in storage['slot_ids']],owner_permission_id=owner['permissionId'])
                for n in range(len(storage['slot_ids'])):
                    transport = slots._transport(n)  # existing private/parent binding checks; NO media read
                    meta = transport._metadata()
                    if meta.get('capabilities',{}).get('canEdit') is not True:fail('slot_not_editable')
            return dict(status='METADATA_VERIFIED_NO_GO',runner_write_allowed=False,
                blockers=sorted(set(blockers)),observations=observations,
                storage_quota=about.get('storageQuota'),firestore_resource=firestore_document_resource(m,expected_sha=expected_sha))
        except StateError:raise
        except Exception:fail('metadata_unavailable_or_revoked')
