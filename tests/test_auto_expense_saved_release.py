"""The saved-archive branch, with real signed capsules and the existing API."""
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace
from urllib.error import HTTPError
import zlib

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest

from app import auto_expense_reconciliation as op, production_flow as flow
from app import aupay_card_window_preview as preview, google_clients as clients
from app import drive_run_state as drive_state, aupay_card_first_window_apply as first
from app import projection_store, projection_cache, aupay_card_incremental_recovery as recovery
from app.aupay_card_aliases import digest
from app.aupay_card_reconciliation_entry import encode
from app.aupay_card_reservation_recovery import summaries
from test_auto_expense_reconciliation import release_case
from test_card_selected_expenses import case


@pytest.fixture(scope='module')
def key():
    # Synthetic key only; cannot be decoded by any production service account.
    return rsa.generate_private_key(public_exponent=65537,key_size=2048)


def capsule(record, att, key):
    signature=key.sign(encode(att),padding.PSS(mgf=padding.MGF1(hashes.SHA256()),salt_length=padding.PSS.MAX_LENGTH),hashes.SHA256())
    body=dict(record=base64.b64encode(encode(record)).decode(),attestation=att,signature=base64.b64encode(signature).decode())
    aes=AESGCM.generate_key(bit_length=256);nonce=os.urandom(12)
    sealed=dict(key=base64.b64encode(key.public_key().encrypt(aes,padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=op.LABEL))).decode(),
        nonce=base64.b64encode(nonce).decode(),data=base64.b64encode(AESGCM(aes).encrypt(nonce,zlib.compress(encode(body)),op.LABEL)).decode())
    return base64.b64encode(encode(sealed)).decode()


def saved_case(monkeypatch,tmp_path,key):
    old,parent,_,_,args=release_case(monkeypatch)
    db,state,durable,_,_=case(tmp_path)
    record=args['record'];record.update(financial_write_authority=False,
        settings_without_validated_sha256='s'*64,bindings=dict(spreadsheet_id=db.sid,folder_id='folder',card_id='file',parent_id='parent',projection_id='projection'),
        parent_before_sha256=op.sha(parent.read()),parent_before_metadata_digest='parent-meta',
        baseline_parent_metadata={'synthetic':True},other_native=[dict(source='bank_pdf_drive',file_id='bank',sha256=op.sha(b'bank'))],
        protected_document_keys=['money','review'],financial_sha256=digest({str(k):v for k,v in db.financial().items()}),
        documents_sha256=digest({'money':None,'review':None}),rules_sha256=digest([]),
        acl_sha256=digest(dict.fromkeys(('card','parent','folder','ledger'),{})))
    monkeypatch.setattr(op,'RECORD_SHA256',digest(record))
    archive=dict(schema=1,kind=op.SCOPE,record_sha256=op.RECORD_SHA256,failed_run=record['failed_run'],operator_run=op.SAVED_OPERATOR,
        original_parent_exact_b64=record['parent_before_raw_b64'],original_parent_revision=record['parent_before_revision'],
        native_before_sha256=record['native_before_sha256'],checkpoint=record['checkpoint'],
        reconciled_parent_exact_b64=base64.b64encode(parent.read()).decode(),parent_sha256=op.sha(parent.read()),
        observation={k:record[k] for k in ('financial_sha256','documents_sha256','rules_sha256','acl_sha256')},
        financial_write_reached=False,exception_identity_confirmed=False)
    monkeypatch.setattr(op,'SAVED_ARCHIVE_SHA256',digest(archive))
    state.record(dict(run_id=op.ARCHIVE_RUN,status='noop',source_window_start=record['checkpoint'],source_window_end=record['checkpoint'],operator_reconciliation=archive),advance_checkpoint=False)
    native=drive_state.envelope(durable.binding,drive_state.snapshot(state.path.parent,durable.binding),generation=129)
    proof=dict(sha256=op.sha(native),metadata_digest='native-meta');monkeypatch.setattr(op,'SAVED_NATIVE_PROOF',proof)
    p=SimpleNamespace(record=record,archive=archive,native=native,parent=parent,old_parent=deepcopy(old),db=db,proof=proof,
        head='a'*40,tree='b'*40,run_id=900000,conflict=False,bank_calls=[],reads=[],transports=[],fault='',key=key)
    after=op.proposed_release(parent.read(),args['binding'],digest(archive))
    p.att=dict(owner_approved=True,record_sha256=digest(record),code_tree_sha256=p.tree,scope=op.SCOPE,
        main_sha=p.head,controls_sha256=record['settings_without_validated_sha256'],operation=op.SAVED_OPERATION,
        observed_at=datetime.now(timezone.utc).isoformat(),approval=dict(expires_at=(datetime.now(timezone.utc)+timedelta(seconds=850)).isoformat(),approval_reference='synthetic independent approval'),
        saved_archive=dict(archive_sha256=digest(archive),native=proof,operator_run=op.SAVED_OPERATOR,
            parent=dict(sha256=op.sha(parent.read()),metadata_digest='parent-meta',revision=record['parent_before_revision']),parent_after_sha256=op.sha(after)))
    pem=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()
    p.env=dict(GITHUB_ACTIONS='true',GITHUB_REF='refs/heads/main',GITHUB_REPOSITORY=preview.REPO,KAKEIBO_PRODUCTION_ENABLED='true',
        GITHUB_SHA=p.head,KAKEIBO_VALIDATED_MAIN_SHA=p.head,GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_ACTOR='tmoriuchi1401-source',
        GITHUB_TRIGGERING_ACTOR='tmoriuchi1401-source',GITHUB_RUN_ATTEMPT='1',GITHUB_RUN_ID=str(p.run_id),RUNNER_ENVIRONMENT='github-hosted',
        WORKFLOW_REF=preview.REPO+'/.github/workflows/kakeibo-production.yml@refs/heads/main',WORKFLOW_SHA=p.head,
        MANUAL_SCOPE='card_expenses',CONFIRM='APPLY',GOOGLE_SERVICE_ACCOUNT_JSON=json.dumps({'private_key':pem}),
        CATEGORY_RULE_AUTO_APPLY_ENABLED='true',KAKEIBO_AMAZON_MONEY_MODE='confirmed-v1',KAKEIBO_BANK_SCHEDULE_ENABLED='true',
        KAKEIBO_LEGACY_DISABLED='true',KAKEIBO_SCHEDULE_ENABLED='false',CATEGORY_BACKFILL_SHEET_RUNNER_ENABLED='false',
        SPREADSHEET_ID=db.sid,KAKEIBO_STATE_FOLDER_ID='folder',AUPAY_CARD_STATE_FILE_ID='file',KAKEIBO_RUN_LEDGER_FILE_ID='parent')
    class GitHub:
        def __init__(self,env):pass
        def get(self,path):
            if path=='git/ref/heads/main':return {'object':{'sha':p.head}}
            if path=='actions/runs/'+str(p.run_id):return dict(head_sha=p.head,run_attempt=1,event='workflow_dispatch',status='in_progress',path=op.SAVED_OPERATOR['path'])
            if path=='actions/runs/'+str(op.FAILED_RUN):return deepcopy(record['failed_run'])
            if path=='actions/runs/'+str(op.SAVED_OPERATOR['id']):return op.SAVED_OPERATOR|dict(status='completed',conclusion='failure')
            if path.startswith('actions/workflows/'):
                return dict(total_count=1,workflow_runs=[dict(id=p.run_id,display_title=op.RELEASE_TITLE)])
            if path.startswith('actions/runs?status='):
                return dict(total_count=1 if p.conflict else 0,workflow_runs=[{'id':123}] if p.conflict else [])
            raise AssertionError('unexpected GitHub GET')
    class ReadState:
        def __init__(self,service,binding):self.binding=binding
        def observe(self):
            p.reads.append(self.binding.source)
            if self.binding.source=='au_pay_card_gmail':return p.native,dict(sha256=op.sha(p.native),metadata_digest='native-meta')
            if self.binding.source=='bank_pdf_drive':return b'bank',{}
            return p.parent.read(),dict(sha256=op.sha(p.parent.read()),metadata_digest='parent-meta')
        def read(self):return self.observe()[0]
    class RO:
        def files(self):return self
        def get(self,**kwargs):
            return SimpleNamespace(execute=lambda **kw:dict(headRevisionId=record['parent_before_revision']) if kwargs.get('fields')=='headRevisionId' else {})
        def spreadsheets(self):return self
        def values(self):return self
    ro=RO()
    monkeypatch.setattr(preview,'GitHub',GitHub);monkeypatch.setattr(preview,'ReadOnlyState',ReadState)
    monkeypatch.setattr(clients,'read_only_drive_service',lambda:ro);monkeypatch.setattr(clients,'read_only_sheets_service',lambda:ro)
    monkeypatch.setattr(clients,'drive_service',lambda:object())
    def transport(service,binding):
        p.transports.append(binding.source);assert binding.source=='production_run','native writer constructed'
        return p.parent
    monkeypatch.setattr(drive_state,'DriveStateTransport',transport)
    monkeypatch.setattr(first,'financial_data',lambda *a,**kw:p.db.financial())
    monkeypatch.setattr(projection_store,'DriveProjectionStore',lambda *a,**kw:object())
    monkeypatch.setattr(projection_cache,'RollingProjectionStore',lambda *a,**kw:SimpleNamespace(read=lambda k:None))
    monkeypatch.setattr(recovery,'preserved_unknown_proofs',lambda *a,**kw:{19:{},20:{},30:{}})
    monkeypatch.setattr(flow.subprocess,'check_output',lambda command,**kw:p.tree if 'HEAD^{tree}' in command else p.head)
    monkeypatch.setattr(op,'persist_archive',lambda *a,**kw:pytest.fail('archive re-saved'))
    p.sign=lambda:p.env.update(CARD_EXPENSE_RECONCILIATION=capsule(p.record,p.att,key))
    p.sign();return p


def test_saved_archive_runtime_releases_only_parent_with_independent_reads(monkeypatch,tmp_path,key):
    p=saved_case(monkeypatch,tmp_path,key);before=(p.native,deepcopy(p.db.financial()))
    try:result=op.run_from_environment(p.env,apply=True)
    except op.AutoExpenseReconciliationFailure as error:pytest.fail(json.dumps(error.diagnostic))
    after=json.loads(p.parent.read())
    assert result['parent_write_requests']==1 and result['native_write_requests']==result['archive_write_requests']==result['financial_write_requests']==0
    expected=deepcopy(p.old_parent);expected['generation']+=1;expected['sources']['auto_expense'].update(phase='ready',attempt='',error='')
    assert after==expected and len(p.parent.writes)==1 and p.transports==['production_run']
    assert before==(p.native,p.db.financial()) and p.reads.count('production_run')>=4 and p.reads.count('au_pay_card_gmail')>=6
    with pytest.raises(op.AutoExpenseReconciliationFailure):op.run_from_environment(p.env,apply=True)
    assert len(p.parent.writes)==1


@pytest.mark.parametrize('change',['archive','native','target','parent_revision','financial','acl','approval','after_hash','operation','attempt2','old_head','competing','expired','failed_operator'])
def test_saved_runtime_rejects_changed_proof_target_authority_before_write(monkeypatch,tmp_path,key,change):
    p=saved_case(monkeypatch,tmp_path,key)
    if change=='archive':p.att['saved_archive']['archive_sha256']='0'*64
    if change=='native':p.native=p.native+b' '
    if change=='target':
        value=json.loads(p.parent.read());value['sources']['auto_expense']['counts']={'failure':1};p.parent.payload=encode(value)
    if change=='parent_revision':p.att['saved_archive']['parent']['revision']='wrong'
    if change=='financial':p.db.rows[0][6]+=1
    if change=='acl':p.record['acl_sha256']='0'*64;monkeypatch.setattr(op,'RECORD_SHA256',digest(p.record));p.att['record_sha256']=digest(p.record)
    if change=='approval':p.att['approval']['approval_reference']=''
    if change=='after_hash':p.att['saved_archive']['parent_after_sha256']='0'*64
    if change=='operation':p.att['operation']='release'
    if change=='attempt2':p.env['GITHUB_RUN_ATTEMPT']='2'
    if change=='old_head':p.head=op.SAVED_OPERATOR['head_sha'];p.env.update(GITHUB_SHA=p.head,WORKFLOW_SHA=p.head,KAKEIBO_VALIDATED_MAIN_SHA=p.head);p.att['main_sha']=p.head
    if change=='competing':p.conflict=True
    if change=='expired':p.att['approval']['expires_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    if change=='failed_operator':p.att['saved_archive']['operator_run']=op.SAVED_OPERATOR|{'id':1}
    p.sign()
    with pytest.raises(op.AutoExpenseReconciliationFailure):op.run_from_environment(p.env,apply=True)
    assert not p.parent.writes and not p.db.writes


@pytest.mark.parametrize('fault',['response_unknown','write_rejected','independent_readback','runner_loss'])
def test_parent_failure_never_retries_or_rewrites_archive(monkeypatch,tmp_path,key,fault):
    p=saved_case(monkeypatch,tmp_path,key);original=p.parent.write
    def write(raw):
        if fault=='write_rejected':raise TimeoutError('SECRET')
        original(raw)
        if fault=='response_unknown':raise TimeoutError('SECRET')
        if fault=='runner_loss':raise KeyboardInterrupt('SECRET')
    p.parent.write=write
    if fault=='independent_readback':
        read=p.parent.read;calls=[]
        def changed_read():
            if p.parent.writes:
                calls.append(1)
                if len(calls)>=2:raise ConnectionError('SECRET')
            return read()
        p.parent.read=changed_read
    with pytest.raises(KeyboardInterrupt if fault=='runner_loss' else op.AutoExpenseReconciliationFailure):op.run_from_environment(p.env,apply=True)
    assert len(p.parent.writes)==(0 if fault=='write_rejected' else 1) and not p.db.writes


def test_target_change_between_initial_get_and_release_is_rejected(monkeypatch,tmp_path,key):
    p=saved_case(monkeypatch,tmp_path,key);read=p.parent.read;calls=[]
    def changed():
        calls.append(1)
        if len(calls)==4:
            value=json.loads(read());value['sources']['auto_expense']['counts']={'failure':1};p.parent.payload=encode(value)
        return read()
    p.parent.read=changed
    with pytest.raises(op.AutoExpenseReconciliationFailure):op.run_from_environment(p.env,apply=True)
    assert not p.parent.writes


def test_deadline_rechecked_after_slow_final_prewrite_gets(monkeypatch,tmp_path,key):
    p=saved_case(monkeypatch,tmp_path,key);calls=[]
    def clock():
        calls.append(1)
        return 901 if len(calls)>=7 else 0
    monkeypatch.setattr(op.time,'monotonic',clock)
    with pytest.raises(op.AutoExpenseReconciliationFailure) as error:op.run_from_environment(p.env,apply=True)
    assert error.value.diagnostic['error_code']=='auto_expense_reconciliation_expired' and not p.parent.writes


@pytest.mark.parametrize('proven',[True,False])
def test_saved_branch_reuses_bank_proof_and_preserves_latest_bank(monkeypatch,tmp_path,key,proven):
    p=saved_case(monkeypatch,tmp_path,key);parent=json.loads(p.parent.read())
    parent['generation']+=2;parent['sources']['bank'].update(last_success='2026-11-28T00:00:00+00:00',counts={'written':0})
    p.parent.payload=encode(parent);p.att['saved_archive']['parent']['sha256']=op.sha(p.parent.read())
    p.att['saved_archive']['parent_after_sha256']=op.sha(op.proposed_release(p.parent.read(),
        drive_state.StateBinding('production_run',p.db.sid,'folder','parent'),digest(p.archive)))
    p.att['bank_manifest']={'signed_reference':'bank-noop'}
    from app import aupay_card_bank_premises as bp
    def collect(record,manifest,baseline,*a,**kw):
        assert record==p.record and manifest==p.att['bank_manifest'] and baseline==record['baseline_parent_metadata']
        p.bank_calls.append(kw)
        if not proven:preview.require(False,'bank_premises_not_noop')
        return {'manifest':{'parent_snapshot':{'sha256':op.sha(p.parent.read())}}}
    monkeypatch.setattr(bp,'collect_bank_premises',collect);p.sign()
    if proven:
        op.run_from_environment(p.env,apply=True)
        assert json.loads(p.parent.read())['sources']['bank']==parent['sources']['bank'] and len(p.parent.writes)==1
    else:
        with pytest.raises(op.AutoExpenseReconciliationFailure) as error:op.run_from_environment(p.env,apply=True)
        assert error.value.diagnostic['error_code']=='bank_premises_not_noop' and not p.parent.writes
    assert len(p.bank_calls)==1 and summaries(json.loads(p.native))[op.ARCHIVE_RUN]['operator_reconciliation']==p.archive


def test_fixed_diagnostic_reaches_existing_parent_output_without_secret(monkeypatch,tmp_path,key,capsys):
    p=saved_case(monkeypatch,tmp_path,key)
    def fail(*a,**kw):raise HTTPError('https://secret.example',403,'SECRET',{},None)
    monkeypatch.setattr(first,'financial_data',fail)
    from app import private_state_bindings
    monkeypatch.setattr(private_state_bindings,'decode_environment',lambda env,**kw:(env,''))
    monkeypatch.setattr(flow.sys,'argv',['production_flow','--scope','card_expenses','--mode','apply'])
    for name in list(os.environ):monkeypatch.delenv(name,raising=False)
    for name,value in p.env.items():monkeypatch.setenv(name,value)
    with pytest.raises(SystemExit):flow.main()
    report=json.loads(capsys.readouterr().out)
    assert report['error']=='auto_expense_reconciliation_stopped' and report['last_stage']=='financial_preflight'
    assert report['exception_class']=='http_error' and report['http_status']==403
    assert report['effects']['financial']=='not_reached' and 'SECRET' not in str(report) and 'secret.example' not in str(report)
    assert not p.parent.writes


def test_reviewed_bank_contract_remains_bound_to_this_candidate():
    from app.aupay_card_bank_premises import digest,code_contract,REVIEWED_BANK_CODE_SHA256
    assert digest(code_contract()) in REVIEWED_BANK_CODE_SHA256
