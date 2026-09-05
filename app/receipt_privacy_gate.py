"""Local fail-closed receipt gate and mandatory external-AI submission boundary."""

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
    PaymentDiagnosticCode,
    ReasonCode,
    ReceiptPrivacyPreview,
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
            "known_sensitive_source",
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
    """Pipeline decision with no OCR text, filenames, or source identifiers."""

    classification: Classification
    extraction_status: ExtractionStatus
    extraction_method: ExtractionMethod
    text_present: bool
    status: GateStatus
    reason_code: ReasonCode
    medical_payment_amount: int | None = Field(default=None, ge=0)
    medical_candidate_count: int = Field(default=0, ge=0)
    category: Literal["医療費"] | None = None
    diagnostic_codes: tuple[PaymentDiagnosticCode, ...] = Field(default=(), exclude=True, repr=False)

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
                or self.extraction_method not in {"image_ocr", "pdf_ocr", "pdf_text"}
                or not self.text_present
                or self.status != "ready_for_gemini"
                or self.reason_code not in _NON_MEDICAL_REASON_CODES["normal"]
                or self.medical_payment_amount is not None
                or self.medical_candidate_count != 0
                or self.category is not None
                or self.diagnostic_codes
            ):
                raise ValueError("invalid normal gate state")
        elif self.classification == "medical":
            if self.category != "医療費" or self.reason_code not in _MEDICAL_REASON_CODES:
                raise ValueError("invalid medical gate state")
            if self.status == "confirmed" and self.medical_payment_amount is None:
                raise ValueError("confirmed medical gate requires amount")
            if self.status == "confirmed" and self.medical_candidate_count < 1:
                raise ValueError("confirmed medical gate requires candidates")
            if self.status == "confirmed" and self.diagnostic_codes:
                raise ValueError("confirmed medical gate cannot contain unresolved evidence")
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
    *,
    known_source_classification: Classification | None = None,
) -> ReceiptPrivacyGateResult:
    """Classify local document text and return a data-minimised Gemini decision."""

    if known_source_classification not in {None, "normal", "medical", "payroll", "sensitive_unknown"}:
        raise SafeModelValidationError()
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
        observation_complete=extracted.observation_complete and extracted.status == "extracted",
    )

    # Source provenance is a restriction, never an authorization. OCR cannot
    # downgrade an already-sensitive source, and a caller-supplied normal value
    # cannot override the local gate. Keep public result fields compatible.
    if preview.classification == "normal":
        from .medical_receipt_privacy import classify_receipt_text

        token_decision = classify_receipt_text(" ".join(t.text for t in extracted.structured_tokens))
        if known_source_classification not in {None, "normal"}:
            preview = ReceiptPrivacyPreview(
                classification="sensitive_unknown", status="not_applicable",
                reason_code="known_sensitive_source", candidate_count=0,
            )
        elif (not extracted.observation_complete or
              (extracted.structured_tokens and token_decision.reason_code in {
                  "medical_strong_signal", "medical_multiple_signals",
                  "payroll_strong_signal", "payroll_multiple_signals",
                  "conflicting_sensitive_evidence", "sensitive_signal_insufficient",
              })):
            preview = ReceiptPrivacyPreview(
                classification="sensitive_unknown", status="not_applicable",
                reason_code="insufficient_evidence", candidate_count=0,
                diagnostic_codes=("observation_incomplete",),
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
        diagnostic_codes=(preview.diagnostic_codes if extracted.status == "extracted"
                          else ("observation_incomplete",)),
    )


class ReceiptPrivacyBlocked(ValueError):
    """Data-free failure raised before any receipt media leaves this process."""

    def __init__(self) -> None:
        super().__init__("receipt external AI submission blocked by privacy gate")


def require_receipt_ai_permission(
    content: bytes, mime_type: str, *, known_source_classification: Classification | None = None,
) -> None:
    """Mandatory adapter boundary: re-evaluate the exact bytes about to be sent.

    No caller-supplied gate result, allow flag, or stale cached permission is
    accepted. Known-sensitive provenance short-circuits before OCR as well.
    """
    allowed = False
    if known_source_classification in {None, "normal"}:
        try:
            result = evaluate_receipt_privacy(content, mime_type)
            # Treat even our own gate's return as a checked boundary contract.
            # Revalidate fields (including excluded diagnostics) strictly; a
            # model_construct result, truthy string, or stale malformed result
            # must not authorize transmission. No raw document data is present.
            if type(result) is not ReceiptPrivacyGateResult:
                raise SafeModelValidationError()
            result = ReceiptPrivacyGateResult.model_validate(
                {name: getattr(result, name) for name in ReceiptPrivacyGateResult.model_fields},
                strict=True,
            )
            allowed = (result.classification == "normal" and result.gemini_allowed is True
                       and result.status == "ready_for_gemini"
                       and result.extraction_status == "extracted" and result.text_present is True)
        except Exception:
            # Never propagate OCR/parser exception messages or their context.
            pass
    if not allowed:
        raise ReceiptPrivacyBlocked()
