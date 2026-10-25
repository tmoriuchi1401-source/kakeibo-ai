from pathlib import Path
import yaml


def test_local_ci_is_exactly_isolated_and_credentials_free():
    from test_google_a0g_ci import test_a0g_ci_is_exactly_isolated_and_credentials_free
    test_a0g_ci_is_exactly_isolated_and_credentials_free()
    root=Path(__file__).resolve().parents[1]
    expected=(root/'.github/workflows/google-a0g-preparation-checks.yml').read_text('utf8')
    expected=expected.replace('name: A0-G bounded metadata preparation','name: A0-G local one-shot lifecycle')
    expected=expected.replace('branches: [codex/google-runner-preflight-audit]','branches: [codex/a0-g-get-only-preparation]')
    expected=expected.replace('kakeibo-a0g-preparation-','kakeibo-a0g-local-')
    expected=expected.replace('tests/test_google_a0g_ci.py --junitxml=',
        'tests/test_google_a0g_ci.py tests/test_google_a0g_local.py tests/test_google_a0g_local_ci.py --junitxml=')
    expected=expected.replace('python -m compileall -q ',
        'python -m compileall -q staging_shadow/google_a0g_ledger.py staging_shadow/google_a0g_loopback.py staging_shadow/google_a0g_local.py tests/test_google_a0g_local.py tests/a0g_local_worker.py tests/a0g_local_fakes.py tests/test_google_a0g_local_ci.py ',1)
    expected=expected.replace('name: a0g-preparation-synthetic-evidence','name: a0g-local-synthetic-evidence')
    source=(root/'.github/workflows/google-a0g-local-checks.yml').read_text('utf8')
    assert yaml.load(source,Loader=yaml.BaseLoader)==yaml.load(expected,Loader=yaml.BaseLoader)
    assert not any(x in source for x in ('secrets.','vars.','environment:','workflow_dispatch',
        'schedule:','pull_request_target','id-token:','actions/cache','continue-on-error'))
