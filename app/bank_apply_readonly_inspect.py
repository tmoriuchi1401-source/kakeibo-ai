"""One-shot operator evaluation. Existing main code, GET-only services, sealed results."""
import base64, hashlib, json, os, sqlite3, subprocess, tempfile, zlib
from collections import Counter
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

WORKFLOW='tmoriuchi1401-source/kakeibo-ai/.github/workflows/bank-apply-readonly-inspect.yml@refs/heads/main'
CONFIG_NAMES=('BANK_CONFIRMED_INTERNAL_TRANSFERS_JSON','BANK_CONFIRMED_NON_OWN_CLASSIFICATIONS_JSON',
    'BANK_CONFIRMED_DOCOMO_SMTB_RULES_JSON','BANK_PDF_RECURRING_AUTHORITY_JSON')


PINS={'EXPECTED_BANK_SHA256': '68e6570a10cdc4d3c5b9d7bf363ca7b24c7213b97ef9e7343570c8844599a81e', 'EXPECTED_PARENT_SHA256': '5ad11d956819ba94d924dfe1980e8d858f1b75965941c33a3250aa031c4fd1e7', 'EXPECTED_PDFS_SHA256': '909b20ec0dd723baacd34a3a5ff220fbd1c1a057f5d12e9dc0e2794aa2106860', 'EXPECTED_PLAN_SHA256': '8f64ac790aafb03c605c270a9540db5826ad248630b0eb2db36a26cc05d99135', 'EXPECTED_RAW_FINANCIAL_SHA256': '0832939f8f64e3373fee76cf4a853c09cdf7fa5e462a46f01392b1eb740e8f3e'}
RECIPIENT_SHA256='49c0618ec2a603b66dd5562f422177e70672b98497758520431cfbe30cece470'

def require_context(env,head):
    import re
    from .authority_report_delivery import _public,_der
    owner="tmoriuchi1401-source"
    assert env.get("GITHUB_ACTIONS")=="true"
    assert env.get("GITHUB_REPOSITORY")==owner+"/kakeibo-ai"
    assert env.get("GITHUB_ACTOR")==env.get("GITHUB_TRIGGERING_ACTOR")==env.get("GITHUB_REPOSITORY_OWNER")==owner
    assert env.get("GITHUB_EVENT_NAME")=="workflow_dispatch" and env.get("GITHUB_REF")=="refs/heads/main"
    assert env.get("GITHUB_WORKFLOW_REF")==WORKFLOW and env.get("GITHUB_RUN_ATTEMPT")=="1"
    assert re.fullmatch(r"[0-9a-f]{40}",head)
    assert env.get("GITHUB_SHA")==env.get("KAKEIBO_VALIDATED_MAIN_SHA")==env.get("EXPECTED_SHA")==head
    assert env.get("KAKEIBO_SCHEDULE_ENABLED")=="false" and env.get("BANK_REVIEW_ENABLED")=="true"
    assert env.get("BANK_INCOME_WRITE_ENABLED")=="true"
    assert env.get("RUNNER_DEBUG","")!="1"
    assert all(env.get(name)==value for name,value in PINS.items())
    assert hashlib.sha256(_der(_public(env.get("KAKEIBO_AUTHORITY_RECIPIENT","")))).hexdigest()==RECIPIENT_SHA256
    return head

def main():
    try:
        evaluate(dict(os.environ))
    except Exception:
        print(json.dumps(dict(read_only_evaluation_failed=True,error="bank_readonly_inspect_failed",no_automatic_retry=True)))
        raise SystemExit(1) from None

def require_github_read_context(env,head,*,get=None):
    """GET-only: reject moved main or any second dispatch, including a rerun."""
    import requests
    get=get or requests.get
    base='https://api.github.com/repos/tmoriuchi1401-source/kakeibo-ai/'
    headers={'Authorization':'Bearer '+env['GH_TOKEN'],'Accept':'application/vnd.github+json'}
    branch=get(base+'branches/main',headers=headers,timeout=30)
    branch.raise_for_status()
    assert branch.json()['commit']['sha']==head
    response=get(base+'actions/workflows/bank-apply-readonly-inspect.yml/runs',
        params={'event':'workflow_dispatch','per_page':2},headers=headers,timeout=30)
    response.raise_for_status();runs=response.json()
    assert runs['total_count']==1 and str(runs['workflow_runs'][0]['id'])==env['GITHUB_RUN_ID']

class GetOnly:
    """A resource cannot construct a non-GET request, including a write retry."""
    ALLOWED={'files','permissions','spreadsheets','values','get','list','get_media','batchGet'}
    def __init__(self,resource):self.resource=resource
    def __getattr__(self,name):
        if name not in self.ALLOWED:raise AssertionError('operator_nonread_request_forbidden')
        def call(*args,**kwargs):
            result=getattr(self.resource,name)(*args,**kwargs)
            if hasattr(result,'execute'):
                assert result.method=='GET','operator_nonread_request_forbidden'
                return result
            return GetOnly(result)
        return call

class ReadState:
    def __init__(self,path):self.path=path;self.observations=[]
    def successful_window_end(self):
        with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True)) as con:
            row=con.execute('SELECT successful_window_end FROM recurring_checkpoint WHERE singleton=1').fetchone()
        return datetime.fromisoformat(row[0]) if row else None
    def record(self,summary,*,advance_checkpoint):
        assert advance_checkpoint is False,'operator_checkpoint_update_forbidden'
        self.observations.append(dict(summary))  # Memory only, no SQLite record.

def calculate_plan(*,recurring,drive,db,state,provider,repo,now,window,download,
                   income_write_enabled,processed_folder_id,resolver,rules):
    """Use dry-run planning with apply's list, never enter an apply function."""
    from .bank_income_recurring import BankRecurringIncome,IncomeAppendEvidence
    original_list=recurring.list_bounded_bank_pdfs;original_plan=BankRecurringIncome.plan;captured={}
    def backlog(service,folder,selected_window,max_files,*,include_backlog=False):
        assert selected_window==window
        return original_list(service,folder,selected_window,max_files,include_backlog=True)
    def capture_plan(owner,expense_ids,**kwargs):
        ids=tuple(expense_ids);result=original_plan(owner,ids,**kwargs)
        captured.update(expense_import_ids=list(ids),import_rows=list(owner.new_imports.values()),income_rows=owner.projected_income)
        return result
    def forbidden(*args,**kwargs):raise AssertionError('operator_mutation_forbidden')
    with patch.object(recurring,'list_bounded_bank_pdfs',backlog),patch.object(BankRecurringIncome,'plan',capture_plan),\
         patch.object(BankRecurringIncome,'apply',side_effect=forbidden) as financial,\
         patch.object(IncomeAppendEvidence,'append',side_effect=forbidden) as append,\
         patch.object(recurring,'_mark_processed',side_effect=forbidden) as move,\
         patch.object(recurring,'run_bank_recurring_production_batch',side_effect=forbidden) as expense:
        summary=recurring.run_bank_pdf_recurring(drive_service=drive,db=db,state=state,authority_provider=provider,
            repo_root=repo,now=now,dry_run=True,download=download,income_write_enabled=income_write_enabled,
            processed_folder_id=processed_folder_id,meaning_resolver=resolver,**rules)
        assert not financial.called and not append.called and not move.called and not expense.called
    return summary,captured

def evaluate(env):
    from app import bank_pdf_recurring as recurring
    from app.bank_income_recurring import BankRecurringIncome,IncomeAppendEvidence
    from app.bank_income import classify_deposit,income_id
    from app.bank_reconciliation import classify_bank_transaction
    from app.auto_expense import expense_id
    from app.bank_review_source import BankReviewSourceReader
    from app.bank_review_store import BankReviewStore
    from app.bank_meaning_resolver import from_environment
    from app.bank_recurring_authority import ProtectedBankRecurringAuthorityProvider
    from app.google_clients import read_only_drive_service,read_only_sheets_service
    from app.sheets import SheetsDB,SheetsReadPacer
    from app.private_state_bindings import decode_environment,unwrap
    from app.settings import Settings,service_account_source
    from app.drive_run_state import StateBinding,DriveStateTransport,validate
    from app.production_ledger import validate_ledger
    from app.bank_partial_recovery import raw_tables
    from app.bank_fixed_backfill import digest,rule_digest,core_digest,batch_ids
    from app.authority_report_delivery import seal_report
    from app.bank_review_refresh import build_view
    from app.bank_review_retirement import BankReviewRetirementReader
    from app.bank_rule_usage import plan_applications
    from app.production_run import execute_serial
    repo=Path(__file__).resolve().parents[1]
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
    MAIN=require_context(env,head)
    require_github_read_context(env,head)
    assert env['GITHUB_ACTOR']==env['GITHUB_TRIGGERING_ACTOR']==env['GITHUB_REPOSITORY_OWNER']=='tmoriuchi1401-source'
    assert env['KAKEIBO_VALIDATED_MAIN_SHA']==MAIN and env['KAKEIBO_SCHEDULE_ENABLED']=='false'
    assert head==MAIN
    raw_config_digest={name:hashlib.sha256(env.get(name,'[]').encode()).hexdigest() for name in CONFIG_NAMES}
    settings=Settings();settings.validate(need_sheet=True)
    rules=dict(confirmed_internal_transfers=settings.bank_confirmed_internal_transfers(),
        confirmed_non_own_classifications=settings.bank_confirmed_non_own_classifications())
    drive=GetOnly(read_only_drive_service())
    db=SheetsDB(settings.spreadsheet_id,service=GetOnly(read_only_sheets_service()),
        read_pacer=SheetsReadPacer(interval=1.25),read_retry_base=20,read_retry_attempts=5)
    download=lambda fid:drive.files().get_media(fileId=fid,supportsAllDrives=True).execute(num_retries=0)
    _,info=service_account_source()
    if info is None:
        path,_=service_account_source();info=json.loads(Path(path).read_bytes())
    plan_id=unwrap('BANK_FIXED_BACKFILL_PLAN_FILE_ID',env['FIXED_PLAN_WRAPPED_ID'],info['private_key'])
    plan=json.loads(download(plan_id))
    assert digest(plan)==env['EXPECTED_PLAN_SHA256'] and core_digest()==plan['code_sha256']
    assert db.sid==plan['target'] and rule_digest(rules)==plan['rules_sha256']
    env_decoded,_=decode_environment(env)
    state_bytes={};values={}
    for source,var in [('bank_pdf_drive','BANK_STATE_FILE_ID'),('production_run','KAKEIBO_RUN_LEDGER_FILE_ID')]:
        binding=StateBinding(source,db.sid,env_decoded['KAKEIBO_STATE_FOLDER_ID'],env_decoded[var])
        payload=DriveStateTransport(drive,binding).read()
        assert hashlib.sha256(payload).hexdigest()==env['EXPECTED_BANK_SHA256' if source=='bank_pdf_drive' else 'EXPECTED_PARENT_SHA256']
        value=(validate if source=='bank_pdf_drive' else validate_ledger)(payload,binding)
        item=value if source=='bank_pdf_drive' else value['sources']['bank']
        assert item['phase']=='ready' and not item['attempt']
        assert value['generation']==(52 if source=='bank_pdf_drive' else 1298)
        state_bytes[source]=(binding,payload);values[source]=value
    baseline=raw_tables(db);assert digest(baseline)==env['EXPECTED_RAW_FINANCIAL_SHA256']
    master=BankReviewStore(db).read();assert master.fingerprint==plan['master_sha256']
    resolver=from_environment(db,legacy_rules=rules,env=env);assert resolver is not None
    now=datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(dir=env['RUNNER_TEMP']) as directory:
        root=Path(directory);auth=root/'authority.json';auth.write_text(env['BANK_PDF_RECURRING_AUTHORITY_JSON'],encoding='utf-8')
        assert hashlib.sha256(auth.read_bytes()).hexdigest()==plan['policy_sha256']
        provider=ProtectedBankRecurringAuthorityProvider(auth,repo_root=repo);policy=provider.load()
        assert policy.valid_from<=now<policy.expires_at and policy.expected_spreadsheet_id==db.sid
        native=base64.b64decode(values['bank_pdf_drive']['files']['bank-recurring.sqlite3']['data'])
        sql=root/'bank-recurring.sqlite3';sql.write_bytes(native);state=ReadState(sql)
        IncomeAppendEvidence(db,state,{}).require_existing_history()  # SELECT only.
        processed_folder_check='not_configured'
        if env.get('BANK_PDF_PROCESSED_DRIVE_FOLDER_ID'):
            try:
                recurring.validate_processed_folder(drive,policy.expected_drive_folder_id,env['BANK_PDF_PROCESSED_DRIVE_FOLDER_ID'])
                processed_folder_check='passed_GET_only'
            except Exception:
                processed_folder_check='unconfirmed'
        with closing(sqlite3.connect(sql.as_uri()+'?mode=ro',uri=True)) as con:
            assert con.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
            rows=con.execute('SELECT summary_json FROM recurring_runs ORDER BY rowid').fetchall()
        assert len(rows)==32
        pending={}
        for (raw,) in rows:
            event=json.loads(raw);status=event.get('status','')
            if status not in {'fixed_backfill_pending','fixed_backfill_verified','income_append_pending','income_append_verified'}:continue
            key=('income',event['income_attempt']) if status.startswith('income_append') else ('fixed',event['fixed_plan'],event['fixed_batch'])
            if status.endswith('_pending'):pending[key]=event
            else:pending.pop(key,None)
        assert not pending
        source=BankReviewSourceReader(db,drive,policy.expected_drive_folder_id,legacy_rules=rules,
            account_alias=policy.account_alias or None,download=download)()
        prior_header,prior_rows=db.bank_review_ui_table()
        retired={}
        if env.get('BANK_PDF_PROCESSED_DRIVE_FOLDER_ID'):
            retired=BankReviewRetirementReader(drive,policy.expected_drive_folder_id,
                env['BANK_PDF_PROCESSED_DRIVE_FOLDER_ID'],download=download)(prior_rows,source)
        ui_counts,ui_header,ui_rows=build_view(source,resolver,prior_rows,retired_originals=retired)
        usage_edits,usage_counts=plan_applications(source,resolver,master,applied_at=now.isoformat())
        normalized=lambda values:[['TRUE' if v is True else 'FALSE' if v is False else str(v) for v in row] for row in values]
        ui_change=prior_header!=ui_header or normalized(prior_rows)!=normalized(ui_rows)
        occurrences=Counter(tx.source_row_identity for f in source.files for tx in f['parsed'].transactions)
        assert len(source.files)==5 and len(source.transactions)==497 and sum(occurrences.values())==547
        assert sum(count>1 for count in occurrences.values())==50
        pdf_digest=digest(sorted((f['file_id'],f['pdf_sha256']) for f in source.files))
        assert pdf_digest==env['EXPECTED_PDFS_SHA256']
        window=recurring.build_incremental_window(policy,state,now)
        actual_list=recurring.list_bounded_bank_pdfs(drive,policy.expected_drive_folder_id,window,policy.max_files,include_backlog=True)
        assert {f['id'] for f in actual_list}=={f['file_id'] for f in source.files}
        assert all(not recurring._in_write_window(f,window) for f in actual_list)
        new_ids=set(source.transactions)-set(plan['items'])-set(plan['protected']);assert len(new_ids)==4
        fixed191=set(batch_ids(plan,0))|{i for i in batch_ids(plan,1) if plan['items'][i]['disposition']=='nonposting'}
        held117={i for i,item in plan['items'].items() if item['disposition']=='hold'}
        assert len(fixed191)==191 and len(held117)==117
        imported={str(row[0]):row for row in source.imports if row and row[0]}
        assert fixed191<=imported.keys() and not held117 & imported.keys()
        incomes={str(row[0]):row for row in source.incomes if row and row[0]}
        expenses={str(row[0]):row for row in source.expenses if row and row[0]}
        for identity in held117|new_ids:
            assert identity not in imported and income_id(identity) not in incomes and expense_id(identity) not in expenses
        per_id=[]
        for identity in sorted(new_ids):
            tx=source.transactions[identity];classification=classify_bank_transaction(tx,**rules,meaning_resolver=resolver)
            deposit=classify_deposit(tx,**rules,meaning_resolver=resolver) if tx.signed_amount>0 else None
            disposition='hold'
            if deposit and deposit.outcome=='confirmed_income':disposition='income'
            elif deposit and deposit.outcome in {'transfer','reimbursement','other_non_income'}:disposition='nonposting'
            elif not deposit and classification.classification in {'transfer','card_settlement','cash_withdrawal','reimbursement','other_nonwrite'}:disposition='nonposting'
            elif not deposit and classification.classification=='expense':disposition='expense_candidate_requires_existing_eligibility'
            per_id.append(dict(transaction=asdict(tx),classification=classification.classification,reason=classification.reason,
                deposit_outcome=deposit.outcome if deposit else None,deposit_reason=deposit.reason if deposit else None,
                meaning=resolver(tx),disposition=disposition,hold=disposition=='hold',in_write_window=False,
                planned_import_write=False,planned_income_write=False,planned_expense_write=False,
                financial_write_permitted=False))
        original_list=recurring.list_bounded_bank_pdfs
        summary,captured=calculate_plan(recurring=recurring,drive=drive,db=db,state=state,provider=provider,
            repo=repo,now=now,window=window,download=download,
            income_write_enabled=env['BANK_INCOME_WRITE_ENABLED']=='true',
            processed_folder_id=env['BANK_PDF_PROCESSED_DRIVE_FOLDER_ID'],resolver=resolver,rules=rules)
        assert summary['failure']==0 and summary['files_seen']==summary['outside_write_window']==5
        assert not captured['expense_import_ids'] and not captured['import_rows'] and not captured['income_rows']
        assert summary['new_eligible']==summary['planned_expense_writes']==summary['planned_income_writes']==summary['planned_deposit_imports']==0
        # With every file outside and withheld, the dry-run planning branches
        # are identical until apply() / finish_pending() / state.record().
        assert summary['files_withheld']==5 and summary['files_processed']==0
        parent_counterfactual=execute_serial({'bank':lambda:dict(summary,status='noop')},
            history=values['production_run']['sources'],preview=False,selected_sources=frozenset({'bank'}))
        assert sql.read_bytes()==native and raw_tables(db)==baseline
        assert db.bank_review_ui_table()==(prior_header,prior_rows)
        resolver.require_unchanged()
        for state_kind,(binding,payload) in state_bytes.items():assert DriveStateTransport(drive,binding).read()==payload
        after=original_list(drive,policy.expected_drive_folder_id,window,policy.max_files,include_backlog=True)
        assert after==actual_list
        fresh=BankReviewSourceReader(db,drive,policy.expected_drive_folder_id,legacy_rules=rules,
            account_alias=policy.account_alias or None,download=download)()
        assert fresh.fingerprint==source.fingerprint and fresh.transactions==source.transactions
        assert raw_config_digest=={name:hashlib.sha256(env.get(name,'[]').encode()).hexdigest() for name in CONFIG_NAMES}
        report=dict(main_sha=MAIN,workflow_sha=env['GITHUB_SHA'],observed_at=now.isoformat(),configuration_raw_sha256=raw_config_digest,
            rules_sha256=rule_digest(rules),expected_rules_sha256=plan['rules_sha256'],master_sha256=master.fingerprint,
            policy_sha256=plan['policy_sha256'],settings_digest_match=True,new4=per_id,exact_duplicate_ids=[i for i,n in occurrences.items() if n>1],
            pdfs=[dict(file_id=f['file_id'],pdf_sha256=f['pdf_sha256'],transactions=[asdict(tx) for tx in f['parsed'].transactions]) for f in source.files],
            candidate_plans=captured,summary=summary,fixed191_unchanged=True,hold117_unposted=True,
            planned_import_additions=[row[0] for row in captured['import_rows']]+captured['expense_import_ids'],
            planned_income_additions=[row[0] for row in captured['income_rows']],
            planned_expense_additions=[expense_id(i) for i in captured['expense_import_ids']],
            planned_import_updates=[],planned_pdf_moves=[],planned_processed_markers=[],
            processed_folder_validation=processed_folder_check,ui_content_change_needed=ui_change,
            planned_ui_rows=len(ui_rows),ui_preview_counts=ui_counts,usage_preview_counts=usage_counts,
            parent_counterfactual=parent_counterfactual,actual_next_apply_ui_refresh=parent_counterfactual['success'],
            apply_noop_checkpoint_advance_expected=window.end.isoformat(),native_checkpoint=state.successful_window_end().isoformat(),
            equivalence=dict(changed_selection_only='include_backlog=True with dry_run planning; native first window unchanged',
                pending_none=True,all5_outside=True,all5_withheld=True,financial_payloads_empty=True,
                unexecuted=['Actions apply entry/CAS','financial apply transport with empty payload','archive transport','checkpoint/state commit']),
            read_metrics=db.sheet_read_metrics(),financial_write_calls=0,drive_update_calls=0,state_update_calls=0,sqlite_unchanged=True)
        payload=json.dumps(dict(compressed_report=base64.b64encode(zlib.compress(json.dumps(report,ensure_ascii=True,sort_keys=True).encode())).decode()),sort_keys=True).encode()
        seal_report(payload,env,Path(env['RUNNER_TEMP']))
    print(json.dumps(dict(read_only=True,pdfs=5,logical_transactions=497,exact_duplicates=50,new_ids=4,financial_write_calls=0,state_update_calls=0)))

if __name__=='__main__':
    main()
