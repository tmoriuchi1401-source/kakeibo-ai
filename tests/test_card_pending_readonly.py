import base64
from copy import deepcopy
from datetime import datetime
from email.message import EmailMessage
import json
from types import SimpleNamespace

import pytest

from app import card_pending_readonly as audit
from app.drive_run_state import StateError


def env():
    return dict(GITHUB_ACTIONS='true',GITHUB_EVENT_NAME='workflow_dispatch',
        GITHUB_REPOSITORY='tmoriuchi1401-source/kakeibo-ai',GITHUB_REF=audit.BRANCH,
        GITHUB_RUN_ATTEMPT='1',GITHUB_ACTOR='tmoriuchi1401-source',
        GITHUB_TRIGGERING_ACTOR='tmoriuchi1401-source',RUNNER_ENVIRONMENT='github-hosted',
        RUNNER_OS='Linux',GITHUB_SHA='a'*40,CARD_EVIDENCE_SHA='a'*40,
        GITHUB_WORKFLOW_SHA='a'*40,
        GITHUB_WORKFLOW_REF='tmoriuchi1401-source/kakeibo-ai/.github/workflows/state-cache-inspect.yml@'+audit.BRANCH)


@pytest.mark.parametrize('key,value', [('GITHUB_REF','refs/heads/main'),('GITHUB_SHA','b'*40),
    ('GITHUB_RUN_ATTEMPT','2'),('GITHUB_ACTOR','other'),('GITHUB_TRIGGERING_ACTOR','other'),
    ('GITHUB_EVENT_NAME','schedule'),('RUNNER_ENVIRONMENT','self-hosted'),('RUNNER_OS','Windows'),
    ('RUNNER_DEBUG','1'),('GEMINI_API_KEY','synthetic-key'),('GITHUB_WORKFLOW_REF','other')])
def test_fixed_readonly_entry_denies_other_authorities(key,value):
    value_env=env();audit.boundary(value_env);value_env[key]=value
    with pytest.raises(StateError):audit.boundary(value_env)


class Request:
    method='GET'
    def __init__(self,value):self.value=value
    def execute(self,**kwargs):return deepcopy(self.value)


class Gmail:
    def __init__(self,*,stamp=None,fail=False):
        mail=EmailMessage()
        mail['Subject']='【ご利用詳細】au PAY カード';mail['Message-ID']='<synthetic-card@example.invalid>'
        mail['From']='info@kddi-fs.com';mail['Date']='Tue, 03 Nov 2026 15:00:00 +0900'
        mail.set_content('▼カード情報\nau PAY カード\n本会員さま ご利用分\nNo.001--------\n▼ご利用日\n2026年11月3日\n▼ご利用金額\n1,200円\n▼ご利用先\n合成カード店舗\n')
        self.value=dict(id='synthetic-mail',raw=base64.urlsafe_b64encode(mail.as_bytes()).decode().rstrip('='),
            internalDate=str(int(datetime.fromisoformat(stamp or '2026-11-03T15:00:00+09:00').timestamp())*1000))
        self.fail=fail;self.calls=[]
    def users(self):return self
    def messages(self):return self
    def list(self,**kwargs):self.calls.append('list');return Request({'messages':[{'id':'synthetic-mail'}]})
    def get(self,**kwargs):
        self.calls.append('get')
        if self.fail:raise TimeoutError('private data must not escape')
        return Request(self.value)


def checkpoint(monkeypatch):
    monkeypatch.setattr(audit,'_rows',lambda *args:[{'successful_window_end':'2026-11-03T14:45:43+09:00'}])


def test_existing_parser_canonical_plan_used_without_financial_permission(monkeypatch):
    checkpoint(monkeypatch);gmail=Gmail()
    result=audit.collect(gmail,[],{})
    assert result['messages']==result['raw_transactions']==result['canonical_transactions']==1
    assert result['absent_raw_identities']==1 and result['conflicting_raw_identities']==0
    assert result['unresolved_card_exclusion_ready'] is False
    rendered=json.dumps(result)
    assert '1200' not in rendered and 'synthetic-mail' not in rendered and '合成カード店舗' not in rendered
    assert gmail.calls==['list','get']


def test_mail_received_after_original_start_cannot_reconstruct_failed_collection(monkeypatch):
    checkpoint(monkeypatch)
    with pytest.raises(StateError,match='collection_incomplete'):
        audit.collect(Gmail(stamp='2026-11-04T21:00:20+09:00'),[],{})


def test_getonly_rejects_mutation_and_does_not_retry_failed_read():
    from app.aupay_card_window_evidence import GetOnly, EvidenceError
    import time
    gmail=Gmail(fail=True);readonly=GetOnly(gmail,time.monotonic()+10)
    with pytest.raises(EvidenceError,match='mutation_forbidden'):readonly.users().messages().delete(id='x')
    with pytest.raises(TimeoutError):readonly.users().messages().get(id='x')
    assert gmail.calls==['get']


def test_auth_failure_is_reported_without_exception_or_credential_text(monkeypatch,capsys):
    from app.amazon_gmail_preview import GmailPreviewAuthError
    def failed(_):raise GmailPreviewAuthError('synthetic-secret-private-error')
    monkeypatch.setattr(audit,'run',failed)
    assert audit.main()==1
    result=json.loads(capsys.readouterr().out)
    assert result['error']=='existing_gmail_secret_auth_unavailable'
    assert result['financial_write_count']==result['state_writes']==result['real_ai_requests']==0


def test_workflow_keeps_evidence_secrets_separate_from_synthetic_receipt_job():
    from pathlib import Path
    import yaml
    workflow=yaml.safe_load((Path(__file__).resolve().parents[1]/'.github/workflows/state-cache-inspect.yml').read_text())
    job=workflow['jobs']['card-pending-evidence']
    assert "inputs.operation == 'card-pending-evidence'" in job['if']
    assert "inputs.receipt_e2e_phase == 'none'" in job['if']
    credentials=job['steps'][-1]['env']
    assert 'GOOGLE_GMAIL_TOKEN_JSON' in credentials
    assert not {'GEMINI_API_KEY','GOOGLE_RECEIPT_OWNER_TOKEN_JSON','GOOGLE_RECEIPT_E2E_OWNER_TOKEN_JSON'} & credentials.keys()
    assert not any('upload-artifact' in step.get('uses','') for step in job['steps'])
