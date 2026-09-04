from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol
from urllib.parse import urlsplit

import httplib2
import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest
from requests.adapters import HTTPAdapter

from .payroll_google_sheets_adapter import (
    PayrollGoogleSheetsAppendAdapter,
    PayrollRequestNotSentError,
)


@dataclass(frozen=True)
class PayrollHttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | str | None


@dataclass(frozen=True)
class PayrollHttpResponse:
    status: int
    reason: str | None
    headers: Mapping[str, str]
    content: bytes


class PayrollSingleSendTransport(Protocol):
    """Transport whose ``send`` dispatches the supplied request at most once."""

    def send(self, request: PayrollHttpRequest) -> PayrollHttpResponse: ...


class RequestsPayrollSingleSendTransport:
    """A requests transport with retries and redirects disabled.

    The owned Session has no application hooks or custom adapters. Its HTTP and
    HTTPS adapters use ``max_retries=0`` and every call passes
    ``allow_redirects=False``. ``Session.send`` is invoked exactly once.
    """

    def __init__(self, *, timeout: float | tuple[float, float] = (10.0, 30.0)):
        self.timeout = timeout
        self._session = requests.Session()
        self._session.mount("http://", HTTPAdapter(max_retries=0))
        self._session.mount("https://", HTTPAdapter(max_retries=0))

    def send(self, request: PayrollHttpRequest) -> PayrollHttpResponse:
        if urlsplit(request.url).scheme != "https":
            raise PayrollRequestNotSentError("payroll transport requires HTTPS")

        try:
            prepared = self._session.prepare_request(requests.Request(
                method=request.method,
                url=request.url,
                headers=dict(request.headers),
                data=request.body,
            ))
            environment = self._session.merge_environment_settings(
                prepared.url, proxies={}, stream=False, verify=None, cert=None,
            )
        except Exception as exc:
            raise PayrollRequestNotSentError(
                "payroll HTTP request preparation failed",
            ) from exc

        # This is the only write dispatch in this transport. Any exception from
        # this point is ambiguous and deliberately propagates without a retry.
        response = self._session.send(
            prepared,
            timeout=self.timeout,
            allow_redirects=False,
            **environment,
        )
        return PayrollHttpResponse(
            status=response.status_code,
            reason=response.reason,
            headers=dict(response.headers),
            content=response.content,
        )

    def close(self) -> None:
        self._session.close()


class PayrollGoogleApiSingleAttemptExecutor:
    """Execute one non-resumable Sheets append through a single-send transport.

    Authentication is prepared before the write dispatch. A credential refresh
    may contact the token endpoint, but it cannot contain or dispatch the Sheets
    write body. A 401 response from Sheets is returned as an HTTP error; this
    executor never refreshes and replays the write after a response.
    """

    def __init__(
        self,
        credentials,
        *,
        transport: PayrollSingleSendTransport | None = None,
        auth_request=None,
    ):
        if credentials is None:
            raise ValueError("credentials are required")
        self._credentials = credentials
        self._transport = transport or RequestsPayrollSingleSendTransport()
        self._auth_request = auth_request or GoogleAuthRequest()

    def execute_once(self, request):
        self._validate_request(request)
        headers = dict(request.headers)
        try:
            self._credentials.before_request(
                self._auth_request,
                request.method,
                request.uri,
                headers,
            )
        except Exception as exc:
            raise PayrollRequestNotSentError(
                "credential preparation failed before payroll write dispatch",
            ) from exc

        response = self._transport.send(PayrollHttpRequest(
            method=request.method,
            url=request.uri,
            headers=headers,
            body=request.body,
        ))
        if not isinstance(response, PayrollHttpResponse):
            raise TypeError("single-send transport returned an invalid response")
        if not isinstance(response.content, bytes):
            raise TypeError("single-send transport response content must be bytes")

        response_headers = dict(response.headers)
        response_headers["status"] = str(response.status)
        if response.reason is not None:
            response_headers["reason"] = response.reason
        api_response = httplib2.Response(response_headers)

        for callback in request.response_callbacks:
            callback(api_response)
        if api_response.status >= 300:
            raise HttpError(api_response, response.content, uri=request.uri)
        return request.postproc(api_response, response.content)

    @staticmethod
    def _validate_request(request) -> None:
        if not isinstance(request, HttpRequest):
            raise PayrollRequestNotSentError(
                "executor accepts googleapiclient HttpRequest only",
            )
        if request.resumable is not None:
            raise PayrollRequestNotSentError(
                "resumable requests are not single-attempt payroll writes",
            )
        target = urlsplit(str(request.uri))
        if (
            request.method != "POST"
            or target.scheme != "https"
            or target.hostname != "sheets.googleapis.com"
            or not target.path.endswith(":append")
        ):
            raise PayrollRequestNotSentError(
                "executor accepts only HTTPS Sheets values.append requests",
            )


class _PayrollRequestConstructionOnlyHttp:
    """Fail closed if a request bypasses the single-attempt executor."""

    def request(self, *args, **kwargs):
        raise PayrollRequestNotSentError(
            "direct request execution is forbidden for payroll writes",
        )


def payroll_sheets_request_service():
    """Build a local-discovery service that can construct, but not send, requests."""
    return build(
        "sheets",
        "v4",
        http=_PayrollRequestConstructionOnlyHttp(),
        cache_discovery=False,
        static_discovery=True,
    )


def production_payroll_google_sheets_adapter(
    spreadsheet_id: str,
    *,
    credentials,
    timeout: float | tuple[float, float] = (10.0, 30.0),
) -> PayrollGoogleSheetsAppendAdapter:
    """Assemble the only production-safe Payroll Sheets append stack."""
    executor = PayrollGoogleApiSingleAttemptExecutor(
        credentials,
        transport=RequestsPayrollSingleSendTransport(timeout=timeout),
    )
    return PayrollGoogleSheetsAppendAdapter(
        spreadsheet_id,
        service=payroll_sheets_request_service(),
        executor=executor,
    )
