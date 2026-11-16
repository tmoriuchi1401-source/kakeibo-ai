"""UUID-only Actions entry for the Sheets-authoritative path. Disabled by default."""
from contextlib import contextmanager
from hashlib import sha256
import json
import os
import re
from urllib.parse import quote

from .receipt_sheets_context import expected_context
from .receipt_sheets_source import checked_source
from .receipt_sheets_owner_check import trial_only
from .receipt_sheets_authority import (TAB, MAX_ROWS, SheetAuthority, UNKNOWN,
    canonical, execute, fail, parse, request_id)

CONFIG = "RECEIPT_VALIDATION_CONFIG_JSON"


def make_binding(catalog, *, spreadsheet_id, folder_id, owner_email, service_email, ref):
    """Offline conversion of an approved baseline. Never reads old journal state."""
    cases = {}
    for case_id, entry in catalog["cases"].items():
        expected = expected_context(case_id, entry)
        cases[case_id] = dict(context_json=canonical(entry), expected_json=canonical(expected))
    value = dict(mode="sheets-authority-v1", enabled=False, synthetic_only=True, spreadsheet_id=spreadsheet_id,
                 folder_id=folder_id, owner_email=owner_email, service_email=service_email,
                 repository="tmoriuchi1401-source/kakeibo-ai", workflow="category-sheet-request.yml",
                 ref=ref, cases=cases)
    validate_binding(value)
    return value


def validate_binding(c):
    try:
        if (set(c) != {"mode", "enabled", "synthetic_only", "spreadsheet_id", "folder_id", "owner_email",
                       "service_email", "repository", "workflow", "ref", "cases"}
                or c["mode"] != "sheets-authority-v1"
                or type(c["enabled"]) is not bool or c["synthetic_only"] is not True
                or c["repository"] != "tmoriuchi1401-source/kakeibo-ai"
                or c["workflow"] != "category-sheet-request.yml"
                or not re.fullmatch(r"review/receipt-sheets-[A-Za-z0-9/_-]+", c["ref"])
                or not c["owner_email"] or not c["service_email"] or c["owner_email"] == c["service_email"]
                or c["spreadsheet_id"] == c["folder_id"]
                or any(not re.fullmatch(r"[A-Za-z0-9_-]{10,150}", c[k]) for k in ("spreadsheet_id", "folder_id"))
                or not c["cases"] or len(canonical(c).encode()) > 9000):
            raise ValueError()
        for cid, item in c["cases"].items():
            if (set(item) != {"context_json", "expected_json"}
                    or canonical(expected_context(cid, parse(item["context_json"]))) != item["expected_json"]):
                raise ValueError()
        return c
    except Exception:
        fail("binding_invalid")


def validate_execution(env, c):
    if (c["enabled"] is not True or env.get("RECEIPT_VALIDATION_ENABLED") != "true"
            or env.get("GITHUB_REPOSITORY") != c["repository"]
            or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or env.get("GITHUB_RUN_ATTEMPT") != "1"
            or env.get("GITHUB_REF") != "refs/heads/" + c["ref"]
            or env.get("RECEIPT_VALIDATION_REF") != c["ref"]
            or not re.fullmatch(r"[a-f0-9]{40}", env.get("RECEIPT_VALIDATION_SHA", ""))
            or env.get("GITHUB_SHA") != env["RECEIPT_VALIDATION_SHA"]
            or env.get("ACTIONS_STEP_DEBUG", "").lower() == "true"):
        fail("execution_not_approved")
    request_id(env.get("RECEIPT_REQUEST_ID"))
    if not re.fullmatch(r"[1-9][0-9]*", env.get("GITHUB_RUN_ID", "")):
        fail("run_identity_invalid")


def first_delivery(env, c):
    """One public GET, no extra token scope or state store.

    A new manual dispatch has run_attempt=1 too. Reject it even if the earlier
    claim never committed. All branch dispatch history must fit one page;
    history deletion/retention must be excluded by the activation gate.
    """
    import requests
    with requests.Session() as http:
        http.trust_env = False
        response = http.get('https://api.github.com/repos/' + c['repository'] +
            '/actions/workflows/' + c['workflow'] + '/runs',
            params=dict(branch=c['ref'], event='workflow_dispatch', per_page=100),
            headers={'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'},
            timeout=20, allow_redirects=False)
    if response.status_code != 200:
        fail('delivery_history_unavailable')
    verify_delivery_history(response.json(), env)


def verify_delivery_history(history, env):
    count, runs = history['total_count'], history['workflow_runs']
    if type(count) is not int or not 1 <= count <= 100 or len(runs) != count:
        fail('delivery_history_incomplete')
    found = [r for r in runs if r['display_title'] == 'Category request ' + env['RECEIPT_REQUEST_ID']]
    if (len(found) != 1 or str(found[0]['id']) != env['GITHUB_RUN_ID']
            or found[0]['run_attempt'] != 1 or found[0]['head_sha'] != env['GITHUB_SHA']
            or found[0]['event'] != 'workflow_dispatch'):
        fail('delivery_replay_rejected')


class GooglePort:
    """HTTP allowlist: management H:J PUT only, Drive metadata/media GET only."""
    def __init__(self, session, binding):
        trial_only(binding)
        self.session, self.c = session, binding
        self.root = "https://sheets.googleapis.com/v4/spreadsheets/" + binding["spreadsheet_id"] + "/values/"

    def read(self):
        value = self.session.get(self.root + quote(f"'{TAB}'!A1:J{MAX_ROWS+1}", safe=""),
            params={"valueRenderOption": "UNFORMATTED_VALUE"}, timeout=20, allow_redirects=False)
        if value.status_code != 200:
            fail("read_failed")
        return value.json().get("values", [])

    def update(self, number, tail):
        if type(number) is not int or not 2 <= number <= MAX_ROWS or len(tail) != 3:
            fail("update_range_invalid")
        response = self.session.put(self.root + quote(f"'{TAB}'!H{number}:J{number}", safe=""),
            params={"valueInputOption": "RAW"}, json={"values": [tail]}, timeout=20, allow_redirects=False)
        if response.status_code != 200:
            fail("write_unknown")

    def metadata(self, fid):
        # Synthetic new mode only. Owner separately checks ACL/parent before
        # and after the trial; this worker cannot monitor those changes.
        response = self.session.get("https://www.googleapis.com/drive/v3/files/" + fid,
            params={"fields": "id,trashed,mimeType,owners(emailAddress)"},
            timeout=20, allow_redirects=False)
        if response.status_code != 200:
            fail("metadata_failed")
        m = response.json()
        if (m.get("id") != fid or m.get("trashed") is not False
                or [o.get("emailAddress") for o in m.get("owners", [])] != [self.c["owner_email"]]):
            fail("identity_changed")
        return dict(id=m["id"], trashed=False, mimeType=m.get("mimeType"),
                    owners=[o["emailAddress"] for o in m["owners"]])

    def source_check(self, source):
        checked_source(source)
        fid = source["source_id"]
        m = self.metadata(fid)
        if m.get("mimeType") != source["mime_type"]:
            fail("source_changed")
        response = self.session.get("https://www.googleapis.com/drive/v3/files/" + fid,
            params={"alt": "media"}, timeout=20, allow_redirects=False)
        if (response.status_code != 200 or sha256(response.content).hexdigest() != source["sha256"]
                or self.metadata(fid) != m):
            fail("source_changed")

    def context_check(self, snapshot):
        if self.c["cases"][snapshot["case_id"]]["context_json"] != snapshot["context_json"]:
            fail("context_changed")


@contextmanager
def connect(c, info):
    trial_only(c)
    # One token request with can_retry=False; no refresh/replay after it.
    import requests
    from google.auth.transport.requests import Request, AuthorizedSession
    from google.oauth2 import service_account, _client
    if info.get("client_email") != c["service_email"]:
        fail("service_account_mismatch")
    creds = service_account.Credentials.from_service_account_info(info, scopes=[
        "https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/spreadsheets"])
    with requests.Session() as auth:
        auth.trust_env = False
        original, calls = auth.request, []
        def once(method, url, **kw):
            if calls or method.upper() != "POST" or url != "https://oauth2.googleapis.com/token":
                fail("auth_replay_forbidden")
            calls.append(True)
            return original(method, url, **dict(kw, timeout=20, allow_redirects=False))
        auth.request = once
        creds.token, creds.expiry, _ = _client.jwt_grant(Request(session=auth),
            "https://oauth2.googleapis.com/token", creds._make_authorization_grant_assertion(), can_retry=False)
    def deny(*a, **kw): fail("auth_refresh_forbidden")
    creds.refresh = deny
    with AuthorizedSession(creds, max_refresh_attempts=0, refresh_status_codes=()) as session:
        session.trust_env = False
        port = GooglePort(session, c)
        m = port.metadata(c["spreadsheet_id"])
        if m.get("mimeType") != "application/vnd.google-apps.spreadsheet":
            fail("spreadsheet_changed")
        yield port


def run(env, *, connection=connect, delivery_check=first_delivery):
    try:
        c = validate_binding(json.loads(env.get(CONFIG, "null")))
        validate_execution(env, c)  # Before credentials, reads and all mutations.
        delivery_check(env, c)  # Reject new manual dispatches, including pre-claim failures.
        with connection(c, json.loads(env["GOOGLE_SERVICE_ACCOUNT_JSON"])) as port:
            output = execute(env["RECEIPT_REQUEST_ID"], authority=SheetAuthority(port),
                             context_check=port.context_check, source_check=port.source_check)
        state = output["state"]
        return (0 if state == "検証済み・未記帳" else 2 if state in {"保留", "修正が必要"} else 3), state
    except Exception:
        return 3, UNKNOWN


if __name__ == "__main__":
    status, message = run(os.environ)
    print(message)  # Closed messages only; never exceptions/private snapshots.
    raise SystemExit(status)
