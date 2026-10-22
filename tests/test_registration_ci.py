"""Narrow PR-only extension of the validated #157 CI, no production scopes."""
from copy import deepcopy
from pathlib import Path
import yaml


def test_registration_ci_is_exactly_isolated():
    from test_sheets_only_ci import test_sheets_only_ci_is_exactly_isolated
    test_sheets_only_ci_is_exactly_isolated()
    root=Path(__file__).resolve().parents[1]
    expected=deepcopy(yaml.load((root/'.github/workflows/sheets-only-review-checks.yml').read_text('utf8'),Loader=yaml.BaseLoader))
    expected['name']='Sheets preauthorized registration'
    expected['on']['pull_request']['branches']=['codex/sheets-only-review']
    expected['concurrency']['group']='kakeibo-standing-registration-${{ github.ref }}'
    job=expected['jobs'].pop('isolated-sheet');expected['jobs']['isolated-registration']=job
    steps=job['steps']
    steps[4]['name']='Standing permission registration and preserved Sheets contracts'
    steps[4]['run']=steps[4]['run'].replace('tests/test_sheets_only_review.py','tests/test_sheets_registration.py tests/test_registration_ci.py tests/test_sheets_only_review.py').replace('sheets-only-core.xml','registration-core.xml')
    steps[5]['run']=steps[5]['run'].replace('python -m pytest -q ','python -m pytest -q tests/test_synthetic_draft_writer.py tests/test_manual_entry.py tests/test_bank_finalization.py tests/test_paypay_pipeline.py tests/test_amazon_money.py tests/test_receipt_pipeline.py ',1).replace('sheets-only-regression.xml','registration-regression.xml')
    steps[6]['run']=steps[6]['run'].replace('tests/test_sheets_only_visual.py','tests/test_registration_visual.py tests/test_sheets_only_visual.py').replace('sheets-only-browser.xml','registration-browser.xml')
    steps[7]['run']=steps[7]['run'].replace('staging_shadow/sheets_only_review.py','staging_shadow/standing_permission.py staging_shadow/sheets_registration.py staging_shadow/sheets_only_review.py').replace('tests/sheets_only_fixtures.py','tests/registration_fixtures.py tests/sheets_only_fixtures.py tests/test_sheets_registration.py tests/test_registration_ci.py tests/test_registration_visual.py')
    steps[7]['run']=steps[7]['run'].replace('git diff --check','git diff --check ${{ github.event.pull_request.base.sha }} ${{ github.event.pull_request.head.sha }}')
    steps[8]['with']['name']='standing-registration-synthetic-evidence'
    steps[8]['with']['path']=steps[8]['with']['path'].replace('.private/sheets-only-*.xml','.private/registration-*.xml')
    source=(root/'.github/workflows/sheets-registration-checks.yml').read_text('utf8')
    assert yaml.load(source,Loader=yaml.BaseLoader)==expected
    assert not any(x in source for x in ('secrets.','vars.','environment:','workflow_dispatch','schedule:','continue-on-error'))
