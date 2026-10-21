"""Read-only presentation on the existing authenticated #155 composition."""
from copy import deepcopy

import pytest

from app.iphone_sheet_handoff_host import PREFIX
from sheet_handoff_host_fixtures import HostRig


@pytest.mark.parametrize('path', ['', '/ui.js', '/original/SYN-G101', '/source/SYN-M101', '/view-results/missing'])
def test_ui_auth_before_any_saved_or_source_read(path, monkeypatch, tmp_path):
    r = HostRig(monkeypatch, tmp_path)
    def forbidden(*args, **kwargs): pytest.fail('unauthenticated_read')
    monkeypatch.setattr(r.adapter, '_state', forbidden)
    monkeypatch.setattr(r.old.ui, 'read', forbidden)
    for c in r.old.native.values(): monkeypatch.setattr(c.readers.source_bytes, 'revision_bytes', forbidden)
    assert r.h.get(PREFIX+path).status_code in {403, 410}


def test_navigation_uses_saved_values_only_and_keeps_holds(monkeypatch, tmp_path):
    r = HostRig(monkeypatch, tmp_path); r.h.login()
    before = r.journal.read_versioned(); raw = deepcopy(r.raw)
    def forbidden(*args, **kwargs): pytest.fail('navigation_observation_or_write')
    monkeypatch.setattr(r.reader, 'read', forbidden)
    monkeypatch.setattr(r.io, 'replace_versioned', forbidden)
    for c in r.old.native.values(): monkeypatch.setattr(c.readers, 'observe', forbidden)
    for path in ['', '/ui.js', '/view-results/missing', '/original/SYN-G101', '/source/SYN-G101']:
        response = r.h.get(PREFIX+path)
        assert response.status_code == 200, response.get_data(as_text=True)
        assert response.headers['Cache-Control'] == 'no-store'
        assert response.headers['Referrer-Policy'] == 'no-referrer'
        assert "default-src 'none'" in response.headers['Content-Security-Policy']
    html = r.h.get(PREFIX).get_data(as_text=True)
    assert '正式「確認」1タブ' in html and 'readonly' in html
    for cid in r.contract['cases']: assert cid in html
    for cid, held in r.old.ui.held.items():
        assert cid in html and held['reason'] in html
        for field in held['inputs']: assert str(field['value']) in html
    assert r.journal.read_versioned() == before and r.raw == raw
    assert {cid:r.old.frozen(cid) for cid in r.old.native} == r.before


def test_persisted_attempt_blocks_all_rows_after_adapter_rebuild(monkeypatch, tmp_path):
    r = HostRig(monkeypatch, tmp_path); r.h.login()
    p = r.prepare('SYN-G101', 'import_draft')
    def fail(*args, **kwargs): raise RuntimeError('synthetic lost response')
    monkeypatch.setattr(r.old.ui, 'post', fail)
    assert r.send(p).status_code == 503
    r.adapter.uncertain = False  # simulates restart; durable journal remains
    html = r.h.get(PREFIX).get_data(as_text=True)
    assert html.count('data-financial-stopped="true"') >= 2
    assert 'UNKNOWN' in html
    assert r.h.get(PREFIX+'/view-results/'+p['request_id']).status_code == 200
    assert {cid:r.old.frozen(cid) for cid in r.old.native} == r.before


def test_ui_ci_is_exactly_isolated():
    from pathlib import Path
    import yaml
    path=Path(__file__).resolve().parents[1]/'.github/workflows/iphone-sheet-handoff-ui-checks.yml'
    source=path.read_text('utf8');w=yaml.load(source, Loader=yaml.BaseLoader)
    assert set(w)=={'name','on','permissions','env','concurrency','jobs'}
    assert w['name']=='iPhone synthetic handoff UI'
    assert w['on']=={'pull_request':{'branches':['codex/iphone-one-tab-draft-handoff']}}
    assert w['permissions']=={'contents':'read'}
    assert w['env']=={'UNIFIED_RECEIPT_WRITE_ENABLED':'false'}
    assert w['concurrency']=={'group':'kakeibo-sheet-handoff-ui-${{ github.ref }}','cancel-in-progress':'true'}
    assert set(w['jobs'])=={'isolated-ui'}
    job=w['jobs']['isolated-ui'];assert set(job)=={'runs-on','timeout-minutes','steps'}
    assert job['runs-on']=='ubuntu-24.04' and job['timeout-minutes']=='25'
    steps=job['steps'];assert len(steps)==9
    assert steps[:3]==[
        {'uses':'actions/checkout@v4','with':{'ref':'${{ github.event.pull_request.head.sha }}','fetch-depth':'0','persist-credentials':'false'}},
        {'uses':'actions/setup-python@v5','with':{'python-version':'3.12'}},
        {'uses':'actions/setup-node@v4','with':{'node-version':'22'}}]
    expected=[
        ('Install synthetic test dependencies','pip install -r requirements-test.txt playwright==1.64.0\npython -m playwright install --with-deps chromium\nsudo apt-get install -y fonts-noto-cjk\n'),
        ('Existing adapter and authenticated UI contracts','python -m pytest -q tests/test_iphone_sheet_handoff.py tests/test_iphone_sheet_handoff_host.py tests/test_iphone_handoff_ui.py --junitxml=.private/handoff-ui-host.xml'),
        ('PC and 390px actual browser with OS egress isolation','for tool in unshare ip runuser sudo; do command -v "$tool" >/dev/null; done\ntask_user=$(id -un)\ntask_python=$(command -v python)\ntask_netns=$(readlink /proc/self/ns/net)\nsudo unshare --net -- sh -eu -c \'\n  ip link set lo up\n  exec runuser -u "$1" -- env HANDOFF_OUTER_NETNS="$3" "$2" -m pytest -q tests/test_iphone_handoff_browser.py --junitxml=.private/handoff-ui-browser.xml\n\' sh "$task_user" "$task_python" "$task_netns"\n'),
        ('Existing one-tab screen and input safety regressions','python -m pytest -q tests/test_iphone_sheet_reader.py tests/test_iphone_draft_journal.py tests/test_iphone_persistent_drafts.py tests/test_iphone_source_observation.py tests/test_iphone_item_drafts.py tests/test_iphone_review_drafts.py tests/test_iphone_confirmation.py tests/test_production_workflows.py tests/test_iphone_r1_adapter.py::test_save_returns_exact_readback_revision_tag_and_replay_metadata tests/test_iphone_r1_confirmation.py::test_partial_save_and_medical_origin_block tests/test_iphone_r1_confirmation.py::test_holds_have_no_qualification --junitxml=.private/handoff-ui-regression.xml'),
        ('Script syntax compile and whitespace','node --check app/unified_shadow_templates/iphone_sheet_handoff.js\npython -m compileall -q app/iphone_sheet_handoff_host.py tests/test_iphone_handoff_ui.py tests/test_iphone_handoff_browser.py\ngit diff --check\n')]
    assert steps[3:8]==[dict(name=name,run=run) for name,run in expected]
    assert steps[8]=={'uses':'actions/upload-artifact@v4','if':'always()','with':{
        'name':'handoff-ui-synthetic-evidence','path':'.private/handoff-ui-*.xml\n.private/handoff-ui/*.png\n.private/handoff-ui/*-evidence.json\n',
        'include-hidden-files':'true','retention-days':'7'}}
    assert not any(s in source for s in ('secrets.','vars.','environment:','workflow_dispatch','schedule:','continue-on-error'))
