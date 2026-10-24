from pathlib import Path
import yaml


def test_a0g_ci_is_exactly_isolated_and_credentials_free():
    from test_google_runner_preflight_ci import test_preflight_ci_is_exactly_isolated
    test_preflight_ci_is_exactly_isolated()
    root = Path(__file__).resolve().parents[1]
    expected = (root/'.github/workflows/google-runner-preflight-checks.yml').read_text('utf8')
    expected = expected.replace('name: Google runner read-only preflight', 'name: A0-G bounded metadata preparation')
    expected = expected.replace('branches: [codex/persistent-runner-cas-checkpoint]', 'branches: [codex/google-runner-preflight-audit]')
    expected = expected.replace('kakeibo-google-runner-preflight-', 'kakeibo-a0g-preparation-')
    expected = expected.replace('tests/test_google_runner_preflight_ci.py --junitxml=',
        'tests/test_google_runner_preflight_ci.py tests/test_google_a0g.py tests/test_google_a0g_oauth.py tests/test_google_a0g_ci.py --junitxml=')
    expected = expected.replace('python -m compileall -q ',
        'python -m compileall -q staging_shadow/google_a0g.py staging_shadow/google_a0g_oauth.py tests/test_google_a0g.py tests/test_google_a0g_oauth.py tests/test_google_a0g_ci.py ',1)
    expected = expected.replace('name: google-runner-preflight-synthetic-evidence', 'name: a0g-preparation-synthetic-evidence')
    source = (root/'.github/workflows/google-a0g-preparation-checks.yml').read_text('utf8')
    assert yaml.load(source, Loader=yaml.BaseLoader) == yaml.load(expected, Loader=yaml.BaseLoader)
    assert not any(x in source for x in ('secrets.', 'vars.', 'environment:', 'workflow_dispatch',
        'schedule:', 'pull_request_target', 'id-token:', 'actions/cache', 'continue-on-error'))
