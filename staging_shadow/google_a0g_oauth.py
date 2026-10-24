"""Separately approved, single-attempt authorization-code exchange adapter.

No browser/server/credential discovery, refresh, revoke, files or CLI. This
adapter is NOT a data transport. Preparing/testing it never contacts OAuth.
"""
from http.client import HTTPSConnection
import json
import threading
import time
from urllib.parse import urlencode

from app.drive_run_state import StateError
from .google_a0g import (
    LIMITS, Response, _bounded_wire, _json_pairs, _json_constant, accept_exchange_result,
    authorization_parameters, check_approval, fail, validate_callback, validate_contract,
)

TOKEN_URL = 'https://oauth2.googleapis.com/token'


def _token_post_direct(params, *, timeout, max_bytes):
    if set(params) != {'client_id', 'client_secret', 'code', 'code_verifier', 'redirect_uri', 'grant_type'}:
        fail('exchange_fields')
    if params['grant_type'] != 'authorization_code':
        fail('refresh_forbidden')
    connection = HTTPSConnection('oauth2.googleapis.com', timeout=timeout)
    try:
        connection.request('POST', '/token', body=urlencode(params).encode(),
            headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'})
        response = connection.getresponse()
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            fail('response_limit')
        return Response(response.status, body)
    finally:
        connection.close()


def _token_worker(pipe, url, params, timeout, max_bytes):
    try:
        if url != TOKEN_URL:
            fail('exchange_endpoint')
        response = _token_post_direct(params, timeout=timeout, max_bytes=max_bytes)
        pipe.send((response.status, response.body))
    except Exception:
        pipe.send(None)
    finally:
        pipe.close()


def token_post(params, *, timeout, max_bytes):
    return _bounded_wire(_token_worker, TOKEN_URL, params, timeout=timeout, max_bytes=max_bytes)


class ApprovedCodeExchange:
    """Memory-only one-attempt handoff. Human approval is enforced by caller.

    A fresh instance must never be used to retry an uncertain exchange. Stop
    and review/re-authorize separately instead. No token is persisted here.
    """
    def __init__(self, contract, *, expected_sha, approval, enabled=False,
                 sender=token_post, wall_clock=time.time, monotonic=time.monotonic):
        if enabled is not True:
            fail('disabled')
        self._contract = validate_contract(contract, expected_sha=expected_sha)
        self._sha, self._approval = expected_sha, approval
        self._wall, self._mono, self._sender = wall_clock, monotonic, sender
        self._start = monotonic(); self._attempted = False; self._lock = threading.Lock()
        check_approval(self._contract, expected_sha, approval, wall_clock())

    def exchange(self, *, callback, expected_state, code_verifier, redirect_uri, client_secret):
        with self._lock:
            try:
                if self._attempted:
                    fail('exchange_attempt_consumed')
                self._attempted = True
                now = self._wall()
                check_approval(self._contract, self._sha, self._approval, now)
                params = authorization_parameters(self._contract, expected_sha=self._sha,
                    approval=self._approval, state=expected_state, code_verifier=code_verifier,
                    redirect_uri=redirect_uri, now=now)
                if type(callback) is not dict:
                    fail('invalid_callback')
                validate_callback(self._contract, expected_sha=self._sha, expected_state=expected_state,
                    returned_state=callback.get('state'), picked_file_ids=callback.get('picked_file_ids'),
                    scopes=callback.get('scopes'), error=callback.get('error'))
                code = callback.get('code')
                if (not isinstance(code, str) or not 1 <= len(code) <= 4096 or
                        any(ch.isspace() for ch in code) or not code.isascii() or
                        not isinstance(client_secret, str) or not client_secret or
                        any(ch.isspace() for ch in client_secret)):
                    fail('exchange_input')
                remaining = LIMITS['process_seconds'] - (self._mono() - self._start)
                timeout = min(LIMITS['http_seconds'], remaining, self._approval.expires_at - now)
                if timeout <= 0:
                    fail('exchange_deadline')
                request = dict(client_id=params['client_id'], client_secret=client_secret,
                    code=code, code_verifier=code_verifier, redirect_uri=redirect_uri,
                    grant_type='authorization_code')
                response = self._sender(request, timeout=timeout, max_bytes=65_536)
                if (not isinstance(response, Response) or response.status != 200 or
                        type(response.body) is not bytes or len(response.body) > 65_536):
                    fail('exchange_denied_or_unavailable')
                if self._mono() - self._start >= LIMITS['process_seconds']:
                    fail('exchange_deadline')
                value = json.loads(response.body, object_pairs_hook=_json_pairs, parse_constant=_json_constant)
                return accept_exchange_result(self._contract, expected_sha=self._sha,
                    approval=self._approval, actual_client_id=request['client_id'],
                    response=value, now=self._wall())
            except StateError:
                raise
            except Exception:
                fail('exchange_unavailable')
