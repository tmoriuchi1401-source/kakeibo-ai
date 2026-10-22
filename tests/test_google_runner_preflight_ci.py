from pathlib import Path

import yaml


def test_preflight_ci_is_exactly_isolated():
    from test_persistent_runner_ci import test_new_ci_reuses_credential_free_ubuntu_contract
    test_new_ci_reuses_credential_free_ubuntu_contract()
    root=Path(__file__).resolve().parents[1]
    expected=(root/'.github/workflows/persistent-registration-runner-checks.yml').read_text('utf8')
    expected=expected.replace('name: Persistent synthetic registration runner','name: Google runner read-only preflight')
    expected=expected.replace('branches: [codex/offline-sheets-registration-runner]','branches: [codex/persistent-runner-cas-checkpoint]')
    expected=expected.replace('kakeibo-persistent-registration-runner-${{ github.ref }}','kakeibo-google-runner-preflight-${{ github.ref }}')
    expected=expected.replace('tests/test_persistent_runner_ci.py --junitxml=',
        'tests/test_persistent_runner_ci.py tests/test_google_runner_preflight.py tests/test_google_runner_preflight_ci.py --junitxml=')
    expected=expected.replace('python -m compileall -q ',
        'python -m compileall -q staging_shadow/google_runner_preflight.py tests/test_google_runner_preflight.py tests/test_google_runner_preflight_ci.py ',1)
    expected=expected.replace('name: persistent-registration-synthetic-evidence','name: google-runner-preflight-synthetic-evidence')
    source=(root/'.github/workflows/google-runner-preflight-checks.yml').read_text('utf8')
    assert yaml.load(source,Loader=yaml.BaseLoader)==yaml.load(expected,Loader=yaml.BaseLoader)
    assert not any(x in source for x in ('secrets.','vars.','environment:','workflow_dispatch','schedule:',
        'pull_request_target','id-token:','actions/cache','continue-on-error'))
