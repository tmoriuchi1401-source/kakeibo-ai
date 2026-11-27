from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization

from app import auto_expense_reconciliation as op,google_clients,drive_run_state,projection_store,projection_cache
from app import aupay_card_window_preview as window,aupay_card_reconciliation_entry as entry
from app.aupay_card_aliases import digest
from app import aupay_card_first_window_apply as apply_module,aupay_card_incremental_recovery as recovery
from test_private_state_bindings import key
from test_card_selected_expenses import case,C1,Transport
from test_auto_expense_reconciliation import release_case
from test_aupay_card_reconciliation_entry import capsule


def hosted(monkeypatch,tmp_path,key,apply):
    db,state,durable,registry,args=case(tmp_path)
    _,parent,_,_,release=release_case(monkeypatch)
    native=durable.transport;native.writes.clear()
    now=datetime.now(timezone.utc);head='a'*40;tree='b'*40
    acl={'permissions':[{'id':'owner','type':'user','role':'owner'}]}
    financial=db.financial()
    record=dict(release['record'],bindings=dict(spreadsheet_id=db.sid,folder_id='folder',card_id='file',parent_id='parent',projection_id='projection'),
        parent_before_sha256=op.sha(parent.read()),parent_before_metadata_digest='parent-metadata',
        native_before_sha256=op.sha(native.read()),native_before_metadata_digest='card-metadata',
        financial_write_authority=False,settings_without_validated_sha256='c'*64,
        financial_sha256=digest({str(k):v for k,v in financial.items()}),protected_document_keys=['money'],
        documents_sha256=digest({'money':None}),rules_sha256=digest([]),acl_sha256=digest({k:acl for k in ('card','parent','folder','ledger')}),
        other_native=[dict(file_id='bank',sha256=op.sha(b'bank'))])
    monkeypatch.setattr(op,'RECORD_SHA256',digest(record));monkeypatch.setattr(entry,'LABEL',op.LABEL)
    rsa=serialization.load_pem_private_key(key.encode(),password=None)
    att=dict(record_sha256=digest(record),code_tree_sha256=tree,owner_approved=True,scope=op.SCOPE,
        main_sha=head,controls_sha256='c'*64,operation='release' if apply else 'preview',observed_at=now.isoformat(),
        approval={'expires_at':(now+timedelta(seconds=600)).isoformat()})
    env=dict(GITHUB_ACTIONS='true',GITHUB_REF='refs/heads/main',GITHUB_REPOSITORY='tmoriuchi1401-source/kakeibo-ai',
        KAKEIBO_PRODUCTION_ENABLED='true',GITHUB_SHA=head,WORKFLOW_SHA=head,KAKEIBO_VALIDATED_MAIN_SHA=head,
        GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_ACTOR='tmoriuchi1401-source',GITHUB_TRIGGERING_ACTOR='tmoriuchi1401-source',
        GITHUB_RUN_ATTEMPT='1',GITHUB_RUN_ID='42',RUNNER_ENVIRONMENT='github-hosted',
        WORKFLOW_REF='tmoriuchi1401-source/kakeibo-ai/.github/workflows/kakeibo-production.yml@refs/heads/main',
        MANUAL_SCOPE='card_expenses',CONFIRM='APPLY',CATEGORY_RULE_AUTO_APPLY_ENABLED='true',KAKEIBO_AMAZON_MONEY_MODE='confirmed-v1',
        KAKEIBO_BANK_SCHEDULE_ENABLED='true',KAKEIBO_CORE_SCHEDULE_ENABLED='false',KAKEIBO_LEGACY_DISABLED='true',
        KAKEIBO_SCHEDULE_ENABLED='false',CATEGORY_BACKFILL_SHEET_RUNNER_ENABLED='false',SPREADSHEET_ID=db.sid,
        KAKEIBO_STATE_FOLDER_ID='folder',AUPAY_CARD_STATE_FILE_ID='file',KAKEIBO_RUN_LEDGER_FILE_ID='parent',
        GOOGLE_SERVICE_ACCOUNT_JSON=json.dumps({'private_key':key}),RUNNER_TEMP=str(tmp_path))
    env['CARD_EXPENSE_RECONCILIATION']=capsule(record,att,rsa)
    def github(path):
        if path=='git/ref/heads/main':return {'object':{'sha':head}}
        if path=='actions/runs/42':return dict(head_sha=head,run_attempt=1,event='workflow_dispatch',status='in_progress',path='.github/workflows/kakeibo-production.yml')
        if path=='actions/runs/'+str(op.FAILED_RUN):return record['failed_run']
        assert path.startswith('actions/runs?');return {'total_count':0,'workflow_runs':[]}
    monkeypatch.setattr(window,'GitHub',lambda env:SimpleNamespace(get=github))
    monkeypatch.setattr('subprocess.check_output',lambda *a,**k:tree+'\n')
    def transport(service,binding):return native if binding.source=='au_pay_card_gmail' else parent
    monkeypatch.setattr(drive_run_state,'DriveStateTransport',transport)
    class ReadState:
        def __init__(self,service,binding):self.binding=binding
        def read(self):return b'bank' if self.binding.source=='bank_pdf_drive' else transport(None,self.binding).read()
        def observe(self):
            return self.read(),dict(sha256=op.sha(self.read()),metadata_digest='card-metadata' if self.binding.source=='au_pay_card_gmail' else 'parent-metadata')
    monkeypatch.setattr(window,'ReadOnlyState',ReadState)
    class ReadOnly:
        def files(self):return self
        def spreadsheets(self):return self
        def values(self):return self
        def get(self,**kw):return SimpleNamespace(execute=lambda **k:({'values':[]} if 'range' in kw else acl))
    ro=ReadOnly();writer_factories=[]
    monkeypatch.setattr(google_clients,'read_only_drive_service',lambda:ro)
    monkeypatch.setattr(google_clients,'read_only_sheets_service',lambda:ro)
    monkeypatch.setattr(google_clients,'drive_service',lambda:writer_factories.append('existing-state-writer') or object())
    monkeypatch.setattr(google_clients,'sheets_service',lambda:pytest.fail('financial client forbidden'))
    monkeypatch.setattr(apply_module,'financial_data',lambda *a,include_controls:financial)
    monkeypatch.setattr(projection_store,'DriveProjectionStore',lambda *a,**k:SimpleNamespace(read=lambda key:None))
    monkeypatch.setattr(projection_cache,'RollingProjectionStore',lambda store:store)
    monkeypatch.setattr(recovery,'preserved_unknown_proofs',lambda *a:{41825241169:True,45899587125:True,45965280576:True})
    return env,native,parent,writer_factories


@pytest.mark.parametrize('apply',[False,True])
def test_actual_hosted_connection_signed_capsule_archive_and_existing_api(monkeypatch,tmp_path,key,apply):
    env,native,parent,factories=hosted(monkeypatch,tmp_path,key,apply)
    result=op.run_from_environment(env,apply=apply)
    assert result['financial_write_requests']==0
    assert len(parent.writes)==int(apply) and len(native.writes)==2*int(apply)
    if apply:
        assert json.loads(parent.read())['sources']['auto_expense']['phase']=='ready'
        from app.aupay_card_reservation_recovery import summaries
        archive=summaries(json.loads(native.read()))[op.ARCHIVE_RUN]['operator_reconciliation']
        assert archive['failed_run']['conclusion']=='failure' and not archive['financial_write_reached']
        assert archive['operator_run']['id']==42 and archive['checkpoint']==C1
        with pytest.raises(RuntimeError,match='native_changed|no_replay'):op.run_from_environment(env,apply=True)
        assert len(parent.writes)==1 and len(native.writes)==2
    else:assert not factories


@pytest.mark.parametrize('field,value',[('GITHUB_RUN_ATTEMPT','2'),('MANUAL_SCOPE','core'),
    ('CARD_EXPENSE_REQUEST','also-finance'),('CATEGORY_RULE_AUTO_APPLY_ENABLED','false'),
    ('RUNNER_ENVIRONMENT','self-hosted'),('KAKEIBO_LEGACY_DISABLED','false')])
def test_wrong_context_and_effective_settings_never_create_state_writer(monkeypatch,tmp_path,key,field,value):
    env,native,parent,factories=hosted(monkeypatch,tmp_path,key,True);env[field]=value
    with pytest.raises(RuntimeError):op.run_from_environment(env,apply=True)
    assert not native.writes and not parent.writes and not factories


def test_state_only_ttl_extension_preserves_original_financial_default(monkeypatch,tmp_path,key):
    env,native,parent,factories=hosted(monkeypatch,tmp_path,key,True)
    rsa=serialization.load_pem_private_key(key.encode(),password=None)
    now=datetime.now(timezone.utc)
    with pytest.raises(RuntimeError,match='repair_signed_approval_invalid'):
        entry.decode_capsule(env['CARD_EXPENSE_RECONCILIATION'],rsa,'b'*40,now,
            record_sha256=op.RECORD_SHA256,label=op.LABEL,scope=op.SCOPE)
    assert not native.writes and not parent.writes and not factories
