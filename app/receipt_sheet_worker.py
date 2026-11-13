"""UUID-only Actions entry, reusing #200 and existing Google credentials.

Only an explicitly pinned feature branch can execute. No resource creation,
credential migration, financial flags, writer, source movement or AI calls.
"""
from copy import deepcopy
import json
import os
import re

from .drive_run_state import StateError
from .page_receipt_model import digest
from .pdf_intake_registry import DriveRegistryIO
from .receipt_private_adapter import ReceiptPrivateAdapter, _expected
from .receipt_sheet_connection import connect_existing_google_services
from .receipt_sheet_validation import UNKNOWN, request_uuid


def binding_from_catalog(catalog, *, spreadsheet_id, private_id, folder_id,
                         owner_email, service_email, ref):
    """Export connection settings only from an already confirmed catalog.

    Does not create confirmation, actor identity, policy, resources or files.
    The unchanged private validator rejects a missing confirmation.
    """
    if len(catalog.get("cases", {})) != 1:
        raise StateError("receipt_worker_single_case_required")
    case_id, entry = next(iter(catalog["cases"].items()))
    expected = _expected(case_id, entry)
    canonical = lambda value: json.dumps(value, ensure_ascii=True, sort_keys=True,
                                         separators=(",", ":"), allow_nan=False)
    binding = dict(enabled=True, synthetic_only=True,
        repository="tmoriuchi1401-source/kakeibo-ai", workflow="category-sheet-request.yml", ref=ref,
        spreadsheet_id=spreadsheet_id, private_id=private_id, folder_id=folder_id,
        owner_email=owner_email, service_email=service_email, case_id=case_id,
        catalog_json=canonical(catalog), catalog_digest=digest(catalog),
        expected_json=canonical(expected), expected_digest=digest(expected))
    validate_binding(binding)
    if len(canonical(binding).encode()) > 9000:
        raise StateError("receipt_worker_script_property_capacity_exceeded")
    return binding


def validate_binding(c):
    try:
        c = deepcopy(c)
        c["expected"] = json.loads(c["expected_json"])
        catalog = json.loads(c["catalog_json"])
        if (c["enabled"] is not True or c["synthetic_only"] is not True
                or c["repository"] != "tmoriuchi1401-source/kakeibo-ai"
                or c["workflow"] != "category-sheet-request.yml"
                or not re.fullmatch(r"feat/receipt-[A-Za-z0-9/_-]+", c["ref"])
                or not re.fullmatch(r"[a-f0-9]{64}", c["catalog_digest"])
                or not re.fullmatch(r"[a-f0-9]{64}", c["expected_digest"])
                or digest(c["expected"]) != c["expected_digest"]
                or digest(catalog) != c["catalog_digest"]
                or json.dumps(catalog, ensure_ascii=True, sort_keys=True, separators=(",", ":")) != c["catalog_json"]
                or json.dumps(c["expected"], ensure_ascii=True, sort_keys=True, separators=(",", ":")) != c["expected_json"]
                or c["expected"]["case_id"] != c["case_id"]
                or c["owner_email"] == c["service_email"]
                or not c["owner_email"] or not c["service_email"]):
            raise ValueError()
        ids = [c[k] for k in ("spreadsheet_id", "private_id", "folder_id")]
        if len(set(ids)) != 3 or any(not re.fullmatch(r"[A-Za-z0-9_-]{10,150}", x) for x in ids):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise StateError("receipt_worker_binding_invalid") from None
    return deepcopy(c)


def validate_execution(env, binding):
    """Stop before credential/client construction on rerun, wrong ref or SHA."""
    if (env.get("RECEIPT_VALIDATION_ENABLED") != "true"
            or env.get("GITHUB_REPOSITORY") != binding["repository"]
            or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or env.get("GITHUB_RUN_ATTEMPT") != "1"
            or env.get("GITHUB_REF") != "refs/heads/" + binding["ref"]
            or env.get("RECEIPT_VALIDATION_REF") != binding["ref"]
            or not re.fullmatch(r"[a-f0-9]{40}", env.get("RECEIPT_VALIDATION_SHA", ""))
            or env.get("GITHUB_SHA") != env.get("RECEIPT_VALIDATION_SHA")
            or env.get("ACTIONS_STEP_DEBUG", "").lower() == "true"):
        raise StateError("receipt_worker_execution_not_approved")
    request_uuid(env.get("RECEIPT_REQUEST_ID"))


class ExistingReceiptDriveIO(DriveRegistryIO):
    """Reuse DriveRegistryIO's existing metadata/media/If-Match operations.

    Its PDF-only inbox/processed anchors are irrelevant here. This constructor
    binds only the precreated JSON and parent; no PDF configuration is changed.
    Session is constructed below with authorization replay disabled.
    """
    def __init__(self, binding, session):
        self.config = dict(file_id=binding["private_id"], folder_id=binding["folder_id"],
                           owner_digest=digest(binding["owner_email"]))
        self.sa, self.http, self.writable = binding["service_email"], session, True
        self.deadline = None


def existing_google_connection(binding, info):
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from google_auth_httplib2 import AuthorizedHttp
    import httplib2

    if info.get("client_email") != binding["service_email"]:
        raise StateError("receipt_worker_service_account_mismatch")
    credentials = service_account.Credentials.from_service_account_info(info,
        scopes=["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets"])
    session = AuthorizedSession(credentials, max_refresh_attempts=0, refresh_status_codes=())
    session.trust_env = False
    private = ReceiptPrivateAdapter(ExistingReceiptDriveIO(binding, session),
        file_id=binding["private_id"], folder_id=binding["folder_id"],
        owner_email=binding["owner_email"], service_email=binding["service_email"],
        catalog_digest=binding["catalog_digest"])
    catalog, _ = private.read_catalog()
    if (list(catalog["cases"]) != [binding["case_id"]]
            or _expected(binding["case_id"], catalog["cases"][binding["case_id"]]) != binding["expected"]):
        raise StateError("receipt_worker_catalog_binding_mismatch")
    # execute(num_retries=0) alone does not disable credential-layer replay.
    http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=20),
                          max_refresh_attempts=0, refresh_status_codes=())
    drive = build("drive", "v3", http=http, cache_discovery=False)
    sheets = build("sheets", "v4", http=http, cache_discovery=False)
    return connect_existing_google_services(private=private, drive=drive, sheets=sheets,
        spreadsheet_id=binding["spreadsheet_id"], source_folder=binding["folder_id"])


def run(env, *, connect=existing_google_connection):
    # Failures expose only fixed codes. API exceptions may contain private IDs.
    try:
        binding = validate_binding(json.loads(env.get("RECEIPT_VALIDATION_CONFIG_JSON", "null")))
        validate_execution(env, binding)
        connection = connect(binding, json.loads(env["GOOGLE_SERVICE_ACCOUNT_JSON"]))
        output = connection.run(env["RECEIPT_REQUEST_ID"])
        state = output["record"]["state"]
        if state == "検証済み・未記帳" and not output["display_pending"]:
            return 0, "登録検証済み・未記帳"
        if state in {"修正が必要", "保留"} and not output["display_pending"]:
            return 2, state
        return 3, UNKNOWN
    except Exception:
        return 3, "結果不明・照合待ち／自動再送しない"


if __name__ == "__main__":
    status, message = run(os.environ)
    print(message)
    raise SystemExit(status)
