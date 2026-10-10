"""Bounded current-page metadata in existing private Drive, strong If-Match.

Only the current registry is read during daily intake. Terminal proofs belong
in annual metadata partitions before removal. No create/delete/ACL mutation,
OAuth/session state, PDF/image bytes, or raw model response is supported here.
"""
from copy import deepcopy
import json
import re
from time import monotonic

from .drive_run_state import StateError
from .pdf_intake_authority import MAX_BYTES, MAX_ACTIVE_PAGES, canonical, digest, page_key, validate_page

SCHEMA = 'pdf-intake-current-v1'
TERMINAL = frozenset({'imported', 'reconciled_existing', 'medical_manual_imported',
                      'duplicate_confirmed', 'intentionally_skipped'})


def strong_etag(value):
    return isinstance(value, str) and re.fullmatch(r'"[^"\r\n]+"', value) is not None


def empty_registry(anchor):
    return {'schema': SCHEMA, 'anchor': anchor, 'generation': 0, 'pages': {}}


def validate(value, anchor):
    try:
        if (set(value) != {'schema', 'anchor', 'generation', 'pages'} or value['schema'] != SCHEMA
                or value['anchor'] != anchor or type(value['generation']) is not int or value['generation'] < 0
                or not isinstance(value['pages'], dict) or len(value['pages']) > MAX_ACTIVE_PAGES
                or len(canonical(value)) > MAX_BYTES):
            raise ValueError()
        for key, record in value['pages'].items():
            if (set(record) != {'page', 'status', 'authority', 'units', 'analysis'}
                    or page_key(record['page']) != key
                    or not isinstance(record['status'], str) or len(record['status']) > 80
                    or not isinstance(record['units'], dict) or len(record['units']) > 20
                    or not isinstance(record['authority'], dict)
                    or set(record['authority']) - {'single_page_ai'}
                    or not isinstance(record['analysis'], dict)
                    or set(record['analysis']) - {'manifest', 'drafts', 'payload_hash', 'analyzed_at', 'expires_at'}):
                raise ValueError()
            for uid, unit in record['units'].items():
                if (not uid.startswith('page-receipt-v1:') or len(uid) != 80
                        or set(unit) != {'status', 'candidate', 'posting_authority', 'intent', 'readback'}
                        or not isinstance(unit['status'], str) or len(unit['status']) > 80
                        or not isinstance(unit['candidate'], dict)
                        or unit['posting_authority'] is not None and not isinstance(unit['posting_authority'], dict)
                        or unit['intent'] is not None and not isinstance(unit['intent'], dict)
                        or unit['readback'] is not None and not isinstance(unit['readback'], dict)):
                    raise ValueError()
            # Prevent generic envelope storage from becoming a secret/raw blob.
            forbidden = {'access_token', 'refresh_token', 'id_token', 'cookie', 'nonce', 'pkce_verifier',
                         'client_secret', 'private_key', 'raw_response', 'pdf_bytes', 'image_bytes'}
            def inspect(node):
                if isinstance(node, dict):
                    if set(node) & forbidden: raise ValueError()
                    for child in node.values(): inspect(child)
                elif isinstance(node, (list, tuple)):
                    for child in node: inspect(child)
                elif not isinstance(node, (str, int, float, bool, type(None))):
                    raise ValueError()
            inspect(record)
    except (TypeError, ValueError, KeyError, StateError):
        raise StateError('pdf_intake_registry_invalid') from None
    return deepcopy(value)


class RegistryStore:
    def __init__(self, io, anchor):
        self.io, self.anchor = io, anchor
        self.payload = self.tag = None

    def load(self):
        payload, tag = self.io.read_versioned()
        if type(payload) is not bytes or len(payload) > MAX_BYTES or not strong_etag(tag):
            raise StateError('pdf_intake_registry_unavailable')
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError()
                result[key] = value
            return result
        try: value = validate(json.loads(payload, object_pairs_hook=unique), self.anchor)
        except (ValueError, TypeError): raise StateError('pdf_intake_registry_invalid') from None
        self.payload, self.tag = payload, tag
        return value

    def save(self, value):
        if self.payload is None:
            raise StateError('pdf_intake_registry_fresh_read_required')
        before = validate(json.loads(self.payload), self.anchor)
        value = validate(value, self.anchor)
        if value == before: return False
        if value['generation'] != before['generation'] + 1:
            raise StateError('pdf_intake_registry_generation_invalid')
        proposed = canonical(value)
        self.io.replace_versioned(self.payload, self.tag, proposed)
        readback, tag = self.io.read_versioned()
        if readback != proposed or not strong_etag(tag):
            raise StateError('pdf_intake_registry_write_outcome_unknown')
        self.payload, self.tag = readback, tag
        return True

    def register(self, page):
        page = validate_page(page); key = page_key(page); current = self.load()
        if key in current['pages']:
            if current['pages'][key]['page'] != page:
                raise StateError('pdf_intake_registered_page_changed')
            return key  # replay is read-only; no generation/bytes change.
        if len(current['pages']) >= MAX_ACTIVE_PAGES:
            raise StateError('pdf_intake_active_page_limit')
        # A changed source with same ID must not coexist as new work.
        if any(r['page']['source']['source_file_id'] == page['source']['source_file_id']
               and r['page']['source'] != page['source'] for r in current['pages'].values()):
            raise StateError('pdf_intake_source_changed')
        current['pages'][key] = {'page': page, 'status': 'observed', 'authority': {}, 'units': {}, 'analysis': {}}
        current['generation'] += 1
        self.save(current)
        return key


class DriveRegistryIO:
    """Same owner + existing service-account ACL; no Firestore capability.

    A configured registry is an anchor, not a browser-supplied Drive ID.
    Read-only callers cannot PUT even if their credential has broader scopes.
    """
    def __init__(self, config, info, *, session=None, writable=False):
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
        self.config, self.sa, self.writable = deepcopy(config), info['client_email'], writable
        required = {'file_id', 'folder_id', 'inbox_id', 'processed_id', 'owner_digest'}
        if (set(config) != required or any(not re.fullmatch('[A-Za-z0-9_-]{10,150}', config[k])
                                          for k in required - {'owner_digest'})
                or not re.fullmatch('[0-9a-f]{64}', config['owner_digest'])
                or len({config[k] for k in required - {'owner_digest'}}) != 4):
            raise StateError('pdf_intake_registry_configuration_invalid')
        scope = 'https://www.googleapis.com/auth/drive' if writable else 'https://www.googleapis.com/auth/drive.readonly'
        self.http = session or AuthorizedSession(service_account.Credentials.from_service_account_info(info, scopes=[scope]))
        self.anchor = digest([SCHEMA, config])
        self.deadline = None

    def begin_request(self): self.deadline = monotonic() + 45
    def end_request(self): self.deadline = None

    def _request(self, method, url, **kwargs):
        remaining = 20 if self.deadline is None else min(20, self.deadline-monotonic())
        if remaining <= 0: raise StateError('pdf_intake_drive_deadline')
        try: response = self.http.request(method, url, timeout=remaining, allow_redirects=False, **kwargs)
        except Exception: raise StateError('pdf_intake_drive_unavailable') from None
        if response.status_code == 412: raise StateError('HTTP_412')
        if response.status_code != 200: raise StateError('pdf_intake_drive_unavailable')
        if len(response.content) > 50 * 1024 * 1024: raise StateError('pdf_intake_drive_size_limit')
        return response

    def metadata(self, fid, fields):
        return self._request('GET', 'https://www.googleapis.com/drive/v2/files/'+fid,
                             params={'fields':fields}).json()

    def acl(self):
        for fid in (self.config['folder_id'], self.config['file_id']):
            meta = self.metadata(fid, 'id,owners(emailAddress),permissions(type,role,emailAddress,deleted)')
            owners = [x.get('emailAddress') for x in meta.get('owners', [])]
            if (meta.get('id') != fid or len(owners) != 1 or digest(owners[0]) != self.config['owner_digest']
                    or {(p.get('type'),p.get('role'),p.get('emailAddress')) for p in meta.get('permissions', [])
                        if not p.get('deleted')} != {('user','owner',owners[0]),('user','writer',self.sa)}):
                raise StateError('pdf_intake_private_acl_changed')

    def _meta(self):
        fid = self.config['file_id']; value = self.metadata(fid, 'id,etag,parents(id),labels(trashed),mimeType')
        if (value.get('id') != fid or not strong_etag(value.get('etag'))
                or value.get('labels',{}).get('trashed') or value.get('mimeType') != 'application/json'
                or value.get('parents') != [{'id': self.config['folder_id']}]):
            raise StateError('pdf_intake_registry_moved_or_changed')
        return value

    def read_versioned(self):
        self.acl(); before = self._meta()
        raw = self._request('GET', 'https://www.googleapis.com/drive/v2/files/'+self.config['file_id'],
                            params={'alt':'media'}).content
        if len(raw) > MAX_BYTES or self._meta() != before:
            raise StateError('pdf_intake_registry_changed_during_read')
        return raw, before['etag']

    def replace_versioned(self, before, tag, after):
        if not self.writable or not strong_etag(tag) or type(after) is not bytes or len(after) > MAX_BYTES:
            raise StateError('pdf_intake_conditional_write_forbidden')
        raw, fresh_tag = self.read_versioned()
        if raw != before or fresh_tag != tag: raise StateError('HTTP_412')
        self._request('PUT', 'https://www.googleapis.com/upload/drive/v2/files/'+self.config['file_id'],
                      params={'uploadType':'media','fields':'id,etag'}, data=after,
                      headers={'If-Match':tag,'Content-Type':'application/json'})
