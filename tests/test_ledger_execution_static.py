"""Fixed-source evidence and exact permitted change scope; not live closure proof."""
import json
from pathlib import Path
import subprocess

import yaml

ROOT=Path(__file__).resolve().parents[1]
BASE='540427f865c47066a7d8b3e3a0e0c7ae714fd7c8'
NEW={'staging_shadow/ledger_execution_boundary.py','tests/ledger_execution_fixtures.py',
     'tests/test_ledger_execution_boundary.py','tests/test_ledger_execution_static.py',
     'docs/r2-2-m5-m6.md','docs/r2-2-m5-m6-inventory.json','.github/workflows/r2-2-m5-m6.yml'}
MODIFIED={'tests/test_mandatory_gate_static.py','tests/r2_1_static_audit.py',
     'tests/test_r2_1_static_audit.py','tests/test_production_workflows.py',
     'tests/test_iphone_mvp_r0_adoption.py','tests/test_iphone_draft_request_design.py'}


def git(*args):return subprocess.check_output(['git','-C',str(ROOT),*args]).decode('utf8')
def inventory():return json.loads((ROOT/'docs/r2-2-m5-m6-inventory.json').read_text('utf8'))


def test_production_148_runtime_and_all_existing_workflows_unchanged():
    assert git('diff','--name-status','--diff-filter=CDMRTUXB',BASE,'--',
        'app','staging_shadow','auth-session','.github/workflows','AGENTS.md',
        'docs/iphone-receipt-review-mvp-spec.md','docs/r2-1-*','docs/r2-2-mandatory-gate.md')==''


def test_only_exact_authorized_new_files_and_test_allowlists():
    for line in git('diff','--name-status',BASE).splitlines():
        status,path=line.split('\t')
        assert (status=='M' and path in MODIFIED) or (status=='A' and path in NEW),line
    assert set(git('ls-files','--others','--exclude-standard').splitlines())<=NEW


def test_evidence_matches_fixed_ref_file_and_line():
    cache={}
    for row in inventory()['ingresses']:
        for e in row['evidence']:
            key=(e['ref'],e['path'])
            assert e['ref'] in (BASE,inventory()['historical'])
            if key not in cache:cache[key]=git('show',e['ref']+':'+e['path']).splitlines()
            assert e['needle'] in cache[key][e['line']-1],(row['id'],e)


def test_all_M6_dimensions_and_unconnected_financial_stage_are_explicit():
    rows=[r for r in inventory()['ingresses'] if r['id'].startswith('A')]
    assert [r['id'] for r in rows]==['A'+str(n).zfill(2) for n in range(1,14)]
    for r in rows:
        for field in ('read_targets','write_targets','source_identity','native_controls','target_projection',
                      'snapshot_effect','parallel_decision','gate_or_drain','external_action','evidence'):
            assert r[field],(r['id'],field)
        assert r['financial_adapter_connected'] is False and r['production_status']=='本番未確認'
    assert inventory()['financial_m6_connections']==0


def test_old_grades_and_all_dangerous_receipt_ingresses_retained():
    old=json.loads((ROOT/'docs/r2-1-ingresses.json').read_text('utf8'))['ingresses']
    new={r['id']:r for r in inventory()['ingresses']}
    assert len(old)==39
    for row in old:
        if row['id'] in new:assert new[row['id']]['grade']==row['grade']
        if row['grade']=='C':assert row['id'] in new
    assert all(r['production_status']=='本番未確認' for r in new.values())


def test_runtime_hashes_and_no_old_PIN_or_bundle_relaxation():
    from staging_shadow.ledger_execution_boundary import verify_pins,HISTORICAL
    verify_pins()
    assert HISTORICAL in git('show',BASE+':auth-session/baseline.json')
    assert 'test_tree' in git('show',BASE+':auth-session/compose.py')
    assert git('diff',BASE,'--','auth-session')==''


def assert_workflow(w):
    text=(ROOT/'.github/workflows/r2-2-m5-m6.yml').read_text('utf8')
    for forbidden in ('secrets.','vars.','environment:','schedule:','workflow_dispatch:',
                      'pull_request_target:','google-github-actions','app.production_flow'):
        assert forbidden not in text
    assert w['on']=={'pull_request':{'branches':['codex/r2-2-isolated-mandatory-gate']}}
    assert w['permissions']=={'contents':'read'}
    assert w['env']=={'UNIFIED_RECEIPT_WRITE_ENABLED':'false'}
    assert w['concurrency']=={'group':'kakeibo-r2-2-m5-m6-${{ github.ref }}','cancel-in-progress':'true'}
    assert set(w['jobs'])=={'isolated-ledger-boundary'}
    job=w['jobs']['isolated-ledger-boundary']
    assert job['runs-on']=='ubuntu-24.04' and job['timeout-minutes']=='45'
    steps=job['steps']
    assert steps[0]=={'uses':'actions/checkout@v4','with':{'ref':'${{ github.event.pull_request.head.sha }}',
        'fetch-depth':'0','persist-credentials':'false'}}
    assert steps[1]=={'uses':'actions/setup-python@v5','with':{'python-version':'3.12'}}
    assert steps[2]=={'name':'Install existing test dependencies','run':'pip install -r requirements-test.txt'}
    assert steps[3]['run']=='python -m pytest -q --junitxml=/tmp/r256-new.xml tests/test_ledger_execution_boundary.py tests/test_ledger_execution_static.py'
    assert steps[4]['run']=='python -m pytest -q --junitxml=/tmp/r256-m1m3.xml tests/test_mandatory_receipt_gate.py tests/test_mandatory_gate_static.py'
    assert steps[5]['run']=='python -m pytest -q --junitxml=/tmp/r256-static.xml tests/test_r2_1_static_audit.py'
    from test_mandatory_gate_static import REGRESSION
    assert steps[6]['run']=='python -m pytest -q --junitxml=/tmp/r256-existing.xml '+REGRESSION+' tests/test_daily_corrections.py tests/test_p14_posting.py tests/test_receipt_unit_posting.py'
    assert steps[7]['if']=='always()' and "('new','m1m3','static','existing')" in steps[7]['run']
    assert steps[8]['run']=='python -m compileall -q staging_shadow/ledger_execution_boundary.py tests/ledger_execution_fixtures.py\ngit diff --check '+BASE+'\n'
    assert len(steps)==9


def test_dedicated_ubuntu_ci_has_no_production_capabilities():
    assert_workflow(yaml.load((ROOT/'.github/workflows/r2-2-m5-m6.yml').read_text('utf8'),Loader=yaml.BaseLoader))
