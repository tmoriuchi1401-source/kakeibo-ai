"""Opt-in PR-B runner. Never run against real data without target approval.

python -m scripts.run_receipt_review --config PRIVATE_JSON --attempt-db PRIVATE_DB
    intake | pending | validate UUID

No ambient production credentials, SA keys, workflow dispatch, or financial DB.
Use an already approved owner OAuth access token; expiration stops this process.
"""
import argparse
import json
import os
from pathlib import Path
import time
import logging
import sqlite3

_diagnostics = None


def review_categories(value):
    if (not isinstance(value, list) or not value or any(
            not isinstance(pair, list) or len(pair) != 2 or
            any(not isinstance(cell, str) or not cell.strip() for cell in pair)
            for pair in value)):
        raise ValueError('invalid_review_categories')
    return [tuple(pair) for pair in value]

def main():
    global _diagnostics
    logging.disable(logging.CRITICAL)  # Private allowlist replaces SDK/body logs in this runner only.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--attempt-db", required=True)
    parser.add_argument("--diagnostic", help="New private diagnostic file; required for intake, never reuse")
    parser.add_argument('--deadline-epoch', type=float)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("intake")
    commands.add_parser("pending")
    commands.add_parser("cycle", help="Validate registrations and intake at most one new receipt; financial writes OFF")
    watch = commands.add_parser("watch")
    watch.add_argument("--poll-seconds",type=int,default=5)
    watch.add_argument("--max-polls",type=int,default=120)
    validate = commands.add_parser("validate")
    validate.add_argument("request_id")
    args = parser.parse_args()
    if args.command in ('intake', 'cycle') and not args.diagnostic:
        raise ValueError('private_diagnostic_required')
    if args.diagnostic:
        from app.receipt_review_diagnostics import ReviewDiagnostics
        _diagnostics = ReviewDiagnostics(args.diagnostic, deadline_epoch=args.deadline_epoch)
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    required = {"schema","enabled","financial_write_enabled","script_id","folder_id","owner_email","categories"}
    operational = {'production_spreadsheet_id', 'review_spreadsheet_id', 'model', 'max_case_count'}
    daily = operational - {'max_case_count'} | {'max_open_case_count'}
    if (set(config) not in (required, required | operational, required | daily) or config["schema"] != "receipt-review-runner-b1" or config["enabled"] is not True
            or config["financial_write_enabled"] is not False):
        raise ValueError("review_runner_disabled_or_invalid")
    if args.command == 'cycle':
        if set(config) == required | daily:
            if type(config['max_open_case_count']) is not int or not 1 <= config['max_open_case_count'] <= 50:
                raise ValueError('daily_operation_bounds_invalid')
        elif (set(config) != required | operational or type(config['max_case_count']) is not int
                or not 1 <= config['max_case_count'] <= 4):
            raise ValueError('limited_operation_config_required')
    # Do not import settings/credentials or read .env for an OAuth token.
    token = os.environ.get("RECEIPT_REVIEW_OWNER_ACCESS_TOKEN", "")
    ai_key = os.environ.get("RECEIPT_REVIEW_GEMINI_API_KEY", "")
    if not token or "\n" in token or "\r" in token:
        raise ValueError("approved_owner_oauth_token_required")
    # Capture explicit credentials BEFORE existing dependencies import settings
    # (which loads .env). Those ambient credentials must not select this runner.
    from app.receipt_review_worker import AttemptFence, ScriptPort, execute_review
    import requests
    fence = AttemptFence(args.attempt_db)
    with requests.Session() as session:
        session.trust_env = False
        session.headers["Authorization"] = "Bearer " + token
        # requests defaults to zero adapter retries; no AuthorizedSession refresh.
        port = ScriptPort(session,script_id=config["script_id"],folder_id=config["folder_id"],owner_email=config["owner_email"])
        history = None
        if set(config) in (required | operational, required | daily):
            from app.receipt_review_history import ReceiptHistory
            history = ReceiptHistory(session, config['production_spreadsheet_id'], config['review_spreadsheet_id'])
        if args.command == 'cycle':
            status = port.status()
            if status['stopped']:
                raise RuntimeError('existing_script_stop_preserved')
            if status['processing_count']:
                raise RuntimeError('existing_claim_outcome_unknown')
            if status['enabled'] is not True:
                print('受付OFF'); return 0
            pending = port.pending()
            if len(pending) != len(set(pending)):
                raise RuntimeError('duplicate_pending_request')
            # Bound validation IO and let registrations made while the PC was
            # offline remain in the immutable Script ledger for later cycles.
            for rid in pending[:1]:
                if fence.attempted(rid):
                    raise RuntimeError('existing_unknown_attempt_preserved')
                outcome = execute_review(rid, port=port, fence=fence, duplicate_check=history.duplicate)
                print(outcome['state'])
                if outcome['state'] == '結果不明・照合待ち':
                    raise RuntimeError('backend_unknown_no_retry')
                if 'existing_receipt_duplicate' in outcome.get('issues', []):
                    raise RuntimeError('backend_duplicate_no_retry')
            status = port.status()
            if status['stopped'] or status['processing_count'] or status['enabled'] is not True:
                raise RuntimeError('backend_state_changed')
            if 'max_open_case_count' in config:
                # Historical results consume no daily allowance. Keep a reserve
                # below the existing Script's 1,000-row read/identity boundary.
                if status['case_count'] >= 900:
                    print('保存容量の確認が必要・新規取込停止'); return 0
                if status['case_count'] - status['request_count'] >= config['max_open_case_count']:
                    print('未登録件数の上限・登録分の検証は継続'); return 0
            elif status['case_count'] >= config['max_case_count']:
                print('限定件数に到達・登録分の検証は継続'); return 0
            # Pending backend work takes priority over another AI request.
            if len(pending) > 1:
                print('受付済み案件を次回へ保持'); return 0
            if args.deadline_epoch is not None and args.deadline_epoch - time.time() < 180:
                print('今回の取込を見送り・原本保持'); return 0
        if args.command in ("intake", 'cycle'):
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            from app.drive_receipts import process_review_inbox
            from app.gemini_ai import GeminiAI
            from app.receipt_pipeline import ReceiptPipeline
            from app.receipt_review_cases import ReviewPipeline
            class Categories:
                def categories(self): return review_categories(config["categories"])
            def make_ai():
                if not ai_key: raise ValueError("explicit_review_ai_key_required")
                return GeminiAI(ai_key, model=config.get('model', 'gemini-3.5-flash-lite'),
                    request_attempts=1,prb_diagnostics=_diagnostics)
            pipeline = ReceiptPipeline(Categories(),None,gemini_factory=make_ai)
            review = ReviewPipeline(pipeline,port,metadata=port.metadata,fence=fence,diagnostics=_diagnostics,
                already_imported=history.already_imported if history else None)
            def download(sid):
                reply = session.get("https://www.googleapis.com/drive/v3/files/"+sid,params={"alt":"media"},
                    timeout=30,allow_redirects=False)
                if reply.status_code != 200: raise RuntimeError("review_download_unknown")
                return reply.content
            service = build("drive","v3",credentials=Credentials(token=token),cache_discovery=False)
            imported_ids = {row[3] for row in history.imports()} if history else set()
            with sqlite3.connect(args.attempt_db) as db:
                db.execute('CREATE TABLE IF NOT EXISTS review_scan_holds (source_id TEXT PRIMARY KEY, version TEXT NOT NULL, reason TEXT NOT NULL)')
                holds = dict((sid, (version, reason)) for sid, version, reason in db.execute('SELECT * FROM review_scan_holds'))
                prior_sources = {key[7:] for (key,) in db.execute("SELECT request_id FROM attempts WHERE request_id LIKE 'source:%'")}
            def eligible(source):
                sid = source['id']
                if [owner.get('emailAddress') for owner in source.get('owners', [])] != [config['owner_email']]:
                    return False
                if sid in imported_ids or sid in prior_sources:
                    return False
                previous = holds.get(sid)
                if previous and (previous[0] == source.get('version') or previous[1] == 'manual_review_only'):
                    return False
                return port.lookup_source(sid, '') is None
            def record_hold(source, result):
                if result['status'] in ('privacy_blocked', 'held'):
                    if result.get('reason') not in ('manual_review_only', 'pdf_manual_structure_review'):
                        raise RuntimeError('source_or_analysis_changed_no_retry')
                    with sqlite3.connect(args.attempt_db) as db:
                        db.execute('INSERT OR REPLACE INTO review_scan_holds VALUES (?,?,?)',
                            (source['id'], source['version'], result['reason']))
            try:
                results = process_review_inbox(config["folder_id"],review,service=service,download=download,
                    eligible=eligible if args.command == 'cycle' else None,
                    on_result=record_hold if args.command == 'cycle' else None)
                for result in results: print(result["status"])
                if args.command != 'cycle' and any(result['status'] != 'review_created' for result in results):
                    return 3
            finally:
                service.close()
            return 0
        code = 0
        polls = args.max_polls if args.command == "watch" else 1
        if not 1 <= polls <= 120 or args.command == "watch" and not 1 <= args.poll_seconds <= 60:
            raise ValueError("review_watch_bounds_invalid")
        for index in range(polls):
            rids = [args.request_id] if args.command == "validate" else port.pending()
            for rid in rids:
                if fence.attempted(rid):
                    print("結果不明・照合待ち／再送禁止")
                    return 3
                outcome = execute_review(rid,port=port,fence=fence)
                print(outcome["state"])  # Never print private snapshots, source IDs, or exceptions.
                if outcome["state"] == "結果不明・照合待ち": return 3
                if outcome["state"] != "検証済み・未記帳": code = 3
            if index+1 < polls: time.sleep(args.poll_seconds)
        return code


if __name__ == "__main__":
    try:
        status = main()
    except Exception as error:
        if _diagnostics is not None and not _diagnostics.failed:
            try:
                _diagnostics.failure('runner', error)
            except Exception:
                pass  # Failed diagnostics also stop; never repeat a side effect.
        print("停止・結果不明／自動再送禁止")
        status = 3
    raise SystemExit(status)
