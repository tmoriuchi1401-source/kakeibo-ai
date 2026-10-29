"""One-time diagnostic boundaries; synthetic data and no live credentials."""
from copy import deepcopy
from datetime import datetime
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import bank_apply_readonly_inspect as inspect
from app.authority_report_delivery import seal_report, recover_report


@pytest.fixture(scope='module')
def key():
    return rsa.generate_private_key(public_exponent=65537,key_size=3072)


def environment(key,monkeypatch):
    public=key.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)
    monkeypatch.setattr(inspect,'RECIPIENT_SHA256',hashlib.sha256(public).hexdigest())
    return dict(inspect.PINS,GITHUB_ACTIONS='true',GITHUB_REPOSITORY='tmoriuchi1401-source/kakeibo-ai',
        GITHUB_ACTOR='tmoriuchi1401-source',GITHUB_TRIGGERING_ACTOR='tmoriuchi1401-source',
        GITHUB_REPOSITORY_OWNER='tmoriuchi1401-source',GITHUB_EVENT_NAME='workflow_dispatch',
        GITHUB_REF='refs/heads/main',GITHUB_WORKFLOW_REF=inspect.WORKFLOW,GITHUB_RUN_ATTEMPT='1',
        GITHUB_SHA='a'*40,KAKEIBO_VALIDATED_MAIN_SHA='a'*40,EXPECTED_SHA='a'*40,
        KAKEIBO_SCHEDULE_ENABLED='false',BANK_REVIEW_ENABLED='true',BANK_INCOME_WRITE_ENABLED='true',
        KAKEIBO_AUTHORITY_RECIPIENT=base64.b64encode(public).decode())


def test_owner_main_exact_sha_and_fixed_recipient_only(key,monkeypatch):
    assert inspect.require_context(environment(key,monkeypatch),'a'*40)=='a'*40


@pytest.mark.parametrize('name,value',[
    ('GITHUB_ACTOR','other'),('GITHUB_TRIGGERING_ACTOR','other'),('GITHUB_REPOSITORY_OWNER','other'),
    ('GITHUB_EVENT_NAME','schedule'),('GITHUB_REF','refs/heads/inspection'),('GITHUB_ACTIONS','false'),
    ('GITHUB_REPOSITORY','other/repository'),('GITHUB_WORKFLOW_REF','other'),('GITHUB_RUN_ATTEMPT','2'),
    ('GITHUB_SHA','b'*40),('EXPECTED_SHA','b'*40),('KAKEIBO_VALIDATED_MAIN_SHA','b'*40),
    ('KAKEIBO_SCHEDULE_ENABLED','true'),('BANK_REVIEW_ENABLED','false'),
    ('BANK_INCOME_WRITE_ENABLED','false'),('RUNNER_DEBUG','1'),('EXPECTED_PDFS_SHA256','b'*64),
    ('EXPECTED_BANK_SHA256','b'*64),('EXPECTED_RAW_FINANCIAL_SHA256','b'*64),
    ('KAKEIBO_AUTHORITY_RECIPIENT','invalid'),
])
def test_context_mismatch_stops_before_google(key,monkeypatch,name,value):
    env=environment(key,monkeypatch);env[name]=value
    monkeypatch.setattr(inspect.subprocess,'check_output',lambda *a,**k:'a'*40)
    from app import google_clients
    calls=[]
    monkeypatch.setattr(google_clients,'read_only_drive_service',lambda:calls.append('drive'))
    monkeypatch.setattr(google_clients,'read_only_sheets_service',lambda:calls.append('sheets'))
    with pytest.raises(Exception):inspect.evaluate(env)
    assert not calls


@pytest.mark.parametrize('method',['append','update','batchUpdate','delete','create','copy'])
def test_get_only_rejects_mutation_before_request_construction(method):
    calls=[]
    class Resource:
        def __getattr__(self,name):return lambda **kw:calls.append(name)
    with pytest.raises(AssertionError):getattr(inspect.GetOnly(Resource()),method)()
    assert not calls


def test_get_only_accepts_real_get_and_rejects_disguised_post():
    calls=[]
    class Request:
        method='GET'
        def execute(self):calls.append('get');return 3
    class Resource:
        def get(self):return Request()
        def batchGet(self):return SimpleNamespace(method='POST',execute=lambda:calls.append('post'))
    proxy=inspect.GetOnly(Resource());assert proxy.get().execute()==3
    with pytest.raises(AssertionError):proxy.batchGet()
    assert calls==['get']


@pytest.mark.parametrize('fault',[None,'moved_main','second_dispatch','wrong_run'])
def test_github_checks_are_get_only_and_reject_moved_or_replayed_run(fault):
    calls=[]
    def get(url,**kwargs):
        calls.append(url)
        data={'commit':{'sha':'b'*40 if fault=='moved_main' else 'a'*40}} if url.endswith('branches/main') else {
            'total_count':2 if fault=='second_dispatch' else 1,
            'workflow_runs':[{'id':124 if fault=='wrong_run' else 123}]}
        return SimpleNamespace(raise_for_status=lambda:None,json=lambda:data)
    if fault:
        with pytest.raises(AssertionError):inspect.require_github_read_context({'GH_TOKEN':'synthetic','GITHUB_RUN_ID':'123'},'a'*40,get=get)
    else:inspect.require_github_read_context({'GH_TOKEN':'synthetic','GITHUB_RUN_ID':'123'},'a'*40,get=get)
    assert all(url.startswith('https://api.github.com/repos/tmoriuchi1401-source/kakeibo-ai/') for url in calls)


def test_snapshot_checkpoint_and_history_are_never_updated(tmp_path):
    path=tmp_path/'synthetic.sqlite3';con=sqlite3.connect(path)
    con.execute('CREATE TABLE recurring_checkpoint(singleton,successful_window_end)')
    con.execute("INSERT INTO recurring_checkpoint VALUES(1,'2026-10-15T09:08:44+09:00')")
    con.commit();con.close();before=path.read_bytes();state=inspect.ReadState(path)
    assert state.successful_window_end().isoformat()=='2026-10-15T09:08:44+09:00'
    state.record({'status':'dry_run_noop'},advance_checkpoint=False)
    with pytest.raises(AssertionError):state.record({},advance_checkpoint=True)
    assert path.read_bytes()==before


def test_failure_log_is_fixed_and_never_transfers_exception_text(monkeypatch,capsys):
    def error(env):raise ValueError('private transaction account and credential')
    monkeypatch.setattr(inspect,'evaluate',error)
    with pytest.raises(SystemExit):inspect.main()
    captured=capsys.readouterr();assert 'private transaction' not in captured.out+captured.err
    assert json.loads(captured.out)['error']=='bank_readonly_inspect_failed'
    assert captured.err==''


def test_encrypted_only_artifact_recipient_and_run_binding(tmp_path,key,monkeypatch):
    env=environment(key,monkeypatch);env.update(GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='1')
    private=tmp_path/'owner.pem';password=b'synthetic-password-for-test'
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(password)))
    payload=b'{"synthetic_private_detail":"not public"}'
    runner=tmp_path/'runner';runner.mkdir()
    envelope=seal_report(payload,env,runner)
    assert b'not public' not in envelope.read_bytes()
    recover_report(envelope,private,tmp_path/'decoded.json',password,run_id='123',attempt='1',head='a'*40)
    assert (tmp_path/'decoded.json').read_bytes()==payload
    with pytest.raises(Exception):recover_report(envelope,private,tmp_path/'wrong.json',password,
        run_id='124',attempt='1',head='a'*40)


def test_five_backlog_pdfs_use_existing_window_and_never_write(tmp_path,monkeypatch):
    from app import bank_pdf_recurring as recurring
    from app.bank_income import classify_deposit,INCOME_SHEET
    from test_bank_income_recurring import Rig
    from test_bank_income import bank,imported
    rig=Rig(tmp_path,monkeypatch,max_files=20)
    saved=[bank('給与',seed=f'posted-{i}') for i in range(6)]
    old=[bank('確認済非記帳',seed=f'old-{i}') for i in range(185)]
    held=[bank('振込 不明',seed=f'hold-{i}') for i in range(117)]
    rig.db.tables['取込データ']=[imported(t,**{'8':'bank_income'}) for t in saved]
    rig.db.tables['取込データ'] += [imported(t,**{'8':'bank_non_expense'}) for t in old]
    rig.db.tables[INCOME_SHEET].extend(classify_deposit(t).row() for t in saved)
    for n in range(5):rig.add('old-pdf-'+str(n),*saved,*old,*held)
    for file in rig.drive._files.response['files']:file['modifiedTime']='2026-09-01T00:00:00Z'
    state=inspect.ReadState(rig.state.path);native=state.path.read_bytes();before=deepcopy(rig.db.tables)
    now=datetime.fromisoformat('2026-09-14T12:00:00+09:00');window=recurring.build_incremental_window(rig.provider.load(),state,now)
    summary,plan=inspect.calculate_plan(recurring=recurring,drive=rig.drive,db=rig.db,state=state,
        provider=rig.provider,repo=Path.cwd(),now=now,window=window,download=lambda fid:fid.encode(),
        income_write_enabled=True,processed_folder_id='',resolver=None,rules={})
    assert not summary['failure'] and summary['outside_write_window']==summary['files_seen']==5
    assert not plan['expense_import_ids'] and not plan['import_rows'] and not plan['income_rows']
    assert 'modifiedTime' not in rig.drive._files.query['q']
    assert not rig.db.writes and not rig.drive._files.updated and not rig.expense_calls
    assert rig.db.tables==before and state.path.read_bytes()==native


def test_workflow_is_manual_owner_main_readonly_and_fixed_recipient():
    definition=yaml.load(Path('.github/workflows/bank-apply-readonly-inspect.yml').read_text(),Loader=yaml.BaseLoader)
    assert set(definition['on'])=={'workflow_dispatch'}
    assert definition['permissions']=={'contents':'read','actions':'read'}
    assert definition['concurrency']=={'group':'kakeibo-production','cancel-in-progress':'false','queue':'max'}
    job=definition['jobs']['inspect'];condition=job['if']
    for expected in ['github.actor == github.repository_owner','github.triggering_actor == github.repository_owner',
        "github.ref == 'refs/heads/main'",'github.sha == vars.KAKEIBO_VALIDATED_MAIN_SHA',
        'inputs.expected_sha == github.sha','github.run_attempt == 1']:
        assert expected in condition
    step=next(s for s in job['steps'] if s.get('run')=='python -m app.bank_apply_readonly_inspect')
    assert set(inspect.PINS)<=step['env'].keys()
    assert all(step['env'][key]==value for key,value in inspect.PINS.items())
    assert hashlib.sha256(base64.b64decode(step['env']['KAKEIBO_AUTHORITY_RECIPIENT'])).hexdigest()==inspect.RECIPIENT_SHA256
    uploads=[s for s in job['steps'] if s.get('uses')=='actions/upload-artifact@v4']
    assert len(uploads)==1 and uploads[0]['with']['path'].endswith('/authority-report.enc.json')
