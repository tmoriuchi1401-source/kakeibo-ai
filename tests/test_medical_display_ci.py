"""Classify this exact credential-free workflow, never exempt a filename alone."""
from pathlib import Path

import yaml


def test_medical_display_ci_is_exactly_isolated():
    from test_live_sheets_ci import test_live_sheets_ci_is_exactly_isolated
    test_live_sheets_ci_is_exactly_isolated()
    root = Path(__file__).resolve().parents[1]
    source = (root/'.github/workflows/live-synthetic-sheets-checks.yml').read_text('utf8')
    replacements = [
        ('Live synthetic Google Sheets boundary', 'Medical input display (synthetic only)'),
        ('branches: [codex/sheets-preauthorized-registration]', 'branches: [codex/live-synthetic-sheets-registration]'),
        ('kakeibo-live-synthetic-sheets-', 'kakeibo-medical-display-'),
        ('isolated-live-sheet:', 'medical-display:'),
        ('python -m pytest -q tests/test_live_synthetic_sheets.py',
         'python -m pytest -q tests/test_medical_input_display.py tests/test_medical_display_ci.py tests/test_live_synthetic_sheets.py'),
        ('tests/test_registration_visual.py tests/test_sheets_only_visual.py',
         'tests/test_medical_input_visual.py tests/test_registration_visual.py tests/test_sheets_only_visual.py'),
        ('python -m compileall -q staging_shadow/live_synthetic_sheets.py',
         'python -m compileall -q staging_shadow/medical_input_display.py tests/test_medical_input_display.py tests/test_medical_input_visual.py staging_shadow/live_synthetic_sheets.py'),
        ('live-sheets-synthetic-evidence', 'medical-display-synthetic-evidence'),
    ]
    for old, new in replacements:
        assert old in source
        source = source.replace(old, new)
    actual = (root/'.github/workflows/medical-input-display-checks.yml').read_text('utf8')
    assert yaml.load(actual, Loader=yaml.BaseLoader) == yaml.load(source, Loader=yaml.BaseLoader)
    assert not any(x in actual for x in ('secrets.', 'vars.', 'environment:', 'workflow_dispatch', 'schedule:', 'continue-on-error'))
