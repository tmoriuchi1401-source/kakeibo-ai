from copy import deepcopy
import json
from pathlib import Path
import sys
import time

import pytest
import yaml

from app.drive_run_state import StateError
from staging_shadow.persistent_registration import supervise


def test_new_ci_reuses_credential_free_ubuntu_contract():
    root=Path(__file__).resolve().parents[1]
    expected=yaml.load((root/'.github/workflows/offline-registration-runner-checks.yml').read_text('utf8'),Loader=yaml.BaseLoader)
    expected['name']='Persistent synthetic registration runner'
    expected['on']['pull_request']['branches']=['codex/offline-sheets-registration-runner']
    expected['concurrency']['group']='kakeibo-persistent-registration-runner-${{ github.ref }}'
    job=expected['jobs']['offline-runner']
    step=next(s for s in job['steps'] if s.get('name')=='Offline runner with blocked egress')
    step['run']=step['run'].replace('tests/test_registration_runner.py tests/test_registration_runner_ci.py --junitxml=',
        'tests/test_registration_runner.py tests/test_registration_runner_ci.py tests/test_persistent_registration.py tests/test_persistent_drive_slots.py tests/test_persistent_runner_ci.py --junitxml=')
    step=next(s for s in job['steps'] if s.get('name')=='Compile preserved scripts and whitespace')
    step['run']=step['run'].replace('python -m compileall -q ',
        'python -m compileall -q staging_shadow/persistent_registration.py tests/persistent_runner_worker.py tests/test_persistent_registration.py tests/test_persistent_drive_slots.py tests/test_persistent_runner_ci.py ',1)
    job['steps'][-1]['with']['name']='persistent-registration-synthetic-evidence'
    source=(root/'.github/workflows/persistent-registration-runner-checks.yml').read_text('utf8')
    assert yaml.load(source,Loader=yaml.BaseLoader)==expected
    assert not any(x in source for x in ('secrets.','vars.','workflow_dispatch','schedule:','environment:',
        'id-token:','pull_request_target','actions/cache','continue-on-error'))
    assert expected['permissions']==dict(contents='read')
    assert job['steps'][-1]['with']['path'].splitlines()==['.private/offline-runner.xml','.private/live-sheets-*.xml']


def test_entire_worker_has_hard_wall_deadline():
    began=time.monotonic()
    result=supervise([sys.executable,'-c','import time; time.sleep(60)'],max_seconds=1)
    assert result['status']=='UNKNOWN' and time.monotonic()-began<8


def test_supervisor_preserves_only_safe_worker_summary():
    from staging_shadow.registration_runner import ShortRunner
    result=ShortRunner(Path('.'),None).summary('COMPLETE')
    assert supervise([sys.executable,'-c','print('+repr(json.dumps(result))+')'],max_seconds=10)==result


def test_supervisor_accepts_only_fixed_summary_and_never_retries(tmp_path):
    sentinel=tmp_path/'calls'
    code="from pathlib import Path; import sys; Path(sys.argv[1]).write_text('once'); print('private-invalid-output')"
    with pytest.raises((StateError, ValueError)):
        supervise([sys.executable,'-c',code,str(sentinel)],max_seconds=10)
    assert sentinel.read_text()=='once'
