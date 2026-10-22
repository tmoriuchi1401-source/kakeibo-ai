from copy import deepcopy
import os
from pathlib import Path
import socket

import pytest
import yaml


ISOLATED='''for tool in unshare ip runuser sudo; do command -v "$tool" >/dev/null; done
task_user=$(id -un)
task_python=$(command -v python)
task_netns=$(readlink /proc/self/ns/net)
sudo unshare --net -- sh -eu -c '
  ip link set lo up
  exec runuser -u "$1" -- env REGISTRATION_OUTER_NETNS="$3" UNIFIED_RECEIPT_WRITE_ENABLED=false "$2" -m pytest -q tests/test_registration_runner.py tests/test_registration_runner_ci.py --junitxml=.private/offline-runner.xml
' sh "$task_user" "$task_python" "$task_netns"
'''


def test_offline_runner_ci_is_exactly_isolated():
    from test_live_sheets_ci import test_live_sheets_ci_is_exactly_isolated
    test_live_sheets_ci_is_exactly_isolated()
    root=Path(__file__).resolve().parents[1]
    expected=deepcopy(yaml.load((root/'.github/workflows/live-synthetic-sheets-checks.yml').read_text('utf8'),Loader=yaml.BaseLoader))
    expected['name']='Offline synthetic registration runner'
    expected['on']['pull_request']['branches']=['codex/live-synthetic-sheets-registration']
    expected['concurrency']=dict(group='kakeibo-offline-registration-runner-${{ github.ref }}',**{'cancel-in-progress':'false'})
    job=expected['jobs'].pop('isolated-live-sheet');expected['jobs']['offline-runner']=job
    job['steps'].insert(4,dict(name='Offline runner with blocked egress',run=ISOLATED))
    compile_step=next(s for s in job['steps'] if s.get('name')=='Compile preserved scripts and whitespace')
    compile_step['run']=compile_step['run'].replace('python -m compileall -q ','python -m compileall -q staging_shadow/registration_runner.py tests/offline_registration_runner.py tests/test_registration_runner.py tests/test_registration_runner_ci.py ',1)
    artifact=job['steps'][-1]['with']
    artifact['name']='offline-registration-synthetic-evidence'
    artifact['path']='.private/offline-runner.xml\n'+artifact['path']
    source=(root/'.github/workflows/offline-registration-runner-checks.yml').read_text('utf8')
    assert yaml.load(source,Loader=yaml.BaseLoader)==expected
    assert not any(v in source for v in ('schedule:','workflow_dispatch','secrets.','vars.','environment:','continue-on-error','actions/cache'))
    assert artifact['path'].splitlines()==['.private/offline-runner.xml','.private/live-sheets-*.xml','.private/sheets-only/*.png','.private/sheets-only/*-evidence.json','.private/handoff-ui/*.png','.private/handoff-ui/*-evidence.json']


def test_runner_ci_has_no_external_network():
    if os.environ.get('GITHUB_ACTIONS')=='true':assert os.environ.get('REGISTRATION_OUTER_NETNS')
    if not os.environ.get('REGISTRATION_OUTER_NETNS'):pytest.skip('Ubuntu network namespace evidence only')
    assert os.readlink('/proc/self/ns/net')!=os.environ['REGISTRATION_OUTER_NETNS']
    assert set(p.name for p in Path('/sys/class/net').iterdir())=={'lo'}
    with pytest.raises(OSError):socket.create_connection(('1.1.1.1',443),timeout=.1)
