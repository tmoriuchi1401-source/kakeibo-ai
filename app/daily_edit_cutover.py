"""Canonical ledger edit protection required before the daily form is enabled."""
from hashlib import sha256
import re

from .monthly_projection import ProjectionError

MARKER = "kakeibo_daily_corrections_v1"
PROTECTION = "家計簿AI: 修正は日常画面の固定IDフォームから"


def verify_cutover(metadata, daily_id, writer_email, *, owner_email=""):
    from .compact_categories import compact_helper
    if not compact_helper(metadata):raise ProjectionError("daily_compact_categories_required")
    if not any(m.get("metadataKey") == MARKER and m.get("metadataValue") == sha256(daily_id.encode()).hexdigest()
               for m in metadata.get("developerMetadata", [])):
        raise ProjectionError("daily_edit_cutover_required")
    ledger = next((s for s in metadata.get("sheets", []) if s["properties"].get("title") == "支出明細"), None)
    if ledger is None:
        raise ProjectionError("daily_ledger_missing")
    target = {"sheetId": ledger["properties"]["sheetId"], "startColumnIndex": 0, "endColumnIndex": 13}
    for item in ledger.get("protectedRanges", []):
        actual = dict(item.get("range", {}))
        # Google omits the default sheetId=0 in native range responses.
        actual.setdefault("sheetId", 0)
        actual.setdefault("startColumnIndex",0)
        if actual.get("startRowIndex") == 0:actual.pop("startRowIndex")
        editors = item.get("editors", {})
        users = set(editors.get("users", []))
        allowed = {writer_email} | ({owner_email} if owner_email else set())
        if (item.get("description") == PROTECTION and actual == target and not item.get("warningOnly", False)
                and not item.get("unprotectedRanges") and writer_email in users and users <= allowed
                and not editors.get("groups") and not editors.get("domainUsersCanEdit")):
            return
    raise ProjectionError("daily_ledger_protection_required")


def cutover_requests(metadata, daily_id, writer_email, *, owner_email=""):
    """Explicit locked migration only; no normal refresh calls this builder.

    The source owner retains Google's inherent ability to edit protections.
    No Drive sharing or authentication scopes are changed. The migration must
    already have verified that this is the existing production service account.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", daily_id) or not writer_email.endswith(".iam.gserviceaccount.com"):
        raise ProjectionError("daily_edit_binding_invalid")
    from .compact_categories import compact_helper
    if not compact_helper(metadata):raise ProjectionError("daily_compact_categories_required")
    from .sheets_ui import CATEGORY_UI_ID, CATEGORY_UI_MARKER, VERSION
    ledger = next((s for s in metadata["sheets"] if s["properties"]["title"] == "支出明細"), None)
    if ledger is None:
        raise ProjectionError("daily_ledger_missing")
    if any(m.get("metadataKey") == MARKER for m in metadata.get("developerMetadata", [])):
        verify_cutover(metadata, daily_id, writer_email, owner_email=owner_email)
        return []
    if any(p.get("description") == PROTECTION for p in ledger.get("protectedRanges", [])):
        raise ProjectionError("daily_edit_partial_cutover")
    requests = [{"addProtectedRange": {"protectedRange": {
        "description": PROTECTION, "range": {"sheetId": ledger["properties"]["sheetId"],
            "startColumnIndex": 0, "endColumnIndex": 13}, "warningOnly": False,
        "editors": {"users": [writer_email]}}}}]
    old_ui = next((s for s in metadata["sheets"] if s["properties"]["title"] == "カテゴリ対応"), None)
    if old_ui:
        if old_ui["properties"]["sheetId"] != CATEGORY_UI_ID or not any(
                m.get("metadataKey") == CATEGORY_UI_MARKER and m.get("metadataValue") == VERSION
                for m in old_ui.get("developerMetadata", [])):
            raise ProjectionError("daily_legacy_edit_ui_not_owned")
        # Remove generated ledger-cell edit links from this owned view only.
        from .compact_categories import _cells
        from .daily_view import SHEETS, link
        requests.append({"updateCells": {"range": {"sheetId": CATEGORY_UI_ID},
            "fields": "userEnteredValue,dataValidation"}})
        requests.append(_cells(CATEGORY_UI_ID, 1, 0,
            [["カテゴリ修正は日常画面へ移動しました"], ["支出IDを選び、変更内容を送信してください。"]], 1))
        requests.append({"updateCells": {"range": {"sheetId": CATEGORY_UI_ID,
            "startRowIndex": 2, "endRowIndex": 3, "startColumnIndex": 0, "endColumnIndex": 1},
            "rows": [{"values": [link(f"https://docs.google.com/spreadsheets/d/{daily_id}/edit#gid={SHEETS['確認'][0]}&range=B61", "確認・修正を開く →")]}],
            "fields": "userEnteredValue"}})
    requests.append({"createDeveloperMetadata": {"developerMetadata": {"metadataKey": MARKER,
        "metadataValue": sha256(daily_id.encode()).hexdigest(), "visibility": "DOCUMENT",
        "location": {"spreadsheet": True}}}})
    return requests
