from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import cli, receipt_privacy_gate
from app.receipt_text_extraction import _ReceiptTextExtraction
from app.settings import Settings


@pytest.mark.parametrize('override,expected', [('', 'original-model'), ('gemini-3.5-flash-lite', 'gemini-3.5-flash-lite')])
def test_normal_receipt_model_selected_only_after_privacy_gate(monkeypatch, override, expected):
    settings = SimpleNamespace(
        gemini_api_key='synthetic-key', gemini_model='original-model',
        normal_receipt_gemini_model=override, validate=Mock(),
    )
    db = Mock(); db.import_ids.return_value = []
    client = Mock()
    client.analyze_receipt.side_effect = RuntimeError('synthetic_transport_boundary')
    factory = Mock(return_value=client)
    monkeypatch.setattr(cli, 'GeminiAI', factory)
    monkeypatch.setattr(receipt_privacy_gate, '_extract_receipt_text', lambda *_:
                        _ReceiptTextExtraction('extracted', 'image_ocr', 'レシート 商品 合計 100円'))
    pipeline = cli.make_receipt_pipeline(settings, db, None)
    factory.assert_not_called()
    with pytest.raises(RuntimeError, match='^synthetic_transport_boundary$'):
        pipeline.process_bytes(b'synthetic receipt', 'image/png', 'synthetic-id')
    factory.assert_called_once_with('synthetic-key', expected)
    settings.validate.assert_called_once_with(need_gemini=True)
    db.append.assert_not_called()


@pytest.mark.parametrize('known', ['medical', 'payroll', 'sensitive_unknown'])
def test_model_override_never_bypasses_known_sensitive_source(monkeypatch, known):
    settings = Settings(gemini_api_key='synthetic-key', normal_receipt_gemini_model='gemini-3.5-flash-lite')
    db = Mock(); db.import_ids.return_value = []
    factory = Mock(side_effect=AssertionError('must not construct Gemini'))
    monkeypatch.setattr(cli, 'GeminiAI', factory)
    pipeline = cli.make_receipt_pipeline(settings, db, None)
    result = pipeline.process_bytes(b'synthetic private source', 'image/png', 'synthetic-id',
                                    known_source_classification=known)
    assert result['status'] == 'privacy_blocked'
    factory.assert_not_called()
    db.append.assert_not_called()


def test_setting_is_optional_and_does_not_change_other_model(monkeypatch):
    monkeypatch.setenv('NORMAL_RECEIPT_GEMINI_MODEL', 'gemini-3.5-flash-lite')
    settings = Settings(gemini_model='original-model')
    assert settings.normal_receipt_gemini_model == 'gemini-3.5-flash-lite'
    assert settings.gemini_model == 'original-model'
    monkeypatch.delenv('NORMAL_RECEIPT_GEMINI_MODEL')
    assert Settings().normal_receipt_gemini_model == ''


def test_explicit_existing_adapter_is_preserved():
    adapter = object()
    settings = Settings(normal_receipt_gemini_model='gemini-3.5-flash-lite')
    assert cli.make_receipt_pipeline(settings, Mock(), adapter).ai is adapter
