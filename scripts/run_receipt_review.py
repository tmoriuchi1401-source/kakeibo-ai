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

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--attempt-db", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("intake")
    commands.add_parser("pending")
    watch = commands.add_parser("watch")
    watch.add_argument("--poll-seconds",type=int,default=5)
    watch.add_argument("--max-polls",type=int,default=120)
    validate = commands.add_parser("validate")
    validate.add_argument("request_id")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    required = {"schema","enabled","financial_write_enabled","script_id","folder_id","owner_email","categories"}
    if (set(config) != required or config["schema"] != "receipt-review-runner-b1" or config["enabled"] is not True
            or config["financial_write_enabled"] is not False):
        raise ValueError("review_runner_disabled_or_invalid")
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
        if args.command == "intake":
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            from app.drive_receipts import process_review_inbox
            from app.gemini_ai import GeminiAI
            from app.receipt_pipeline import ReceiptPipeline
            from app.receipt_review_cases import ReviewPipeline
            class Categories:
                def categories(self): return config["categories"]
            def make_ai():
                if not ai_key: raise ValueError("explicit_review_ai_key_required")
                return GeminiAI(ai_key,request_attempts=1)
            pipeline = ReceiptPipeline(Categories(),None,gemini_factory=make_ai)
            review = ReviewPipeline(pipeline,port,metadata=port.metadata,fence=fence)
            def download(sid):
                reply = session.get("https://www.googleapis.com/drive/v3/files/"+sid,params={"alt":"media"},
                    timeout=30,allow_redirects=False)
                if reply.status_code != 200: raise RuntimeError("review_download_unknown")
                return reply.content
            service = build("drive","v3",credentials=Credentials(token=token),cache_discovery=False)
            try:
                results = process_review_inbox(config["folder_id"],review,service=service,download=download)
                for result in results: print(result["status"])
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
    except Exception:
        print("停止・結果不明／自動再送禁止")
        status = 3
    raise SystemExit(status)
