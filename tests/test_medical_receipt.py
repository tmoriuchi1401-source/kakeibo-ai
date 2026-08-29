from dataclasses import dataclass

import pytest

from app.medical_receipt import (
    analyze_medical_receipt,
    classify_medical_text,
    extract_payment_amount,
    extract_positioned_payment_amount,
    yen_amount_tokens,
)


@dataclass(frozen=True)
class Token:
    text: str
    page: int
    x: float
    y: float
    width: float = 50
    height: float = 10
    confidence: float = 100


def classification(text):
    return classify_medical_text(text)[0]


def test_medical_receipt_with_patient_clinical_and_amount():
    result = analyze_medical_receipt("領収書 患者 診療 病院\n領収金額 3,000円")
    assert (result.classification, result.amount) == ("medical", 3000)


def test_strong_medical_pair_is_medical_without_amount():
    assert classification("領収書 診療報酬 一部負担金") == "medical"


@pytest.mark.parametrize("text", ["領収書", "病院", "保険"])
def test_single_keyword_is_not_medical(text):
    assert classification(text) != "medical"


def test_retail_pharmacy_is_not_medical():
    assert classification("薬局 日用品") != "medical"


def test_dispensing_pharmacy_has_safe_exception():
    assert classification("薬局 調剤 処方箋 領収書") == "medical"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("総医療費 10,000円\n自己負担額 3,000円", 3000),
        ("預り金 5,000円 / おつり 2,000円 / 領収金額 3,000円", 3000),
        ("保険点数 1000", None),
        ("総医療費 10,000円\n自己負担額 3,000円", 3000),
        ("領収金額 3,000円\n今回支払額 3,000円", 3000),
        ("領収金額 3,000円\n今回支払額 4,000円", None),
        ("領収金額 3,000円 4,000円", None),
        ("領収金額 ３，０００円", 3000),
        ("領収金額 ￥3,000", 3000),
        ("領収金額 ¥3,000", 3000),
        ("領収金額 3000", 3000),
        ("領収金額 3 0 0 0 円", 3000),
        ("領収金額\n3,000円", 3000),
    ],
)
def test_plain_payment_amount(text, expected):
    assert extract_payment_amount(text).amount == expected


def test_maximum_excluded_amount_is_never_selected():
    result = extract_payment_amount("総医療費 50,000円\n今回支払額 4,000円")
    assert result.amount == 4000


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("3,000円", (3000,)), ("￥3,000", (3000,)), ("¥3000", (3000,)),
        ("３，０００円", (3000,)), ("3 0 0 0 円", (3000,)),
        ("保険点数1000", ()),
    ],
)
def test_yen_token_normalization(value, expected):
    assert yen_amount_tokens(value) == expected


def test_positioned_same_row_right():
    result = extract_positioned_payment_amount((Token("領収金額", 1, 10, 10), Token("3,000円", 1, 100, 10)))
    assert result.amount == 3000


def test_positioned_directly_below():
    result = extract_positioned_payment_amount((Token("領収金額", 1, 10, 10), Token("3,000円", 1, 12, 30)))
    assert result.amount == 3000


def test_positioned_other_page_is_not_used():
    result = extract_positioned_payment_amount((Token("領収金額", 1, 10, 10), Token("3,000円", 2, 100, 10)))
    assert result.amount is None


def test_positioned_exclusion_label_conflict_is_not_used():
    result = extract_positioned_payment_amount((
        Token("領収金額", 1, 10, 10), Token("総医療費", 1, 70, 10),
        Token("10,000円", 1, 140, 10),
    ))
    assert result.amount is None


def test_positioned_multiple_nearby_amounts_are_ambiguous():
    result = extract_positioned_payment_amount((
        Token("領収金額", 1, 10, 10), Token("3,000円", 1, 100, 10),
        Token("4,000円", 1, 180, 10),
    ))
    assert result.amount is None
