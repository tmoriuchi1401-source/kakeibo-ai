import json
from types import SimpleNamespace

import pytest

from app.gemini_ai import GeminiAI


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
def test_receipt_media_uses_matching_interaction_type(mime_type, expected_type):
    interactions = FakeInteractions()
    ai = object.__new__(GeminiAI)
    ai.client = SimpleNamespace(interactions=interactions)
    ai.model = "test-model"

    result = ai.analyze_receipt(b"receipt", mime_type, [("食費", "食品")])

    assert result.total == 100
    media = interactions.request["input"][1]
    assert media["type"] == expected_type
    assert media["mime_type"] == mime_type
