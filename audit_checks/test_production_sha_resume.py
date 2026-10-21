"""Read Git objects and run workflow routing with a recording stub, never app CLI."""
import ast
import itertools
import json
import os
from pathlib import Path
import re
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
OLD = 'fffbe7568db3acd647e9a088280bd4fcbed6c5c9'
MAIN = 'd62e1e254692e676f4e4a72dc593524a80fbea08'


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args])


def production():
    return yaml.load(git('show', MAIN + ':.github/workflows/kakeibo-production.yml'), Loader=yaml.BaseLoader)


def test_entire_tree_diff_is_two_documentation_files():
    assert git('merge-base', OLD, MAIN).decode().strip() == OLD
    assert git('rev-list', '--count', OLD + '..' + MAIN).strip() == b'4'
    assert git('rev-list', '--first-parent', '--count', OLD + '..' + MAIN).strip() == b'2'
    assert set(git('diff', '--name-only', OLD, MAIN).decode().splitlines()) == {
        'AGENTS.md', 'docs/iphone-receipt-review-mvp-spec.md'}
    def runtime_tree(ref):
        return [row for row in git('ls-tree', '-r', ref).splitlines()
                if row.split(b'\t', 1)[1] not in (b'AGENTS.md', b'docs/iphone-receipt-review-mvp-spec.md')]
    assert runtime_tree(OLD) == runtime_tree(MAIN)


def job_condition(context):
    # Interpret only the boolean/string comparison grammar present in the real YAML.
    expression = production()['jobs']['production']['if']
    for key, value in sorted(context.items(), key=lambda item: -len(item[0])):
        expression = expression.replace(key, repr(value))
    expression = expression.replace('&&', ' and ').replace('||', ' or ')
    tree = ast.parse(expression.strip(), mode='eval')
    def evaluate(node):
        if isinstance(node, ast.Expression): return evaluate(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, str): return node.value
        if isinstance(node, ast.BoolOp):
            values = [evaluate(x) for x in node.values]
            if isinstance(node.op, ast.And): return all(values)
            if isinstance(node.op, ast.Or): return any(values)
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = evaluate(node.left), evaluate(node.comparators[0])
            # GitHub string comparisons ignore case; contexts here are strings.
            if isinstance(node.ops[0], ast.Eq): return left.casefold() == right.casefold()
            if isinstance(node.ops[0], ast.NotEq): return left.casefold() != right.casefold()
        raise AssertionError('Unexpected job-condition grammar')
    return evaluate(tree)


def context(event='schedule', validated=OLD, sha=MAIN):
    return {'github.repository': 'tmoriuchi1401-source/kakeibo-ai',
            'github.ref': 'refs/heads/main', 'github.event_name': event, 'github.sha': sha,
            'vars.KAKEIBO_PRODUCTION_ENABLED': 'true', 'vars.KAKEIBO_LEGACY_DISABLED': 'true',
            'vars.KAKEIBO_SCHEDULE_ENABLED': 'true', 'vars.KAKEIBO_VALIDATED_MAIN_SHA': validated}


@pytest.mark.parametrize('event', ['schedule', 'workflow_dispatch'])
@pytest.mark.parametrize('sha,validated,expected', [(MAIN, OLD, False), (MAIN, MAIN, True),
                                                   (OLD, OLD, True), (OLD, MAIN, False)])
def test_real_condition_sha_truth_table(event, sha, validated, expected):
    assert job_condition(context(event, validated, sha)) is expected


@pytest.mark.parametrize('key,value', [
    ('github.repository', 'fork/repo'), ('github.ref', 'refs/heads/feature'),
    *[(key, value) for key in ('vars.KAKEIBO_PRODUCTION_ENABLED', 'vars.KAKEIBO_LEGACY_DISABLED',
                              'vars.KAKEIBO_SCHEDULE_ENABLED') for value in ('', 'false', 'not-true')]])
def test_missing_or_disabled_gate_rejects_schedule(key, value):
    values = context(validated=MAIN)
    values[key] = value
    assert job_condition(values) is False


def test_github_case_insensitive_strings_are_not_python_boundary_approval():
    values = context(validated=MAIN.upper())
    values['vars.KAKEIBO_PRODUCTION_ENABLED'] = 'TRUE'
    assert job_condition(values) is True  # The later Bash/Python exact guards still differ.


@pytest.mark.parametrize('production_flag,legacy_flag,schedule_flag', list(itertools.product(['true', 'false'], repeat=3)))
def test_dispatch_keeps_production_and_legacy_gates(production_flag, legacy_flag, schedule_flag):
    values = context('workflow_dispatch', MAIN)
    values.update({'vars.KAKEIBO_PRODUCTION_ENABLED': production_flag,
                   'vars.KAKEIBO_LEGACY_DISABLED': legacy_flag,
                   'vars.KAKEIBO_SCHEDULE_ENABLED': schedule_flag})
    assert job_condition(values) is (production_flag == legacy_flag == 'true')


@pytest.mark.skipif(os.name == 'nt', reason='Execute original Bash routing on dedicated Ubuntu CI')
@pytest.mark.parametrize('event,schedule,mode,scope,confirm,bank,expected', [
    ('schedule', '17 9,21 * * *', '', '', '', '', ['--mode', 'apply', '--scope', 'core']),
    ('schedule', '31 */3 * * *', '', '', '', '', ['--mode', 'apply', '--scope', 'drive_receipts']),
    ('schedule', '47 21 * * *', '', '', '', '', ['--mode', 'apply', '--bank-apply', '--scope', 'drive_bank']),
    ('schedule', '11 23 * * *', '', '', '', '', ['--mode', 'apply', '--scope', 'drive_paypay']),
    ('schedule', 'unknown', '', '', '', '', None),
    ('workflow_dispatch', '', 'preview', 'all', '', 'false', ['--mode', 'preview', '--scope', 'all', '--canary-source', 'amazon']),
    ('workflow_dispatch', '', 'apply', 'all', '', 'false', None),
    ('workflow_dispatch', '', 'preview', 'all', '', 'true', None),
    ('workflow_dispatch', '', 'apply', 'core', 'APPLY', 'false', ['--mode', 'apply', '--scope', 'core', '--canary-source', 'amazon']),
])
def test_original_bash_routes_to_recording_stub_only(tmp_path, event, schedule, mode, scope, confirm, bank, expected):
    output = tmp_path / 'argv.json'
    stub = tmp_path / 'python'
    # Fixed synthetic recorder replaces python; no app module can be invoked.
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$RECORDER"\n', encoding='utf-8')
    stub.chmod(0o700)
    step = next(x for x in production()['jobs']['production']['steps']
                if x.get('name') == 'Execute sources and dependent accounting serially')
    env = {'PATH': str(tmp_path) + ':/usr/bin:/bin', 'RECORDER': str(output),
           'EVENT_NAME': event, 'EVENT_SCHEDULE': schedule, 'MANUAL_MODE': mode,
           'MANUAL_SCOPE': scope, 'CONFIRM': confirm, 'BANK_APPLY': bank,
           'CANARY_SOURCE': 'amazon', 'PROJECTION_BOOTSTRAP': 'false', 'AMAZON_TARGET': '',
           'RECEIPT_STORE': '', 'RECEIPT_MANIFEST': '', 'RECEIPT_OPERATION': 'replay', 'RECEIPT_LIMIT': '1'}
    result = subprocess.run(['bash', '-c', step['run']], env=env, capture_output=True, timeout=10)
    if expected is None:
        assert result.returncode != 0 and not output.exists()
    else:
        assert result.returncode == 0
        assert output.read_text().splitlines() == ['-m', 'app.production_flow', *expected]


def test_checkout_race_and_transport_limit_are_preserved():
    workflow = production()
    assert workflow['concurrency'] == {'group': 'kakeibo-production', 'cancel-in-progress': 'false', 'queue': 'max'}
    steps = workflow['jobs']['production']['steps']
    assert steps[0]['with']['ref'] == 'main'
    assert steps[0]['with']['persist-credentials'] == 'false'
    assert 'test "$(git rev-parse HEAD)" = "$KAKEIBO_VALIDATED_MAIN_SHA"' in steps[1]['run']
    transport = git('show', MAIN + ':app/drive_run_state.py').decode()
    assert 'module is not a distributed lock' in transport
    assert 'If-Match' not in transport


def test_new_ci_is_pr_only_and_pins_real_main_composition():
    path = ROOT / '.github/workflows/production-sha-audit.yml'
    source = path.read_text()
    workflow = yaml.load(source, Loader=yaml.BaseLoader)
    assert workflow['on'] == {'pull_request': {'branches': ['codex/r2-3-additional-readonly-audit']}}
    assert workflow['permissions'] == {'contents': 'read'}
    assert all(word not in source for word in ('secrets.', 'vars.', 'environment:', 'workflow_dispatch', 'pull_request_target'))
    assert 'git worktree add --detach "$RUNNER_TEMP/observed-main" ' + MAIN in source
    assert 'python -m app.' not in source
