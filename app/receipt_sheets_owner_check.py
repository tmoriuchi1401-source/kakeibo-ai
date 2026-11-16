"""Pure owner-side pre/post checks for one independent synthetic Sheets trial.

No worker credentials, IO, grant, signature or financial authority. A PASS is
only a check of the supplied owner GET observations, never a continuous lock.
"""
import json
import re

from .drive_run_state import StateError
from .page_receipt_model import digest
from .receipt_sheets_source import checked_source


def trial_only(c):
    if (c.get("mode") != "sheets-authority-v1" or c.get("synthetic_only") is not True
            or not re.fullmatch(r"review/receipt-sheets-[A-Za-z0-9/_-]+", c.get("ref", ""))):
        raise StateError("receipt_sheets_synthetic_trial_required")


def owner_binding_digest(c):
    # Acceptance can be enabled after the owner preflight. Assets, baseline,
    # principal and ref must stay pinned; enabled is not source identity.
    return digest({k: v for k, v in c.items() if k != "enabled"})


def check_owner_metadata(fid, m, c, *, worker_role, mime_type):
    """Check owner GET's complete active ACL and parent; not worker runtime IO."""
    trial_only(c)
    if worker_role not in {"reader", "writer"}:
        raise StateError("receipt_sheets_metadata_role_invalid")
    if type(m.get("permissions")) is not list:
        raise StateError("receipt_sheets_owner_acl_or_identity_changed")
    permissions = [p for p in m["permissions"] if not p.get("deleted")]
    if (m.get("id") != fid or m.get("mimeType") != mime_type
            or m.get("parents") != [c["folder_id"]]
            or len(permissions) != 2
            or any(p.get("expirationTime") or p.get("pendingOwner") for p in permissions)
            or {(p.get("type"), p.get("role"), p.get("emailAddress")) for p in permissions}
                != {("user", "owner", c["owner_email"]), ("user", worker_role, c["service_email"])}):
        raise StateError("receipt_sheets_owner_acl_or_identity_changed")
    return dict(id=fid, mime_type=mime_type, parents=list(m["parents"]),
                permissions=sorted((p["type"], p["role"], p["emailAddress"]) for p in permissions))


def check_owner_assets(c, sheet_metadata, original_metadata, *, phase):
    """Bind owner observations to the approved Sheet/case/source, before or after.

    Caller separately validates the full worker binding. This function neither
    verifies an authenticated owner nor proves when/how the observations arose.
    """
    trial_only(c)
    if phase not in {"before", "after"} or len(c["cases"]) != 1:
        raise StateError("receipt_sheets_owner_check_scope_invalid")
    cid, item = next(iter(c["cases"].items()))
    entry, expected = json.loads(item["context_json"]), json.loads(item["expected_json"])
    source = checked_source(entry["anchor"]["source"])
    sid = source["source_id"]
    if (expected["case_id"] != cid or expected["source"] != source
            or entry["import_row"][0] != cid or entry["import_row"][3] != sid
            or entry["decoder"]["machine"][0:2] != [cid, sid]
            or c["spreadsheet_id"] == sid or set(original_metadata) != {sid}):
        raise StateError("receipt_sheets_owner_asset_binding_invalid")
    sheet = check_owner_metadata(c["spreadsheet_id"], sheet_metadata, c,
        worker_role="writer", mime_type="application/vnd.google-apps.spreadsheet")
    original = check_owner_metadata(sid, original_metadata[sid], c,
        worker_role="reader", mime_type=source["mime_type"])
    return dict(schema="receipt-sheets-owner-check-v1", phase=phase, binding_digest=owner_binding_digest(c),
                case_id=cid, source_id=sid, source_sha256=source["sha256"], sheet=sheet, original=original,
                continuous_acl_parent_verification=False, posting_authority=False, financial_write_count=0)


def check_owner_after(c, sheet_metadata, original_metadata, before):
    """Check fresh owner GETs against the same recorded preflight binding."""
    if (before.get("schema") != "receipt-sheets-owner-check-v1" or before.get("phase") != "before"
            or before.get("binding_digest") != owner_binding_digest(c)):
        raise StateError("receipt_sheets_owner_preflight_binding_changed")
    return check_owner_assets(c, sheet_metadata, original_metadata, phase="after")
