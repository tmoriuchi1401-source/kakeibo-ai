from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.bank_meaning_source_guard import BankMeaningSourceGuard


def fixture():
    files = {name: {"id": name, "mimeType": "application/pdf", "parents": ["inbox"],
                    "modifiedTime": "2026-10-01T00:00:00Z", "appProperties": {}}
             for name in ("first", "second")}
    payloads = {name: name.encode() for name in files}
    guards = []
    class Resolver:
        def __call__(self, tx): return {"state": "no_sheet_rule"}
        def require_unchanged(self): guards.append("master")
    class Drive:
        def files(self): return self
        def get(self, *, fileId, **kwargs):
            value = deepcopy(files[fileId])
            return SimpleNamespace(execute=lambda: value)
    guard = BankMeaningSourceGuard(Resolver(), Drive(), "inbox", lambda key: payloads[key])
    for key, file in files.items():
        guard.observe(file, payloads[key])
    return guard, files, payloads, guards


def test_originals_are_reread_in_addition_to_master_and_own_verified_move_is_removed():
    guard, files, _, checks = fixture()
    guard.require_unchanged()
    files["first"]["parents"] = ["processed"]
    guard.archived("first")
    guard.require_unchanged()
    assert checks == ["master", "master"] and list(guard.originals) == ["second"]


@pytest.mark.parametrize("fault", ["id", "parent", "time", "mime", "trashed", "marker", "bytes"])
def test_changed_original_prevents_the_existing_write_or_move_dispatch(fault):
    guard, files, payloads, _ = fixture()
    value = files["first"]
    if fault == "id": value["id"] = "different"
    if fault == "parent": value["parents"] = ["elsewhere"]
    if fault == "time": value["modifiedTime"] = "2026-10-01T00:01:00Z"
    if fault == "mime": value["mimeType"] = "text/plain"
    if fault == "trashed": value["trashed"] = True
    if fault == "marker": value["appProperties"] = {"kakeiboBankPdfProcessedAt": "changed"}
    if fault == "bytes": payloads["first"] = b"different pdf despite same metadata"
    with pytest.raises(ValueError, match="original_changed_before_apply"):
        guard.require_unchanged()


def test_planned_income_requires_original_in_this_scan_not_only_an_import_label():
    guard, _, _, _ = fixture()
    guard.parsed([SimpleNamespace(source_row_identity="known_original")])
    with pytest.raises(ValueError, match="settlement_original_not_eligible"):
        guard.require_settlement_scope(iter(["known_original"]))
    guard.permit_settlement(["known_original"])
    guard.require_settlement_scope(["known_original"])
    with pytest.raises(ValueError, match="settlement_original_unavailable"):
        guard.require_settlement_scope(["old_unseen_import"])
    with pytest.raises(ValueError, match="settlement_original_unavailable"):
        guard.permit_settlement(["old_unseen_import"])
