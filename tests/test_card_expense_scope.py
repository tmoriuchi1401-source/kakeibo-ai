from types import SimpleNamespace
from pathlib import Path
import json

import pytest
import yaml

from app import production_flow as flow
from app.private_state_bindings import wrap,unwrap
from test_private_state_bindings import key


def args(scope='card_expenses',**changes):
    return SimpleNamespace(**(dict(scope=scope,bank_apply=False,amazon_target='',receipt_store='',receipt_manifest='',
        projection_bootstrap=False,mode='apply',canary_source='amazon')|changes))


def test_fixed_scope_runs_only_existing_autoexpense_without_global_sort(monkeypatch):
    monkeypatch.setenv('CARD_EXPENSE_REQUEST','{"imports":[],"plan_sha256":"prepared"}')
    monkeypatch.setenv('GITHUB_EVENT_NAME','workflow_dispatch')
    flow.validate_scope(args())
    assert flow.MANUAL_SOURCE_SCOPES['card_expenses']=={'auto_expense'}
    assert flow.command('auto_expense',apply=True)[-1]=='auto-expense'
    assert not flow.needs_ledger_order('card_expenses',{},bank_apply=False)
    for scope in ('core','drive_bank','all'):
        with pytest.raises(RuntimeError):flow.validate_scope(args(scope))


@pytest.mark.parametrize('change',[dict(bank_apply=True),dict(amazon_target='other'),dict(receipt_store='other'),
    dict(receipt_manifest='other'),dict(projection_bootstrap=True)])
def test_selected_expenses_cannot_add_other_authority(monkeypatch,change):
    monkeypatch.setenv('CARD_EXPENSE_REQUEST','prepared');monkeypatch.setenv('GITHUB_EVENT_NAME','workflow_dispatch')
    with pytest.raises(RuntimeError):flow.validate_scope(args(**change))


def test_card_owner_id_uses_existing_key_without_plaintext_export(key):
    owner='aupaycard-mail:'+'a'*24+':001'
    cipher=wrap('CARD_EXPENSE_IMPORT_ID',owner,key)
    assert owner not in cipher and unwrap('CARD_EXPENSE_IMPORT_ID',cipher,key)==owner
    with pytest.raises(RuntimeError):unwrap('AMAZON_TARGET',cipher,key)


def test_existing_workflows_keep_shared_lock_and_bank_schedule():
    root=Path(flow.REPO)/'.github/workflows'
    production=yaml.load((root/'kakeibo-production.yml').read_text(encoding='utf-8'),Loader=yaml.BaseLoader)
    card=yaml.load((root/'aupay-card-incremental-apply.yml').read_text(encoding='utf-8'),Loader=yaml.BaseLoader)
    assert production['concurrency']['group']==card['concurrency']['group']=='kakeibo-production'
    assert {'cron':'47 21 * * *'} in production['on']['schedule']
    assert card['on']['schedule']==[{'cron':'23 0 * * *'}]
    assert 'cutoff' in card['on']['workflow_dispatch']['inputs']
    assert 'card_expense_request' in production['on']['workflow_dispatch']['inputs']
    assert flow.SCHEDULE_SCOPES['23 20 * * *']==('core','KAKEIBO_CORE_SCHEDULE_ENABLED')
