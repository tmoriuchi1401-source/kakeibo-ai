import pytest
from pydantic import ValidationError

from app.medical_receipt_privacy import (
    ClassificationDecision,
    PaymentAmountCandidate,
    ReceiptPrivacyPreview,
    SafeModelValidationError,
    _StructuredOcrToken,
    build_receipt_privacy_preview,
    classify_receipt_text,
    extract_medical_payment_candidates,
    extract_structured_medical_payment_candidates,
    gemini_allowed_for,
    resolve_medical_payment_candidates,
)


@pytest.mark.parametrize(
    ("text", "classification"),
    [
        ("レシート 商品 小計 1,000円 現金 1,000円", "normal"),
        ("お買上 商品A 500円 合計 500円", "normal"),
        ("テスト病院 診療費 患者負担額 3,250円", "medical"),
        ("給与明細 基本給 300,000円 控除 50,000円", "payroll"),
        (None, "sensitive_unknown"),
        ("", "sensitive_unknown"),
        ("  \n\t", "sensitive_unknown"),
        ("\n\n", "sensitive_unknown"),
        ("書類番号 123", "sensitive_unknown"),
        ("病院 診療 給与明細 基本給", "sensitive_unknown"),
        ("領収書", "sensitive_unknown"),
        ("健康保険", "sensitive_unknown"),
        ("病院", "sensitive_unknown"),
        ("薬局", "sensitive_unknown"),
        ("小計 1,000円 現金 1,000円", "sensitive_unknown"),
        ("消費税 100円 税込 1,100円", "sensitive_unknown"),
        ("領収書 現金 3,000円", "sensitive_unknown"),
        ("給与 レシート 商品 合計 1,000円", "sensitive_unknown"),
        ("保険 レシート 商品 合計 1,000円", "sensitive_unknown"),
        ("患者 レシート 商品 合計 1,000円", "sensitive_unknown"),
        ("診療 レシート 商品 合計 1,000円", "sensitive_unknown"),
        ("レシート 商品 小計 400円 患者番号DUMMY", "medical"),
        ("レシート 商品 小計 400円 給与明細", "payroll"),
    ],
)
def test_classification_is_conservative(text, classification):
    assert classify_receipt_text(text).classification == classification


@pytest.mark.parametrize(
    ("classification", "expected"),
    [
        ("normal", True),
        ("medical", False),
        ("payroll", False),
        ("sensitive_unknown", False),
        ("unexpected_value", False),
    ],
)
def test_only_normal_is_allowed_to_send_to_gemini(classification, expected):
    assert gemini_allowed_for(classification) is expected


def test_preview_derives_gemini_policy_from_classification():
    normal = build_receipt_privacy_preview("レシート 商品 合計 500円")
    medical = build_receipt_privacy_preview("病院 診療 患者負担額 500円")

    assert normal.gemini_allowed is True
    assert medical.gemini_allowed is False


def test_preview_rejects_explicit_contradictory_gemini_policy():
    with pytest.raises(ValidationError):
        ReceiptPrivacyPreview(
            classification="medical",
            gemini_allowed=True,
            status="needs_review",
            payment_amount=None,
            candidate_count=0,
            reason_code="no_candidate",
            category="医療費",
        )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("領収金額 3,250円", 3250),
        ("領収金額 3250円", 3250),
        ("お支払額 ¥3,250", 3250),
        ("お支払額 ￥3,250", 3250),
        ("領収金額 3,250 円", 3250),
        ("自己負担額 ０円", 0),
        ("患者支払額 １２，３４０円", 12340),
        ("お支払い額 ￥１２，３４０", 12340),
        ("9,000円 支払額", 9000),
        ("領収金額 0009円", 9),
        ("領収金額 999,999,999円", 999999999),
    ],
)
def test_extracts_supported_complete_amount_tokens(text, expected):
    candidates = extract_medical_payment_candidates(text)

    assert len(candidates) == 1
    assert candidates[0].amount == expected
    assert candidates[0].strength == "strong"


@pytest.mark.parametrize(
    "text",
    [
        "領収金額 3 250円",
        "領収金額 1,23,456円",
        "領収金額 -500円",
        "領収金額 −500円",
        "領収金額 －500円",
        "領収金額 - 500円",
        "領収金額 1.250円",
        "領収金額 1'250円",
        "領収金額 1_250円",
        "領収金額 1/250円",
        "お支払額 ¥1.250",
        "お支払額 ¥1'250",
    ],
)
def test_rejects_invalid_or_negative_amount_tokens_without_partial_match(text):
    assert extract_medical_payment_candidates(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "保険支払額 21,000円",
        "保険者支払額 21,000円",
        "公費支払額 10,000円",
        "保険者請求額 21,000円",
        "保険請求額 21,000円",
        "保険負担額 21,000円",
        "医療費総額 30,000円",
        "診療点数 1250点",
        "預り金 10,000円",
        "お釣り 2,500円",
        "保険 支払額 21,000円",
        "保険　支払額 21,000円",
        "保険者 支払額 21,000円",
        "保険者　支払額 21,000円",
        "公費 支払額 10,000円",
        "公費　支払額 10,000円",
        "保険料 支払額 21,000円",
    ],
)
def test_excludes_non_patient_payment_context(text):
    assert extract_medical_payment_candidates(text) == []


@pytest.mark.parametrize(
    ("text", "label_type"),
    [
        ("患者支払額 9,000円", "patient_responsibility"),
        ("患者負担額 9,000円", "patient_responsibility"),
        ("自己負担額 9,000円", "self_pay"),
        ("支払額 9,000円", "payment_amount"),
        ("お支払額 9,000円", "payment_amount"),
        ("お支払い額 9,000円", "payment_amount"),
        ("患者 支払額 9,000円", "patient_responsibility"),
    ],
)
def test_keeps_allowed_specific_payment_labels(text, label_type):
    candidates = extract_medical_payment_candidates(text)

    assert len(candidates) == 1
    assert candidates[0].amount == 9000
    assert candidates[0].label_type == label_type


@pytest.mark.parametrize(
    "text",
    [
        "患者負担額 5,000円 自己負担額 7,000円",
        "患者負担額 5,000円 預り金 10,000円 お釣り 5,000円",
        "領収金額 5,000円 7,000円",
        "領収金額 5,000円 領収金額 5,000円",
        "領収金額\n3,250円",
    ],
)
def test_ambiguous_or_cross_line_relationships_produce_no_candidate(text):
    assert extract_medical_payment_candidates(text) == []


def _ocr_token(
    text: str,
    x: float,
    *,
    confidence: float = 96,
    page: int = 1,
    line: int = 1,
    width: float = 50,
) -> _StructuredOcrToken:
    return _StructuredOcrToken(
        text=text,
        page=page,
        x=x,
        y=float(line * 20),
        width=width,
        height=12,
        confidence=confidence,
        line_key=(1, 1, line, 5),
    )


def test_structured_ocr_confirms_one_high_confidence_same_line_payment():
    candidates = extract_structured_medical_payment_candidates(
        (_ocr_token("支払額", 10), _ocr_token("3,250円", 100))
    )

    assert [(candidate.amount, candidate.label_type, candidate.strength) for candidate in candidates] == [
        (3250, "payment_amount", "strong")
    ]


def test_structured_ocr_supports_only_unambiguous_truncated_strong_label():
    candidates = extract_structured_medical_payment_candidates(
        (_ocr_token("支額", 10), _ocr_token("3,250円", 100))
    )

    assert len(candidates) == 1
    assert candidates[0].amount == 3250
    assert candidates[0].strength == "strong"


@pytest.mark.parametrize(
    "tokens",
    [
        (_ocr_token("支払額", 10), _ocr_token("38.430円", 100)),
        (_ocr_token("支払額", 10), _ocr_token("3,250円", 100), _ocr_token("4,000円", 180)),
        (_ocr_token("保険", 10), _ocr_token("支払額", 70), _ocr_token("3,250円", 150)),
        (_ocr_token("支払額", 10), _ocr_token("3,250円", 100, page=2)),
        (_ocr_token("支払額", 10), _ocr_token("3,250円", 100, confidence=69)),
    ],
)
def test_structured_ocr_rejects_ambiguous_or_unsafe_payment_evidence(tokens):
    assert extract_structured_medical_payment_candidates(tokens) == []


def test_dot_separated_structured_ocr_amount_stays_needs_review():
    preview = build_receipt_privacy_preview(
        "病院 診療",
        (_ocr_token("支払額", 10), _ocr_token("38.430円", 100)),
    )

    assert preview.classification == "medical"
    assert preview.status == "needs_review"
    assert preview.payment_amount is None
    assert preview.reason_code == "no_candidate"


def _candidate(
    amount: int,
    strength: str,
    *,
    label_type: str = "receipt_amount",
    line_index: int = 0,
) -> PaymentAmountCandidate:
    return PaymentAmountCandidate(
        amount=amount,
        label_type=label_type,
        strength=strength,
        rank=1,
        source_line_index=line_index,
    )


@pytest.mark.parametrize(
    ("candidates", "status", "amount", "reason"),
    [
        ([_candidate(5000, "strong")], "confirmed", 5000, "unique_strong_candidate"),
        (
            [_candidate(5000, "strong"), _candidate(5000, "strong", line_index=1)],
            "confirmed",
            5000,
            "duplicate_same_amount",
        ),
        (
            [_candidate(5000, "strong"), _candidate(5000, "weak", line_index=1)],
            "confirmed",
            5000,
            "duplicate_same_amount",
        ),
        (
            [
                _candidate(5000, "strong"),
                _candidate(5000, "strong", line_index=1),
                _candidate(5000, "weak", line_index=2),
            ],
            "confirmed",
            5000,
            "duplicate_same_amount",
        ),
        (
            [_candidate(5000, "strong"), _candidate(7000, "strong", line_index=1)],
            "needs_review",
            None,
            "conflicting_candidates",
        ),
        (
            [_candidate(5000, "strong"), _candidate(7000, "weak", line_index=1)],
            "needs_review",
            None,
            "conflicting_candidates",
        ),
        (
            [
                _candidate(5000, "strong"),
                _candidate(7000, "strong", line_index=1),
                _candidate(5000, "weak", line_index=2),
            ],
            "needs_review",
            None,
            "conflicting_candidates",
        ),
        (
            [_candidate(5000, "weak", label_type="billing_amount")],
            "needs_review",
            None,
            "weak_candidate_only",
        ),
        ([], "needs_review", None, "no_candidate"),
    ],
)
def test_resolves_all_strong_and_weak_candidates(candidates, status, amount, reason):
    resolution = resolve_medical_payment_candidates(candidates)

    assert resolution.status == status
    assert resolution.amount == amount
    assert resolution.reason_code == reason


def test_negative_candidate_cannot_be_constructed_directly():
    with pytest.raises(ValidationError):
        _candidate(-500, "strong")


def test_result_models_are_frozen():
    preview = build_receipt_privacy_preview("レシート 商品 合計 500円")

    with pytest.raises(ValidationError):
        preview.classification = "medical"


def _serialized_forms(model) -> tuple[str, str, str]:
    return repr(model), str(model.model_dump()), model.model_dump_json()


def _contains_sensitive_marker(values: tuple[str, ...], markers: tuple[str, ...]) -> bool:
    return any(marker in value for value in values for marker in markers)


def test_all_returned_models_exclude_synthetic_sensitive_source_data():
    ocr_text = (
        "テスト病院\n氏名 山田太郎\n患者番号ABC123\n保険者番号99999999\n"
        "診療内容 胃炎\n患者負担額 1,234円"
    )
    decision = classify_receipt_text(ocr_text)
    candidate = extract_medical_payment_candidates(ocr_text)[0]
    resolution = resolve_medical_payment_candidates([candidate])
    preview = build_receipt_privacy_preview(ocr_text)
    serialized = tuple(
        value
        for model in (decision, candidate, resolution, preview)
        for value in _serialized_forms(model)
    )
    sensitive_markers = (
        "山田太郎",
        "患者番号ABC123",
        "保険者番号99999999",
        "胃炎",
        "テスト病院",
        ocr_text,
    )

    if _contains_sensitive_marker(serialized, sensitive_markers):
        pytest.fail("safe result model retained synthetic source data", pytrace=False)


def test_reason_code_rejects_free_text_without_echoing_it_in_error():
    synthetic_marker = "患者番号ABC123"

    with pytest.raises(ValidationError) as captured:
        ClassificationDecision(
            classification="sensitive_unknown",
            reason_code=synthetic_marker,
        )
    if synthetic_marker in str(captured.value):
        pytest.fail("validation error echoed rejected source data", pytrace=False)


def test_safe_validation_boundary_exposes_only_data_free_error():
    synthetic_marker = "山田太郎 患者番号ABC123 保険者番号99999999 胃炎 テスト病院"

    with pytest.raises(SafeModelValidationError) as captured:
        ClassificationDecision.safe_validate(
            {
                "classification": "sensitive_unknown",
                "reason_code": synthetic_marker,
            }
        )
    error = captured.value
    serialized_error = (str(error), repr(error), repr(vars(error)))
    if _contains_sensitive_marker(serialized_error, (synthetic_marker,)):
        pytest.fail("safe validation error retained synthetic source data", pytrace=False)
    assert not isinstance(error, ValidationError)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_safe_validation_rejects_raw_extra_field_without_retaining_it():
    synthetic_marker = "患者番号ABC123"
    data = {
        "classification": "medical",
        "status": "needs_review",
        "payment_amount": None,
        "candidate_count": 0,
        "reason_code": "no_candidate",
        "category": "医療費",
        "raw_text": synthetic_marker,
    }

    with pytest.raises(SafeModelValidationError) as captured:
        ReceiptPrivacyPreview.safe_validate(data)
    serialized_error = (str(captured.value), repr(captured.value), repr(vars(captured.value)))
    if _contains_sensitive_marker(serialized_error, (synthetic_marker,)):
        pytest.fail("safe validation error retained rejected extra data", pytrace=False)


@pytest.mark.parametrize(
    ("model", "update"),
    [
        (
            build_receipt_privacy_preview("病院 診療 患者負担額 500円"),
            {"classification": "normal"},
        ),
        (_candidate(5000, "strong"), {"amount": -1}),
        (
            classify_receipt_text("レシート 商品 合計 500円"),
            {"reason_code": "arbitrary raw reason"},
        ),
        (
            build_receipt_privacy_preview("病院 診療 患者負担額 500円"),
            {"raw_text": "synthetic raw OCR"},
        ),
    ],
)
def test_model_copy_update_cannot_bypass_safe_invariants(model, update):
    with pytest.raises(SafeModelValidationError):
        model.model_copy(update=update)


def test_exact_model_copy_remains_safe_and_allowed():
    preview = build_receipt_privacy_preview("病院 診療 患者負担額 500円")

    copied = preview.model_copy()

    assert copied == preview
    assert copied.gemini_allowed is False


def test_preview_and_candidate_fields_contain_only_safe_metadata():
    preview = build_receipt_privacy_preview("病院 診療 患者負担額 1,234円")
    candidate = extract_medical_payment_candidates("患者負担額 1,234円")[0]

    assert set(preview.model_dump()) == {
        "classification",
        "status",
        "payment_amount",
        "candidate_count",
        "reason_code",
        "category",
        "gemini_allowed",
    }
    assert set(candidate.model_dump()) == {
        "amount",
        "label_type",
        "strength",
        "rank",
        "source_line_index",
    }
