"""Explicitly approved AI-free PR-B intake only; no copy, auth, Start or writer."""
import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path


def main():
    logging.disable(logging.CRITICAL)
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('config','manifest','attempt-db','diagnostic','original-file','copy-file','result'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--enable-intake',action='store_true')
    args=parser.parse_args()
    config=json.loads(Path(args.config).read_text(encoding='utf-8'))
    required={'schema','enabled','financial_write_enabled','script_id','folder_id',
        'owner_email','original_parent_id','oauth_expiry'}
    if (set(config)!=required or config['schema']!='receipt-review-reuse-runner-v1'
            or config['enabled'] is not True or config['financial_write_enabled'] is not False
            or not args.enable_intake):
        raise ValueError('reuse_runner_not_enabled')
    # Read explicit token before any existing dependency can load settings/.env.
    token=os.environ.get('RECEIPT_REVIEW_OWNER_ACCESS_TOKEN','')
    expiry=datetime.fromisoformat(config['oauth_expiry'])
    if expiry.tzinfo is None or expiry<=datetime.now(timezone.utc):raise ValueError('expired_owner_token')
    if not token or any(c.isspace() for c in token):raise ValueError('explicit_owner_token_required')
    manifest=json.loads(Path(args.manifest).read_text(encoding='utf-8'))
    original_bytes=Path(args.original_file).read_bytes();copy_bytes=Path(args.copy_file).read_bytes()
    from app.receipt_review_reuse import ReuseFence, build_reused_case, intake_reused, original_metadata_matches
    from app.receipt_review_diagnostics import ReviewDiagnostics
    from app.receipt_review_worker import ScriptPort
    from app.utils import now_jst_string
    import requests
    # Validate confirmed one-item data BEFORE creating an attempt DB or sending IO.
    build_reused_case(manifest,clock=now_jst_string)
    diagnostics=ReviewDiagnostics(args.diagnostic)
    with Path(args.result).open('x',encoding='utf-8') as output:
        output.write('{"state":"STARTED_NO_AUTOMATIC_RETRY"}\n');output.flush();os.fsync(output.fileno())
        fence=ReuseFence(args.attempt_db)
        class ExpiringSession(requests.Session):
            def request(self,*a,**k):
                if expiry<=datetime.now(timezone.utc):raise ValueError('expired_owner_token')
                return super().request(*a,**k)
        with ExpiringSession() as session:
            session.trust_env=False
            session.headers['Authorization']='Bearer '+token
            # No AuthorizedSession, refresh, retries, dispatch, SDK or financial DB.
            def live():
                if expiry<=datetime.now(timezone.utc):raise ValueError('expired_owner_token')
            class ExpiringPort(ScriptPort):
                def rpc(self,*a,**k):live();return super().rpc(*a,**k)
                def metadata(self,*a,**k):live();return super().metadata(*a,**k)
                def source_check(self,*a,**k):live();return super().source_check(*a,**k)
            port=ExpiringPort(session,script_id=config['script_id'],folder_id=config['folder_id'],owner_email=config['owner_email'])
            def original_check(source):
                live()
                reply=session.get('https://www.googleapis.com/drive/v3/files/'+source['source_id'],
                    params={'fields':'id,version,mimeType,trashed,parents,owners(emailAddress),sha256Checksum,appProperties'},
                    timeout=20,allow_redirects=False)
                if reply.status_code!=200:raise RuntimeError('original_read_unknown')
                return original_metadata_matches(reply.json(),source,
                    parent_id=config['original_parent_id'],owner_email=config['owner_email'])
            result=intake_reused(manifest,original_bytes=original_bytes,copy_bytes=copy_bytes,
                original_check=original_check,port=port,fence=fence,diagnostics=diagnostics,clock=now_jst_string)
        output.write(json.dumps(result,separators=(',',':'))+'\n');output.flush();os.fsync(output.fileno())
    print('編集中・未登録／AI送信0／金融write 0')
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception:
        print('停止・結果不明／自動再送禁止')
        raise SystemExit(3)
