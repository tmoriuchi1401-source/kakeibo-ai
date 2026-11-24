"""Explicit one-shot reevaluation of a historical tax-only hold (never intake)."""
import argparse
import json
import logging
import os
from pathlib import Path
import subprocess


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('config','attempt-db','state-dir','request-id'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    if not args.execute:
        print('NOT_ACTIVATED');return 0
    logging.disable(logging.CRITICAL)
    state=Path(args.state_dir);state.mkdir(parents=True,exist_ok=True)
    if (state/'STOP.json').exists() or (state/'closed.json').exists():
        print('STOP_PRESERVED');return 3
    config=json.loads(Path(args.config).read_text(encoding='utf-8'))
    if config.get('financial_write_enabled') is not False or config.get('enabled') is not True:
        raise ValueError('policy_runner_disabled')
    token=os.environ.get('RECEIPT_REVIEW_OWNER_ACCESS_TOKEN','')
    if not token or '\n' in token or '\r' in token or not Path(args.attempt_db).is_file():
        raise ValueError('existing_owner_token_and_fence_required')
    import requests
    from app.sheets import SheetsDB
    from app.receipt_pipeline import ReceiptPipeline
    def deny(*args,**kwargs):raise RuntimeError('policy_financial_write_forbidden')
    SheetsDB.__init__=ReceiptPipeline._materialize_result=deny
    from app.receipt_review_policy_recheck import RecheckAttemptFence, RestRecheckReader, execute_recheck
    from app.receipt_review_finish import RestReviewReader
    from app.receipt_review_worker import ScriptPort
    from app.receipt_review_history import ReceiptHistory
    from app.receipt_sheets_authority import UNKNOWN
    result=None;closed=False
    with requests.Session() as s:
        s.trust_env=False;s.headers['Authorization']='Bearer '+token
        try:
            port=ScriptPort(s,script_id=config['script_id'],folder_id=config['folder_id'],owner_email=config['owner_email'])
            reader=RestRecheckReader(s,config['review_spreadsheet_id'],
                RestReviewReader(s,config['review_spreadsheet_id']))
            history=ReceiptHistory(s,config['production_spreadsheet_id'],config['review_spreadsheet_id'])
            result=execute_recheck(args.request_id,reader=reader,port=port,
                fence=RecheckAttemptFence(Path(args.attempt_db)),duplicate_check=history.duplicate)
            if result['state']==UNKNOWN:
                with (state/'STOP.json').open('x',encoding='utf-8') as out:
                    json.dump({'reason':'policy_recheck_unknown','retry':False},out)
        finally:
            gh_off=False;script_off=False
            try:
                reply=subprocess.run(['gh','api','repos/tmoriuchi1401-source/kakeibo-ai/actions/variables/RECEIPT_VALIDATION_ENABLED',
                    '--method','PATCH','--input','-'],input=json.dumps({'name':'RECEIPT_VALIDATION_ENABLED','value':'false'}),
                    capture_output=True,text=True,timeout=20)
                gh_off=reply.returncode==0
            except Exception:pass
            try:
                reply=s.post('https://script.googleapis.com/v1/scripts/'+config['script_id']+':run',
                    json={'function':'receiptReviewStop','parameters':[],'devMode':False},timeout=30,allow_redirects=False)
                body=reply.json() if reply.status_code==200 else {}
                script_off=body.get('response',{}).get('result')=={'state':'受付OFF・金融write OFF'}
            except Exception:pass
            closed=gh_off and script_off
            if not closed and not (state/'STOP.json').exists():
                with (state/'STOP.json').open('x',encoding='utf-8') as out:
                    json.dump({'reason':'policy_shutdown_unconfirmed','retry':False},out)
            with (state/'closed.json').open('x',encoding='utf-8') as out:
                json.dump({'both_off_ack':closed,'financial_write_count':0},out)
    print(json.dumps({'state':result['state'] if result else UNKNOWN,'both_off_ack':closed,'financial_write_count':0},ensure_ascii=True))
    return 0 if result and result['state']!='UNKNOWN' and closed else 3


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception:
        print('STOP_UNKNOWN_NO_RETRY');raise SystemExit(3)
