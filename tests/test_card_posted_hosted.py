from copy import deepcopy
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
import json

import pytest
from cryptography.hazmat.primitives import serialization

from app import auto_expense_reconciliation as op,google_clients,drive_run_state
from app import aupay_card_window_preview as window,aupay_card_reconciliation_entry as entry
from app import aupay_card_expense_stage as stage,aupay_card_first_window_apply as financial
from app import projection_store,projection_cache,sheets
from app.aupay_card_aliases import digest
from app.aupay_card_saved_readback import standard
from test_card_posted_confirmation import posted_case
from test_aupay_card_saved_readback import Drive
from test_aupay_card_reconciliation_entry import capsule
from test_private_state_bindings import key


def hosted(tmp_path,monkeypatch,key,apply=True):
    db,nt,pt,cb,arguments=posted_case(tmp_path,monkeypatch);kw=arguments();record=kw['record'];pb=kw['ledger'].binding
    now=datetime.now(timezone.utc).replace(microsecond=0);head='d'*40;tree='e'*40
    drives={cb.file_id:Drive(nt,cb),pb.file_id:Drive(pt,pb)}
    acl=dict(id='synthetic',permissions=[dict(type='user',role='owner')])
    record.update(bindings=dict(spreadsheet_id=db.sid,folder_id=cb.folder_id,card_id=cb.file_id,parent_id=pb.file_id,projection_id='projection'),
        other_native=[dict(file_id='bank',sha256=op.sha(b'bank'))],navigation=None,journal_before=dict(generation=338,ranges=[],months=[],append=False),
        settings_without_validated_sha256='f'*64,profile_sha256=digest(stage.planning_profile(db,dict(CATEGORY_RULE_AUTO_APPLY_ENABLED='true',KAKEIBO_AMAZON_MONEY_MODE='confirmed-v1'))),
        expense_before_rows=1,protected_document_keys=['journal','money'],rules_sha256=digest(db.get_raw("'カテゴリ自動分類ルール'!A:R")),
        acl_sha256=digest({k:acl for k in ('card','parent','folder','ledger')}))
    for prefix,binding in [('native',cb),('parent',pb)]:record[prefix+'_before_metadata_digest']=digest(standard(drives[binding.file_id].meta(0)))
    docs=dict(journal=projection_store.expected_append_journal(record['journal_before']),money=None)
    record['documents_sha256']=digest(docs);record['documents_before_sha256']=digest(dict(docs,journal=record['journal_before']))
    record['financial_sha256']=digest({str(k):v for k,v in db.financial().items()})
    before=db.financial()
    for render in ('UNFORMATTED_VALUE','FORMULA'):
        del before[('支出明細',render)][-2:]
        for row in record['plan']['rows']:
            v=list(row['before'])
            while v and v[-1]=='':v.pop()
            before[('取込データ',render)][row['row_num']-1]=v
    # Our synthetic financial reader uses untrimmed imports, as Sheets trims them.
    original_financial=db.financial
    def trimmed():
        values=original_financial()
        for rows in values.values():
            for row in rows:
                while row and row[-1]=='':row.pop()
        return values
    db.financial=trimmed
    record['financial_before_sha256']=digest({str(k):v for k,v in before.items()})
    record['financial_sha256']=digest({str(k):v for k,v in db.financial().items()})
    monkeypatch.setattr(op,'POSTED_RECORD_SHA256',digest(record));monkeypatch.setattr(entry,'LABEL',op.LABEL)
    env=dict(GITHUB_ACTIONS='true',GITHUB_REF='refs/heads/main',GITHUB_REPOSITORY='tmoriuchi1401-source/kakeibo-ai',KAKEIBO_PRODUCTION_ENABLED='true',
        GITHUB_SHA=head,WORKFLOW_SHA=head,KAKEIBO_VALIDATED_MAIN_SHA=head,GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_ACTOR='tmoriuchi1401-source',
        GITHUB_TRIGGERING_ACTOR='tmoriuchi1401-source',GITHUB_RUN_ATTEMPT='1',GITHUB_RUN_ID='42',RUNNER_ENVIRONMENT='github-hosted',
        WORKFLOW_REF='tmoriuchi1401-source/kakeibo-ai/.github/workflows/kakeibo-production.yml@refs/heads/main',MANUAL_SCOPE='card_expenses',CONFIRM='APPLY',
        CATEGORY_RULE_AUTO_APPLY_ENABLED='true',KAKEIBO_AMAZON_MONEY_MODE='confirmed-v1',KAKEIBO_BANK_SCHEDULE_ENABLED='true',KAKEIBO_CORE_SCHEDULE_ENABLED='false',
        KAKEIBO_LEGACY_DISABLED='true',KAKEIBO_SCHEDULE_ENABLED='false',CATEGORY_BACKFILL_SHEET_RUNNER_ENABLED='false',SPREADSHEET_ID=db.sid,
        KAKEIBO_STATE_FOLDER_ID=cb.folder_id,AUPAY_CARD_STATE_FILE_ID=cb.file_id,KAKEIBO_RUN_LEDGER_FILE_ID=pb.file_id,
        GOOGLE_SERVICE_ACCOUNT_JSON=json.dumps({'private_key':key}),RUNNER_TEMP=str(tmp_path))
    proof=lambda binding:dict(sha256=op.sha(drives[binding.file_id].source.payload),metadata_digest=digest(standard(drives[binding.file_id].meta(drives[binding.file_id].offset()))))
    att=dict(record_sha256=digest(record),code_tree_sha256=tree,owner_approved=True,scope=op.SCOPE,main_sha=head,controls_sha256='f'*64,
        operation=op.POSTED_OPERATION if apply else 'preview_posted_card_expenses',observed_at=now.isoformat(),
        approval=dict(expires_at=(now+timedelta(seconds=600)).isoformat(),approval_reference='explicit-new-approval'),state_snapshot=dict(native=proof(cb),parent=proof(pb)))
    rsa=serialization.load_pem_private_key(key.encode(),password=None)
    env['CARD_EXPENSE_RECONCILIATION']=op.POSTED_PREFIX+capsule(record,att,rsa)
    title=op.RELEASE_TITLE if apply else op.RELEASE_TITLE.removesuffix('apply')+'preview'
    current=dict(id=42,head_sha=head,run_attempt=1,event='workflow_dispatch',path='.github/workflows/kakeibo-production.yml',status='in_progress',created_at=now.isoformat(),display_title=title)
    history=[current];active=[]
    def github(path):
        if path=='git/ref/heads/main':return dict(object=dict(sha=head))
        if path=='actions/runs/42':return current
        if path=='actions/runs/'+str(record['failed_run']['id']):return record['failed_run']
        if path.startswith('actions/workflows/'):return dict(total_count=len(history),workflow_runs=history)
        assert path.startswith('actions/runs?');return dict(total_count=len(active),workflow_runs=active)
    monkeypatch.setattr(window,'GitHub',lambda env:SimpleNamespace(get=github))
    monkeypatch.setattr('subprocess.check_output',lambda *a,**k:tree+'\n')
    class ReadState:
        def __init__(self,service,binding):self.binding=binding;self.drive=drives.get(binding.file_id)
        def read(self):return b'bank' if self.drive is None else self.drive.source.payload
        def observe(self):return self.read(),proof(self.binding)
    monkeypatch.setattr(window,'ReadOnlyState',ReadState)
    factories=[]
    monkeypatch.setattr(google_clients,'drive_service',lambda:factories.append('state-only') or object())
    ro=SimpleNamespace(files=lambda:SimpleNamespace(get=lambda **k:SimpleNamespace(execute=lambda **k:acl)))
    monkeypatch.setattr(google_clients,'read_only_drive_service',lambda:ro)
    monkeypatch.setattr(google_clients,'read_only_sheets_service',lambda:object())
    monkeypatch.setattr(google_clients,'sheets_service',lambda:pytest.fail('financial writer forbidden'))
    monkeypatch.setattr(drive_run_state,'DriveStateTransport',lambda svc,binding:nt if binding==cb else pt)
    # Keep the real SheetsDB getters. Google defaults to FORMATTED_VALUE;
    # only get_raw()/get_raw_ranges() request literal integer amounts.
    class ReadSheets:
        def __init__(self):self.calls=[]
        def spreadsheets(self):return self
        def values(self):return self
        def rows(self,rng,render):
            if rng=='取込データ!A2:L':values=deepcopy(db.rows)
            elif rng=='支出明細!A2:M':values=deepcopy(db.expenses)
            elif rng in {"'カテゴリ'!A:B",'カテゴリ!A2:B'}:
                values=[list(v) for v in db.categories()]
                if rng.startswith("'"):values=[['大','小']]+values
            elif rng=="'カテゴリ自動分類ルール'!A:R":values=db.get_raw(rng)
            elif rng=='カテゴリ自動分類ルール!A2:R':values=db.category_rules()
            else:raise AssertionError(rng)
            self.calls.append((rng,render))
            if render=='FORMATTED_VALUE':
                return [[format(v,',') if type(v) is int else v for v in row] for row in values]
            assert render=='UNFORMATTED_VALUE'
            return values
        def get(self,**kw):
            if 'range' not in kw:
                value=dict(sheets=[dict(properties=dict(title='カテゴリ自動分類ルール'))])
            else:value=dict(values=self.rows(kw['range'],kw.get('valueRenderOption','FORMATTED_VALUE')))
            return SimpleNamespace(execute=lambda **kwargs:deepcopy(value))
        def batchGet(self,**kw):
            value=dict(valueRanges=[dict(values=self.rows(rng,kw.get('valueRenderOption','FORMATTED_VALUE'))) for rng in kw['ranges']])
            return SimpleNamespace(execute=lambda **kwargs:deepcopy(value))
    read_sheets=ReadSheets()
    monkeypatch.setattr(google_clients,'read_only_sheets_service',lambda:read_sheets)
    monkeypatch.setattr(stage,'navigation_context',lambda db:None)
    monkeypatch.setattr(financial,'financial_data',lambda *a,**k:db.financial())
    monkeypatch.setattr(projection_store,'DriveProjectionStore',lambda *a,**k:SimpleNamespace(read=lambda key:deepcopy(docs[key])))
    monkeypatch.setattr(projection_cache,'RollingProjectionStore',lambda store:store)
    return env,nt,pt,factories,history,active,att,record,rsa


@pytest.mark.parametrize('apply',[False,True])
def test_signed_hosted_entry_readonly_or_state_only(tmp_path,monkeypatch,key,apply):
    env,nt,pt,factories,*_=hosted(tmp_path,monkeypatch,key,apply)
    result=op.run_from_environment(env,apply=apply)
    assert result['success'] and result['financial_write_requests']==0
    assert len(nt.writes)==2*int(apply) and len(pt.writes)==int(apply)
    if not apply:assert not factories


def test_real_getters_preserve_render_difference_and_original_posted_contract(tmp_path,monkeypatch,key):
    from app.aupay_card_aliases import LiteralRows
    env,nt,pt,factories,history,active,att,record,rsa=hosted(tmp_path,monkeypatch,key,False)
    db=sheets.SheetsDB(env['SPREADSHEET_ID'],service=google_clients.read_only_sheets_service())
    shown=db.get('支出明細!A2:M');raw=db.get_raw('支出明細!A2:M')
    assert shown!=raw and type(shown[0][4]) is str and type(raw[0][4]) is int
    args=(record['plan']['import_ids'],record['plan']['categories_by_id'],True,record['business_contract'])
    with pytest.raises(RuntimeError,match='^card_expense_existing_conflict$'):
        stage.selected_plan(db,*args,expected_profile=record['profile_sha256'],env=env)
    plan=stage.selected_plan(LiteralRows(db),*args,expected_profile=record['profile_sha256'],env=env)
    assert len(plan['already_posted'])==2 and plan['expense_rows']==0
    assert stage.contract_sha256(plan)==record['business_contract']
    assert not nt.writes and not pt.writes and not factories


def test_missing_literal_connection_reproduces_preflight_failure_before_state_writers(tmp_path,monkeypatch,key):
    from app import aupay_card_aliases
    env,nt,pt,factories,*_=hosted(tmp_path,monkeypatch,key,False)
    monkeypatch.setattr(aupay_card_aliases,'LiteralRows',lambda db:db)
    with pytest.raises(op.AutoExpenseReconciliationFailure) as caught:
        op.run_from_environment(env,apply=False)
    assert caught.value.diagnostic['last_stage']=='financial_preflight'
    assert caught.value.diagnostic['error_code']=='repair_failed_closed'
    assert not nt.writes and not pt.writes and not factories


@pytest.mark.parametrize('change',['attempt','settings','scope','expired','snapshot','old_operation','replayed_approval','competing','record'])
def test_unapproved_or_changed_premises_reject_before_writers(tmp_path,monkeypatch,key,change):
    env,nt,pt,factories,history,active,att,record,rsa=hosted(tmp_path,monkeypatch,key)
    if change=='attempt':env['GITHUB_RUN_ATTEMPT']='2'
    if change=='settings':env['CATEGORY_RULE_AUTO_APPLY_ENABLED']='false'
    if change=='scope':env['MANUAL_SCOPE']='core'
    if change=='expired':att['approval']['expires_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    if change=='snapshot':att['state_snapshot']['native']['metadata_digest']='0'*64
    if change=='old_operation':att['operation']=op.SAVED_OPERATION
    if change=='replayed_approval':history.append(dict(history[0],id=41))
    if change=='competing':active.append(dict(id=99))
    if change=='record':record['plan']['expense_yen']+=1
    env['CARD_EXPENSE_RECONCILIATION']=op.POSTED_PREFIX+capsule(record,att,rsa)
    with pytest.raises(RuntimeError):op.run_from_environment(env,apply=True)
    assert not nt.writes and not pt.writes and not factories
