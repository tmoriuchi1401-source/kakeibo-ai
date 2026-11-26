"""Fixed failed-card evidence only. No writer, state save, retry or release."""
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta
import hashlib
import io
import json
import logging
import os
import re
import time
from types import SimpleNamespace

from .drive_run_state import StateBinding, StateError, validate
from .production_ledger import validate_ledger
from .aupay_card_incremental_recovery import _rows
from .aupay_card_window_evidence import CapturedGmail, GetOnly
from .aupay_mail_pipeline import AuPayCardMailPipeline
from .transaction_plan import normalize_card_transaction, reconcile_transactions
from .aupay_card_apply_plan import build_canonical_apply_plan
from .aupay_card_executor import ExistingCanonicalRecord
from .page_receipt_model import digest

BRANCH = 'refs/heads/codex/receipt-canary-resume'
FAILED_RUN = '41825241169'
STARTED = datetime.fromisoformat('2026-11-04T20:58:12+09:00')
FINISHED = datetime.fromisoformat('2026-11-04T21:00:33+09:00')
NATIVE_SHA256 = 'c5a1b95eb9dd62d3ba5950adac63967902061355d7f9f6b0cfcd86dfd46b7fe7'


def boundary(env):
    if (env.get('GITHUB_ACTIONS') != 'true' or env.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
            or env.get('GITHUB_REPOSITORY') != 'tmoriuchi1401-source/kakeibo-ai'
            or env.get('GITHUB_REF') != BRANCH or env.get('GITHUB_RUN_ATTEMPT') != '1'
            or env.get('GITHUB_ACTOR') != 'tmoriuchi1401-source'
            or env.get('GITHUB_TRIGGERING_ACTOR') != 'tmoriuchi1401-source'
            or env.get('GITHUB_WORKFLOW_REF') != 'tmoriuchi1401-source/kakeibo-ai/.github/workflows/state-cache-inspect.yml@'+BRANCH
            or env.get('RUNNER_ENVIRONMENT') != 'github-hosted' or env.get('RUNNER_OS') != 'Linux'
            or not re.fullmatch('[a-f0-9]{40}', env.get('CARD_EVIDENCE_SHA', ''))
            or env.get('GITHUB_SHA') != env['CARD_EVIDENCE_SHA']
            or env.get('GITHUB_WORKFLOW_SHA') != env['CARD_EVIDENCE_SHA']
            or env.get('RUNNER_DEBUG') == '1' or env.get('GEMINI_API_KEY')):
        raise StateError('card_evidence_boundary_required')


def collect(gmail, imports, native):
    """Reuse strict Gmail capture/parser/canonical projection in one process."""
    rows = _rows(native, 'recurring.sqlite3', 'recurring_checkpoint')
    if len(rows) != 1 or rows[0]['successful_window_end'] != '2026-11-03T14:45:43+09:00':
        raise StateError('card_evidence_checkpoint_changed')
    start = datetime.fromisoformat(rows[0]['successful_window_end'])-timedelta(days=2)
    capture = CapturedGmail(gmail, window=SimpleNamespace(start=start, end=FINISHED))
    query='in:anywhere from:kddi-fs.com subject:"【ご利用詳細】au PAY カード" after:'+str(int(start.timestamp()))+' before:'+str(int(FINISHED.timestamp()))
    money, notices = [], []
    transactions, collection = AuPayCardMailPipeline._collect(capture, query, 500,
        sleeper=lambda _: None, request_interval=0, money_records=money, money_notices=notices)
    if (not collection['collection_complete'] or collection['found'] != collection['fetched']
            or collection['found'] != len(capture.inventory)
            or any(int(r['internal_date_ms']) >= int(STARTED.timestamp())*1000 for r in capture.inventory)):
        raise StateError('card_evidence_collection_incomplete')
    normalized = [normalize_card_transaction(t) for t in transactions]
    reconciliation = reconcile_transactions(transactions, imports)
    plan = build_canonical_apply_plan(collection, reconciliation)
    exact = conflicts = absent = 0
    for tx in normalized:
        matches = [r for r in imports if r and r[0] == tx.identity]
        if not matches:
            absent += 1
            continue
        expected = ExistingCanonicalRecord.from_candidate(tx)
        actual = []
        for row in matches:
            r = list(row)+['']*max(0,12-len(row))
            actual.append(ExistingCanonicalRecord(r[0],r[2],r[3],str(r[4]),str(r[5]),
                int(r[6]),str(r[7]),str(r[10]),str(r[11])))
        if actual == [expected]: exact += 1
        else: conflicts += 1
    # Per-transaction/source identities and financial values never leave here.
    return dict(messages=collection['found'], raw_transactions=len(transactions),
        canonical_transactions=reconciliation['summary']['canonical_transactions'],
        exact_raw_identities=exact, absent_raw_identities=absent, conflicting_raw_identities=conflicts,
        canonical_candidate_count=len(plan.candidates), canonical_plan_status=plan.status,
        parser_review_count=collection['parser_review_mail_count'],
        parser_review_lines=collection['review_line_items'], money_inputs=len(money), notices=len(notices),
        source_inventory_sha256=digest(capture.inventory), canonical_plan_sha256=digest(plan.summary()),
        recovery_classification='unknown_missing_failed_attempt_journal_or_before_snapshot',
        unresolved_card_exclusion_ready=False)


def run(env):
    boundary(env)
    from .google_clients import read_only_drive_service, read_only_sheets_service
    from .amazon_gmail_preview import gmail_readonly_service
    from .private_state_bindings import unwrap
    from .aupay_card_window_preview import ReadOnlyState
    from .aupay_card_first_window_apply import financial_data
    info = json.loads(env['GOOGLE_SERVICE_ACCOUNT_JSON'])
    ids = {name:unwrap(name,env[name],info['private_key']) for name in
        ('KAKEIBO_STATE_FOLDER_ID','AUPAY_CARD_STATE_FILE_ID','KAKEIBO_RUN_LEDGER_FILE_ID')}
    deadline=time.monotonic()+300
    drive=GetOnly(read_only_drive_service(),deadline)
    sheets=GetOnly(read_only_sheets_service(),deadline)
    sid,folder=env['SPREADSHEET_ID'],ids['KAKEIBO_STATE_FOLDER_ID']
    card_binding=StateBinding('au_pay_card_gmail',sid,folder,ids['AUPAY_CARD_STATE_FILE_ID'])
    parent_binding=StateBinding('production_run',sid,folder,ids['KAKEIBO_RUN_LEDGER_FILE_ID'])
    card=ReadOnlyState(drive,card_binding); parent=ReadOnlyState(drive,parent_binding)
    raw,card_proof=card.observe(); parent_raw,parent_proof=parent.observe()
    native=validate(raw,card_binding); ledger=validate_ledger(parent_raw,parent_binding)
    if hashlib.sha256(raw).hexdigest()!=NATIVE_SHA256 or native['phase']!='pending' or ledger['sources']['aupay_card']['phase']!='pending':
        raise StateError('card_evidence_fixed_pending_changed')
    before=financial_data(sheets,sid)
    gmail=GetOnly(gmail_readonly_service(env['GOOGLE_GMAIL_TOKEN_JSON']),deadline)
    result=collect(gmail,before[('取込データ','UNFORMATTED_VALUE')][1:],native)
    if (card.observe()[1]!=card_proof or parent.observe()[1]!=parent_proof or financial_data(sheets,sid)!=before):
        raise StateError('card_evidence_snapshot_changed')
    return dict(result,failed_run=FAILED_RUN,native_sha256=card_proof['sha256'],
        parent_sha256=parent_proof['sha256'],independent_readback_unchanged=True,
        money_reconciliation_complete=False,financial_write_count=0,state_writes=0,real_ai_requests=0)


def main():
    logging.disable(logging.CRITICAL)
    try:
        with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            result=run(dict(os.environ))
        result=dict(success=True,**result)
    except BaseException as error:
        from .amazon_gmail_preview import GmailPreviewAuthError
        result=dict(success=False,error='existing_gmail_secret_auth_unavailable' if isinstance(error,GmailPreviewAuthError)
            else str(error) if isinstance(error,StateError) else 'card_evidence_failed_private_details_suppressed',
            financial_write_count=0,state_writes=0,real_ai_requests=0)
        if isinstance(error,GmailPreviewAuthError):
            allowed={'gmail_token_status: invalid_json','gmail_token_status: invalid',
                'gmail_token_status: expired','gmail_token_status: refresh_failed',
                'gmail_token_status: invalid_or_revoked','gmail_scope_status: insufficient_or_unexpected',
                'gmail_scope_status: insufficient'}
            result['auth_error_code']=str(error) if str(error) in allowed else 'unclassified_auth_failure'
    print(json.dumps(result,ensure_ascii=True,sort_keys=True))
    return 0 if result['success'] else 1


if __name__=='__main__':
    raise SystemExit(main())
