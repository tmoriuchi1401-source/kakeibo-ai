"""Small semantic checks of actual production entry and its dependencies.

Execution tests live in test_receipt_hosted; a manifest alone never proves GO.
"""
import ast
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def tree(path):
    return ast.parse((ROOT/path).read_text(encoding='utf-8'))


def test_receipt_has_existing_hosted_scheduled_production_owner():
    flow = yaml.load((ROOT/'.github/workflows/kakeibo-production.yml').read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
    assert {'cron': '31 22 * * *'} in flow['on']['schedule']
    assert flow['concurrency'] == {'group': 'kakeibo-production', 'cancel-in-progress': 'false', 'queue': 'max'}
    job = flow['jobs']['production']
    assert job['runs-on'].startswith('ubuntu-')
    commands = '\n'.join(s.get('run', '') for s in job['steps'])
    assert 'python -m app.production_flow' in commands and 'scope=drive_receipts' in commands
    # Inspect the actual source factory and registration polling call; a new
    # script/manifest with no production caller cannot satisfy these assertions.
    functions = {n.name: n for n in tree('app/production_source.py').body if isinstance(n, ast.FunctionDef)}
    calls = [n.func for n in ast.walk(functions['receipts']) if isinstance(n, ast.Call)]
    assert any(isinstance(f, ast.Name) and f.id == 'open_pipeline' for f in calls)
    assert any(isinstance(f, ast.Attribute) and f.attr == 'process_pending' for f in calls)
    assert any(isinstance(f, ast.Name) and f.id == 'process_inbox' for f in calls)


def test_new_production_composition_has_no_local_fence_or_pat_transport():
    module = tree('app/receipt_hosted.py')
    imports = {n.module for n in ast.walk(module) if isinstance(n, ast.ImportFrom)}
    imports |= {a.name for n in ast.walk(module) if isinstance(n, ast.Import) for a in n.names}
    assert not imports & {'sqlite3', 'ctypes', 'win32crypt', 'subprocess', 'requests'}
    assert not any('credentials' in (name or '') or 'daily_permit' in (name or '') for name in imports)
    source = (ROOT/'app/receipt_hosted.py').read_text(encoding='utf-8')
    assert 'RECEIPT_GITHUB_TOKEN' not in source and 'ghp_' not in source
    assert 'verify_execution_boundary' in imports or any(
        isinstance(n, ast.ImportFrom) and any(a.name == 'verify_execution_boundary' for a in n.names)
        for n in ast.walk(module))
    # Live connection has no secret-bearing fallback or fake authority switch.
    factory = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == 'open_pipeline')
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == 'IsolatedReceiptWriter' for n in ast.walk(factory))


def test_selected_pr_logic_excludes_trial_runtime_and_keeps_ui_width():
    from app.receipt_review_cases import WIDTH
    assert WIDTH == 98
    module = tree('app/receipt_review_cases.py')
    assert not any(isinstance(n, ast.ClassDef) and n.name == 'ReviewPipeline' for n in module.body)
    for path in ('scripts/install_receipt_review_daily.ps1', 'scripts/receipt_review_credentials.py',
                 'app/receipt_review_daily_permit.py'):
        assert not (ROOT/path).exists()
