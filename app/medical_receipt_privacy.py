"""Pure, privacy-first receipt classification and medical payment extraction.

The functions here do not perform I/O and deliberately do not retain OCR text.
They are intended to run before any future Gemini submission decision.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    computed_field,
    model_validator,
)


Classification = Literal["normal", "medical", "payroll", "sensitive_unknown"]
PreviewStatus = Literal["confirmed", "needs_review", "not_applicable"]
CandidateStrength = Literal["strong", "weak"]
LabelType = Literal[
    "patient_responsibility",
    "self_pay",
    "receipt_amount",
    "payment_amount",
    "billing_amount",
]
ClassificationReasonCode = Literal[
    "ocr_or_text_extraction_failed",
    "empty_text",
    "conflicting_sensitive_evidence",
    "medical_strong_signal",
    "medical_multiple_signals",
    "payroll_strong_signal",
    "payroll_multiple_signals",
    "sensitive_signal_insufficient",
    "normal_receipt_evidence",
    "insufficient_evidence",
]
PaymentReasonCode = Literal[
    "unique_strong_candidate",
    "duplicate_same_amount",
    "conflicting_candidates",
    "weak_candidate_only",
    "no_candidate",
]
ReasonCode = ClassificationReasonCode | PaymentReasonCode


_CLASSIFICATION_REASONS: dict[Classification, frozenset[str]] = {
    "normal": frozenset({"normal_receipt_evidence"}),
    "medical": frozenset({"medical_strong_signal", "medical_multiple_signals"}),
    "payroll": frozenset({"payroll_strong_signal", "payroll_multiple_signals"}),
    "sensitive_unknown": frozenset(
        {
            "ocr_or_text_extraction_failed",
            "empty_text",
            "conflicting_sensitive_evidence",
            "sensitive_signal_insufficient",
            "insufficient_evidence",
        }
    ),
}
_CONFIRMED_REASONS = frozenset({"unique_strong_candidate", "duplicate_same_amount"})
_REVIEW_REASONS = frozenset(
    {"conflicting_candidates", "weak_candidate_only", "no_candidate"}
)


class SafeModelValidationError(ValueError):
    """Fixed, data-free error exposed by safe model construction APIs."""

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("safe model validation failed")


class _SafeResultModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    @classmethod
    def safe_validate(cls, value: object) -> Self:
        """Validate untrusted data without exposing Pydantic's input-bearing error."""
        try:
            return cls.model_validate(value)
        except ValidationError:
            pass
        # Clear the untrusted input before creating the public exception. Raising
        # outside the except block also avoids retaining the original exception as
        # __context__ or __cause__.
        value = None
        raise SafeModelValidationError()

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """Allow exact copies, but forbid Pydantic's unvalidated update path."""
        if update is not None:
            raise SafeModelValidationError()
        return super().model_copy(deep=deep)

    def copy(
        self,
        *,
        include: object = None,
        exclude: object = None,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """Block the legacy copy API from bypassing model invariants."""
        if include is not None or exclude is not None or update is not None:
            raise SafeModelValidationError()
        return self.model_copy(deep=deep)


class ClassificationDecision(_SafeResultModel):
    """A decision without any source text or matched terms."""

    classification: Classification
    reason_code: ClassificationReasonCode

    @model_validator(mode="after")
    def validate_reason_for_classification(self) -> Self:
        if self.reason_code not in _CLASSIFICATION_REASONS[self.classification]:
            raise ValueError("reason code does not match classification")
        return self


class PaymentAmountCandidate(_SafeResultModel):
    """A non-PII candidate; source text is intentionally not represented."""

    amount: int = Field(ge=0)
    label_type: LabelType
    strength: CandidateStrength
    rank: int = Field(ge=0)
    source_line_index: int = Field(ge=0)


class PaymentAmountResolution(_SafeResultModel):
    status: Literal["confirmed", "needs_review"]
    amount: int | None = Field(default=None, ge=0)
    candidate_count: int = Field(ge=0)
    reason_code: PaymentReasonCode

    @model_validator(mode="after")
    def validate_resolution_state(self) -> Self:
        if self.status == "confirmed":
            if (
                self.amount is None
                or self.candidate_count < 1
                or self.reason_code not in _CONFIRMED_REASONS
            ):
                raise ValueError("invalid confirmed payment resolution")
        elif self.amount is not None or self.reason_code not in _REVIEW_REASONS:
            raise ValueError("invalid review payment resolution")
        return self


class ReceiptPrivacyPreview(_SafeResultModel):
    """Read-only, decision-only preview safe for non-sensitive reporting."""

    classification: Classification
    status: PreviewStatus
    payment_amount: int | None = Field(default=None, ge=0)
    candidate_count: int = Field(ge=0)
    reason_code: ReasonCode
    category: Literal["医療費"] | None = None

    @computed_field(return_type=bool)
    @property
    def gemini_allowed(self) -> bool:
        return gemini_allowed_for(self.classification)

    @model_validator(mode="after")
    def validate_preview_state(self) -> Self:
        if self.classification == "medical":
            if self.category != "医療費" or self.status == "not_applicable":
                raise ValueError("invalid medical preview state")
            if self.reason_code not in _CONFIRMED_REASONS | _REVIEW_REASONS:
                raise ValueError("invalid medical preview reason")
            if self.status == "confirmed" and self.payment_amount is None:
                raise ValueError("confirmed medical preview requires amount")
            if self.status == "needs_review" and self.payment_amount is not None:
                raise ValueError("review medical preview cannot contain amount")
        elif (
            self.status != "not_applicable"
            or self.payment_amount is not None
            or self.candidate_count != 0
            or self.category is not None
            or self.reason_code not in _CLASSIFICATION_REASONS[self.classification]
        ):
            raise ValueError("invalid non-medical preview state")
        return self


_MEDICAL_SIGNALS = (
    "診療",
    "診療費",
    "医療費",
    "患者",
    "患者番号",
    "保険者",
    "保険証",
    "自己負担",
    "患者負担",
    "診療点数",
    "初診",
    "再診",
    "病院",
    "医院",
    "クリニック",
    "薬局",
    "保険医療機関",
)
_PAYROLL_SIGNALS = (
    "給与明細",
    "給与支給明細",
    "給料",
    "基本給",
    "総支給",
    "支給額",
    "控除",
    "厚生年金",
    "健康保険",
    "雇用保険",
    "所得税",
    "住民税",
    "差引支給額",
)
_MEDICAL_STRONG_SIGNALS = ("患者番号", "保険者", "保険証", "診療点数", "保険医療機関")
_PAYROLL_STRONG_SIGNALS = ("給与明細", "給与支給明細", "差引支給額")
_AMBIGUOUS_SENSITIVE_SIGNALS = (
    "給与",
    "給料",
    "保険",
    "健康保険",
    "支給",
    "控除",
    "患者",
    "診療",
    "医療",
    "病院",
    "医院",
    "クリニック",
    "薬局",
)
_NORMAL_RECEIPT_ANCHORS = (
    "レシート",
    "お買い上げ",
    "お買上げ",
    "お買上",
    "ご利用明細",
    "ご購入",
    "購入",
    "商品",
    "品名",
    "店舗",
    "店名",
)
_NORMAL_TRANSACTION_SIGNALS = (
    "小計",
    "合計",
    "税込",
    "消費税",
    "現金",
    "クレジット",
    "カード",
    "電子マネー",
    "PayPay",
    "お預り",
    "お釣り",
)

_LABEL_RULES: tuple[tuple[str, LabelType, CandidateStrength, int], ...] = (
    ("患者支払額", "patient_responsibility", "strong", 0),
    ("患者負担額", "patient_responsibility", "strong", 0),
    ("自己負担額", "self_pay", "strong", 0),
    ("領収金額", "receipt_amount", "strong", 1),
    ("お支払い額", "payment_amount", "strong", 1),
    ("お支払額", "payment_amount", "strong", 1),
    ("ご請求額", "billing_amount", "weak", 2),
)
_SPECIFIC_LABEL_PATTERNS = tuple(
    (
        re.compile(r"\s*".join(re.escape(character) for character in label)),
        label_type,
        strength,
        rank,
    )
    for label, label_type, strength, rank in _LABEL_RULES
)
_GENERIC_PAYMENT_LABEL_RE = re.compile(
    r"(?<![一-龯々ぁ-ゖァ-ヺA-Za-z0-9])支払額(?![一-龯々ぁ-ゖァ-ヺA-Za-z])"
)
_EXCLUDED_AMOUNT_CONTEXT = (
    "医療費総額",
    "医療費合計",
    "保険者請求額",
    "保険請求額",
    "保険負担額",
    "保険者支払額",
    "保険支払額",
    "公費支払額",
    "診療点数",
    "合計点数",
    "点数",
    "預り金",
    "お預り",
    "お釣り",
    "釣銭",
    "公費",
    "保険分",
)
_EXCLUDED_COMPOUND_PAYMENT_RE = re.compile(
    r"(?:保険|公費)[一-龯々ぁ-ゖァ-ヺ]*支払額"
)
_CURRENCY_TOKEN_RE = re.compile(
    r"(?P<prefix_sign>[-−]?)\s*¥\s*(?P<prefix_number>[0-9][0-9,\s]*)"
    r"|(?P<suffix_sign>[-−]?)\s*(?P<suffix_number>[0-9][0-9,\s]*)\s*円"
)
_PLAIN_NUMBER_RE = re.compile(r"[0-9]+")
_GROUPED_NUMBER_RE = re.compile(r"[0-9]{1,3}(?:,[0-9]{3})+")
_JAPANESE_BOUNDARY_RE = re.compile(r"[一-龯々ぁ-ゖァ-ヺ]")


def _matched_signals(text: str, signals: tuple[str, ...]) -> set[str]:
    return {signal for signal in signals if signal in text}


def classify_receipt_text(text: str | None) -> ClassificationDecision:
    """Classify OCR text conservatively; no non-sensitive fallback is allowed."""
    if text is None:
        return ClassificationDecision(
            classification="sensitive_unknown", reason_code="ocr_or_text_extraction_failed"
        )
    normalized = unicodedata.normalize("NFKC", text)
    if not normalized.strip():
        return ClassificationDecision(classification="sensitive_unknown", reason_code="empty_text")

    medical = _matched_signals(normalized, _MEDICAL_SIGNALS)
    payroll = _matched_signals(normalized, _PAYROLL_SIGNALS)
    medical_confirmed = bool(medical.intersection(_MEDICAL_STRONG_SIGNALS)) or len(medical) >= 2
    payroll_confirmed = bool(payroll.intersection(_PAYROLL_STRONG_SIGNALS)) or len(payroll) >= 2

    if medical_confirmed and payroll_confirmed:
        return ClassificationDecision(
            classification="sensitive_unknown", reason_code="conflicting_sensitive_evidence"
        )
    if medical_confirmed:
        reason: ClassificationReasonCode = (
            "medical_strong_signal"
            if medical.intersection(_MEDICAL_STRONG_SIGNALS)
            else "medical_multiple_signals"
        )
        return ClassificationDecision(classification="medical", reason_code=reason)
    if payroll_confirmed:
        reason = (
            "payroll_strong_signal"
            if payroll.intersection(_PAYROLL_STRONG_SIGNALS)
            else "payroll_multiple_signals"
        )
        return ClassificationDecision(classification="payroll", reason_code=reason)
    ambiguous_sensitive = _matched_signals(normalized, _AMBIGUOUS_SENSITIVE_SIGNALS)
    if medical or payroll or ambiguous_sensitive:
        return ClassificationDecision(
            classification="sensitive_unknown", reason_code="sensitive_signal_insufficient"
        )

    anchors = _matched_signals(normalized, _NORMAL_RECEIPT_ANCHORS)
    transactions = _matched_signals(normalized, _NORMAL_TRANSACTION_SIGNALS)
    if anchors and transactions:
        return ClassificationDecision(classification="normal", reason_code="normal_receipt_evidence")
    return ClassificationDecision(
        classification="sensitive_unknown", reason_code="insufficient_evidence"
    )


def gemini_allowed_for(classification: str) -> bool:
    """Allow Gemini only for the one explicitly safe classification."""
    return classification == "normal"


def _parse_number_token(number: str, sign: str) -> int | None:
    if sign:
        return None
    token = number.strip()
    if any(character.isspace() for character in token):
        return None
    if not (_PLAIN_NUMBER_RE.fullmatch(token) or _GROUPED_NUMBER_RE.fullmatch(token)):
        return None
    try:
        return int(token.replace(",", ""))
    except ValueError:
        return None


def _is_safe_number_start(line: str, start: int) -> bool:
    if start == 0:
        return True
    previous = line[start - 1]
    return (
        previous.isspace()
        or previous == "¥"
        or previous in ":：=([{"
        or bool(_JAPANESE_BOUNDARY_RE.fullmatch(previous))
    )


def _is_safe_number_end(line: str, end: int) -> bool:
    if end == len(line):
        return True
    following = line[end]
    return (
        following.isspace()
        or following == "円"
        or following in ":：=)]}"
        or bool(_JAPANESE_BOUNDARY_RE.fullmatch(following))
    )


def _amounts_on_line(line: str) -> list[int] | None:
    amounts: list[int] = []
    for match in _CURRENCY_TOKEN_RE.finditer(line):
        if match.group("prefix_number") is not None:
            number_group = "prefix_number"
            amount = _parse_number_token(
                match.group("prefix_number"), match.group("prefix_sign")
            )
        else:
            number_group = "suffix_number"
            amount = _parse_number_token(
                match.group("suffix_number"), match.group("suffix_sign")
            )
        number_start, number_end = match.span(number_group)
        if (
            amount is None
            or not _is_safe_number_start(line, number_start)
            or not _is_safe_number_end(line, number_end)
        ):
            return None
        amounts.append(amount)
    return amounts


def _payment_labels_on_line(
    line: str,
) -> list[tuple[LabelType, CandidateStrength, int]]:
    specific_matches: list[tuple[int, int, LabelType, CandidateStrength, int]] = []
    for pattern, label_type, strength, rank in _SPECIFIC_LABEL_PATTERNS:
        specific_matches.extend(
            (match.start(), match.end(), label_type, strength, rank)
            for match in pattern.finditer(line)
        )
    matches = [
        (label_type, strength, rank)
        for _, _, label_type, strength, rank in specific_matches
    ]
    for generic_match in _GENERIC_PAYMENT_LABEL_RE.finditer(line):
        overlaps_specific = any(
            generic_match.start() < specific_end and generic_match.end() > specific_start
            for specific_start, specific_end, _, _, _ in specific_matches
        )
        if not overlaps_specific:
            matches.append(("payment_amount", "strong", 1))
    return matches


def extract_medical_payment_candidates(text: str) -> list[PaymentAmountCandidate]:
    """Return only unambiguous same-line, labelled payment candidates."""
    candidates: list[PaymentAmountCandidate] = []
    for line_index, raw_line in enumerate(text.splitlines()):
        line = unicodedata.normalize("NFKC", raw_line)
        compact_label_line = re.sub(r"\s+", "", line)
        if any(
            excluded in compact_label_line for excluded in _EXCLUDED_AMOUNT_CONTEXT
        ) or _EXCLUDED_COMPOUND_PAYMENT_RE.search(compact_label_line):
            continue
        labels = _payment_labels_on_line(line)
        amounts = _amounts_on_line(line)
        if len(labels) != 1 or amounts is None or len(amounts) != 1:
            continue
        label_type, strength, rank = labels[0]
        candidates.append(
            PaymentAmountCandidate(
                amount=amounts[0],
                label_type=label_type,
                strength=strength,
                rank=rank,
                source_line_index=line_index,
            )
        )
    return candidates


def resolve_medical_payment_candidates(
    candidates: list[PaymentAmountCandidate],
) -> PaymentAmountResolution:
    """Confirm only when every candidate agrees with at least one strong label."""
    if not candidates:
        return PaymentAmountResolution(
            status="needs_review", amount=None, candidate_count=0, reason_code="no_candidate"
        )

    strong_candidates = [candidate for candidate in candidates if candidate.strength == "strong"]
    if not strong_candidates:
        return PaymentAmountResolution(
            status="needs_review",
            amount=None,
            candidate_count=len(candidates),
            reason_code="weak_candidate_only",
        )

    distinct_amounts = {candidate.amount for candidate in candidates}
    if len(distinct_amounts) != 1:
        return PaymentAmountResolution(
            status="needs_review",
            amount=None,
            candidate_count=len(candidates),
            reason_code="conflicting_candidates",
        )

    amount = next(iter(distinct_amounts))
    reason: PaymentReasonCode = (
        "duplicate_same_amount" if len(candidates) > 1 else "unique_strong_candidate"
    )
    return PaymentAmountResolution(
        status="confirmed",
        amount=amount,
        candidate_count=len(candidates),
        reason_code=reason,
    )


def build_receipt_privacy_preview(text: str | None) -> ReceiptPrivacyPreview:
    """Build a decision-only preview without retaining raw OCR data."""
    decision = classify_receipt_text(text)
    if decision.classification != "medical":
        return ReceiptPrivacyPreview(
            classification=decision.classification,
            status="not_applicable",
            payment_amount=None,
            candidate_count=0,
            reason_code=decision.reason_code,
        )

    resolution = resolve_medical_payment_candidates(extract_medical_payment_candidates(text or ""))
    return ReceiptPrivacyPreview(
        classification="medical",
        status=resolution.status,
        payment_amount=resolution.amount,
        candidate_count=resolution.candidate_count,
        reason_code=resolution.reason_code,
        category="医療費",
    )
