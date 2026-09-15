"""One fixed backfill, manually dispatched under the production Actions lock.

Only a commitment to the private plan is published. It binds the target and
every column of every approved row, including the original bank import ID.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import hashlib
import json
import os
import subprocess
import time

from .bank_income import (
    BankIncomePipeline, INCOME_HEADERS, INCOME_SHEET, deposit_decisions,
    deposits_from_imports, monthly_income, validate_income_rows,
)
from .google_clients import read_only_sheets_service, sheets_service
from .settings import Settings
from .sheets import SheetsDB

CONTRACT_SHA256 = "05b3fdc7b64b2592c280d1a9ad1f718649bed52ff2b6b8d0e6337f05d966f11b"
IMPORTS_THROUGH = "2026-09-14 07:24:35"
FIXED_COUNT = 27
PROTECTED_RANGES = (
    "取込データ!A:L", "支出明細!A:M", "給与明細ヘッダ!A:O", "給与明細項目!A:K",
    "給与標準項目!A:G", "給与項目別名!A:F", "勤務先マスタ!A:E", "ホーム!A1:I5001",
    "要確認!A:T",
)


class BackfillSheetsDB(SheetsDB):
    """Pace reads below the per-user quota; never retry an uncertain write."""
    _last_read = 0.0

    def _read(self, call):
        time.sleep(max(0, 1.2 - (time.monotonic() - self._last_read)))
        self._last_read = time.monotonic()
        return call()

    def get(self, rng):
        return self._read(lambda: self.svc.spreadsheets().values().get(
            spreadsheetId=self.sid, range=rng,
            valueRenderOption="UNFORMATTED_VALUE").execute()).get("values", [])

    def sheet_titles(self):
        return self._read(lambda: super(BackfillSheetsDB, self).sheet_titles())

    def protected_values(self):
        return self._read(lambda: self.svc.spreadsheets().values().batchGet(
            spreadsheetId=self.sid, ranges=list(PROTECTED_RANGES),
            valueRenderOption="FORMULA").execute())["valueRanges"]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def current_fixed_rows(db, rules):
    # Later imports are outside this one-off plan. The cutoff is only a
    # reconstruction aid: the complete commitment, not time, grants membership.
    eligible = []
    for row in db.get("取込データ!A2:L"):
        if not row or not str(row[0]).startswith("bankpdf:"):
            continue
        if len(row) < 2:
            raise RuntimeError("fixed_import_timestamp_missing")
        try:
            when = (datetime(1899, 12, 30) + timedelta(seconds=round(row[1] * 86400))
                    if type(row[1]) in (int, float)
                    else datetime.strptime(str(row[1]), "%Y-%m-%d %H:%M:%S"))
        except (ValueError, OverflowError):
            raise RuntimeError("fixed_import_timestamp_invalid") from None
        if when <= datetime.strptime(IMPORTS_THROUGH, "%Y-%m-%d %H:%M:%S"):
            eligible.append(row)
    decisions, duplicates = deposit_decisions(deposits_from_imports(eligible), **rules)
    if duplicates or any(d.reason == "bank_identity_collision" for d in decisions):
        raise RuntimeError("fixed_import_identity_conflict")
    rows = sorted([d.row() for d in decisions if d.outcome == "confirmed_income"], key=lambda r: r[6])
    contract = {"spreadsheet_id": db.sid, "imports_through": IMPORTS_THROUGH, "rows": rows}
    if len(rows) != FIXED_COUNT or digest(contract) != CONTRACT_SHA256:
        raise RuntimeError("fixed_plan_content_or_rule_changed")
    validate_income_rows(rows)
    return rows


def existing_income(db):
    if INCOME_SHEET not in db.sheet_titles():
        return {}
    if db.get(f"{INCOME_SHEET}!A1:J1") != [INCOME_HEADERS]:
        raise RuntimeError("fixed_income_header_mismatch")
    return validate_income_rows(db.get(f"{INCOME_SHEET}!A2:J"))


def classify_existing(db, rows):
    existing = existing_income(db)
    missing, matched = [], []
    for row in rows:
        if row[0] not in existing:
            missing.append(row)
        elif existing[row[0]] == row:
            matched.append(row)
        else:
            raise RuntimeError("fixed_existing_income_conflict")
    return missing, matched


class IncomeOnlyDB:
    """The reused writer may append only exact approved rows to this one sheet."""
    def __init__(self, db, rows):
        self.db, self.sid = db, db.sid
        self.approved = {row[0]: row for row in rows}

    def get(self, rng):
        return self.db.get(rng)

    def sheet_titles(self):
        return self.db.sheet_titles()

    def append_raw(self, sheet, rows):
        if (sheet != INCOME_SHEET or len(rows) > FIXED_COUNT
                or len({row[0] for row in rows}) != len(rows)
                or any(self.approved.get(row[0]) != row for row in rows)):
            raise RuntimeError("fixed_write_payload_changed")
        self.db.append_raw(sheet, rows)


def install_income_sheet(db):
    if INCOME_SHEET in db.sheet_titles():
        existing_income(db)  # Never overwrite/repair an existing header.
        return False
    db.svc.spreadsheets().batchUpdate(spreadsheetId=db.sid, body={"requests": [
        {"addSheet": {"properties": {"title": INCOME_SHEET,
                                    "gridProperties": {"rowCount": 1001, "columnCount": 10}}}},
    ]}).execute()
    if db.get(f"{INCOME_SHEET}!A1:J1"):
        raise RuntimeError("fixed_new_sheet_unexpected_header")
    db.svc.spreadsheets().values().update(spreadsheetId=db.sid, range=f"{INCOME_SHEET}!A1:J1",
        valueInputOption="RAW", body={"values": [INCOME_HEADERS]}).execute()
    existing_income(db)
    return True


def protected_snapshot(db):
    values = db.protected_values()
    if len(values) != len(PROTECTED_RANGES):
        raise RuntimeError("fixed_protected_snapshot_incomplete")
    return [digest(value.get("values", [])) for value in values]


def run_backfill(db, rules, *, apply=False, emit=lambda result: None):
    rows = current_fixed_rows(db, rules)
    missing, matched = classify_existing(db, rows)
    before = protected_snapshot(db)
    result = {"read_only": not apply, "fixed_count": len(rows), "existing_skip": len(matched),
              "planned_new": len(missing), "created": 0, "canary_created": 0,
              "canary_replay_created": 0, "replay_created": 0,
              "fixed_commitment_verified": True, "income_recurring_enabled": False}
    emit({"phase": "preflight", **result})
    if not apply:
        return result
    guarded = IncomeOnlyDB(db, rows)
    pipeline = BankIncomePipeline(guarded, **rules)

    def check_unchanged():
        if protected_snapshot(db) != before:
            raise RuntimeError("fixed_protected_data_changed")
        if current_fixed_rows(db, rules) != rows:
            raise RuntimeError("fixed_plan_changed_during_run")

    check_unchanged()
    result["sheet_created"] = install_income_sheet(db)
    check_unchanged()
    if missing:
        one = (missing[0][6],)
        result["canary_created"] = pipeline.apply(one, income_write_enabled=True,
            approved_spreadsheet_id=db.sid)["incomes_created"]
        if result["canary_created"] != 1:
            raise RuntimeError("fixed_canary_count_changed")
        check_unchanged()
        result["canary_replay_created"] = pipeline.apply(one, income_write_enabled=True,
            approved_spreadsheet_id=db.sid)["incomes_created"]
        if result["canary_replay_created"] != 0:
            raise RuntimeError("fixed_canary_replay_wrote")
        check_unchanged()
        emit({"phase": "canary_verified", "created": result["canary_created"], "replay_created": 0})
        rest = tuple(row[6] for row in missing[1:])
        written = pipeline.apply(rest, income_write_enabled=True,
            approved_spreadsheet_id=db.sid)["incomes_created"] if rest else 0
        result["created"] = result["canary_created"] + written
        check_unchanged()
    missing_final, matched_final = classify_existing(db, rows)
    if missing_final or len(matched_final) != FIXED_COUNT:
        raise RuntimeError("fixed_final_set_incomplete")
    final_preview = pipeline.preview(tuple(row[6] for row in rows))
    if (final_preview["existing_income"] != FIXED_COUNT or final_preview["planned_rows"]
            or final_preview["conflicting_import_ids"] or final_preview["review_groups"]):
        raise RuntimeError("fixed_final_preview_mismatch")
    result["replay_created"] = pipeline.apply(tuple(row[6] for row in rows),
        income_write_enabled=True, approved_spreadsheet_id=db.sid)["incomes_created"]
    if result["replay_created"] != 0:
        raise RuntimeError("fixed_final_replay_wrote")
    check_unchanged()
    missing_final, matched_final = classify_existing(db, rows)
    if missing_final or monthly_income(matched_final) != monthly_income(rows):
        raise RuntimeError("fixed_final_monthly_mismatch")
    if sum(monthly_income(matched_final).values()) != sum(row[2] for row in rows):
        raise RuntimeError("fixed_final_total_mismatch")
    result.update(final_count=len(matched_final), missing=0, unknown=0,
                  read_back_verified=True, monthly_total_verified=True, protected_unchanged=True)
    emit({"phase": "complete", **result})
    return result


def require_actions_main(expected_head):
    ref = "tmoriuchi1401-source/kakeibo-ai/.github/workflows/bank-income-backfill.yml@refs/heads/main"
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    remote = subprocess.check_output(["git", "rev-parse", "origin/main"], text=True).strip()
    if (os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_WORKFLOW_REF") != ref
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or not expected_head or actual != expected_head or remote != expected_head
            or os.environ.get("GITHUB_SHA") != expected_head):
        raise RuntimeError("fixed_backfill_requires_dispatched_validated_main")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-head", required=True)
    args = parser.parse_args()
    try:
        require_actions_main(args.expected_head)
        settings = Settings()
        settings.validate(need_sheet=True)
        service = sheets_service() if args.apply else read_only_sheets_service()
        db = BackfillSheetsDB(settings.spreadsheet_id, service=service)
        rules = dict(confirmed_internal_transfers=settings.bank_confirmed_internal_transfers(),
                     confirmed_non_own_classifications=settings.bank_confirmed_non_own_classifications())
        run_backfill(db, rules, apply=args.apply, emit=lambda result: print(json.dumps(result), flush=True))
    except Exception as exc:
        # Do not expose provider errors, target IDs or bank values in public logs.
        print(json.dumps({"status": "stopped", "failure_type": type(exc).__name__,
                          "remaining_not_attempted": True}), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
