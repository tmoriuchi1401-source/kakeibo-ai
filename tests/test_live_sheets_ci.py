from copy import deepcopy
from pathlib import Path
import yaml


def test_live_sheets_ci_is_exactly_isolated():
    from test_registration_ci import test_registration_ci_is_exactly_isolated
    test_registration_ci_is_exactly_isolated()
    root=Path(__file__).resolve().parents[1]
    expected=deepcopy(yaml.load((root/'.github/workflows/sheets-registration-checks.yml').read_text('utf8'),Loader=yaml.BaseLoader))
    expected['name']='Live synthetic Google Sheets boundary'
    expected['on']['pull_request']['branches']=['codex/sheets-preauthorized-registration']
    expected['concurrency']['group']='kakeibo-live-synthetic-sheets-${{ github.ref }}'
    job=expected['jobs'].pop('isolated-registration');expected['jobs']['isolated-live-sheet']=job
    steps=job['steps'];steps[4]['name']='Live boundary contracts without Google credentials'
    steps[4]['run']=steps[4]['run'].replace('tests/test_sheets_registration.py','tests/test_live_synthetic_sheets.py tests/test_live_sheets_ci.py tests/test_sheets_registration.py').replace('registration-core.xml','live-sheets-core.xml')
    steps[5]['run']=steps[5]['run'].replace('registration-regression.xml','live-sheets-regression.xml')
    steps[6]['run']=steps[6]['run'].replace('registration-browser.xml','live-sheets-browser.xml')
    steps[7]['run']=steps[7]['run'].replace('python -m compileall -q ','python -m compileall -q staging_shadow/live_synthetic_sheets.py tests/live_google_sheets_probe.py tests/test_live_synthetic_sheets.py tests/test_live_sheets_ci.py ',1)
    steps[8]['with']['name']='live-sheets-synthetic-evidence'
    steps[8]['with']['path']=steps[8]['with']['path'].replace('.private/registration-*.xml','.private/live-sheets-*.xml')
    source=(root/'.github/workflows/live-synthetic-sheets-checks.yml').read_text('utf8')
    assert yaml.load(source,Loader=yaml.BaseLoader)==expected
    assert not any(x in source for x in ('secrets.','vars.','environment:','workflow_dispatch','schedule:','continue-on-error'))
