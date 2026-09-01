"""Pure local gate for deciding whether a receipt may reach Gemini in a future phase."""

from __future__ import annotations

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

from .medical_receipt_privacy import (
    Classification,
    ReasonCode,
    SafeModelValidationError,
    build_receipt_privacy_preview,
    gemini_allowed_for,
)
from .receipt_text_extraction import (
    ExtractionMethod,
    ExtractionStatus,
    _extract_receipt_text,
)


GateStatus = Literal["ready_for_gemini", "blocked", "confirmed", "needs_review"]

_MEDICAL_REASON_CODES = frozenset(
    {
        "unique_strong_candidate",
        "duplicate_same_amount",
        "conflicting_candidates",
        "weak_candidate_only",
        "no_candidate",
    }
)
_NON_MEDICAL_REASON_CODES: dict[str, frozenset[str]] = {
    "normal": frozenset({"normal_receipt_evidence"}),
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


class _SafeGateModel(BaseModel):
    """Protect gate results from extra data and unvalidated update copies."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    @classmethod
    def safe_validate(cls, value: object) -> Self:
        try:
            return cls.model_validate(value)
        except ValidationError:
            value = None
        raise SafeModelValidationError()

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
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
        if include is not None or exclude is not None or update is not None:
            raise SafeModelValidationError()
        return self.model_copy(deep=deep)


class ReceiptPrivacyGateResult(_SafeGateModel):
    """Future-pipeline decision with no OCR text, filenames, or source identifiers."""

    classification: Classification
    extraction_status: ExtractionStatus
    extraction_method: ExtractionMethod
    text_present: bool
    status: GateStatus
    reason_code: ReasonCode
    medical_payment_amount: int | None = Field(default=None, ge=0)
    medical_candidate_count: int = Field(default=0, ge=0)
    category: Literal["医療費"] | None = None

    @computed_field(return_type=bool)
    @property
    def gemini_allowed(self) -> bool:
        return gemini_allowed_for(self.classification)

    @model_validator(mode="after")
    def validate_gate_state(self) -> Self:
        if self.extraction_status == "extracted":
            if not self.text_present:
                raise ValueError("extracted result requires text")
        else:
            if self.text_present:
                raise ValueError("failed extraction cannot contain text")
            if self.classification != "sensitive_unknown":
                raise ValueError("failed extraction must remain sensitive unknown")

        if self.classification == "normal":
            if (
                self.extraction_status != "extracted"
                or not self.text_present
                or self.status != "ready_for_gemini"
                or self.reason_code not in _NON_MEDICAL_REASON_CODES["normal"]
                or self.medical_payment_amount is not None
                or self.medical_candidate_count != 0
                or self.category is not None
            ):
                raise ValueError("invalid normal gate state")
        elif self.classification == "medical":
            if self.category != "医療費" or self.reason_code not in _MEDICAL_REASON_CODES:
                raise ValueError("invalid medical gate state")
            if self.status == "confirmed" and self.medical_payment_amount is None:
                raise ValueError("confirmed medical gate requires amount")
            if self.status == "confirmed" and self.medical_candidate_count < 1:
                raise ValueError("confirmed medical gate requires candidates")
            if self.status == "needs_review" and self.medical_payment_amount is not None:
                raise ValueError("review medical gate cannot contain amount")
            if self.status not in {"confirmed", "needs_review"}:
                raise ValueError("invalid medical gate status")
        elif (
            self.status != "blocked"
            or self.reason_code not in _NON_MEDICAL_REASON_CODES[self.classification]
            or self.medical_payment_amount is not None
            or self.medical_candidate_count != 0
            or self.category is not None
        ):
            raise ValueError("invalid blocked gate state")
        return self


def evaluate_receipt_privacy(
    content: bytes | None,
    mime_type: str | None,
) -> ReceiptPrivacyGateResult:
    """Classify local document text and return a data-minimised Gemini decision."""

    extracted = _extract_receipt_text(content, mime_type)
    if extracted.status == "extracted" and not extracted.text_present:
        # Keep the public boundary fail-closed even if a low-level adapter violates
        # its private status/text contract.
        failure_status: ExtractionStatus = (
            "pdf_text_empty"
            if extracted.method == "pdf_text"
            else "ocr_empty"
            if extracted.method in {"image_ocr", "pdf_ocr"}
            else "extraction_failed"
        )
        extracted = type(extracted)(failure_status, extracted.method, None)
    preview = build_receipt_privacy_preview(
        extracted.text if extracted.status == "extracted" else None,
        extracted.structured_tokens if extracted.status == "extracted" else (),
    )

    if preview.classification == "normal":
        status: GateStatus = "ready_for_gemini"
    elif preview.classification == "medical":
        status = preview.status
    else:
        status = "blocked"

    return ReceiptPrivacyGateResult(
        classification=preview.classification,
        extraction_status=extracted.status,
        extraction_method=extracted.method,
        text_present=extracted.text_present,
        status=status,
        reason_code=preview.reason_code,
        medical_payment_amount=preview.payment_amount,
        medical_candidate_count=preview.candidate_count,
        category=preview.category,
    )
