"""Temporary metadata-only probe. No receipt, worker, journal or Sheets imports."""
import hashlib
import hmac
import json
import logging
import os
import re
import sys
from urllib.parse import urlencode

REPOSITORY = "tmoriuchi1401-source/kakeibo-ai"
EXECUTION_TAG = "receipt-metadata-preflight-once-20261114"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive.metadata.readonly"
BINDING_SHA256 = "5ac8b2185a703d33f0a4c45c761b9603b60c969f0b55964e825629979ea1caa5"
FIELDS = "id,mimeType,labels(trashed)"


class BoundedSession:
    """Exactly one auth POST and one GET per pinned metadata URL; no replay."""
    def __init__(self, metadata_urls):
        import requests
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        self.allowed = {("POST", TOKEN_URI), *(("GET", u) for u in metadata_urls)}
        self.used = set()
        self.last_status = 0

    def request(self, method, url, **kwargs):
        key = (method, url)
        if key not in self.allowed or key in self.used or method == "GET" and kwargs.get("data") is not None:
            raise RuntimeError("PREFLIGHT_TRANSPORT_BLOCKED")
        self.used.add(key)  # Consumed before sending; a timeout never permits replay.
        self.last_status = 0
        response = self.session.request(method, url, data=kwargs.get("data"), headers=kwargs.get("headers"),
                                        timeout=(10, 20), allow_redirects=False, verify=True)
        self.last_status = response.status_code
        return response

    def close(self):
        self.session.close()


def probe(env, *, session_factory=BoundedSession):
    # google-auth's debug transport can include request bodies. Silence library
    # logging altogether in this standalone process; only fixed rows leave it.
    logging.disable(logging.CRITICAL)
    rows = [dict(stage=s, http_status=0, code="NOT_RUN")
            for s in ("identity", "auth", "private_metadata", "folder_metadata")]

    def stop(index, code, status=0):
        rows[index].update(code=code, http_status=status)
        return 1, rows

    if (env.get("GITHUB_REPOSITORY") != REPOSITORY or env.get("GITHUB_EVENT_NAME") != "push"
            or env.get("GITHUB_REF") != "refs/tags/" + EXECUTION_TAG
            or env.get("PREFLIGHT_REF_CREATED") != "true" or env.get("GITHUB_RUN_ATTEMPT") != "1"
            or env.get("RECEIPT_VALIDATION_ENABLED") != "false"
            or env.get("ACTIONS_STEP_DEBUG", "").lower() == "true"):
        return stop(0, "EXECUTION_NOT_APPROVED")
    try:
        raw = json.loads(env["RECEIPT_VALIDATION_CONFIG_JSON"])
        binding = {k: raw[k] for k in ("service_email", "private_id", "folder_id")}
        canonical = json.dumps(binding, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        if (not hmac.compare_digest(hashlib.sha256(canonical.encode()).hexdigest(), BINDING_SHA256)
                or binding["private_id"] == binding["folder_id"]
                or any(not re.fullmatch(r"[A-Za-z0-9_-]{10,150}", binding[k])
                       for k in ("private_id", "folder_id"))):
            return stop(0, "BINDING_PIN_MISMATCH")
        info = json.loads(env["GOOGLE_SERVICE_ACCOUNT_JSON"])
        if info.get("type") != "service_account" or info.get("client_email") != binding["service_email"]:
            return stop(0, "SA_IDENTITY_MISMATCH")
    except Exception:
        return stop(0, "IDENTITY_INPUT_INVALID")
    rows[0]["code"] = "SA_IDENTITY_PASS"
    if info.get("token_uri") != TOKEN_URI:
        return stop(1, "AUTH_TOKEN_URI_REJECTED")
    urls = ["https://www.googleapis.com/drive/v2/files/" + binding[k] + "?" + urlencode({"fields": FIELDS})
            for k in ("private_id", "folder_id")]
    session = None
    try:
        from google.oauth2 import service_account, _client
        from google.auth.transport.requests import Request
        credentials = service_account.Credentials.from_service_account_info(info, scopes=[SCOPE])
        assertion = credentials._make_authorization_grant_assertion()
        session = session_factory(urls)
        token, _, _ = _client.jwt_grant(Request(session=session), TOKEN_URI, assertion, can_retry=False)
        if not isinstance(token, str) or not token:
            session.close()
            return stop(1, "AUTH_RESPONSE_INVALID", session.last_status)
        rows[1].update(code="AUTH_PASS", http_status=session.last_status)
    except Exception:
        status = session.last_status if session is not None else 0
        if session is not None:
            session.close()
        return stop(1, "AUTH_FAILED", status)
    try:
        for index, (url, fid, mime) in enumerate(zip(urls,
                (binding["private_id"], binding["folder_id"]),
                ("application/json", "application/vnd.google-apps.folder")), 2):
            try:
                response = session.request("GET", url, headers={"Authorization": "Bearer " + token})
                if response.status_code != 200:
                    return stop(index, "METADATA_HTTP_FAILED", response.status_code)
                if len(response.content) > 65536:
                    return stop(index, "METADATA_RESPONSE_INVALID", 200)
                metadata = response.json()
                if (metadata.get("id") != fid or metadata.get("mimeType") != mime
                        or metadata.get("labels", {}).get("trashed") is not False):
                    return stop(index, "METADATA_IDENTITY_MISMATCH", 200)
                rows[index].update(code="METADATA_PASS", http_status=200)
            except Exception:
                return stop(index, "METADATA_FAILED", session.last_status)
        return 0, rows
    finally:
        session.close()


def main():
    try:
        if len(sys.argv) != 1:
            status, rows = 1, [dict(stage="gate", http_status=0, code="ARGUMENTS_FORBIDDEN")]
        else:
            status, rows = probe(os.environ)
    except Exception:
        status, rows = 1, [dict(stage="gate", http_status=0, code="PREFLIGHT_INTERNAL_ERROR")]
    for row in rows:
        print(json.dumps(row, sort_keys=True, separators=(",", ":")))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
