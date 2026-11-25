"""Existing-PC daily entry; explicit existing OAuth client, local fences, no finance.

Installation/continuous owner authorization is a separate explicit activation.
This file creates no account, server, credential, task or Drive resource.
"""
import argparse
import json
import logging
import os
import re
from pathlib import Path
import sqlite3
import subprocess
import sys
import time


REQUIRED_SCOPES = {'openid'} | {'https://www.googleapis.com/auth/' + name for name in
    ('spreadsheets', 'script.scriptapp', 'script.external_request', 'userinfo.email', 'drive.readonly')}


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def access_token(client, owner, expected_owner, session):
    cfg = client['installed']
    if (not re.fullmatch(r'[A-Za-z0-9_-]+\.apps\.googleusercontent\.com', cfg['client_id'])
            or owner['client_id'] != cfg['client_id'] or owner['project_id'] != cfg['project_id']
            or owner['owner_email'] != expected_owner
            or not REQUIRED_SCOPES <= set(owner['scopes'])
            or not isinstance(owner.get('refresh_token'), str) or not owner['refresh_token']):
        raise ValueError('existing_owner_oauth_binding_invalid')
    # One token exchange per scheduled invocation; no refresh/retry adapter.
    reply = session.post('https://oauth2.googleapis.com/token', data={
        'grant_type': 'refresh_token', 'client_id': cfg['client_id'],
        'client_secret': cfg['client_secret'], 'refresh_token': owner['refresh_token']},
        timeout=25, allow_redirects=False)
    if reply.status_code != 200:
        raise RuntimeError('owner_token_exchange_unknown')
    value = reply.json()
    if not isinstance(value.get('access_token'), str) or value.get('expires_in', 0) < 900:
        raise ValueError('short_owner_token_invalid')
    token = value['access_token']
    identity = session.get('https://www.googleapis.com/oauth2/v2/userinfo',
        headers={'Authorization': 'Bearer ' + token}, timeout=20, allow_redirects=False)
    if identity.status_code != 200 or identity.json().get('email') != expected_owner:
        raise ValueError('owner_identity_mismatch')
    return token


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('config', 'attempt-db', 'fence-seed', 'owner-token', 'client', 'gemini-key', 'state-dir', 'expected-sha'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    state = Path(args.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    stop = state / 'STOP.json'
    if stop.exists():
        print('STOP保持・自動再送なし'); return 3
    config = load(args.config)
    if config.get('financial_write_enabled') is not False:
        raise ValueError('financial_write_must_remain_off')
    if config.get('enabled') is not True:
        print('限定実運用OFF'); return 0
    try:
        repo = Path(__file__).resolve().parents[1]
        if (not re.fullmatch('[a-f0-9]{40}', args.expected_sha)
                or subprocess.check_output(['git','rev-parse','HEAD'], cwd=repo, text=True).strip() != args.expected_sha
                or subprocess.check_output(['git','status','--porcelain'], cwd=repo, text=True).strip()):
            raise ValueError('deployed_python_revision_changed')
        dbpath = Path(args.attempt_db)
        if not dbpath.is_file():
            raise ValueError('existing_permanent_attempt_db_required')
        with sqlite3.connect(dbpath.resolve().as_uri() + '?mode=ro', uri=True) as db:
            consumed = {row[0] for row in db.execute('SELECT request_id FROM attempts')}
        if not set(load(args.fence_seed)['required_fences']) <= consumed:
            raise ValueError('prior_fences_not_preserved')
        import requests
        with requests.Session() as session:
            session.trust_env = False
            token = access_token(load(args.client), load(args.owner_token), config['owner_email'], session)
        # Explicit values are captured by the existing runner before its imports.
        os.environ['RECEIPT_REVIEW_OWNER_ACCESS_TOKEN'] = token
        os.environ['RECEIPT_REVIEW_GEMINI_API_KEY'] = Path(args.gemini_key).read_text(encoding='utf-8').strip()
        from scripts.run_receipt_review import main as cycle
        from app.sheets import SheetsDB
        from app.receipt_pipeline import ReceiptPipeline
        def deny_financial_write(*args, **kwargs):
            raise RuntimeError('financial_write_disabled_for_receipt_review')
        SheetsDB.__init__ = ReceiptPipeline._materialize_result = deny_financial_write
        sys.argv = ['receipt-review', '--config', args.config, '--attempt-db', args.attempt_db,
            '--diagnostic', str(state / ('cycle-' + str(time.time_ns()) + '.jsonl')), 'cycle']
        code = cycle()
        if code:
            raise RuntimeError('receipt_cycle_stopped')
        return 0
    except Exception as error:
        with stop.open('x', encoding='utf-8') as out:
            json.dump({'exception_type': type(error).__name__, 'epoch': time.time(), 'retry': False}, out)
        print('STOP保持・結果不明の再送なし')
        return 3
    finally:
        os.environ.pop('RECEIPT_REVIEW_OWNER_ACCESS_TOKEN', None)
        os.environ.pop('RECEIPT_REVIEW_GEMINI_API_KEY', None)


if __name__ == '__main__':
    raise SystemExit(main())
