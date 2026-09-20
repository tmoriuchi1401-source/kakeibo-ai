from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.drive_run_state import StateError
from app.receipt_recovery_audit import audit_rows


def fixture():
    raw = {"spreadsheet_id": "private-source", "imports": [["receipt:one", "", "receipt", "one"]],
           "expenses": [["R-one-01", "", "private merchant", "private item", 1234, "", "", "", "receipt", "R-one", "receipt:one"]]}
    receipts = [["R-one", "", "private merchant", 1234]]
    confirmation = SimpleNamespace(items={"review": {"status": "waiting"}},
        store=SimpleNamespace(payload=b"private confirmation", value={"records": {"one": {"phase": "complete"}}}))
    ledger = SimpleNamespace(payload=b"private pending ledger", value={"sources": {"receipts": {"phase": "pending"}}})
    return raw, receipts, confirmation, ledger


def test_read_only_audit_binds_private_snapshots_and_emits_no_business_values():
    raw, receipts, confirmation, ledger = fixture()
    before = deepcopy((raw, receipts, confirmation, ledger))
    report = audit_rows(raw, receipts, confirmation, ledger)
    assert report["counts"] == {"receipt_rows": 1, "receipt_markers": 1, "receipt_missing_counterparts": 0,
        "receipt_orphan_parts": 0, "confirmation_pending": 0, "analysis_requested": 0, "production_receipts_pending": 1}
    assert (raw, receipts, confirmation, ledger) == before
    assert "private" not in json.dumps(report) and "1234" not in json.dumps(report)
    confirmation.store.payload += b"changed"
    assert audit_rows(raw, receipts, confirmation, ledger)["evidence_sha256"] != report["evidence_sha256"]


@pytest.mark.parametrize("change,field", [("receipt-missing", "receipt_missing_counterparts"),
    ("marker-missing", "receipt_missing_counterparts"), ("orphan", "receipt_orphan_parts"),
    ("pending", "confirmation_pending"), ("requested", "analysis_requested")])
def test_partial_outcomes_remain_visible_for_operator_reconciliation(change, field):
    raw, receipts, confirmation, ledger = fixture()
    if change == "receipt-missing": receipts.clear()
    if change == "marker-missing": raw["imports"].clear()
    if change == "orphan": raw["expenses"][0][9] = "R-other"
    if change == "pending": confirmation.items["review"]["status"] = "pending"
    if change == "requested": confirmation.store.value["records"]["one"]["phase"] = "requested"
    report = audit_rows(raw, receipts, confirmation, ledger)
    assert report["counts"][field] > 0 and report["counts"]["production_receipts_pending"] == 1


@pytest.mark.parametrize("table", ["expenses", "imports", "receipts"])
def test_duplicate_fixed_ids_reject_the_audit(table):
    raw, receipts, confirmation, ledger = fixture()
    rows = receipts if table == "receipts" else raw[table]
    rows.append(deepcopy(rows[0]))
    with pytest.raises(StateError, match="identity_invalid"):
        audit_rows(raw, receipts, confirmation, ledger)
