from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.daily_runtime import daily_from_environment, refresh_daily
from app.monthly_projection import ProjectionError
from test_production_flow import valid_env


def test_disabled_daily_has_no_services_or_side_effects():
    assert refresh_daily(object(),None,{})=={}


@pytest.mark.parametrize("key,value",[("GITHUB_ACTIONS","false"),("GITHUB_REF","refs/heads/feature"),
    ("GITHUB_SHA","b"*40),("KAKEIBO_VALIDATED_MAIN_SHA",""),("GITHUB_REPOSITORY","other/repo")])
def test_daily_rejects_execution_outside_validated_main(key,value):
    env=dict(valid_env(),KAKEIBO_DAILY_SPREADSHEET_ID="encrypted");env[key]=value
    with pytest.raises(ProjectionError,match="validated_main_required"):
        daily_from_environment(object(),object(),env)


@pytest.mark.parametrize("grant",[{"id":"other","type":"user"},{"id":"public","type":"anyone"}])
def test_daily_cannot_copy_financial_data_to_wider_sharing(monkeypatch,grant):
    monkeypatch.setattr("app.settings.service_account_source",lambda:("",{"private_key":"synthetic"}))
    monkeypatch.setattr("app.private_state_bindings.unwrap",lambda *args:"daily")
    drive=Mock()
    drive.files().get().execute.side_effect=[{"permissions":[{"id":"owner","type":"user"}]},
        {"mimeType":"application/vnd.google-apps.spreadsheet","permissions":[grant]}]
    with pytest.raises(ProjectionError,match="daily_sharing_mismatch"):
        daily_from_environment(SimpleNamespace(sid="source"),SimpleNamespace(service=drive),
            dict(valid_env(),KAKEIBO_DAILY_SPREADSHEET_ID="encrypted"))


def test_refresh_uses_actual_ledger_tab_id_and_never_submits(monkeypatch):
    daily=Mock();daily.refresh.return_value={"daily_changed_blocks":0}
    monkeypatch.setattr("app.daily_runtime.daily_from_environment",lambda *args:daily)
    monkeypatch.setattr("app.daily_sheets.read_existing_reviews",lambda db:[])
    source=Mock();source._sheet_metadata.return_value={"sheets":[{"properties":{"title":"支出明細","sheetId":987}}]}
    assert refresh_daily(source,object(),{})=={"daily_changed_blocks":0}
    assert daily.refresh.call_args.kwargs["ledger_sheet_id"]==987
    daily.submit.assert_not_called()
