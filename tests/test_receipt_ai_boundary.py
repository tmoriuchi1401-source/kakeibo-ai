import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from app import cli, receipt_privacy_gate as gate, receipt_text_extraction as extraction
from app.gemini_ai import GeminiAI
from app.medical_receipt_privacy import _StructuredOcrToken
from app.receipt_pipeline import ReceiptPipeline


def image_bytes():
    output = BytesIO()
    Image.new("RGB", (8, 8), "white").save(output, format="PNG")
    return output.getvalue()


def fake_ai():
    transport = Mock(return_value=SimpleNamespace(output_text=json.dumps({
        "merchant": "テスト店", "date": "2026-01-01", "total": 100,
        "items": [{"name": "商品", "amount": 100, "major_category": "食費", "minor_category": "食品"}],
    }, ensure_ascii=False)))
    ai = object.__new__(GeminiAI)
    ai.client = SimpleNamespace(interactions=SimpleNamespace(create=transport))
    ai.model = "synthetic-model"
    return ai, transport


def local_result(monkeypatch, text, *, complete=True, tokens=()):
    local = Mock(return_value=extraction._ReceiptTextExtraction(
        "extracted", "image_ocr", text, tokens, complete))
    monkeypatch.setattr(gate, "_extract_receipt_text", local)
    return local


@pytest.mark.parametrize("text", [
    "病院 診療\n支払額 1200円", "給与明細 基本給", "保険", "", "unknown",
])
@pytest.mark.parametrize("mime", ["image/png", "application/pdf"])
def test_direct_adapter_blocks_medical_sensitive_and_insufficient_input(monkeypatch, text, mime):
    ai, transport = fake_ai()
    local_result(monkeypatch, text)
    with pytest.raises(gate.ReceiptPrivacyBlocked):
        ai.analyze_receipt(image_bytes(), mime, [])
    transport.assert_not_called()


def test_normal_adapter_evaluates_exact_media_before_one_transport_call(monkeypatch):
    ai, transport = fake_ai()
    local = local_result(monkeypatch, "レシート 商品 合計 100円")
    data = image_bytes()
    result = ai.analyze_receipt(data, "image/png", [("食費", "食品")])
    local.assert_called_once_with(data, "image/png")
    transport.assert_called_once()
    assert result.total == 100


@pytest.mark.parametrize("known", ["medical", "payroll", "sensitive_unknown", "invalid"])
@pytest.mark.parametrize("text", ["レシート 商品 合計 100円", "", "unknown"])
def test_known_sensitive_source_cannot_be_downgraded_by_ocr(monkeypatch, known, text):
    ai, transport = fake_ai()
    local = local_result(monkeypatch, text)
    with pytest.raises(gate.ReceiptPrivacyBlocked):
        ai.analyze_receipt(image_bytes(), "image/png", [], known_source_classification=known)
    transport.assert_not_called()
    local.assert_not_called()


def test_declared_normal_source_does_not_override_medical_ocr(monkeypatch):
    ai, transport = fake_ai()
    local_result(monkeypatch, "病院 診療")
    with pytest.raises(gate.ReceiptPrivacyBlocked):
        ai.analyze_receipt(image_bytes(), "image/png", [], known_source_classification="normal")
    transport.assert_not_called()


def test_ocr_exception_never_reaches_transport_or_public_error(monkeypatch):
    ai, transport = fake_ai()
    monkeypatch.setattr(gate, "_extract_receipt_text", Mock(side_effect=RuntimeError("SYNTHETIC_PRIVATE_MARKER")))
    with pytest.raises(gate.ReceiptPrivacyBlocked) as captured:
        ai.analyze_receipt(image_bytes(), "image/png", [])
    assert "SYNTHETIC_PRIVATE_MARKER" not in str(captured.value)
    assert captured.value.__context__ is None
    assert captured.value.__cause__ is None
    transport.assert_not_called()


def test_incomplete_structured_ocr_cannot_authorize_normal_text(monkeypatch):
    ai, transport = fake_ai()
    local_result(monkeypatch, "レシート 商品 合計 100円", complete=False)
    with pytest.raises(gate.ReceiptPrivacyBlocked):
        ai.analyze_receipt(image_bytes(), "image/png", [])
    transport.assert_not_called()


def test_sensitive_structured_signal_cannot_be_bypassed_by_normal_text(monkeypatch):
    ai, transport = fake_ai()
    local_result(monkeypatch, "レシート 商品 合計 100円", tokens=(
        _StructuredOcrToken("患者番号", 1, 10, 20, 50, 12, 96, (1, 1, 1, 5)),
    ))
    with pytest.raises(gate.ReceiptPrivacyBlocked):
        ai.analyze_receipt(image_bytes(), "image/png", [])
    transport.assert_not_called()


def test_pipeline_preserves_known_source_and_does_not_touch_writers(monkeypatch):
    ai, transport = fake_ai()
    db = SimpleNamespace(import_ids=Mock(return_value=set()), categories=Mock(), append=Mock())
    local_result(monkeypatch, "レシート 商品 合計 100円")
    result = ReceiptPipeline(db, ai).process_bytes(
        image_bytes(), "image/png", "synthetic-source", known_source_classification="medical")
    assert result["status"] == "privacy_blocked"
    assert result["classification"] == "sensitive_unknown"
    assert result["reason_code"] == "known_sensitive_source"
    assert result["gemini_allowed"] is False
    db.categories.assert_not_called()
    db.append.assert_not_called()
    transport.assert_not_called()


@pytest.mark.parametrize("command", ["analyze", "receipt"])
@pytest.mark.parametrize("text,known", [
    ("病院 診療\n支払額 1200円", None),
    ("保険", None),
    ("レシート 商品 合計 100円", "medical"),
])
def test_cli_entries_cannot_bypass_real_adapter(monkeypatch, tmp_path, capsys, command, text, known):
    ai, transport = fake_ai()
    db = SimpleNamespace(import_ids=Mock(return_value=set()),
                         categories=Mock(return_value=[("食費", "食品")]), append=Mock())
    local_result(monkeypatch, text)
    monkeypatch.setattr(cli, "make", lambda: (None, db, ai))
    path = tmp_path / "synthetic.png"
    path.write_bytes(image_bytes())
    args = ["cli", command, str(path)]
    if known:
        args += ["--source-classification", known]
    monkeypatch.setattr("sys.argv", args)
    cli.main()
    transport.assert_not_called()
    db.append.assert_not_called()
    output = capsys.readouterr().out
    assert "privacy_blocked" in output
    assert text not in output
    assert str(path) not in output


def test_normal_analyze_cli_keeps_permitted_behavior(monkeypatch, tmp_path, capsys):
    ai, transport = fake_ai()
    db = SimpleNamespace(categories=Mock(return_value=[("食費", "食品")]))
    local_result(monkeypatch, "レシート 商品 合計 100円")
    monkeypatch.setattr(cli, "make", lambda: (None, db, ai))
    path = tmp_path / "synthetic.png"
    path.write_bytes(image_bytes())
    monkeypatch.setattr("sys.argv", ["cli", "analyze", str(path)])
    cli.main()
    transport.assert_called_once()
    assert "100" in capsys.readouterr().out


def test_real_extraction_marks_structured_failure_without_exposing_ocr(monkeypatch):
    monkeypatch.setattr(extraction, "_extract_image_text", lambda content: "病院 診療\n支払額 1200円")
    monkeypatch.setattr(extraction, "_extract_image_ocr_tokens",
                        Mock(side_effect=RuntimeError("SYNTHETIC_PRIVATE_MARKER")))
    data = image_bytes()
    internal = extraction._extract_receipt_text(data, "image/png")
    assert internal.observation_complete is False
    result = gate.evaluate_receipt_privacy(data, "image/png")
    assert result.status == "needs_review"
    assert result.medical_payment_amount is None
    assert "observation_incomplete" in result.diagnostic_codes
    assert "SYNTHETIC_PRIVATE_MARKER" not in result.model_dump_json()
    assert "diagnostic_codes" not in result.model_dump()


def test_adapter_denial_survives_later_missing_classification_signals(monkeypatch):
    ai, transport = fake_ai()
    local_result(monkeypatch, "病院 診療")
    data = image_bytes()
    with pytest.raises(gate.ReceiptPrivacyBlocked):
        ai.analyze_receipt(data, "image/png", [])
    local = local_result(monkeypatch, "レシート 商品 合計 100円")
    with pytest.raises(gate.ReceiptPrivacyBlocked):
        ai.analyze_receipt(data, "image/png", [])
    local.assert_not_called()
    transport.assert_not_called()


def test_pipeline_sensitive_source_survives_changed_bytes_and_missing_hint(monkeypatch):
    ai, transport = fake_ai()
    db = SimpleNamespace(import_ids=Mock(return_value=set()), categories=Mock(), append=Mock())
    pipeline = ReceiptPipeline(db, ai)
    local_result(monkeypatch, "病院 診療")
    assert pipeline.process_bytes(image_bytes(), "image/png", "synthetic-source")["status"] == "privacy_blocked"
    local_result(monkeypatch, "レシート 商品 合計 100円")
    result = pipeline.process_bytes(b"changed synthetic bytes", "image/png", "synthetic-source")
    assert result["status"] == "privacy_blocked"
    transport.assert_not_called()
    db.append.assert_not_called()


def test_drive_input_preserves_known_source_without_archive_or_write(monkeypatch):
    from app import drive_receipts
    ai, transport = fake_ai()
    db = SimpleNamespace(import_ids=Mock(return_value=set()), categories=Mock(), append=Mock())
    local_result(monkeypatch, "レシート 商品 合計 100円")
    service = Mock()
    service.files.return_value.list.return_value.execute.return_value = {"files": [
        {"id": "synthetic-id", "name": "synthetic.png", "mimeType": "image/png", "parents": []},
    ]}
    monkeypatch.setattr(drive_receipts, "drive_service", lambda: service)
    monkeypatch.setattr(drive_receipts, "download_drive_file", lambda file_id: image_bytes())
    result = drive_receipts.process_inbox(
        "synthetic-inbox", ReceiptPipeline(db, ai), "synthetic-processed",
        known_source_classification="medical")
    assert result[0][1]["status"] == "privacy_blocked"
    service.files.return_value.update.assert_not_called()
    db.append.assert_not_called()
    transport.assert_not_called()


def test_mutable_media_cannot_change_after_authorization(monkeypatch):
    ai, transport = fake_ai()
    local = local_result(monkeypatch, "レシート 商品 合計 100円")
    with pytest.raises(gate.ReceiptPrivacyBlocked):
        ai.analyze_receipt(bytearray(image_bytes()), "image/png", [])
    local.assert_not_called()
    transport.assert_not_called()


def test_second_pipeline_gate_denial_stops_before_write(monkeypatch):
    ai, transport = fake_ai()
    local = local_result(monkeypatch, "レシート 商品 合計 100円")
    local.side_effect = [
        extraction._ReceiptTextExtraction("extracted", "image_ocr", "レシート 商品 合計 100円"),
        extraction._ReceiptTextExtraction("extracted", "image_ocr", "病院 診療"),
    ]
    db = SimpleNamespace(import_ids=Mock(return_value=set()), categories=Mock(return_value=[]), append=Mock())
    with pytest.raises(gate.ReceiptPrivacyBlocked):
        ReceiptPipeline(db, ai).process_bytes(image_bytes(), "image/png", "synthetic-source")
    transport.assert_not_called()
    db.append.assert_not_called()


@pytest.mark.parametrize("confidence,width", [("-1", 50), ("69", 50), ("96", 0), ("nan", 50)])
def test_raw_ocr_adapter_cannot_silently_erase_competing_numeric_observation(monkeypatch, confidence, width):
    import sys
    data = {
        "text": ["支払額", "1200円", "3400"], "conf": ["96", "96", confidence],
        "left": [10, 100, 180], "top": [20, 20, 20], "width": [50, 50, width],
        "height": [12, 12, 12], "block_num": [1, 1, 1], "par_num": [1, 1, 1],
        "line_num": [1, 1, 1], "level": [5, 5, 5],
    }
    monkeypatch.setitem(sys.modules, "pytesseract", SimpleNamespace(
        Output=SimpleNamespace(DICT="dict"), image_to_data=lambda *args, **kwargs: data))
    monkeypatch.setattr(extraction, "_extract_image_text", lambda content: "病院 診療\n支払額 1200円")
    result = gate.evaluate_receipt_privacy(image_bytes(), "image/png")
    assert result.status == "needs_review"
    assert result.medical_payment_amount is None
    assert set(result.diagnostic_codes) & {"observation_incomplete", "amount_observation_low_confidence"}


@pytest.mark.parametrize("known", [None, "normal"])
@pytest.mark.parametrize("second_text", ["病院 診療", "保険", "", "unknown"])
def test_new_adapter_rechecks_rejected_bytes_without_source_hint_or_registry(monkeypatch, known, second_text):
    data = image_bytes()
    first, first_transport = fake_ai()
    local_result(monkeypatch, "病院 診療")
    with pytest.raises(gate.ReceiptPrivacyBlocked):
        first.analyze_receipt(data, "image/png", [])
    first_transport.assert_not_called()

    second, second_transport = fake_ai()
    assert not hasattr(second, "_blocked_receipts")
    local = local_result(monkeypatch, second_text)
    with pytest.raises(gate.ReceiptPrivacyBlocked):
        second.analyze_receipt(data, "image/png", [], known_source_classification=known)
    local.assert_called_once_with(data, "image/png")
    second_transport.assert_not_called()


@pytest.mark.parametrize("stage", ["ocr", "classification", "gate"])
def test_each_validation_exception_fails_closed_without_caller_hint(monkeypatch, stage):
    from app import medical_receipt_privacy
    ai, transport = fake_ai()
    local_result(monkeypatch, "レシート 商品 合計 100円")
    failure = Mock(side_effect=RuntimeError("SYNTHETIC_PRIVATE_EXCEPTION"))
    if stage == "ocr":
        monkeypatch.setattr(gate, "_extract_receipt_text", failure)
    elif stage == "classification":
        monkeypatch.setattr(medical_receipt_privacy, "classify_receipt_text", failure)
    else:
        monkeypatch.setattr(gate, "evaluate_receipt_privacy", failure)
    with pytest.raises(gate.ReceiptPrivacyBlocked) as captured:
        ai.analyze_receipt(image_bytes(), "image/png", [])
    failure.assert_called_once()
    transport.assert_not_called()
    assert "SYNTHETIC_PRIVATE_EXCEPTION" not in repr(captured.value)
    assert captured.value.__context__ is None
    assert captured.value.__cause__ is None


@pytest.mark.parametrize("classification", ["unknown", "sensitive_unknown", None])
def test_unrecognized_classification_is_not_an_allow_even_with_other_allow_fields(monkeypatch, classification):
    ai, transport = fake_ai()
    result = SimpleNamespace(
        classification=classification, gemini_allowed=True, status="ready_for_gemini",
        extraction_status="extracted", text_present=True,
    )
    local = Mock(return_value=result)
    monkeypatch.setattr(gate, "evaluate_receipt_privacy", local)
    with pytest.raises(gate.ReceiptPrivacyBlocked):
        ai.analyze_receipt(image_bytes(), "image/png", [], known_source_classification="normal")
    local.assert_called_once()
    transport.assert_not_called()


@pytest.mark.parametrize("repeat_same_bytes", [True, False])
def test_previous_normal_result_never_authorizes_next_call(monkeypatch, repeat_same_bytes):
    import base64
    ai, transport = fake_ai()
    data = image_bytes()
    local_result(monkeypatch, "レシート 商品 合計 100円")
    ai.analyze_receipt(data, "image/png", [])
    transport.assert_called_once()
    assert base64.b64decode(transport.call_args.kwargs["input"][1]["data"]) == data

    # The adapter takes no source ID: even if the caller considers this the same
    # source, neither unchanged nor replaced bytes inherit a previous allow.
    updated = data if repeat_same_bytes else data + b"synthetic revision"
    local = local_result(monkeypatch, "病院 診療")
    with pytest.raises(gate.ReceiptPrivacyBlocked):
        ai.analyze_receipt(updated, "image/png", [], known_source_classification="normal")
    local.assert_called_once_with(updated, "image/png")
    assert transport.call_count == 1


@pytest.mark.parametrize("buffer_type", [bytearray, memoryview])
def test_mutable_or_view_buffer_is_rejected_before_validation_or_transport(monkeypatch, buffer_type):
    ai, transport = fake_ai()
    backing = bytearray(image_bytes())
    value = backing if buffer_type is bytearray else memoryview(backing)
    local = local_result(monkeypatch, "レシート 商品 合計 100円")
    with pytest.raises(gate.ReceiptPrivacyBlocked):
        ai.analyze_receipt(value, "image/png", [])
    backing[0] ^= 1
    local.assert_not_called()
    transport.assert_not_called()


def test_validated_immutable_object_is_the_one_encoded_for_transport(monkeypatch):
    import base64
    ai, transport = fake_ai()
    backing = bytearray(image_bytes())
    snapshot = bytes(backing)
    checked = []

    def inspect(content, mime):
        assert type(content) is bytes
        checked.append(content)
        backing[:] = b"synthetic changed source"
        return extraction._ReceiptTextExtraction(
            "extracted", "image_ocr", "レシート 商品 合計 100円")

    monkeypatch.setattr(gate, "_extract_receipt_text", inspect)
    ai.analyze_receipt(snapshot, "image/png", [])
    assert checked[0] is snapshot
    assert base64.b64decode(transport.call_args.kwargs["input"][1]["data"]) == snapshot
    assert bytes(backing) != snapshot


@pytest.mark.parametrize("changes", [
    {"diagnostic_codes": ("observation_incomplete",)},
    {"reason_code": "insufficient_evidence"},
    {"text_present": "yes"},
    {"status": "blocked"},
    {"medical_payment_amount": 100},
    {"extraction_method": "none"},
    {"diagnostic_codes": ("SYNTHETIC_PRIVATE_MARKER",)},
])
def test_inconsistent_normal_gate_result_cannot_authorize_transport(monkeypatch, changes):
    ai, transport = fake_ai()
    # A future gate bug must not turn an invalid result object into permission.
    fields = {
        "classification": "normal", "extraction_status": "extracted",
        "extraction_method": "image_ocr", "text_present": True,
        "status": "ready_for_gemini", "reason_code": "normal_receipt_evidence",
        **changes,
    }
    result = gate.ReceiptPrivacyGateResult.model_construct(**fields)
    monkeypatch.setattr(gate, "evaluate_receipt_privacy", Mock(return_value=result))
    with pytest.raises(gate.ReceiptPrivacyBlocked) as captured:
        ai.analyze_receipt(image_bytes(), "image/png", [])
    transport.assert_not_called()

    assert "SYNTHETIC_PRIVATE_MARKER" not in repr(captured.value)
    assert captured.value.__context__ is None


def test_normal_receipt_is_independently_validated_in_each_adapter(monkeypatch):
    data = image_bytes()
    local = local_result(monkeypatch, "レシート 商品 合計 100円")
    for _ in range(2):
        ai, transport = fake_ai()
        assert not hasattr(ai, "_blocked_receipts")
        ai.analyze_receipt(data, "image/png", [])
        transport.assert_called_once()
    assert local.call_count == 2
    assert all(call.args == (data, "image/png") for call in local.call_args_list)
