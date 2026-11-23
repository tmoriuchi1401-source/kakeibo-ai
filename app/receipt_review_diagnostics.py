"""PR-B private allowlisted diagnostics and a single HTTP attempt boundary.

Counts describe entries into the HTTP transport, not proof of server delivery.
Timeouts, lost replies and missing instrumentation always remain UNKNOWN.
"""
import json
import os
from pathlib import Path
import threading
import time

import httpx

# One source, one request. HTTPX/SDK units differ (seconds/milliseconds).
PRB_TIMEOUT_SECONDS = 60.0
PRB_FINISH_RESERVE_SECONDS = 75.0


class DiagnosticFailure(RuntimeError):
    pass


class SingleSendBlocked(RuntimeError):
    pass


class AnalysisUnknown(RuntimeError):
    pass


STAGES = frozenset({'runner', 'lookup', 'privacy', 'source_metadata', 'fence',
    'categories', 'analysis', 'http', 'response', 'validation', 'source_recheck',
    'candidate', 'intake', 'complete'})
RESULTS = frozenset({'STARTED', 'PASS', 'FAIL', 'UNKNOWN', 'BLOCKED'})
CLASSES = frozenset({'DiagnosticFailure', 'SingleSendBlocked', 'AnalysisUnknown',
    'ValidationError', 'ValueError', 'RuntimeError', 'TypeError', 'OSError',
    'ReadTimeout', 'ConnectTimeout', 'WriteTimeout', 'PoolTimeout', 'ReadError',
    'ConnectError', 'RemoteProtocolError', 'ClientError', 'ServerError',
    'APIError', 'APIConnectionError', 'APITimeoutError', 'ReceiptPrivacyBlocked'})
CATEGORIES = frozenset({'NONE', 'AUTH', 'QUOTA', 'SERVER', 'HTTP_ERROR',
    'TRANSPORT_UNKNOWN', 'INVALID_RESPONSE', 'VALIDATION', 'LOCAL_ERROR',
    'SEND_LIMIT', 'SDK_UNSUPPORTED', 'DIAGNOSTIC_IO', 'COUNT_UNKNOWN'})


class ReviewDiagnostics:
    def __init__(self, path, *, deadline_epoch=None):
        self.path = Path(path)
        self.ai_count = 0
        self.count_known = True
        self.intake_attempted = False
        self.failed = False
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.http_started = None
        self.deadline_epoch = deadline_epoch
        try:
            # Never truncate an earlier run, including an UNKNOWN run.
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except OSError:
            raise DiagnosticFailure() from None
        self.event('runner', 'STARTED')

    def event(self, stage, result, *, error=None, status=None, category='NONE'):
        if self.failed:
            raise DiagnosticFailure()
        if stage not in STAGES or result not in RESULTS or category not in CATEGORIES:
            raise ValueError('invalid_diagnostic_enum')
        if status is not None and (type(status) is not int or not 100 <= status <= 599):
            status = None
        name = type(error).__name__ if error is not None else None
        if name is not None and name not in CLASSES:
            name = 'OtherException'
        record = dict(stage=stage, result=result, exception_class=name,
            http_status=status, error_category=category,
            elapsed_ms=round((time.monotonic()-self.started)*1000),
            http_elapsed_ms=None if self.http_started is None else round((time.monotonic()-self.http_started)*1000),
            ai_attempted=self.ai_count > 0 if self.count_known else None,
            ai_http_attempt_count=self.ai_count if self.count_known else None,
            intake_attempted=self.intake_attempted)
        try:
            with self.path.open('a', encoding='utf-8') as out:
                out.write(json.dumps(record, separators=(',', ':')) + '\n')
                out.flush()
                os.fsync(out.fileno())
        except OSError:
            self.failed = True
            raise DiagnosticFailure() from None

    def failure(self, stage, error):
        if isinstance(error, DiagnosticFailure):
            # The failed journal cannot safely be used again.
            raise error
        response = getattr(error, 'response', None)
        status = getattr(response, 'status_code', None)
        if type(status) is not int:
            status = getattr(error, 'status_code', None)
        if type(status) is not int:
            status = getattr(error, 'code', None)
        if type(status) is not int or not 100 <= status <= 599:
            status = None
        category = ('AUTH' if status in (401, 403) else 'QUOTA' if status == 429 else
            'SERVER' if status and status >= 500 else 'HTTP_ERROR' if status and status >= 300 else
            'INVALID_RESPONSE' if type(error).__name__ == 'ValidationError' else
            'SEND_LIMIT' if isinstance(error, SingleSendBlocked) else
            'TRANSPORT_UNKNOWN' if isinstance(error, (httpx.TransportError, TimeoutError)) or
                type(error).__name__ in {'APITimeoutError','APIConnectionError'} else 'LOCAL_ERROR')
        self.event(stage, 'UNKNOWN' if self.ai_count or not self.count_known else 'FAIL', error=error,
                   status=status, category=category)


class SingleSendTransport(httpx.BaseTransport):
    def __init__(self, diagnostics, delegate=None):
        self.diagnostics = diagnostics
        self.delegate = delegate if delegate is not None else httpx.HTTPTransport(retries=0, http2=False)

    def handle_request(self, request):
        d = self.diagnostics
        with d.lock:
            if d.ai_count:
                d.event('http', 'BLOCKED', category='SEND_LIMIT')
                raise SingleSendBlocked()
            if (request.method != 'POST' or request.url.scheme != 'https' or
                    request.url.host != 'generativelanguage.googleapis.com' or
                    request.url.port not in (None, 443) or
                    request.url.path != '/v1/interactions'):
                d.event('http', 'BLOCKED', category='SDK_UNSUPPORTED')
                raise SingleSendBlocked()
            if d.deadline_epoch is not None and (
                    d.deadline_epoch-time.time() < PRB_TIMEOUT_SECONDS+PRB_FINISH_RESERVE_SECONDS):
                d.event('http', 'BLOCKED', category='SEND_LIMIT')
                raise AnalysisUnknown()
            # Commit diagnostic before delegating. Failure prevents any send.
            d.ai_count = 1
            d.http_started = time.monotonic()
            d.event('http', 'STARTED')
        try:
            response = self.delegate.handle_request(request)
            # Include body receipt/lost-body errors, not just response headers.
            # HTTPX's normal non-streaming call also buffers this body.
            try:
                response.read()
            finally:
                response.close()
            d.event('response', 'PASS' if response.status_code < 300 else 'FAIL',
                status=response.status_code, category=('AUTH' if response.status_code in (401,403) else
                'QUOTA' if response.status_code == 429 else 'SERVER' if response.status_code >= 500 else
                'HTTP_ERROR' if response.status_code >= 300 else 'NONE'))
            return response
        except Exception as error:
            d.failure('http', error)
            raise

    def close(self):
        self.delegate.close()


def prb_client(api_key, diagnostics, *, transport=None):
    from importlib.metadata import version
    from google import genai
    # Only the audited SDK/HTTP stack is admitted on the opt-in PR-B path.
    if (version('google-genai') != '3.6.1' or version('httpx') != '0.28.1'
            or version('httpcore') != '1.0.9'):
        diagnostics.event('analysis', 'BLOCKED', category='SDK_UNSUPPORTED')
        raise AnalysisUnknown()
    guard = SingleSendTransport(diagnostics, transport)
    http = httpx.Client(transport=guard, timeout=httpx.Timeout(PRB_TIMEOUT_SECONDS),
        follow_redirects=False, trust_env=False)
    client = genai.Client(api_key=api_key, vertexai=False, http_options={
        'api_version':'v1', 'timeout':int(PRB_TIMEOUT_SECONDS*1000),
        'retry_options':{'attempts':1}, 'httpx_client':http})
    if client._api_client._httpx_client is not http:
        http.close()
        raise AnalysisUnknown()
    return client
