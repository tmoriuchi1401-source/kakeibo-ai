from copy import deepcopy
import os
from pathlib import Path
import socket
import subprocess
import sys

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
    install=next(s for s in job['steps'] if s.get('name')=='Install synthetic verification dependencies')
    install['run']='pip install -r requirements-test.txt formulas==1.3.4\n'
    job['steps']=[s for s in job['steps'] if s.get('name')!='Static Sheet fixture visibility and existing browser regressions with blocked egress']
    job['steps'].insert(4,dict(name='Offline runner with blocked egress',run=ISOLATED))
    compile_step=next(s for s in job['steps'] if s.get('name')=='Compile preserved scripts and whitespace')
    compile_step['run']=compile_step['run'].replace('python -m compileall -q ','python -m compileall -q staging_shadow/registration_runner.py tests/offline_registration_runner.py tests/test_registration_runner.py tests/test_registration_runner_ci.py ',1)
    compile_step['run']+='git diff --exit-code ${{ github.event.pull_request.base.sha }} ${{ github.event.pull_request.head.sha }} -- app/unified_shadow_templates tests/test_registration_visual.py tests/test_sheets_only_visual.py tests/test_iphone_handoff_browser.py\n'
    artifact=job['steps'][-1]['with']
    artifact['name']='offline-registration-synthetic-evidence'
    artifact['path']='.private/offline-runner.xml\n.private/live-sheets-*.xml\n'
    source=(root/'.github/workflows/offline-registration-runner-checks.yml').read_text('utf8')
    assert yaml.load(source,Loader=yaml.BaseLoader)==expected
    assert not any(v in source for v in ('schedule:','workflow_dispatch','secrets.','vars.','environment:','continue-on-error','actions/cache'))
    assert artifact['path'].splitlines()==['.private/offline-runner.xml','.private/live-sheets-*.xml']


def test_runner_ci_has_no_external_network():
    if os.environ.get('GITHUB_ACTIONS')=='true':assert os.environ.get('REGISTRATION_OUTER_NETNS')
    if not os.environ.get('REGISTRATION_OUTER_NETNS'):pytest.skip('Ubuntu network namespace evidence only')
    assert os.readlink('/proc/self/ns/net')!=os.environ['REGISTRATION_OUTER_NETNS']
    # /sys can remain the inherited host mount after unshare --net. Query
    # interfaces in the CURRENT network namespace, not that mount's view.
    assert {name for _,name in socket.if_nameindex()}=={'lo'}
    # Keep conftest's socket-denial fixture intact. A fresh child inherits the
    # OS namespace and probes the kernel fence without pytest monkeypatches.
    script="import socket,sys\ntry:\n socket.create_connection(('1.1.1.1',443),timeout=.1)\nexcept OSError:\n print('OS_NETWORK_BLOCKED')\nelse:\n sys.exit(5)"
    child=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,timeout=3)
    assert child.returncode==0 and child.stdout.strip()=='OS_NETWORK_BLOCKED'
