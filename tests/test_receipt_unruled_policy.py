"""Synthetic image routing tests. No source receipts or external AI calls."""
from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from app import receipt_classification_ocr as reread
from app import receipt_privacy_gate as gate
from app import receipt_unruled_policy as policy
from app.medical_receipt_privacy import _StructuredOcrToken
from app.receipt_text_extraction import _ReceiptTextExtraction


def receipt_image(*, table=False, angle=0, color=0):
    image = Image.new('L', (600, 900), 255)
    draw = ImageDraw.Draw(image)
    if table:
        for y in (180, 300, 420):
            draw.line((70, y, 530, y), fill=color, width=2)
        for x in (70, 300, 530):
            draw.line((x, 180, x, 420), fill=color, width=2)
    else:
        # Separators, barcode strokes, and aligned text do not form table cells.
        for y in (180, 300, 420):
            draw.line((70, y, 530, y), fill=0, width=2)
            draw.text((70, y + 40), 'ITEM 100  TAX 10  TOTAL 110', fill=0)
        for x in range(70, 400, 5):
            draw.line((x, 600, x, 670), fill=0, width=2)
        # A paper edge spanning multiple separators is not a table by itself.
        draw.line((70, 100, 70, 450), fill=0, width=2)
    image = image.rotate(angle, fillcolor=255)
    out = BytesIO()
    image.save(out, format='PNG')
    return out.getvalue()


@pytest.mark.parametrize('angle', [0, -10, 10])
@pytest.mark.parametrize('color', [0, 200])
def test_ruled_cells_block_even_when_tilted_or_light(angle, color):
    assert policy.has_ruled_table(receipt_image(table=True, angle=angle, color=color), 'image/png') is True


@pytest.mark.parametrize('angle', [0, -10, 10])
def test_separators_barcode_and_paper_edge_are_not_a_ruled_table(angle):
    assert policy.has_ruled_table(receipt_image(angle=angle), 'image/png') is False


@pytest.mark.parametrize('data,mime', [(b'bad image', 'image/png'), (b'pdf', 'application/pdf'), (None, None)])
def test_uninspected_media_is_unknown(data, mime):
    assert policy.has_ruled_table(data, mime) is None


def test_extreme_aspect_ratio_cannot_be_cleared_by_downsampling():
    out = BytesIO()
    Image.new('L', (150, 6000), 255).save(out, format='PNG')
    assert policy.has_ruled_table(out.getvalue(), 'image/png') is None


@pytest.mark.parametrize('value', [None, '', 'true', 'owner-unruled-v2'])
def test_explicit_version_is_required(monkeypatch, value):
    monkeypatch.delenv('RECEIPT_UNRULED_POLICY', raising=False)
    if value is not None:
        monkeypatch.setenv('RECEIPT_UNRULED_POLICY', value)
    assert not policy.allows(receipt_image(), 'image/png', ['領収書'])


def setup_gate(monkeypatch, text='領収書 110円', *, observations=None, complete=True, tokens=()):
    monkeypatch.setenv('RECEIPT_UNRULED_POLICY', policy.POLICY)
    monkeypatch.setattr(gate, '_extract_receipt_text', lambda *_: _ReceiptTextExtraction(
        'extracted', 'image_ocr', text, tokens, observation_complete=complete))
    monkeypatch.setattr(reread, '_read_page', lambda _: observations if observations is not None else (text, text))


@pytest.mark.parametrize('text', ['領収書 110円', 'スギ薬局 買上点数 3', 'ピザ 1080円 内税 98円'])
def test_complete_unruled_image_passes_gate_and_adapter(monkeypatch, text):
    setup_gate(monkeypatch, text)
    data = receipt_image()
    result = gate.evaluate_receipt_privacy(data, 'image/png')
    assert result.gemini_allowed and result.reason_code == policy.REASON
    assert text not in result.model_dump_json()
    gate.require_receipt_ai_permission(data, 'image/png')
    # No stale allow flag: replacement bytes with a grid are checked again.
    with pytest.raises(gate.ReceiptPrivacyBlocked):
        gate.require_receipt_ai_permission(receipt_image(table=True), 'image/png')


@pytest.mark.parametrize('text', ['診療 領収書', '処方せん', '調剤', '保険薬局', '給与明細 基本給', '患者番号'])
@pytest.mark.parametrize('where', ['original', 'reread', 'tokens'])
def test_explicit_sensitive_evidence_in_any_observation_stays_blocked(monkeypatch, text, where):
    token = _StructuredOcrToken(text, 1, 10, 20, 50, 12, 96, (1, 1, 1, 5))
    setup_gate(monkeypatch,
               text if where == 'original' else '領収書 110円',
               observations=(text, '領収書 110円') if where == 'reread' else None,
               tokens=(token,) if where == 'tokens' else ())
    assert not gate.evaluate_receipt_privacy(receipt_image(), 'image/png').gemini_allowed


def test_drugstore_name_in_tokens_is_allowed(monkeypatch):
    token = _StructuredOcrToken('スギ薬局', 1, 10, 20, 50, 12, 96, (1, 1, 1, 5))
    setup_gate(monkeypatch, 'スギ薬局 110円', tokens=(token,))
    assert gate.evaluate_receipt_privacy(receipt_image(), 'image/png').gemini_allowed


@pytest.mark.parametrize('known', ['medical', 'payroll', 'sensitive_unknown'])
def test_known_sensitive_source_remains_blocked(monkeypatch, known):
    setup_gate(monkeypatch)
    data = receipt_image()
    assert not gate.evaluate_receipt_privacy(data, 'image/png', known_source_classification=known).gemini_allowed
    with pytest.raises(gate.ReceiptPrivacyBlocked):
        gate.require_receipt_ai_permission(data, 'image/png', known_source_classification=known)


@pytest.mark.parametrize('observations,complete', [(('', '領収書'), True), (('領収書', ''), True), (('領収書', '領収書'), False)])
def test_incomplete_observation_cannot_authorize(monkeypatch, observations, complete):
    setup_gate(monkeypatch, observations=observations, complete=complete)
    assert not gate.evaluate_receipt_privacy(receipt_image(), 'image/png').gemini_allowed


def test_detector_failure_stays_blocked(monkeypatch):
    import cv2
    setup_gate(monkeypatch)
    def fail(*args, **kwargs):
        raise RuntimeError('private error')
    monkeypatch.setattr(cv2, 'Canny', fail)
    result = gate.evaluate_receipt_privacy(receipt_image(), 'image/png')
    assert not result.gemini_allowed
    assert 'private error' not in result.model_dump_json()
