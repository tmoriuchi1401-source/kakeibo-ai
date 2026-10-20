"""Exact change boundary; static checks do not establish production deployment."""
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
BASE='c46e694aeec3cff03e193db562b9b8064e1dbb0f'
NEW={'staging_shadow/mandatory_receipt_gate.py','tests/mandatory_gate_fixtures.py',
    'tests/test_mandatory_receipt_gate.py','tests/test_mandatory_gate_static.py',
    'docs/r2-2-mandatory-gate.md','.github/workflows/r2-2-mandatory-gate.yml'}
MODIFIED={'tests/test_production_workflows.py','tests/r2_1_static_audit.py',
    'tests/test_r2_1_static_audit.py','tests/test_iphone_mvp_r0_adoption.py',
    'tests/test_iphone_draft_request_design.py'}
REGRESSION=(
    'tests/test_accounting_ownership.py tests/test_accounting_enforced_gate.py '
    'tests/test_accounting_recovery.py tests/test_native_ingress_safety_stop.py '
    'tests/test_auto_native_evidence.py tests/test_synthetic_existing_writer.py '
    'tests/test_legacy_confirmation_connection_stop.py tests/test_receipt_confirmation_contract.py '
    'tests/test_synthetic_legacy_writer.py tests/test_receipt_draft_runtime.py '
    'tests/test_synthetic_draft_writer.py tests/test_iphone_r1_adapter.py '
    'tests/test_iphone_r1_confirmation.py tests/test_pdf_native_readonly_inventory.py '
    'tests/test_production_workflows.py tests/test_receipt_pipeline.py tests/test_receipt_confirmation.py '
    'tests/test_receipt_privacy_gate.py tests/test_receipt_text_extraction.py '
    'tests/test_iphone_mvp_r0_adoption.py tests/test_iphone_draft_request_design.py')


def git(*args):return subprocess.check_output(['git','-C',str(ROOT),*args]).decode('utf-8')


def test_all_existing_runtime_spec_and_workflows_unchanged():
    assert git('diff','--name-status','--diff-filter=CDMRTUXB',BASE,'--',
        'app','auth-session','staging_shadow','.github/workflows','docs/r2-1-*',
        'AGENTS.md','docs/iphone-receipt-review-mvp-spec.md')==''


def test_only_authorized_isolation_files_changed():
    for line in git('diff','--name-status',BASE).splitlines():
        status,path=line.split('\t')
        assert (status=='M' and path in MODIFIED) or (status=='A' and path in NEW),line
    assert set(git('ls-files','--others','--exclude-standard').splitlines())<=NEW


def test_inventory_grades_remain_historical():
    import json
    rows=json.loads((ROOT/'docs/r2-1-ingresses.json').read_text('utf8'))['ingresses']
    assert len(rows)==39 and [r['id'] for r in rows if r['grade']=='A']==['S01']
    assert all(r['production_status']=='本番未確認' for r in rows)


def assert_workflow(w):
    text=(ROOT/'.github/workflows/r2-2-mandatory-gate.yml').read_text('utf8')
    for forbidden in ('secrets.','vars.','environment:','schedule:','workflow_dispatch:',
                      'pull_request_target:','google-github-actions','app.production_flow'):
        assert forbidden not in text
    assert w['on']=={'pull_request':{'branches':['codex/r2-1-writer-inventory-drain']}}
    assert w['permissions']=={'contents':'read'}
    assert w['env']=={'UNIFIED_RECEIPT_WRITE_ENABLED':'false'}
    assert w['concurrency']=={'group':'kakeibo-r2-2-isolated-${{ github.ref }}','cancel-in-progress':'true'}
    assert set(w['jobs'])=={'isolated-mandatory-gate'}
    job=w['jobs']['isolated-mandatory-gate']
    assert job['runs-on']=='ubuntu-24.04' and job['timeout-minutes']=='35'
    assert job['steps']==[
        {'uses':'actions/checkout@v4','with':{'ref':'${{ github.event.pull_request.head.sha }}',
            'fetch-depth':'0','persist-credentials':'false'}},
        {'uses':'actions/setup-python@v5','with':{'python-version':'3.12'}},
        {'name':'Install existing test dependencies','run':'pip install -r requirements-test.txt'},
        {'name':'New isolated mandatory gate and static checks','run':
            'python -m pytest -q --junitxml=/tmp/r2-2-new.xml tests/test_mandatory_receipt_gate.py tests/test_mandatory_gate_static.py'},
        {'name':'Inherited R2-1 static inventory','run':
            'python -m pytest -q --junitxml=/tmp/r2-2-static.xml tests/test_r2_1_static_audit.py'},
        {'name':'Existing R1 normal buyback legacy safety regressions','run':
            'python -m pytest -q --junitxml=/tmp/r2-2-existing.xml '+REGRESSION},
        {'name':'Actual separate test counts','if':'always()','run':
            "python - <<'PY'\nimport pathlib, xml.etree.ElementTree as ET\nfor group in ('new','static','existing'):\n    p=pathlib.Path('/tmp/r2-2-'+group+'.xml')\n    if p.exists():\n        cases=ET.parse(p).getroot().findall('.//testcase')\n        failed=sum(c.find('failure') is not None or c.find('error') is not None for c in cases)\n        skipped=sum(c.find('skipped') is not None for c in cases)\n        print(f'{group}: {len(cases)-failed-skipped} passed; {failed} failed/error; {skipped} skipped')\nPY\n"},
        {'name':'Syntax and whitespace','run':
            'python -m compileall -q staging_shadow/mandatory_receipt_gate.py tests/mandatory_gate_fixtures.py\n'
            'git diff --check '+BASE+'\n'}]


def test_dedicated_ci_is_isolated():
    import yaml
    assert_workflow(yaml.load((ROOT/'.github/workflows/r2-2-mandatory-gate.yml').read_text('utf8'),Loader=yaml.BaseLoader))
