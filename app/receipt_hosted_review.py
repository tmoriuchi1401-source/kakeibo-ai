"""Owner Execution API transport. Single attempt; no GitHub credential relay.

Uses the existing authorized-user credential format and receiver ScriptLock.
Provisioning, consent and API executable deployment are separate operations.
"""
import json
import re

from .drive_run_state import StateError

SCOPES = (
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/script.scriptapp',
    'https://www.googleapis.com/auth/script.external_request',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/drive.readonly',
)


class OwnerReviewPort:
    def __init__(self, session, script_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{10,150}', script_id):
            raise StateError('receipt_review_script_binding_invalid')
        self.session, self.script_id = session, script_id

    def rpc(self, action, **data):
        if action not in {'lookup', 'pending', 'read', 'intake', 'claim', 'finish', 'rearm', 'status'}:
            raise StateError('receipt_review_command_forbidden')
        response = self.session.post(
            'https://script.googleapis.com/v1/scripts/' + self.script_id + ':run',
            json={'function': 'receiptHostedBackend', 'parameters': [dict(action=action, **data)]},
            timeout=30, allow_redirects=False)
        if response.status_code != 200:
            raise StateError('receipt_review_rpc_unknown')
        body = response.json()
        if (body.get('done') is not True or 'error' in body
                or body.get('response', {}).get('@type') !=
                'type.googleapis.com/google.apps.script.v1.ExecutionResponse'
                or 'result' not in body['response']):
            raise StateError('receipt_review_rpc_unknown')
        return body['response']['result']

    def lookup_source(self, sid, hashed): return self.rpc('lookup', source_id=sid, sha256=hashed)
    def pending(self): return self.rpc('pending')
    def read(self, rid): return self.rpc('read', request_id=rid)
    def intake(self, entry, row): return self.rpc('intake', entry=entry, row=row)
    def claim(self, rid, before): return self.rpc('claim', request_id=rid, before=before)
    def finish(self, rid, before, result): return self.rpc('finish', request_id=rid, before=before, result=result)
    def rearm(self, rid, before, entry): return self.rpc('rearm', request_id=rid, before=before, entry=entry)


def open_review(env, manifest, spreadsheet_id, folder_id):
    """No token minting, interactive consent, SA fallback or broad scope upgrade."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import AuthorizedSession
    config = json.loads(env.get('RECEIPT_HOSTED_REVIEW_BINDING', 'null'))
    if (type(config) is not dict or set(config) != {'script_id', 'owner_email', 'spreadsheet_id', 'folder_id'}
            or config['owner_email'] != manifest['owner_email']
            or not re.fullmatch(r'[A-Za-z0-9_-]{10,150}', config['spreadsheet_id'])
            or config['folder_id'] != folder_id or manifest['spreadsheet_id'] != spreadsheet_id):
        raise StateError('receipt_review_binding_required')
    info = json.loads(env.get('GOOGLE_RECEIPT_OWNER_TOKEN_JSON', 'null'))
    if (type(info) is not dict or info.get('type', 'authorized_user') != 'authorized_user'
            or info.get('token_uri') != 'https://oauth2.googleapis.com/token'
            or any(type(info.get(k)) is not str or not info[k] for k in ('refresh_token','client_id','client_secret'))
            or not set(SCOPES) <= set(info.get('scopes', []))):
        raise StateError('receipt_review_existing_owner_scopes_required')
    credentials = Credentials.from_authorized_user_info(info, scopes=list(SCOPES))
    # AuthorizedSession normally retries an RPC after refreshing a rejected
    # token. Refresh before the request instead; never replay a Sheets command.
    session = AuthorizedSession(credentials, max_refresh_attempts=0)
    port = OwnerReviewPort(session, config['script_id'])
    status = port.rpc('status')
    if (type(status) is not dict or status.get('enabled') is not True or status.get('stopped') is not False
            or status.get('binding') != config or status.get('financial_write_count') != 0):
        raise StateError('receipt_review_receiver_not_ready')
    return port
