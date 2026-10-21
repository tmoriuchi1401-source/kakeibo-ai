"""Exact authorization scope and no production capabilities in the new CI."""
import ast
import hashlib
from pathlib import Path
import subprocess

import yaml

ROOT=Path(__file__).resolve().parents[1]
BASE='d25d3823310c832fe955a2b3d51eb1df539c7184'
NEW={'staging_shadow/isolated_update_adapters.py','tests/isolated_update_fixtures.py',
     'tests/test_isolated_update_adapters.py','tests/test_isolated_update_static.py',
     'docs/r2-4-isolated-updates.md','docs/r2-4-update-validation.json','docs/r2-4-pr-baseline.json',
     '.github/workflows/r2-4-isolated-updates.yml'}
MODIFIED={'tests/test_ledger_execution_static.py','tests/test_mandatory_gate_static.py',
          'tests/r2_1_static_audit.py','tests/test_r2_1_static_audit.py','tests/test_production_workflows.py',
          'tests/test_iphone_mvp_r0_adoption.py','tests/test_iphone_draft_request_design.py'}


def git(*args):return subprocess.check_output(['git','-C',str(ROOT),*args]).decode('utf8')


def test_exact_separate_branch_scope():
    for line in git('diff','--name-status',BASE).splitlines():
        status,path=line.split('\t')
        assert (status=='A' and path in NEW) or (status=='M' and path in MODIFIED),line
    assert set(git('ls-files','--others','--exclude-standard').splitlines())<=NEW
    assert git('diff',BASE,'--','app','auth-session','AGENTS.md','docs/iphone-receipt-review-mvp-spec.md')==''
    for path in git('ls-tree','-r','--name-only',BASE,'.github/workflows','staging_shadow').splitlines():
        assert (ROOT/path).read_bytes().replace(b'\r\n',b'\n')==subprocess.check_output(['git','-C',str(ROOT),'show',BASE+':'+path]).replace(b'\r\n',b'\n')


def test_original_writers_validators_and_CAS_are_pinned_and_reused():
    from staging_shadow.isolated_update_adapters import PINS, Envelope, ENTRIES
    from app.unified_receipt_write import ConditionalRequests
    assert Envelope.replace is ConditionalRequests.replace
    assert set(ENTRIES)=={'A01','A02','A03','A03-bank','A10'}
    for path,pin in PINS.items():
        assert hashlib.sha256((ROOT/path).read_bytes().replace(b'\r\n',b'\n')).hexdigest()==pin
        assert git('diff',BASE,'--',path)==''
    source=(ROOT/'staging_shadow/isolated_update_adapters.py').read_text('utf8')
    tree=ast.parse(source)
    imports=[n.module or '' for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
    assert not any(x.startswith(('google','requests','httpx','subprocess')) for x in imports)
    assert not any(isinstance(n,ast.FunctionDef) and n.name in {'main','from_environment','from_env'} for n in ast.walk(tree))
    assert 'receipt-reimport-v3' not in source


def assert_workflow(workflow):
    text=(ROOT/'.github/workflows/r2-4-isolated-updates.yml').read_text('utf8')
    for word in ('secrets.','vars.','environment:','workflow_dispatch:','schedule:',
                 'pull_request_target:','google-github-actions','app.production_flow','upload-artifact'):
        assert word not in text
    assert workflow['on']=={'pull_request':{'branches':['codex/r2-3-read-only-audit']}}
    assert workflow['permissions']=={'contents':'read'}
    assert workflow['env']=={'UNIFIED_RECEIPT_WRITE_ENABLED':'false'}
    assert workflow['concurrency']=={'group':'kakeibo-r2-4-updates-${{ github.ref }}','cancel-in-progress':'true'}
    assert set(workflow['jobs'])=={'isolated-update-adapters'}
    job=workflow['jobs']['isolated-update-adapters']
    assert job['runs-on']=='ubuntu-24.04' and job['timeout-minutes']=='60'
    assert job['steps'][0]=={'uses':'actions/checkout@v4','with':{
        'ref':'${{ github.event.pull_request.head.sha }}','fetch-depth':'0','persist-credentials':'false'}}
    expected=[
        'tests/test_isolated_update_adapters.py tests/test_isolated_update_static.py',
        'tests/test_ledger_execution_boundary.py tests/test_ledger_execution_static.py',
        'tests/test_mandatory_receipt_gate.py tests/test_mandatory_gate_static.py',
        'tests/test_r2_1_static_audit.py']
    for index,names in enumerate(expected,3):assert job['steps'][index]['run'].endswith(names)
    from test_mandatory_gate_static import REGRESSION
    assert job['steps'][7]['run']=='python -m pytest -q --junitxml=/tmp/r256-existing.xml '+REGRESSION+' tests/test_daily_corrections.py tests/test_auto_expense.py tests/test_review_pipeline.py tests/test_reconciliation.py tests/test_bank_finalization.py tests/test_p14_posting.py tests/test_receipt_unit_posting.py'


def test_dedicated_ubuntu_CI():
    assert_workflow(yaml.load((ROOT/'.github/workflows/r2-4-isolated-updates.yml').read_text('utf8'),Loader=yaml.BaseLoader))
