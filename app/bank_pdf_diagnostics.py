"""Fixed-inbox diagnosis with read-only Google scopes and content-free output."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile

from .bank_pdf_recurring import BankPdfWindow, _existing_income_settled, _in_write_window
from .bank_reconciliation import build_bank_shadow_result
from .bank_steady_state import build_bank_daily_preview
from .reconciliation import parse_import_rows

BASELINE = "ae4c8a4c91e4dd8b591782b72b029b9712925a83"
FILE_REFERENCES = frozenset({
    "07c4768a0a86b86a5f4936b63d317b2a3a96a277582838b10e5e6822bcc7f24c",
    "5d13a1eb434e32a5d64abefabbc941355c6fbe4a2ff52d0de317826935eb0c05",
    "c4243583a0aaaee95e0148275c823fa70e0187140fe53385c99ef8fecde9973e",
    "5079e9b2bdb868bdb07d88cd1b5475993d5af4232db172c424104f3d21976e47",
})
REFERENCE_WINDOW_END_EPOCH = 1790729267
SAFE_IMPORT_STATUSES = frozenset({"bank_income", "bank_non_expense", "auto_expense", "bank_expense",
    "needs_review", "bank_asset_formation_expense", "bank_loan_repayment"})
SAFE_INCOME_ERRORS = frozenset({"bank_income_row_schema_mismatch", "bank_income_amount_invalid",
    "bank_income_provenance_invalid", "bank_income_duplicate_existing_id"})


def diagnose_pdf(db, path, *, file, window, rules, account_alias=None):
    imports = db.get("取込データ!A2:L")
    existing = parse_import_rows(imports)
    daily = build_bank_daily_preview(db, path, target_spreadsheet_id=db.sid,
        expected_git_head=BASELINE, account_alias=account_alias, **rules)
    parsed = daily.parsed_result
    shadow = build_bank_shadow_result(parsed, existing, **rules)
    by_id = defaultdict(list)
    for row in existing:
        by_id[row.import_id].append(row)
    transactions = []
    for decision in shadow.decisions:
        classified = decision.classification
        tx = classified.transaction
        matches = by_id[tx.source_row_identity]
        exact = len(matches) == 1 and all((
            matches[0].source == tx.source, matches[0].row[3] == tx.source_row_identity,
            matches[0].date == tx.transaction_date, matches[0].merchant == tx.description,
            matches[0].amount == tx.signed_amount, matches[0].row[7] == "銀行口座",
            matches[0].row[10] == tx.source_row_hash,
        ))
        transactions.append({
            "ref": hashlib.sha256(tx.source_row_identity.encode()).hexdigest()[:24],
            "page": tx.source_page, "row": tx.source_row,
            "classification": classified.classification, "reason": classified.reason,
            "existing_count": len(matches), "existing_exact": exact,
            "existing_status": (matches[0].status if matches[0].status in SAFE_IMPORT_STATUSES
                else "unrecognized_status") if matches else "missing",
            "existing_row": matches[0].row_num if matches else None,
        })
    from .bank_income import deposit_decisions, income_id, validate_income_rows
    deposits, _ = deposit_decisions(parsed.transactions, **rules)
    income_error = ""
    try:
        incomes = validate_income_rows(db.get("収入明細!A2:J"))
    except RuntimeError as exc:
        income_error = str(exc) if str(exc) in SAFE_INCOME_ERRORS else "income_readback_failed"
        incomes = {}
    income_rows = [{
        "ref": hashlib.sha256(d.transaction.source_row_identity.encode()).hexdigest()[:24],
        "page": d.transaction.source_page, "row": d.transaction.source_row,
        "outcome": d.outcome, "reason": d.reason,
        "existing_import": bool(by_id[d.transaction.source_row_identity]),
        "ledger_exact": not income_error and d.outcome == "confirmed_income"
            and incomes.get(income_id(d.transaction.source_row_identity)) == d.row(),
    } for d in deposits]
    try:
        settled = _existing_income_settled(db, daily, imports, **rules)
        settled_error = ""
    except RuntimeError as exc:
        settled = None
        settled_error = str(exc) if str(exc) in SAFE_INCOME_ERRORS else "income_readback_failed"
    details = daily.summary
    parse_ok = bool(parsed.transactions and not parsed.issues and not parsed.balance_consistency_failures)
    outside = not _in_write_window(file, window)
    review = details["true_unknown"] + details["operator_confirmed_non_own_review"]
    reasons = []
    if parsed.issues: reasons.append("parse_issue")
    if parsed.balance_consistency_failures: reasons.append("balance_consistency_failure")
    if not parsed.transactions: reasons.append("parsed_zero")
    if details["collision"]: reasons.append("collision")
    if any(t["existing_count"] and not t["existing_exact"] for t in transactions):
        reasons.append("existing_content_mismatch")
    if review: reasons.append("transaction_review")
    if daily.expense_candidate_identities and outside: reasons.append("new_expense_outside_write_window")
    if settled is not True: reasons.append("unresolved_income")
    if settled_error: reasons.append("income_ledger_invalid")
    report = {
        "file_ref": hashlib.sha256(file["id"].encode()).hexdigest()[:24], "pdf_sha256": daily.pdf_sha256,
        "bank": parsed.adapter_key, "parsed": details["parsed"],
        "existing_duplicate": details["existing_duplicate"],
        "new_eligible": len(daily.expense_candidate_identities),
        "true_unknown": details["true_unknown"],
        "operator_confirmed_non_own_review": details["operator_confirmed_non_own_review"],
        "household_income_needs_review": details["household_income"]["classification"]["needs_review"]["count"],
        "collision": details["collision"],
        "issues": [{"page": x.page, "row": x.row, "reason": x.reason} for x in parsed.issues],
        "balance_consistency_failures": parsed.balance_consistency_failures,
        "account_opening_rows": parsed.account_opening_rows,
        "parse_ok": parse_ok, "outside_write_window": outside,
        "existing_income_settled": settled, "income_settled_error": settled_error,
        "income_ledger_error": income_error,
        "archive_ready": not reasons, "reasons": reasons,
        "duplicate_exact": sum(t["existing_exact"] for t in transactions),
        "duplicate_mismatch": sum(bool(t["existing_count"]) and not t["existing_exact"] for t in transactions),
        "review_existing_exact": sum(t["existing_exact"] and t["classification"] == "needs_review" for t in transactions),
        "transactions": transactions, "income": income_rows,
    }
    return report


def main():
    # Never acquire write scopes, call a writer, save state, or move a file.
    from .google_clients import read_only_drive_service, read_only_sheets_service, download_drive_file
    from .settings import Settings, service_account_source
    from .private_state_bindings import unwrap
    from .drive_run_state import DurableState, DriveStateTransport, StateBinding
    from .bank_recurring_authority import ProtectedBankRecurringAuthorityProvider
    global _stage
    _stage = "baseline_guard"
    root = Path(__file__).resolve().parents[1]
    if os.getenv("GITHUB_ACTIONS") != "true" or os.getenv("KAKEIBO_VALIDATED_MAIN_SHA") != BASELINE:
        raise RuntimeError("diagnosis_baseline_required")
    if subprocess.check_output(["git", "rev-parse", "origin/main"], cwd=root, text=True).strip() != BASELINE:
        raise RuntimeError("diagnosis_main_changed")
    subprocess.run(["git", "merge-base", "--is-ancestor", BASELINE, "HEAD"], cwd=root, check=True,
        capture_output=True)
    _stage = "configuration"
    settings = Settings()
    rules = dict(confirmed_internal_transfers=settings.bank_confirmed_internal_transfers(),
        confirmed_non_own_classifications=settings.bank_confirmed_non_own_classifications())
    _stage = "read_only_clients"
    drive = read_only_drive_service()
    sheets = read_only_sheets_service()
    class ReadOnlyDB:
        sid = settings.spreadsheet_id
        def __init__(self):
            self.cache = {}
        def get(self, range_name):
            if range_name not in self.cache:
                self.cache[range_name] = sheets.spreadsheets().values().get(spreadsheetId=self.sid, range=range_name).execute().get("values", [])
            return self.cache[range_name]
    db = ReadOnlyDB()
    with tempfile.TemporaryDirectory(prefix="bank-diagnosis-") as temp:
        directory = Path(temp)
        _stage = "authority"
        authority_path = directory / "authority.json"
        authority_path.write_text(os.environ["BANK_PDF_RECURRING_AUTHORITY_JSON"], encoding="utf-8")
        authority_path.chmod(0o600)
        policy = ProtectedBankRecurringAuthorityProvider(authority_path, repo_root=root).load()
        if policy.expected_spreadsheet_id != db.sid:
            raise RuntimeError("diagnosis_target_mismatch")
        _stage = "durable_state_read"
        key_path, info = service_account_source()
        key = (info or json.loads(Path(key_path).read_bytes()))["private_key"]
        binding = StateBinding("bank_pdf_drive", db.sid,
            unwrap("KAKEIBO_STATE_FOLDER_ID", os.environ["KAKEIBO_STATE_FOLDER_ID"], key),
            unwrap("BANK_STATE_FILE_ID", os.environ["BANK_STATE_FILE_ID"], key))
        local = directory / "state"
        DurableState(DriveStateTransport(drive, binding), binding).restore(local)
        with sqlite3.connect(f"file:{(local / 'bank-recurring.sqlite3').as_posix()}?mode=ro", uri=True) as con:
            histories = [json.loads(row[0]) for row in con.execute("SELECT summary_json FROM recurring_runs")]
        _stage = "reference_window"
        reference = [h for h in histories if int(datetime.fromisoformat(h["source_window_end"]).timestamp()) == REFERENCE_WINDOW_END_EPOCH]
        if len(reference) != 1:
            raise RuntimeError("diagnosis_reference_window_missing")
        previous = reference[0]
        window = BankPdfWindow(datetime.fromisoformat(previous["source_window_start"]),
            datetime.fromisoformat(previous["source_window_end"]))
        inbox = policy.expected_drive_folder_id
        listing = drive.files().list(q=f"'{inbox}' in parents and trashed=false and mimeType='application/pdf'",
            pageSize=policy.max_files + 1, fields="files(id),nextPageToken", supportsAllDrives=True,
            includeItemsFromAllDrives=True).execute()
        file_ids = [f["id"] for f in listing.get("files", [])]
        if listing.get("nextPageToken") or {hashlib.sha256(x.encode()).hexdigest() for x in file_ids} != FILE_REFERENCES:
            raise RuntimeError("diagnosis_inventory_changed")
        reports = []
        fixed_files, fixed_bytes = [], {}
        for file_id in sorted(file_ids, key=lambda x: hashlib.sha256(x.encode()).hexdigest()):
            _stage = "fixed_file_read"
            file = drive.files().get(fileId=file_id, fields="id,parents,mimeType,trashed,modifiedTime", supportsAllDrives=True).execute()
            if file.get("trashed") or file.get("parents") != [inbox] or file.get("mimeType") != "application/pdf":
                raise RuntimeError("diagnosis_file_changed")
            path = directory / (hashlib.sha256(file_id.encode()).hexdigest() + ".pdf")
            path.write_bytes(download_drive_file(file_id, service=drive))
            fixed_files.append(file)
            fixed_bytes[file_id] = path.read_bytes()
            _stage = "fixed_file_diagnosis"
            from .bank_pdf_pipeline import BankPdfError
            try:
                reports.append(diagnose_pdf(db, path, file=file, window=window, rules=rules,
                    account_alias=policy.account_alias or None))
            except BankPdfError as exc:
                allowed = {"bank_document_empty", "bank_document_unrecognized", "native_text_unavailable",
                    "header_geometry_unresolved", "column_boundary_unresolved"}
                reports.append({"file_ref": hashlib.sha256(file_id.encode()).hexdigest()[:24], "parse_ok": False, "archive_ready": False,
                    "reasons": [str(exc) if str(exc) in allowed else "bank_pdf_parse_error"]})
        # Replay the real recurring decision path against the exact frozen reads.
        # This adapter has no Drive update method and DB has no sheet writer.
        from .bank_pdf_recurring import run_bank_pdf_recurring
        from .aupay_card_recurring import SqliteRecurringRunState
        from .bank_pdf_status import safe_file_statuses
        class FixedInventory:
            def files(self): return self
            def list(self, **kwargs): return self
            def execute(self): return {"files": fixed_files}
        replay_state = SqliteRecurringRunState(directory / "replay.sqlite3", repo_root=root)
        _stage = "fixed_inventory_read_only_replay"
        replays = []
        for _ in range(2):
            replay = run_bank_pdf_recurring(drive_service=FixedInventory(), db=db,
                state=replay_state, authority_provider=ProtectedBankRecurringAuthorityProvider(authority_path, repo_root=root),
                repo_root=root, now=window.end, dry_run=True, preview_window=window,
                download=lambda file_id: fixed_bytes[file_id], **rules)
            if replay.get("failure") or replay.get("written") or replay.get("write_requests"):
                raise RuntimeError("diagnosis_replay_failed")
            replays.append({**{k: replay[k] for k in ("files_seen", "files_withheld", "parse_failed",
                "outside_write_window", "duplicate", "review", "collision", "unresolved_income",
                "files_processed", "written", "write_requests")},
                "file_statuses": safe_file_statuses(replay["file_statuses"])})
        print(json.dumps({"schema": 1, "baseline": BASELINE, "read_only": True,
            "reference": {k: previous.get(k) for k in ("source_window_start", "source_window_end", "files_seen",
                "duplicate", "review", "files_withheld", "parse_failed", "outside_write_window")},
            "rules_digest": hashlib.sha256(json.dumps({k: sorted(v) for k,v in rules.items()}, sort_keys=True).encode()).hexdigest(),
            "reports": reports, "replays": replays}, sort_keys=True))


_stage = "startup"
if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Do not emit API bodies, private configuration, or source exception text.
        print(json.dumps({"read_only": True, "error": "bank_diagnosis_failed", "stage": _stage,
            "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
