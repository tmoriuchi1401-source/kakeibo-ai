from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import Mock

import pytest

from app import cli
from app.medical_inbox_handoff_shadow import MedicalInboxHandoffShadow
from app.settings import Settings
from tests.test_medical_inbox_handoff_shadow import medical_gate
from tests.test_receipt_pipeline import FakeDB, FakeAI, _normal_gate, _normal_receipt_result


def test_configured_store_parent_is_initialized_and_key_decoded(tmp_path):
    store = tmp_path / "nested" / "medical-review.json"
    key = base64.b64encode(b"configured-medical-key").decode("ascii")
    settings = Settings(
        medical_review_store_path=str(store),
        medical_review_identity_key=key,
    )

    assert not store.parent.exists()
    assert settings.ensure_medical_review_store_parent() == store.parent
    assert store.parent.is_dir()
    assert settings.medical_review_identity_key_bytes() == b"configured-medical-key"


def test_explicit_handoff_factory_uses_configured_persistent_store(tmp_path):
    store = tmp_path / "nested" / "medical-review.json"
    key = base64.b64encode(b"configured-medical-key").decode("ascii")
    settings = Settings(
        medical_review_store_path=str(store),
        medical_review_identity_key=key,
        medical_review_shadow_enabled=True,
    )

    handoff = settings.medical_review_handoff()
    assert store.parent.is_dir()
    handoff.observe(source_id="factory-source", gate=medical_gate())
    assert store.exists()


@pytest.mark.parametrize(
    "value",
    ["", "not-base64", base64.b64encode(b"short").decode("ascii")],
)
def test_invalid_identity_key_fails_closed(value, tmp_path):
    settings = Settings(
        medical_review_store_path=str(tmp_path / "review.json"),
        medical_review_identity_key=value,
    )

    with pytest.raises(RuntimeError):
        settings.medical_review_identity_key_bytes()


def test_relative_store_path_is_rejected(tmp_path):
    settings = Settings(
        medical_review_store_path="relative-review.json",
        medical_review_identity_key=base64.b64encode(b"configured-medical-key").decode("ascii"),
    )

    with pytest.raises(RuntimeError):
        settings.medical_review_store_parent()


def test_repository_store_path_is_rejected():
    settings = Settings(
        medical_review_store_path=str(Path(__file__).resolve().parents[1] / "review.json"),
        medical_review_identity_key=base64.b64encode(b"configured-medical-key").decode("ascii"),
    )

    with pytest.raises(RuntimeError):
        settings.medical_review_store_parent()


def test_cli_lists_and_shows_persistent_pending_item_read_only(tmp_path, monkeypatch, capsys):
    store = tmp_path / "review.json"
    handoff = MedicalInboxHandoffShadow(identity_key=b"configured-medical-key", store_path=store)
    handoff.observe(source_id="cli-source", gate=medical_gate())
    settings = Settings(medical_review_store_path=str(store))
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    monkeypatch.setattr("sys.argv", ["kakeibo-ai", "medical-review", "list"])
    cli.main()
    listed = capsys.readouterr().out
    assert handoff.items()[0].review_item_id in listed
    assert "cli-source" not in listed

    monkeypatch.setattr(
        "sys.argv", ["kakeibo-ai", "medical-review", "show", handoff.items()[0].review_item_id]
    )
    cli.main()
    shown = capsys.readouterr().out
    assert handoff.items()[0].review_item_id in shown
    assert "cli-source" not in shown


def test_enabled_but_invalid_handoff_config_keeps_medical_blocked(monkeypatch):
    settings = Settings(medical_review_shadow_enabled=True)
    db = FakeDB()
    ai = FakeAI(_normal_receipt_result())
    monkeypatch.setattr(
        "app.receipt_pipeline.evaluate_receipt_privacy", lambda content, mime: medical_gate()
    )

    result = cli.make_receipt_pipeline(settings, db, ai).process_bytes(
        b"medical", "image/png", "invalid-config-source"
    )

    assert result["status"] == "privacy_blocked"
    assert result["medical_shadow_status"] == "handoff_failed"
    ai.analyze_receipt.assert_not_called()
    assert db.append_calls == []


def test_medical_shadow_runs_without_gemini_key_or_client(tmp_path, monkeypatch):
    store = tmp_path / "review.json"
    settings = Settings(
        gemini_api_key="",
        medical_review_store_path=str(store),
        medical_review_identity_key=base64.b64encode(
            b"configured-medical-key"
        ).decode("ascii"),
        medical_review_shadow_enabled=True,
    )
    db = FakeDB()
    gemini_client = Mock(side_effect=AssertionError("Gemini client must not be created"))
    monkeypatch.setattr(cli, "GeminiAI", gemini_client)
    monkeypatch.setattr(
        "app.receipt_pipeline.evaluate_receipt_privacy", lambda content, mime: medical_gate()
    )

    result = cli.make_receipt_pipeline(settings, db, None).process_bytes(
        b"medical", "image/png", "medical-shadow-source"
    )

    assert result["status"] == "privacy_blocked"
    assert result["medical_shadow_status"] == "new_item"
    assert store.exists()
    gemini_client.assert_not_called()
    assert db.append_calls == []


def test_normal_receipt_without_gemini_key_uses_existing_validation(monkeypatch):
    settings = Settings(gemini_api_key="")
    db = FakeDB()
    gemini_client = Mock(side_effect=AssertionError("invalid client construction"))
    monkeypatch.setattr(cli, "GeminiAI", gemini_client)
    monkeypatch.setattr(
        "app.receipt_pipeline.evaluate_receipt_privacy", lambda content, mime: _normal_gate()
    )

    with pytest.raises(RuntimeError, match="未設定: GEMINI_API_KEY"):
        cli.make_receipt_pipeline(settings, db, None).process_bytes(
            b"normal", "image/png", "normal-source"
        )

    gemini_client.assert_not_called()
    assert db.append_calls == []
