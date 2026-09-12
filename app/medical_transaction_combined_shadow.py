"""Shadow-only combination of Medical amount and issuer facility evidence.

Amount and facility provenance remain separate.  A successful combination may
reach only the input boundary of the existing authority evaluator; it grants no
production or write authority and creates no write plan.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .medical_image_ai_admission_shadow import (
    AdmissionShadowValidationError,
    ImageAiAdmissionPolicy,
    ImageAiAdmissionSignals,
    evaluate_image_ai_admission,
)
from .medical_image_ai_result_shadow import (
    ImageAiCropBinding,
    ImageAiProvenance,
    ImageAiShadowResult,
    ImageAiShadowValidationError,
)
from .medical_issuer_selector_shadow import (
    FacilityCandidate,
    FacilityType,
    IssuerBinding,
    IssuerSelectionShadowResult,
    OcrPageForIssuerSelection,
    SELECTOR_VERSION,
    select_medical_issuer_shadow,
)


COMBINED_SCHEMA_VERSION = "medical-transaction-combined-shadow-v1"
COMBINER_VERSION = "medical-transaction-combiner-shadow-v1"
_HEX = re.compile(r"[0-9a-f]{64}\Z")

CombinedVerdict = Literal[
    "READY_FOR_EXISTING_AUTHORITY_EVALUATION", "REQUIRES_HUMAN_REVIEW"
]
ReviewReason = Literal[
    "ready",
    "amount_not_admitted",
    "amount_validation_failure",
    "facility_missing",
    "facility_not_selected",
    "facility_result_mismatch",
    "facility_evidence_mismatch",
    "document_binding_mismatch",
    "duplicate_replay",
    "conflicting_replay",
]


class CombinedShadowValidationError(ValueError):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("medical combined shadow validation failed")


class _SafeModel(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True, strict=True
    )

    @classmethod
    def safe_validate(cls, value: object) -> Self:
        try:
            return cls.model_validate(value)
        except ValidationError:
            value = None
            raise CombinedShadowValidationError() from None

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        if update is not None:
            raise CombinedShadowValidationError()
        return super().model_copy(deep=deep)


class MedicalDocumentBinding(_SafeModel):
    source_sha256: str
    source_image_sha256: str
    unit: int = Field(ge=1, le=10_000)
    page: int = Field(ge=1, le=1_000)

    @model_validator(mode="after")
    def validate_hashes(self) -> Self:
        if not _HEX.fullmatch(self.source_sha256) or not _HEX.fullmatch(
            self.source_image_sha256
        ):
            raise ValueError("invalid document binding")
        return self


class CombinedAmountEvidence(_SafeModel):
    amount_yen: int = Field(ge=1, le=999_999_999)
    amount_origin: Literal["IMAGE_AI_CANDIDATE"] = "IMAGE_AI_CANDIDATE"
    image_ai_result_id: str
    image_ai_provenance: ImageAiProvenance
    image_ai_crop_binding: ImageAiCropBinding
    admission_status: Literal["AUTO_ADMITTED_FOR_AUTHORITY_EVALUATION"]
    admission_reason: str

    @model_validator(mode="after")
    def validate_result_id(self) -> Self:
        if not _HEX.fullmatch(self.image_ai_result_id):
            raise ValueError("invalid amount result id")
        return self


class CombinedFacilityEvidence(_SafeModel):
    facility_name: str = Field(min_length=1, max_length=512)
    facility_origin: Literal["LOCAL_OCR_ISSUER_SELECTOR"] = (
        "LOCAL_OCR_ISSUER_SELECTOR"
    )
    facility_region_ordinal: int = Field(ge=0)
    facility_type: FacilityType
    issuer_role: Literal["issuer"] = "issuer"
    issuer_selector_version: Literal["medical-issuer-selector-shadow-v1"] = (
        SELECTOR_VERSION
    )
    facility_ocr_evidence_sha256: str
    referenced_providers: tuple[FacilityCandidate, ...]
    selector_reason: str

    @model_validator(mode="after")
    def validate_evidence_digest(self) -> Self:
        if not _HEX.fullmatch(self.facility_ocr_evidence_sha256):
            raise ValueError("invalid facility evidence digest")
        return self


class MedicalTransactionCombinedShadow(_SafeModel):
    schema_version: Literal["medical-transaction-combined-shadow-v1"] = (
        COMBINED_SCHEMA_VERSION
    )
    binding: MedicalDocumentBinding
    amount: CombinedAmountEvidence | None
    facility: CombinedFacilityEvidence | None
    verdict: CombinedVerdict
    ambiguity: bool
    review_required: bool
    review_reasons: tuple[ReviewReason, ...] = Field(min_length=1)
    authority_ready: bool
    next_boundary: Literal[
        "existing_authority_evaluation", "human_review"
    ]
    combination_id: str
    integrity_tag: str
    combiner_version: Literal["medical-transaction-combiner-shadow-v1"] = (
        COMBINER_VERSION
    )
    production_authority: Literal[False] = False
    write_authority: Literal[False] = False
    write_plan_created: Literal[False] = False
    state_changed: Literal[False] = False

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        ready = self.verdict == "READY_FOR_EXISTING_AUTHORITY_EVALUATION"
        if not _HEX.fullmatch(self.combination_id) or not _HEX.fullmatch(
            self.integrity_tag
        ):
            raise ValueError("invalid combined integrity")
        if ready:
            if (
                self.amount is None
                or self.facility is None
                or self.ambiguity
                or self.review_required
                or self.review_reasons != ("ready",)
                or not self.authority_ready
                or self.next_boundary != "existing_authority_evaluation"
            ):
                raise ValueError("invalid ready state")
        elif (
            not self.ambiguity
            or not self.review_required
            or self.authority_ready
            or self.next_boundary != "human_review"
            or "ready" in self.review_reasons
        ):
            raise ValueError("invalid review state")
        return self


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _hmac(key: bytes, purpose: bytes, payload: bytes) -> str:
    return hmac.new(key, purpose + b"\0" + payload, hashlib.sha256).hexdigest()


def facility_ocr_evidence_sha256(page: OcrPageForIssuerSelection) -> str:
    """Digest the complete bound OCR page used by the deterministic selector."""
    if type(page) is not OcrPageForIssuerSelection:
        raise CombinedShadowValidationError()
    return hashlib.sha256(_canonical(page.model_dump(mode="json"))).hexdigest()


def document_binding_from_amount(binding: ImageAiCropBinding) -> MedicalDocumentBinding:
    return MedicalDocumentBinding(
        source_sha256=binding.source_sha256,
        source_image_sha256=binding.source_image_sha256,
        unit=binding.unit,
        page=binding.page,
    )


def _facility_document_binding(binding: IssuerBinding) -> MedicalDocumentBinding:
    return MedicalDocumentBinding(
        source_sha256=binding.source_sha256,
        source_image_sha256=binding.image_sha256,
        unit=binding.unit,
        page=binding.page,
    )


def _seal(
    *,
    binding: MedicalDocumentBinding,
    amount: CombinedAmountEvidence | None,
    facility: CombinedFacilityEvidence | None,
    reasons: tuple[ReviewReason, ...],
    identity_key: bytes,
    previously_emitted_combination_id: str | None,
) -> MedicalTransactionCombinedShadow:
    base = {
        "schema_version": COMBINED_SCHEMA_VERSION,
        "binding": binding,
        "amount": amount,
        "facility": facility,
        "combiner_version": COMBINER_VERSION,
    }
    base_wire = {
        "schema_version": COMBINED_SCHEMA_VERSION,
        "binding": binding.model_dump(mode="json"),
        "amount": amount.model_dump(mode="json") if amount else None,
        "facility": facility.model_dump(mode="json") if facility else None,
        "combiner_version": COMBINER_VERSION,
    }
    combination_id = _hmac(
        identity_key,
        b"medical-transaction-combination-id-v1",
        _canonical(base_wire),
    )
    if previously_emitted_combination_id is not None:
        replay_reason: ReviewReason = (
            "duplicate_replay"
            if hmac.compare_digest(combination_id, previously_emitted_combination_id)
            else "conflicting_replay"
        )
        reasons = tuple(
            dict.fromkeys(
                (*(() if reasons == ("ready",) else reasons), replay_reason)
            )
        )
    ready = reasons == ("ready",)
    unsigned = {
        **base,
        "verdict": (
            "READY_FOR_EXISTING_AUTHORITY_EVALUATION"
            if ready
            else "REQUIRES_HUMAN_REVIEW"
        ),
        "ambiguity": not ready,
        "review_required": not ready,
        "review_reasons": reasons,
        "authority_ready": ready,
        "next_boundary": (
            "existing_authority_evaluation" if ready else "human_review"
        ),
        "combination_id": combination_id,
        "production_authority": False,
        "write_authority": False,
        "write_plan_created": False,
        "state_changed": False,
    }
    integrity_payload = {
        **unsigned,
        "binding": base_wire["binding"],
        "amount": base_wire["amount"],
        "facility": base_wire["facility"],
    }
    integrity = _hmac(
        identity_key,
        b"medical-transaction-combination-integrity-v1",
        _canonical(integrity_payload),
    )
    return MedicalTransactionCombinedShadow.safe_validate(
        {**unsigned, "integrity_tag": integrity}
    )


def combine_medical_transaction_shadow(
    *,
    amount_result: ImageAiShadowResult,
    amount_signals: ImageAiAdmissionSignals,
    current_amount_binding: ImageAiCropBinding,
    current_amount_provenance: ImageAiProvenance,
    amount_crop_bytes: bytes,
    amount_identity_key: bytes,
    amount_policy: ImageAiAdmissionPolicy,
    facility_page: OcrPageForIssuerSelection,
    facility_result: IssuerSelectionShadowResult | None,
    expected_document_binding: MedicalDocumentBinding,
    expected_facility_evidence_sha256: str,
    combination_identity_key: bytes,
    previously_accepted_amount_result_id: str | None = None,
    previously_emitted_combination_id: str | None = None,
) -> MedicalTransactionCombinedShadow:
    """Revalidate both origins and stop immediately before existing authority."""
    if (
        type(expected_document_binding) is not MedicalDocumentBinding
        or type(facility_page) is not OcrPageForIssuerSelection
        or type(combination_identity_key) is not bytes
        or len(combination_identity_key) < 16
        or type(expected_facility_evidence_sha256) is not str
        or not _HEX.fullmatch(expected_facility_evidence_sha256)
        or (
            previously_emitted_combination_id is not None
            and (
                type(previously_emitted_combination_id) is not str
                or not _HEX.fullmatch(previously_emitted_combination_id)
            )
        )
    ):
        raise CombinedShadowValidationError()

    reasons: list[ReviewReason] = []
    amount_evidence = None
    facility_evidence = None
    try:
        admission = evaluate_image_ai_admission(
            amount_result,
            amount_signals,
            current_binding=current_amount_binding,
            current_provenance=current_amount_provenance,
            crop_bytes=amount_crop_bytes,
            identity_key=amount_identity_key,
            policy=amount_policy,
            previously_accepted_result_id=previously_accepted_amount_result_id,
        )
        amount_document = document_binding_from_amount(amount_result.binding)
        if amount_document != expected_document_binding:
            reasons.append("document_binding_mismatch")
        if admission.verdict != "AUTO_ADMITTED_FOR_AUTHORITY_EVALUATION":
            reasons.append("amount_not_admitted")
        elif (
            admission.amount_yen != amount_result.amount_yen
            or admission.result_id != amount_result.result_id
        ):
            reasons.append("amount_validation_failure")
        else:
            amount_evidence = CombinedAmountEvidence(
                amount_yen=admission.amount_yen,
                image_ai_result_id=amount_result.result_id,
                image_ai_provenance=amount_result.provenance,
                image_ai_crop_binding=amount_result.binding,
                admission_status=admission.verdict,
                admission_reason=admission.admission_reason,
            )
    except (
        AdmissionShadowValidationError,
        ImageAiShadowValidationError,
        AttributeError,
        TypeError,
        ValueError,
    ):
        reasons.append("amount_validation_failure")

    facility_document = _facility_document_binding(facility_page.binding)
    if facility_document != expected_document_binding:
        reasons.append("document_binding_mismatch")
    actual_facility_digest = facility_ocr_evidence_sha256(facility_page)
    if not hmac.compare_digest(
        actual_facility_digest, expected_facility_evidence_sha256
    ):
        reasons.append("facility_evidence_mismatch")
    expected_issuer_binding = IssuerBinding(
        source_sha256=expected_document_binding.source_sha256,
        image_sha256=expected_document_binding.source_image_sha256,
        unit=expected_document_binding.unit,
        page=expected_document_binding.page,
    )
    recomputed = select_medical_issuer_shadow(
        facility_page, expected_binding=expected_issuer_binding
    )
    if facility_result is None:
        reasons.append("facility_missing")
    elif type(facility_result) is not IssuerSelectionShadowResult:
        reasons.append("facility_result_mismatch")
    elif facility_result.model_dump(mode="json") != recomputed.model_dump(mode="json"):
        reasons.append("facility_result_mismatch")
    elif recomputed.verdict != "SELECTED_ISSUER":
        reasons.append("facility_not_selected")
    else:
        facility_evidence = CombinedFacilityEvidence(
            facility_name=recomputed.issuer_facility_name,
            facility_region_ordinal=recomputed.issuer_region_ordinal,
            facility_type=recomputed.issuer_facility_type,
            issuer_selector_version=recomputed.selector_version,
            facility_ocr_evidence_sha256=actual_facility_digest,
            referenced_providers=recomputed.referenced_providers,
            selector_reason=recomputed.selection_reason,
        )

    unique_reasons = tuple(dict.fromkeys(reasons))
    if not unique_reasons:
        unique_reasons = ("ready",)
    return _seal(
        binding=expected_document_binding,
        amount=amount_evidence,
        facility=facility_evidence,
        reasons=unique_reasons,
        identity_key=combination_identity_key,
        previously_emitted_combination_id=previously_emitted_combination_id,
    )
