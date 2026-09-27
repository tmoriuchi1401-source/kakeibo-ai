import json
import base64
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import cli, receipt_privacy_gate
from app.gemini_ai import GeminiAI
from app.receipt_privacy_gate import ReceiptPrivacyBlocked
from app.receipt_text_extraction import _ReceiptTextExtraction


class FakeInteractions:
    def __init__(self):
        self.request = None

    def create(self, **kwargs):
        self.request = kwargs
        result = {
            "merchant": "テスト店",
            "date": "2026-08-16",
            "total": 100,
            "items": [
                {
                    "name": "商品",
                    "amount": 100,
                    "major_category": "食費",
                    "minor_category": "食品",
                }
            ],
        }
        return SimpleNamespace(output_text=json.dumps(result, ensure_ascii=False))


@pytest.mark.parametrize(
    ("mime_type", "expected_type"),
    [("application/pdf", "document"), ("image/jpeg", "image")],
)
def test_receipt_media_uses_matching_interaction_type(monkeypatch, mime_type, expected_type):
    monkeypatch.setattr(
        receipt_privacy_gate,
        "_extract_receipt_text",
        lambda content, mime: _ReceiptTextExtraction(
            "extracted", "image_ocr", "レシート 商品 合計 100円"
        ),
    )
    interactions = FakeInteractions()
    ai = object.__new__(GeminiAI)
    ai.client = SimpleNamespace(interactions=interactions)
    ai.model = "test-model"

    result = ai.analyze_receipt(
        b"receipt",
        mime_type,
        [("食費", "食品")],
        known_source_classification="normal",
    )

    assert result.total == 100
    media = interactions.request["input"][1]
    assert media["type"] == expected_type
    assert media["mime_type"] == mime_type


def test_known_sensitive_source_never_reaches_ocr_or_transport(monkeypatch):
    extraction = Mock(side_effect=AssertionError("OCR must not run"))
    monkeypatch.setattr(receipt_privacy_gate, "_extract_receipt_text", extraction)
    interactions = FakeInteractions()
    ai = object.__new__(GeminiAI)
    ai.client = SimpleNamespace(interactions=interactions)
    ai.model = "test-model"

    with pytest.raises(ReceiptPrivacyBlocked):
        ai.analyze_receipt(
            b"private receipt",
            "application/pdf",
            [],
            known_source_classification="medical",
        )

    extraction.assert_not_called()
    assert interactions.request is None


def test_heic_is_normalized_and_rechecked_as_exact_png_before_transport(monkeypatch):
    from PIL import Image
    from app import receipt_text_extraction

    image = Image.new('RGB', (10, 20), 'white')
    monkeypatch.setattr(receipt_text_extraction, '_normalized_image',
                        lambda content, mime: image.copy())
    observed = []
    def extracted(content, mime):
        observed.append((content, mime))
        return _ReceiptTextExtraction('extracted', 'image_ocr', 'レシート 商品 合計 100円')
    monkeypatch.setattr(receipt_privacy_gate, '_extract_receipt_text', extracted)
    interactions = FakeInteractions()
    ai = object.__new__(GeminiAI)
    ai.client = SimpleNamespace(interactions=interactions)
    ai.model = 'test-model'

    ai.analyze_receipt(b'synthetic-heic', 'image/heic', [('食費', '食品')])
    media = interactions.request['input'][1]
    assert media['mime_type'] == 'image/png'
    sent = base64.b64decode(media['data'])
    assert sent.startswith(b'\x89PNG\r\n\x1a\n')
    assert observed == [(sent, 'image/png')]


def test_direct_adapter_blocks_medical_ocr_result(monkeypatch):
    monkeypatch.setattr(
        receipt_privacy_gate,
        "_extract_receipt_text",
        lambda content, mime: _ReceiptTextExtraction(
            "extracted", "image_ocr", "病院 診療 支払額 1200円"
        ),
    )
    interactions = FakeInteractions()
    ai = object.__new__(GeminiAI)
    ai.client = SimpleNamespace(interactions=interactions)
    ai.model = "test-model"

    with pytest.raises(ReceiptPrivacyBlocked):
        ai.analyze_receipt(b"private receipt", "image/png", [])

    assert interactions.request is None


def test_analyze_cli_blocks_medical_before_real_adapter_transport(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        receipt_privacy_gate,
        "_extract_receipt_text",
        lambda content, mime: _ReceiptTextExtraction(
            "extracted", "image_ocr", "病院 診療 支払額 1200円"
        ),
    )
    interactions = FakeInteractions()
    ai = object.__new__(GeminiAI)
    ai.client = SimpleNamespace(interactions=interactions)
    ai.model = "test-model"
    db = SimpleNamespace(categories=lambda: [("食費", "食品")])
    image = tmp_path / "medical.png"
    image.write_bytes(b"private receipt")
    monkeypatch.setattr(cli, "make", lambda: (object(), db, ai))
    monkeypatch.setattr("sys.argv", ["kakeibo-ai", "analyze", str(image)])

    cli.main()

    assert "privacy_blocked" in capsys.readouterr().out
    assert interactions.request is None


def test_analyze_cli_normal_media_reaches_real_adapter_once(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        receipt_privacy_gate,
        "_extract_receipt_text",
        lambda content, mime: _ReceiptTextExtraction(
            "extracted", "image_ocr", "レシート 商品 合計 100円"
        ),
    )
    interactions = FakeInteractions()
    ai = object.__new__(GeminiAI)
    ai.client = SimpleNamespace(interactions=interactions)
    ai.model = "test-model"
    db = SimpleNamespace(categories=lambda: [("食費", "食品")])
    image = tmp_path / "normal.png"
    image.write_bytes(b"normal receipt")
    monkeypatch.setattr(cli, "make", lambda: (object(), db, ai))
    monkeypatch.setattr("sys.argv", ["kakeibo-ai", "analyze", str(image)])

    cli.main()

    assert "100" in capsys.readouterr().out
    assert interactions.request is not None
