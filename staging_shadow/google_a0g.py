"""A0-G's six metadata GETs, with an explicit credential/approval handoff.

No OAuth launch/exchange/refresh/revoke, credential discovery, storage, CLI or
runner factory. Importing this module performs no I/O. Real use needs separate
human approval and a reviewed, short-lived access grant. Tests use synthetic
grants and deny all live sockets.
"""
from copy import deepcopy
from base64 import urlsafe_b64encode
from dataclasses import dataclass, field
from hashlib import sha256
import hmac
from http.client import HTTPSConnection
import json
import math
import multiprocessing
import re
import threading
import time
from urllib.parse import urlencode, urlsplit

from app.drive_run_state import StateError
from .google_runner_preflight import (
    BOOK, DRIVE_FILE, FIELDS, ReadOnlyPreflight, validate_manifest,
)

SCHEMA = 'synthetic-a0g-six-get-v1'
ABOUT_FIELDS = 'user(permissionId,emailAddress),storageQuota(limit,usage)'
ACL_FIELDS = 'nextPageToken,permissions(id,type,role,deleted)'
SHEET_FIELDS = 'spreadsheetId,sheets.properties(sheetId,title)'
LIMITS = dict(max_requests=6, http_seconds=10, process_seconds=180,
              response_bytes=1_048_576, retries=0, redirects=False,
              refresh=False, adc=False, runner_write_allowed=False)


def fail(code):
    raise StateError('a0g_' + code) from None


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def validate_contract(value, *, expected_sha):
    """Pure fixed contract validation; does not turn preparation into approval."""
    try:
        c = deepcopy(value)
        if set(c) != {'schema', 'preflight', 'oauth', 'book_version', 'limits', 'approved'}:
            fail('contract_fields')
        if c['schema'] != SCHEMA or c['approved'] is not False:
            fail('preparation_is_not_approval')
        m = validate_manifest(c['preflight'], expected_sha=expected_sha)
        if m['storage'] is not None or len(m['sources']) != 1:
            fail('two_files_only_no_storage')
        if m['sources'][0]['case_id'] != 'SYN-G101' or m['sources'][0]['mime_type'] != 'image/png':
            fail('initial_source_only')
        if c['oauth'] != dict(client_type='Desktop', audience='External',
                              publishing_status='Testing', scopes=[DRIVE_FILE]):
            fail('oauth_contract')
        if canonical(c['limits']) != canonical(LIMITS):
            fail('limits_changed')
        version = c['book_version']
        if version is not None and (not isinstance(version, str) or not version.isdigit()):
            fail('book_revision')
        c['preflight'] = m
        return c
    except StateError:
        raise
    except (TypeError, ValueError, KeyError, AttributeError):
        fail('contract_invalid')


def contract_digest(contract, *, expected_sha):
    return sha256(canonical(validate_contract(contract, expected_sha=expected_sha)).encode()).hexdigest()


def request_plan(contract, *, expected_sha):
    c = validate_contract(contract, expected_sha=expected_sha)
    m = c['preflight']; book = m['book']['file_id']; source = m['sources'][0]['file_id']
    file_query = dict(fields=FIELDS, supportsAllDrives=True)
    acl_query = dict(fields=ACL_FIELDS, supportsAllDrives=True)
    return (
        ('https://www.googleapis.com/drive/v3/about', dict(fields=ABOUT_FIELDS)),
        ('https://www.googleapis.com/drive/v3/files/' + book, file_query),
        ('https://www.googleapis.com/drive/v3/files/' + book + '/permissions', acl_query),
        ('https://sheets.googleapis.com/v4/spreadsheets/' + book,
         dict(fields=SHEET_FIELDS, includeGridData=False)),
        ('https://www.googleapis.com/drive/v3/files/' + source, deepcopy(file_query)),
        ('https://www.googleapis.com/drive/v3/files/' + source + '/permissions', deepcopy(acl_query)),
    )


@dataclass(frozen=True)
class ApprovedAttempt:
    """Operator handoff, NOT proof that human authorization has been obtained.

    The calling approval system must establish that fact before creating this.
    Bound to the reviewed source and exact private contract, with a deadline.
    """
    candidate_sha: str
    contract_digest: str
    expires_at: float
    oauth_authorization: bool = False
    token_exchange: bool = False
    refresh_token_issuance: bool = False
    metadata_get: bool = False


@dataclass(frozen=True)
class AccessGrant:
    access_token: str = field(repr=False)
    client_id: str = field(repr=False)
    scopes: tuple
    expires_at: float

    def close(self):
        # Release this reference, NOT a claim of secure Python/OS RAM erasure.
        object.__setattr__(self, 'access_token', '')


def valid_deadline(value, now):
    return type(value) in (int, float) and math.isfinite(value) and value > now


def check_approval(contract, expected_sha, approval, now, *, metadata=False):
    if not isinstance(approval, ApprovedAttempt):
        fail('separate_approval_required')
    if (approval.candidate_sha != expected_sha or
            approval.contract_digest != contract_digest(contract, expected_sha=expected_sha) or
            not valid_deadline(approval.expires_at, now)):
        fail('approval_binding_or_expiry')
    flags = [approval.oauth_authorization, approval.token_exchange, approval.refresh_token_issuance]
    if metadata:
        flags.append(approval.metadata_get)
    if any(flag is not True for flag in flags):
        fail('separate_approval_required')


def validate_callback(contract, *, expected_sha, expected_state, returned_state,
                      picked_file_ids, scopes, error=None):
    """Pure callback checks. Does not accept/exchange an authorization code."""
    c = validate_contract(contract, expected_sha=expected_sha)
    m = c['preflight']
    if error is not None:
        fail('authorization_denied')
    if (not isinstance(expected_state, str) or len(expected_state) < 32 or
            not isinstance(returned_state, str) or
            not hmac.compare_digest(expected_state.encode(), returned_state.encode())):
        fail('callback_state')
    targets = [m['book']['file_id'], m['sources'][0]['file_id']]
    if (type(picked_file_ids) not in (list, tuple) or
            any(not isinstance(v, str) for v in picked_file_ids) or
            len(picked_file_ids) != 2 or set(picked_file_ids) != set(targets)):
        fail('picked_file_ids')
    if type(scopes) not in (list, tuple) or tuple(scopes) != (DRIVE_FILE,):
        fail('callback_scopes')
    return 'CALLBACK_BOUND_NO_TOKEN_EXCHANGE'


def authorization_parameters(contract, *, expected_sha, approval, state,
                             code_verifier, redirect_uri, now):
    """Pure future request plan; does NOT open a browser or contact OAuth."""
    c = validate_contract(contract, expected_sha=expected_sha)
    check_approval(c, expected_sha, approval, now)
    try:
        if not isinstance(redirect_uri, str):
            fail('loopback_redirect_required')
        u = urlsplit(redirect_uri)
        port = u.port
    except ValueError:
        fail('loopback_redirect_required')
    if (u.scheme != 'http' or u.hostname != '127.0.0.1' or u.username or u.password or
            u.query or u.fragment or u.path != '/oauth2callback' or
            port is None or not 1024 <= port <= 65535):
        fail('loopback_redirect_required')
    if not isinstance(state, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', state):
        fail('state_required')
    if not isinstance(code_verifier, str) or not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', code_verifier):
        fail('pkce_required')
    m = c['preflight']
    return dict(client_id=m['oauth_client_id'], scope=DRIVE_FILE, redirect_uri=redirect_uri,
        response_type='code', access_type='offline', prompt='consent', trigger_onepick='true',
        allow_multiple='true', allow_folder_selection='false', include_granted_scopes='false',
        file_ids=','.join([m['book']['file_id'], m['sources'][0]['file_id']]), state=state,
        code_challenge=urlsafe_b64encode(sha256(code_verifier.encode()).digest()).rstrip(b'=').decode(),
        code_challenge_method='S256')


def _json_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            fail('invalid_metadata')
        value[key] = item
    return value


def _json_constant(_value):
    fail('invalid_metadata')


def accept_exchange_result(contract, *, expected_sha, approval, actual_client_id,
                           response, now):
    """Pure validation of a future approved exchange result; no HTTP or save.

    The actual client ID must come from the reviewed OAuth request composition,
    not from a user-controlled Sheet or merely the manifest. Refresh tokens are
    not returned to the data transport; their issuance still needs approval.
    """
    c = validate_contract(contract, expected_sha=expected_sha)
    check_approval(c, expected_sha, approval, now)
    if actual_client_id != c['preflight']['oauth_client_id']:
        fail('oauth_client_changed')
    if not isinstance(response, dict) or 'error' in response:
        fail('exchange_denied_or_invalid_grant')
    if response.get('token_type') != 'Bearer' or response.get('scope') != DRIVE_FILE:
        fail('exchange_scope_or_type')
    token, lifetime = response.get('access_token'), response.get('expires_in')
    if (not isinstance(token, str) or not token or any(ch.isspace() for ch in token) or
            type(lifetime) not in (int, float) or not math.isfinite(lifetime) or lifetime <= 0):
        fail('exchange_token_or_expiry')
    return AccessGrant(token, actual_client_id, (DRIVE_FILE,), now + lifetime)


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes = field(repr=False)


def _https_get_direct(url, headers, *, timeout, max_bytes):
    """Wire primitive in a disposable worker. GET is literal, no redirects.

    Called only behind GuardedMetadataHTTP. No SDK discovery, ambient auth,
    proxies, OAuth refresh, logging or response/credential persistence.
    """
    u = urlsplit(url)
    if u.scheme != 'https' or u.hostname not in {'www.googleapis.com', 'sheets.googleapis.com'} or u.port is not None:
        fail('wire_target')
    connection = HTTPSConnection(u.hostname, timeout=timeout)
    try:
        connection.request('GET', u.path + ('?' + u.query if u.query else ''), headers=headers)
        result = connection.getresponse()
        body = result.read(max_bytes + 1)
        if len(body) > max_bytes:
            fail('response_limit')
        return Response(result.status, body)
    finally:
        connection.close()


def _wire_worker(pipe, url, headers, timeout, max_bytes):
    try:
        response = _https_get_direct(url, headers, timeout=timeout, max_bytes=max_bytes)
        pipe.send((response.status, response.body))
    except Exception:
        pipe.send(None)  # Never send SDK errors, credentials or URLs to logs.
    finally:
        pipe.close()


def _bounded_wire(worker, url, headers, *, timeout, max_bytes):
    """Parent watchdog also bounds DNS/TLS and multiple blocking socket reads."""
    context = multiprocessing.get_context('spawn')
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(target=worker, args=(sending, url, headers, timeout, max_bytes), daemon=True)
    deadline = time.monotonic() + timeout
    try:
        process.start()
        sending.close()
        if not receiving.poll(max(0, deadline - time.monotonic())):
            fail('http_deadline')
        result = receiving.recv()
        if time.monotonic() >= deadline:
            fail('http_deadline')
        if result is None:
            fail('transport_unavailable')
        status, body = result
        return Response(status, body)
    finally:
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
            process.close()
        receiving.close()
        sending.close()


def https_get(url, headers, *, timeout, max_bytes):
    return _bounded_wire(_wire_worker, url, headers, timeout=timeout, max_bytes=max_bytes)


class GuardedMetadataHTTP:
    """Single-use, ordered six-request session; every failure latches stopped."""
    def __init__(self, contract, *, expected_sha, approval, grant, enabled=False,
                 sender=https_get, wall_clock=time.time, monotonic=time.monotonic):
        self._stopped = False; self._index = 0; self._lock = threading.Lock()
        self._wall, self._mono, self._sender = wall_clock, monotonic, sender
        self._start = monotonic()
        self._contract = validate_contract(contract, expected_sha=expected_sha)
        self._sha, self._approval, self._grant = expected_sha, approval, grant
        self._plan = request_plan(self._contract, expected_sha=expected_sha)
        self._responses = []
        if enabled is not True:
            fail('disabled')
        try:
            self._check_handoff()
            if sender is https_get:
                fail('durable_local_entry_required')
        except BaseException:
            self.close()
            raise

    def close(self):
        self._stopped = True
        if isinstance(self._grant, AccessGrant): self._grant.close()
        self._grant = None
        self._responses.clear()
        self._sender = None

    def _check_handoff(self):
        now = self._wall()
        check_approval(self._contract, self._sha, self._approval, now, metadata=True)
        g = self._grant
        if (not isinstance(g, AccessGrant) or g.client_id != self._contract['preflight']['oauth_client_id'] or
                type(g.scopes) is not tuple or g.scopes != (DRIVE_FILE,) or
                not isinstance(g.access_token, str) or not g.access_token or
                any(ch.isspace() for ch in g.access_token) or
                not valid_deadline(g.expires_at, now)):
            fail('grant_binding_scope_or_expiry')

    @property
    def count(self):
        return self._index

    def request(self, method, url, query, *, body=None, headers=None):
        with self._lock:
            try:
                if self._stopped:
                    fail('session_stopped')
                self._check_handoff()
                remaining = LIMITS['process_seconds'] - (self._mono() - self._start)
                if remaining <= 0:
                    fail('process_deadline')
                if self._index >= LIMITS['max_requests']:
                    fail('request_limit')
                target, expected = self._plan[self._index]
                if (method != 'GET' or body is not None or headers is not None or
                        url != target or type(query) is not dict or canonical(query) != canonical(expected)):
                    fail('request_not_allowlisted')
                encoded = urlencode({k: str(v).lower() if type(v) is bool else v for k, v in expected.items()})
                token = self._grant.access_token
                timeout = min(LIMITS['http_seconds'], remaining,
                              self._grant.expires_at - self._wall(), self._approval.expires_at - self._wall())
                if timeout <= 0:
                    fail('grant_binding_scope_or_expiry')
                self._index += 1  # Consume attempt even if delivery is uncertain; no retry.
                result = self._sender(target + '?' + encoded,
                    {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'},
                    timeout=timeout, max_bytes=LIMITS['response_bytes'])
                if not isinstance(result, Response) or result.status != 200:
                    fail('http_denied_or_unavailable')
                if type(result.body) is not bytes or len(result.body) > LIMITS['response_bytes']:
                    fail('response_limit')
                if self._mono() - self._start >= LIMITS['process_seconds']:
                    fail('process_deadline')
                self._check_handoff()
                value = json.loads(result.body, object_pairs_hook=_json_pairs, parse_constant=_json_constant)
                if not isinstance(value, dict) or 'error' in value:
                    fail('invalid_metadata')
                pinned_book = self._contract['book_version']
                if self._index == 2 and pinned_book is not None and value.get('version') != pinned_book:
                    fail('book_revision_changed')
                self._responses.append(deepcopy(value))
                return value
            except StateError:
                self.close()
                raise
            except Exception:
                self.close()
                fail('transport_unavailable')


class _Request:
    def __init__(self, http, url, query):
        self.http, self.url, self.query = http, url, query

    def execute(self, *, num_retries=0):
        if type(num_retries) is not int or num_retries != 0:
            fail('retry_forbidden')
        return self.http.request('GET', self.url, self.query)


class _MetadataServices:
    """Only SDK shapes used by the existing preflight; no discovery or media."""
    def __init__(self, http, kind=None):
        self.http, self.kind = http, kind

    def about(self):
        return _MetadataServices(self.http, 'about')

    def files(self):
        return _MetadataServices(self.http, 'file')

    def permissions(self):
        return _MetadataServices(self.http, 'permissions')

    def spreadsheets(self):
        return _MetadataServices(self.http, 'sheet')

    def get(self, **query):
        q = deepcopy(query)
        if self.kind == 'about':
            url = 'https://www.googleapis.com/drive/v3/about'
        elif self.kind == 'file':
            url = 'https://www.googleapis.com/drive/v3/files/' + str(q.pop('fileId', ''))
        elif self.kind == 'sheet':
            url = 'https://sheets.googleapis.com/v4/spreadsheets/' + str(q.pop('spreadsheetId', ''))
        else:
            fail('sdk_operation_forbidden')
        return _Request(self.http, url, q)

    def list(self, **query):
        q = deepcopy(query)
        if self.kind != 'permissions':
            fail('sdk_operation_forbidden')
        url = 'https://www.googleapis.com/drive/v3/files/' + str(q.pop('fileId', '')) + '/permissions'
        return _Request(self.http, url, q)


def run_metadata_audit(contract, *, expected_sha, approval=None, grant=None,
                       enabled=False, sender=https_get, wall_clock=time.time,
                       monotonic=time.monotonic):
    """Reviewed composition; explicit arguments only. Returns PRIVATE metadata.

    The caller must not publish returned owner IDs/revisions/quota. No save or
    pin occurs here. Missing initial observations remain blockers, never GO.
    """
    if enabled is not True:
        return dict(status='DISABLED', runner_write_allowed=False)
    c = validate_contract(contract, expected_sha=expected_sha)
    http = GuardedMetadataHTTP(c, expected_sha=expected_sha, approval=approval,
        grant=grant, enabled=True, sender=sender, wall_clock=wall_clock, monotonic=monotonic)
    services = _MetadataServices(http)
    try:
        result = ReadOnlyPreflight(services, services).run(c['preflight'], expected_sha=expected_sha,
            actual_client_id=grant.client_id, granted_scopes=grant.scopes, enabled=True)
        if http.count != 6:
            fail('six_requests_required')
        book_version = http._responses[1].get('version')
        if not isinstance(book_version, str) or not book_version.isdigit():
            result['blockers'].append('RAW_BOOK_VERSION_REQUIRED')
        if c['book_version'] is None:
            result['blockers'].append('BOOK_VERSION_REVIEW_REQUIRED')
        elif c['book_version'] != book_version:
            fail('book_revision_changed')
        result['book_version'] = book_version
        result['blockers'] = sorted(set(result['blockers']))
        return result
    finally:
        http.close()
