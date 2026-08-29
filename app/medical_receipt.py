from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Literal, Protocol


Classification = Literal["medical", "suspected_medical", "non_medical", "unknown"]

RECEIPT_TERMS = ("領収書", "領収金額", "お支払額", "今回支払額")
CLINICAL_TERMS = (
    "診療報酬", "医療費", "患者", "自己負担額", "自己負担", "一部負担金",
    "調剤", "処方箋", "診療",
)
INSTITUTION_TERMS = ("医療機関", "クリニック", "診療所", "病院", "医院", "薬局")
INSURANCE_TERMS = ("保険点数", "保険負担", "公費負担")

STRONG_AMOUNT_LABELS = (
    "領収金額", "今回支払額", "お支払額", "支払額", "ご負担額", "自己負担額",
)
MEDIUM_AMOUNT_LABELS = ("一部負担金", "請求額", "合計金額")
WEAK_AMOUNT_LABELS = ("合計",)
EXCLUDED_AMOUNT_LABELS = (
    "保険点数", "診療点数", "点数", "総医療費", "10割金額", "保険負担額",
    "公費負担額", "小計", "内訳", "消費税", "税額", "預り金", "お預り",
    "おつり", "釣銭",
)

MAX_HORIZONTAL_GAP = 240.0
MAX_VERTICAL_GAP = 80.0
MAX_BELOW_X_OFFSET = 150.0
ROW_TOLERANCE_FACTOR = 0.7


class PositionedToken(Protocol):
    text: str
    page: int
    x: float
    y: float
    width: float
    height: float
    confidence: float


@dataclass(frozen=True)
class AmountExtraction:
    amount: int | None
    label: str | None
    certainty: Literal["high", "none"]
    evidence: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class MedicalReceiptAnalysis:
    classification: Classification
    amount: int | None
    amount_label: str | None
    certainty: Literal["high", "medium", "low", "none"]
    evidence: tuple[str, ...]
    reason: str


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    return re.sub(r"(?<=\d)[ \t](?=\d)", "", value)


_YEN_AMOUNT = re.compile(r"(?:[¥￥]\s*)?(\d[\d,\s]*\d|\d)\s*円")
_PREFIX_AMOUNT = re.compile(r"[¥￥]\s*(\d[\d,\s]*\d|\d)(?!\s*点)")
_BARE_AMOUNT = re.compile(r"(?<![\d.,])\d[\d,\s]*\d|(?<![\d.,])\d(?![\d.,])")


def _integer(raw: str) -> int | None:
    digits = re.sub(r"[\s,]", "", normalize_text(raw))
    if not digits.isdigit():
        return None
    value = int(digits)
    return value if value > 0 else None


def yen_amount_tokens(value: str, *, label_context: bool = False) -> tuple[int, ...]:
    """Return yen-like values; bare numbers are accepted only beside a payment label."""
    text = normalize_text(value)
    patterns = (_YEN_AMOUNT, _PREFIX_AMOUNT, _BARE_AMOUNT) if label_context else (
        _YEN_AMOUNT, _PREFIX_AMOUNT,
    )
    found: list[int] = []
    occupied: list[tuple[int, int]] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            amount = _integer(match.group(1) if match.lastindex else match.group(0))
            if amount is not None:
                found.append(amount)
                occupied.append(match.span())
    return tuple(found)


def _terms(text: str, vocabulary: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(term for term in vocabulary if term in text)


def classify_medical_text(text: str) -> tuple[Classification, tuple[str, ...], str]:
    normalized = normalize_text(text)
    if not normalized.strip():
        return "unknown", (), "text is empty"
    groups = {
        "receipt": _terms(normalized, RECEIPT_TERMS),
        "clinical": _terms(normalized, CLINICAL_TERMS),
        "institution": _terms(normalized, INSTITUTION_TERMS),
        "insurance": _terms(normalized, INSURANCE_TERMS),
    }
    evidence = tuple(f"{name}:{term}" for name, terms in groups.items() for term in terms)
    medical_group_count = sum(bool(groups[name]) for name in ("clinical", "institution", "insurance"))
    has_money = bool(yen_amount_tokens(normalized)) or any(
        yen_amount_tokens(segment, label_context=True)
        for segment in _label_segments(normalized)
    )
    base_rule = bool(groups["receipt"]) and medical_group_count >= 2 and has_money
    strong_pair = (
        "診療報酬" in normalized
        and any(term in normalized for term in ("一部負担金", "自己負担額"))
        and bool(groups["receipt"])
    )
    pharmacy_rule = (
        "薬局" in normalized and bool(groups["receipt"])
        and sum(term in normalized for term in ("調剤", "処方箋", "一部負担金", "診療報酬")) >= 2
    )
    if base_rule or strong_pair or pharmacy_rule:
        return "medical", evidence, "high-precision medical evidence matched"
    medical_signals = sum(bool(groups[name]) for name in ("clinical", "institution", "insurance"))
    if medical_signals and (groups["receipt"] or medical_signals >= 2):
        return "suspected_medical", evidence, "medical evidence is insufficient for confirmation"
    if groups["receipt"] or has_money:
        return "non_medical", evidence, "no corroborating medical evidence"
    return "unknown", evidence, "insufficient receipt and medical evidence"


def _label_segments(text: str) -> tuple[str, ...]:
    labels = STRONG_AMOUNT_LABELS + MEDIUM_AMOUNT_LABELS
    lines = normalize_text(text).splitlines()
    segments: list[str] = []
    for index, line in enumerate(lines):
        parts = re.split(r"[/／|｜]", line)
        for part in parts:
            if any(label in part for label in labels):
                segments.append(part)
        if any(line.strip() == label for label in labels) and index + 1 < len(lines):
            segments.append(lines[index + 1])
    return tuple(segments)


def _plain_occurrences(text: str) -> list[tuple[str, tuple[int, ...], str]]:
    normalized = normalize_text(text)
    lines = normalized.splitlines()
    occurrences = []
    labels = STRONG_AMOUNT_LABELS + MEDIUM_AMOUNT_LABELS
    for line_index, line in enumerate(lines):
        for part in re.split(r"[/／|｜]", line):
            for label in labels:
                start = part.find(label)
                if start < 0:
                    continue
                tail = part[start + len(label):]
                values = yen_amount_tokens(tail, label_context=True)
                source = part
                if not values and not tail.strip() and line_index + 1 < len(lines):
                    source = lines[line_index + 1]
                    values = yen_amount_tokens(source, label_context=True)
                if any(excluded in source for excluded in EXCLUDED_AMOUNT_LABELS):
                    values = ()
                occurrences.append((label, values, source.strip()))
    return occurrences


def extract_payment_amount(text: str) -> AmountExtraction:
    occurrences = _plain_occurrences(text)
    if not occurrences:
        return AmountExtraction(None, None, "none", reason="no supported payment label")
    if any(len(set(values)) != 1 for _, values, _ in occurrences):
        return AmountExtraction(None, None, "none", reason="a payment label has no unique nearby amount")
    resolved = [(label, values[0], source) for label, values, source in occurrences]
    amounts = {amount for _, amount, _ in resolved}
    if len(amounts) != 1:
        return AmountExtraction(None, None, "none", reason="payment labels resolve to different amounts")
    amount = next(iter(amounts))
    label = min((item[0] for item in resolved), key=(STRONG_AMOUNT_LABELS + MEDIUM_AMOUNT_LABELS).index)
    evidence = tuple(f"{item_label}:{item_amount}" for item_label, item_amount, _ in resolved)
    return AmountExtraction(amount, label, "high", evidence, "all payment labels converge")


def _overlaps_row(left: PositionedToken, right: PositionedToken) -> bool:
    return abs(float(left.y) - float(right.y)) <= max(float(left.height), float(right.height)) * ROW_TOLERANCE_FACTOR


def _paired(label: PositionedToken, number: PositionedToken) -> tuple[str, float] | None:
    if int(label.page) != int(number.page):
        return None
    horizontal_gap = float(number.x) - (float(label.x) + float(label.width))
    if _overlaps_row(label, number) and 0 <= horizontal_gap <= MAX_HORIZONTAL_GAP:
        return "right", horizontal_gap
    vertical_gap = float(number.y) - (float(label.y) + float(label.height))
    if (0 <= vertical_gap <= MAX_VERTICAL_GAP
            and abs(float(number.x) - float(label.x)) <= MAX_BELOW_X_OFFSET):
        return "below", vertical_gap
    return None


def extract_positioned_payment_amount(tokens: Iterable[PositionedToken]) -> AmountExtraction:
    items = tuple(tokens)
    payment_labels = [
        (token, label) for token in items
        for label in STRONG_AMOUNT_LABELS + MEDIUM_AMOUNT_LABELS if label in normalize_text(token.text)
    ]
    if not payment_labels:
        return AmountExtraction(None, None, "none", reason="no positioned payment label")
    exclusion_labels = [token for token in items if any(
        label in normalize_text(token.text) for label in EXCLUDED_AMOUNT_LABELS
    )]
    number_tokens = [(token, yen_amount_tokens(token.text, label_context=True)) for token in items]
    number_tokens = [(token, values[0]) for token, values in number_tokens if len(set(values)) == 1]
    resolved = []
    for label_token, label in payment_labels:
        candidates = []
        for number, amount in number_tokens:
            relation = _paired(label_token, number)
            if relation is None:
                continue
            conflicts = [
                excluded for excluded in exclusion_labels
                if (excluded_relation := _paired(excluded, number)) is not None
                and excluded_relation[0] == relation[0]
            ]
            if conflicts:
                continue
            candidates.append((number, amount, relation))
        unique_amounts = {amount for _, amount, _ in candidates}
        if len(unique_amounts) != 1:
            return AmountExtraction(None, None, "none", reason="positioned payment candidate is ambiguous")
        resolved.append((label, next(iter(unique_amounts))))
    amounts = {amount for _, amount in resolved}
    if len(amounts) != 1:
        return AmountExtraction(None, None, "none", reason="positioned payment labels disagree")
    amount = next(iter(amounts))
    label = min((item[0] for item in resolved), key=(STRONG_AMOUNT_LABELS + MEDIUM_AMOUNT_LABELS).index)
    return AmountExtraction(amount, label, "high", tuple(f"{a}:{b}" for a, b in resolved),
                            "positioned payment labels converge")


def analyze_medical_receipt(
    text: str, positioned_tokens: Iterable[PositionedToken] = (),
) -> MedicalReceiptAnalysis:
    classification, evidence, classification_reason = classify_medical_text(text)
    plain = extract_payment_amount(text)
    positioned_items = tuple(positioned_tokens)
    positioned = extract_positioned_payment_amount(positioned_items) if positioned_items else None
    amount_result = plain
    if positioned is not None:
        if plain.amount is not None and positioned.amount is not None and plain.amount != positioned.amount:
            amount_result = AmountExtraction(None, None, "none", reason="plain and positioned amounts disagree")
        elif positioned.amount is not None:
            amount_result = positioned
        elif plain.amount is None:
            amount_result = positioned
    certainty = "high" if classification == "medical" and amount_result.amount is not None else (
        "medium" if classification == "medical" else "low" if classification == "suspected_medical" else "none"
    )
    reason = f"{classification_reason}; {amount_result.reason}"
    return MedicalReceiptAnalysis(classification, amount_result.amount, amount_result.label,
                                  certainty, evidence + amount_result.evidence, reason)
