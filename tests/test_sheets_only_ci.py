"""A narrowly enumerated new PR-only CI, no production exception widening."""
from copy import deepcopy
from pathlib import Path

import yaml


def test_sheets_only_ci_is_exactly_isolated():
    root=Path(__file__).resolve().parents[1]
    # Reuse and first validate #156's exact CI. Only enumerated test deltas.
    from test_iphone_handoff_ui import test_ui_ci_is_exactly_isolated
    test_ui_ci_is_exactly_isolated()
    expected=yaml.load((root/'.github/workflows/iphone-sheet-handoff-ui-checks.yml').read_text('utf8'),Loader=yaml.BaseLoader)
    expected=deepcopy(expected)
    expected['name']='Sheets-only synthetic review'
    expected['on']['pull_request']['branches']=['codex/iphone-sheet-handoff-ui']
    expected['concurrency']['group']='kakeibo-sheets-only-${{ github.ref }}'
    job=expected['jobs'].pop('isolated-ui');expected['jobs']['isolated-sheet']=job
    steps=job['steps']
    steps[3]={'name':'Install synthetic verification dependencies','run':'pip install -r requirements-test.txt playwright==1.64.0 formulas==1.3.4\npython -m playwright install --with-deps chromium\nsudo apt-get install -y fonts-noto-cjk\n'}
    steps[4]={'name':'Sheets-only polling CAS formula and approval-denial contracts','run':'python -m pytest -q tests/test_sheets_only_review.py tests/test_sheets_only_ci.py --junitxml=.private/sheets-only-core.xml'}
    regress=steps[6]['run'].replace('python -m pytest -q ','python -m pytest -q tests/test_iphone_sheet_handoff.py tests/test_iphone_sheet_handoff_host.py tests/test_iphone_handoff_ui.py ',1).replace('handoff-ui-regression.xml','sheets-only-regression.xml')
    sealed=steps[5]['run'].replace('HANDOFF_OUTER_NETNS="$3"','HANDOFF_OUTER_NETNS="$3" UNIFIED_RECEIPT_WRITE_ENABLED=false').replace('tests/test_iphone_handoff_browser.py','tests/test_sheets_only_visual.py tests/test_iphone_handoff_browser.py').replace('handoff-ui-browser.xml','sheets-only-browser.xml')
    steps[5]={'name':'Existing reader journal adapter host and input regressions','run':regress}
    steps[6]={'name':'Static Sheet fixture visibility and existing browser regressions with blocked egress','run':sealed}
    steps[7]={'name':'Compile preserved scripts and whitespace','run':'python -m compileall -q staging_shadow/sheets_only_review.py staging_shadow/sheets_only_surface.py tests/sheets_only_fixtures.py tests/test_sheets_only_review.py tests/test_sheets_only_ci.py tests/test_sheets_only_visual.py\nnode --check app/unified_shadow_templates/iphone_sheet_handoff.js\ngit diff --check\n'}
    steps[8]['with']['name']='sheets-only-synthetic-evidence'
    steps[8]['with']['path']='.private/sheets-only-*.xml\n.private/sheets-only/*.png\n.private/sheets-only/*-evidence.json\n.private/handoff-ui/*.png\n.private/handoff-ui/*-evidence.json\n'
    path=root/'.github/workflows/sheets-only-review-checks.yml'
    text=path.read_text('utf8')
    assert yaml.load(text,Loader=yaml.BaseLoader)==expected
    assert not any(s in text for s in ('secrets.','vars.','environment:','workflow_dispatch','schedule:','continue-on-error'))
