"""Explicit owner activation using the existing Google OAuth client and PKCE.

No credential is created unless --activate is supplied after owner approval.
Authorization is completed manually in the owner's browser. No financial call.
"""
import argparse
from datetime import datetime, timezone
import json
import logging
import os
import re
from pathlib import Path

from .run_receipt_review_daily import REQUIRED_SCOPES


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client', required=True)
    parser.add_argument('--owner-email', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--activate', action='store_true')
    args = parser.parse_args()
    if not args.activate:
        raise ValueError('explicit_continuous_owner_authorization_required')
    output = Path(args.output)
    if output.exists():
        raise ValueError('existing_credential_must_not_be_replaced')
    client = json.loads(Path(args.client).read_text(encoding='utf-8'))
    if not re.fullmatch(r'[A-Za-z0-9_-]+\.apps\.googleusercontent\.com', client['installed']['client_id']):
        raise ValueError('existing_oauth_client_required')
    from google_auth_oauthlib.flow import InstalledAppFlow
    import requests
    scopes = sorted(REQUIRED_SCOPES | {'https://www.googleapis.com/auth/script.projects.readonly'})
    flow = InstalledAppFlow.from_client_config(client, scopes, autogenerate_code_verifier=True)
    creds = flow.run_local_server(host='localhost', bind_addr='127.0.0.1', port=0,
        open_browser=False, timeout_seconds=600, access_type='offline', prompt='consent',
        include_granted_scopes='false', login_hint=args.owner_email,
        authorization_prompt_message='OWNER_AUTH_URL {url}',
        success_message='Owner callback received. Independent verification is pending.')
    if not creds.refresh_token or set(creds.granted_scopes or []) != set(scopes):
        raise ValueError('continuous_owner_scopes_invalid')
    with requests.Session() as s:
        s.trust_env = False
        reply = s.get('https://www.googleapis.com/oauth2/v2/userinfo',
            headers={'Authorization':'Bearer ' + creds.token}, timeout=20, allow_redirects=False)
        if reply.status_code != 200 or reply.json().get('email') != args.owner_email:
            raise ValueError('owner_identity_mismatch')
    value = {'client_id':client['installed']['client_id'], 'project_id':client['installed']['project_id'],
        'owner_email':args.owner_email,
        'scopes':scopes, 'refresh_token':creds.refresh_token,
        'authorized_at':datetime.now(timezone.utc).isoformat()}
    output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        json.dump(value, out); out.flush(); os.fsync(out.fileno())
    print('CONTINUOUS_OWNER_AUTH_SAVED; ACTIVATION_NOT_STARTED')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('OWNER_ACTIVATION_STOP', type(error).__name__)
        raise SystemExit(3)
