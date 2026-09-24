"""Post private manual-entry requests through the established ledger writer.

Only a UUID reaches Actions. Inputs stay in the management spreadsheet. The
workflow shares the production concurrency group with all other ledger jobs.
"""
from __future__ import annotations

from datetime import date
import hashlib
import json
import re

from .drive_run_state import StateError
from .sheets import SheetsDB

SHEET = "_手入力受付"
HEADER = ["request_id", "state", "payload_json", "submitted_at", "expense_id", "message"]
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z", re.I)
FALLBACK = ("その他", "未分類")


def expense_id(request_id: str) -> str:
    return "MAN-" + hashlib.sha256(request_id.lower().encode("ascii")).hexdigest()[:24]


def _requests(db):
    rows = db.get_raw(f"'{SHEET}'!A1:F")
    if not rows or rows[0][:6] != HEADER:
        raise StateError("manual_sheet_header_invalid")
    requests = {}
    for number, raw in enumerate(rows[1:], 2):
        row = (list(raw) + [""] * 6)[:6]
        if not row[0]:
            continue
        if row[0] in requests:
            raise StateError("manual_duplicate_request_id")
        requests[str(row[0])] = (number, row)
    return requests


def _update(db, number, row):
    db.set_raw_range(f"'{SHEET}'!A{number}", [row])


def _payload(raw, categories):
    try:
        value = json.loads(raw)
        day = str(value["date"])
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError()
        amount = value["amount"]
        if type(amount) is not int or not 1 <= amount <= 99999999:
            raise ValueError()
        merchant, note = value["merchant"], value["note"]
        major, minor = value["major"], value["minor"]
        if not all(isinstance(x, str) for x in (merchant, note, major, minor)):
            raise ValueError()
        if len(merchant) > 100 or len(note) > 300 or value["payment"] != "現金":
            raise ValueError()
        if (major, minor) != ("", "") and (major, minor) not in categories:
            raise ValueError()
        if (major, minor) == ("", ""):
            major, minor = FALLBACK
        return day, amount, merchant, note, major, minor
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise StateError("manual_payload_invalid") from None


def execute(db, request_id, *, refresh_projection=lambda: {}):
    if not UUID.fullmatch(request_id):
        raise StateError("manual_request_id_invalid")
    requests = _requests(db)
    if request_id not in requests:
        raise StateError("manual_request_missing")
    number, row = requests[request_id]
    if row[1] != "dispatching":
        return {"manual_ignored": 1}
    try:
        payload = json.loads(row[2])
        if isinstance(payload, dict) and "target" in payload:
            target = str(payload["target"])
            if not UUID.fullmatch(target) or target == request_id or target not in requests:
                raise StateError("manual_cancel_target_invalid")
            target_num, original = requests[target]
            if "target" in json.loads(original[2]):
                raise StateError("manual_cancel_chain_invalid")
            row[1] = "running"
            _update(db, number, row)
            ledger_id = expense_id(target)
            records = db.expense_records()
            entry = records.get(ledger_id)
            if entry:
                ledger_row, values = entry
                if values[8] != "manual" or values[10] != "manual:" + target:
                    raise StateError("manual_cancel_identity_conflict")
                if values[12] in ("", "active"):
                    db.set_raw_range(f"'支出明細'!M{ledger_row}", [["void"]])
            elif original[1] in {"running", "complete"}:
                raise StateError("manual_cancel_missing_expense")
            original[1] = "cancelled"
            _update(db, target_num, original)
            row[1], row[4] = "complete", ledger_id
            _update(db, number, row)
            if entry:
                refresh_projection()
            return {"manual_cancelled": 1}
        categories = set(db.categories())
        if FALLBACK not in categories:
            raise StateError("manual_fallback_category_missing")
        day, amount, merchant, note, major, minor = _payload(row[2], categories)
        row[1] = "running"
        _update(db, number, row)
        ledger_id = expense_id(request_id)
        records = db.expense_records()
        entry = records.get(ledger_id)
        if entry:
            _, values = entry
            if values[8] != "manual" or values[10] != "manual:" + request_id:
                raise StateError("manual_expense_identity_conflict")
        else:
            db.ensure_expense_status_column()
            db.append_raw("支出明細", [[ledger_id, day, merchant, "手入力", amount,
                major, minor, "現金", "manual", "", "manual:" + request_id, note, "active"]])
            if ledger_id not in db.expense_index():
                raise StateError("manual_expense_readback_failed")
        row[1], row[4] = "complete", ledger_id
        _update(db, number, row)
        refresh_projection()
        return {"manual_posted": 1}
    except Exception:
        # A claimed request may have changed the ledger. Never auto-replay it.
        if row[1] in {"running", "dispatching"}:
            row[1], row[5] = "error", "処理状態を確認してください"
            try:
                _update(db, number, row)
            except Exception:
                pass
        raise


def main():
    import os
    import subprocess
    from .production_flow import verify_execution_boundary
    from .sheets import SheetsReadPacer
    from .projection_runtime import run_projection
    env = dict(os.environ)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    verify_execution_boundary(env, head)
    if env.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise StateError("manual_dispatch_required")
    pacer = SheetsReadPacer()
    db = SheetsDB(env["SPREADSHEET_ID"], read_pacer=pacer, read_retry_base=20)
    try:
        result = execute(db, env.get("MANUAL_REQUEST_ID", ""),
            refresh_projection=lambda: run_projection(env, apply=True, read_pacer=pacer))
        print(json.dumps(result, sort_keys=True))
    except Exception:
        # Google API exceptions can contain private request content.
        print(json.dumps({"success": False, "error": "manual_entry_failed"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
