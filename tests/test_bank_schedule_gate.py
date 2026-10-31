"""Evaluate the actual workflow conditions with synthetic Actions contexts."""
from itertools import product
from pathlib import Path
import re

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
BANK_CRON = '47 21 * * *'


def workflow(name):
    return yaml.load((ROOT / '.github/workflows' / name).read_text(encoding='utf-8'), Loader=yaml.BaseLoader)


def context(*, event='schedule', cron=BANK_CRON, bank='', general='false'):
    return {
        'github.repository': 'tmoriuchi1401-source/kakeibo-ai', 'github.ref': 'refs/heads/main',
        'github.event_name': event, 'github.event.schedule': cron,
        'github.actor': 'owner', 'github.triggering_actor': 'owner', 'github.repository_owner': 'owner',
        'github.sha': 'a' * 40, 'vars.KAKEIBO_VALIDATED_MAIN_SHA': 'a' * 40,
        'vars.KAKEIBO_PRODUCTION_ENABLED': 'true', 'vars.KAKEIBO_LEGACY_DISABLED': 'true',
        'vars.KAKEIBO_BANK_SCHEDULE_ENABLED': bank, 'vars.KAKEIBO_SCHEDULE_ENABLED': general,
        'vars.CATEGORY_BACKFILL_SHEET_RUNNER_ENABLED': 'false',
    }


def allowed(expression, values):
    # This test-only evaluator handles the equality/boolean subset used by these
    # actual conditions. Repr substitution prevents context values becoming code.
    expression = expression.removeprefix('${{').removesuffix('}}').strip()
    expression = re.sub(r'\b(?:github|vars|inputs)\.[A-Za-z_.]+',
        lambda match: repr(values.get(match.group(), '')), expression)
    expression = expression.replace('&&', ' and ').replace('||', ' or ')
    return bool(eval('(' + expression + ')', {'__builtins__': {}}, {}))


def parent_allowed(values):
    return allowed(workflow('kakeibo-production.yml')['jobs']['production']['if'], values)


@pytest.mark.parametrize('general,bank', tuple(product(('', 'false', 'true'), repeat=2)))
def test_each_integrated_cron_uses_its_own_flag(general, bank):
    scheduled = workflow('kakeibo-production.yml')['on']['schedule']
    assert len(scheduled) == 4
    for item in scheduled:
        cron = item['cron']
        assert parent_allowed(context(cron=cron, bank=bank, general=general)) is (
            (bank if cron == BANK_CRON else general) == 'true')


def test_bank_only_enables_exactly_0647_and_preserves_scope():
    parent = workflow('kakeibo-production.yml')
    crons = [item['cron'] for item in parent['on']['schedule']]
    assert [cron for cron in crons if parent_allowed(context(cron=cron, bank='true'))] == [BANK_CRON]
    assert not parent_allowed(context(cron='unrecognized', bank='true'))
    execute = next(s for s in parent['jobs']['production']['steps'] if s.get('name') == 'Execute sources and dependent accounting serially')
    assert "'47 21 * * *') scope=drive_bank; args+=(--bank-apply) ;;" in execute['run']
    from app.production_flow import DRIVE_SCHEDULE_SCOPES
    assert DRIVE_SCHEDULE_SCOPES['drive_bank'] == frozenset({'bank'})


@pytest.mark.parametrize('general,bank', tuple(product(('', 'false', 'true'), repeat=2)))
@pytest.mark.parametrize('mode', ['preview', 'apply'])
def test_manual_dispatch_does_not_depend_on_schedule_flags(general, bank, mode):
    values = context(event='workflow_dispatch', cron='', bank=bank, general=general)
    values.update({'inputs.mode': mode, 'inputs.scope': 'drive_bank'})
    assert parent_allowed(values)


@pytest.mark.parametrize('field,value', [
    ('github.ref', 'refs/heads/unvalidated'), ('github.sha', 'b' * 40),
    ('vars.KAKEIBO_PRODUCTION_ENABLED', 'false'), ('vars.KAKEIBO_LEGACY_DISABLED', 'false')])
def test_bank_flag_does_not_bypass_existing_guards(field, value):
    values = context(bank='true'); values[field] = value
    assert not parent_allowed(values)


def test_all_other_existing_scheduled_jobs_stay_stopped_in_bank_only_mode():
    values = context(bank='true')
    names = []
    for path in (ROOT / '.github/workflows').glob('*.yml'):
        definition = workflow(path.name)
        if 'schedule' not in definition.get('on', {}) or path.name == 'kakeibo-production.yml':
            continue
        names.append(path.name)
        for item in definition['on']['schedule']:
            for job in definition['jobs'].values():
                assert not allowed(job['if'], dict(values, **{'github.event.schedule': item['cron']})), path.name
    assert {'bank-pdf-recurring.yml', 'process-receipts.yml', 'aupay-card-recurring-production.yml',
        'monthly-maintenance.yml', 'category-backfill-sheet.yml'} <= set(names)


def test_monthly_manual_dispatch_is_preserved():
    condition = workflow('monthly-maintenance.yml')['jobs']['maintain']['if']
    assert allowed(condition, context(event='workflow_dispatch', bank='true'))
    assert allowed(condition, context(general='true'))
    assert not allowed(condition, context(general='false', bank='true'))


def test_held_backlog_still_counts_toward_authority_file_bound():
    from datetime import datetime
    from app.bank_pdf_recurring import BankPdfWindow, list_bounded_bank_pdfs
    class Request:
        def __init__(self, value): self.value = value
        def execute(self): return self.value
    class Drive:
        def __init__(self, files): self.items = files
        def files(self): return self
        def list(self, **kwargs):
            assert 'modifiedTime >=' not in kwargs['q']
            assert kwargs['pageSize'] == 21
            return Request({'files': self.items})
    held = [{'id': 'synthetic-held-' + str(i), 'mimeType': 'application/pdf'} for i in range(5)]
    fresh = [{'id': 'synthetic-new-' + str(i), 'mimeType': 'application/pdf'} for i in range(16)]
    window = BankPdfWindow(datetime.fromisoformat('2026-10-30T00:00:00+09:00'),
                           datetime.fromisoformat('2026-10-31T00:00:00+09:00'))
    folder = 'A' * 20
    assert len(list_bounded_bank_pdfs(Drive(held + fresh[:15]), folder, window, 20, include_backlog=True)) == 20
    with pytest.raises(RuntimeError, match='bank_recurring_file_bound_exceeded'):
        list_bounded_bank_pdfs(Drive(held + fresh), folder, window, 20, include_backlog=True)
