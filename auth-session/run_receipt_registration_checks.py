"""Compose an isolated fixture and test registration; never deploy or dispatch."""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from compose import compose, ROOT, HERE


def main():
    with tempfile.TemporaryDirectory(prefix='receipt-registration-contracts-') as temporary:
        fixture = Path(temporary) / 'fixture'
        compose(fixture, test_tree=True)  # Unchanged pinned OIDC/session core.
        # Separate namespace: current PR #196 validation must NOT replace any
        # hash-pinned historical auth model/validation module, even in a test.
        preview = fixture / 'receipt_registration_preview'
        preview.mkdir()
        (preview / '__init__.py').write_text('', encoding='utf8')
        for name in ('receipt_item_correction_plan.py', 'receipt_item_correction_review.py',
                     'receipt_review_snapshot.py', 'category_rules.py', 'reconciliation.py',
                     'receipt_validation.py', 'models.py'):
            shutil.copyfile(ROOT / 'app' / name, preview / name)
        for name, symbol in [('sheets', 'SheetsDB'), ('transaction_plan', 'Transaction'),
                             ('utils', 'normalize_store'), ('page_receipt_model', 'digest')]:
            (preview / (name + '.py')).write_text('from app.'+name+' import '+symbol+'\n', encoding='utf8')
        # Test-only additions. compose.py --output remains exactly unchanged.
        shutil.copyfile(HERE / 'receipt_correction_registration.py',
                        fixture / 'services/human_general/receipt_correction_registration.py')
        for name in ('receipt_correction_io.py','receipt_correction_web.py'):
            shutil.copyfile(HERE/name,fixture/'services/human_general'/name)
        shutil.copyfile(HERE / 'receipt_correction_contracts.py',
                        fixture / 'tests/test_receipt_correction_registration.py')
        shutil.copyfile(HERE/'receipt_correction_flow_contracts.py',fixture/'tests/test_receipt_correction_flow.py')
        subprocess.run([sys.executable, '-m', 'pytest', '-q', '--tb=short',
                        'tests/test_receipt_correction_registration.py',
                        'tests/test_receipt_correction_flow.py'], cwd=fixture, check=True)
        print('receipt_registration_synthetic_passed; financial_write=0; deployed=false')


if __name__ == '__main__':
    main()
