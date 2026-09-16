"""Shadow-only admission policy for validated Medical image-AI candidates.

Admission means only that the candidate may be presented to the existing
authority evaluator.  This module has no production caller and cannot grant
authority, build a write plan, or mutate state.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_validator

from .medical_image_ai_result_shadow import (
    ANSWER_SCHEMA_VERSION,
    ImageAiCropBinding,
    ImageAiProvenance,
    ImageAiShadowResult,
    evaluate_image_ai_for_authority_boundary,
)


ADMISSION_POLICY_SCHEMA_VERSION = "medical-image-ai-admission-policy-v1"
ADMISSION_SIGNALS_SCHEMA_VERSION = "medical-image-ai-admission-signals-v1"
ADMISSION_OUTCOME_SCHEMA_VERSION = "medical-image-ai-admission-outcome-v1"

_HEX = re.compile(r"[0-9a-f]{64}\Z")
_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")
_POSITIVE_CONTEXTS: tuple[tuple[str, str], ...] = (
    ("領収", "receipt"),
    ("入金", "payment"),
    ("支払", "payment"),
    ("請求", "billing"),
    ("receipt", "receipt"),
    ("payment", "payment"),
    ("billing", "billing"),
)
_NEGATIVE_CONTEXTS = (
    "未収",
    "預り",
    "預かり",
    "釣銭",
    "お釣",
    "点数",
    "医療費総額",
    "unpaid",
    "change",
    "deposit",
    "points",
)

AdmissionReason = Literal[
    "all_shadow_admission_conditions_satisfied",
    "not_image_ai_candidate",
    "ambiguous_amount",
    "unreadable",
    "multiple_payment_candidates",
    "missing_payment_context",
    "invalid_crop_binding",
    "invalid_provenance",
    "negative_context",
    "integrity_failure",
    "duplicate_replay",
    "conflicting_replay",
    "human_adjudication_conflict",
    "unknown_manual_conflict_state",
    "invalid_admission_signals",
]
Verdict = Literal[
    "AUTO_ADMITTED_FOR_AUTHORITY_EVALUATION", "REQUIRES_HUMAN_REVIEW"
]
ContextKind = Literal["payment", "receipt", "billing", "none"]


class AdmissionShadowValidationError(ValueError):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("image AI admission validation failed")


class _SafeModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        hide_input_in_errors=True,
        strict=True,
    )

    @classmethod
    def safe_validate(cls, value: object) -> Self:
        try:
            return cls.model_validate(value)
        except ValidationError:
            value = None
            raise AdmissionShadowValidationError() from None

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        if update is not None:
            raise AdmissionShadowValidationError()
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
            raise AdmissionShadowValidationError()
        return self.model_copy(deep=deep)


class ImageAiAdmissionPolicy(_SafeModel):
    schema_version: Literal["medical-image-ai-admission-policy-v1"] = (
        ADMISSION_POLICY_SCHEMA_VERSION
    )
    policy_version: str
    allowed_models: tuple[str, ...] = Field(min_length=1, max_length=8)
    allowed_prompt_sha256s: tuple[str, ...] = Field(min_length=1, max_length=8)
    allowed_approval_refs: tuple[str, ...] = Field(min_length=1, max_length=8)
    allowed_crop_provenances: tuple[str, ...] = Field(min_length=1, max_length=8)
    required_answer_schema_version: Literal["medical-image-ai-answer-v1"] = (
        ANSWER_SCHEMA_VERSION
    )
    use_ai_confidence_threshold: Literal[False] = False
    require_ocr_candidate_match: Literal[False] = False
    grants_production_authority: Literal[False] = False
    grants_write_authority: Literal[False] = False

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        values = (
            self.allowed_models
            + self.allowed_approval_refs
            + self.allowed_crop_provenances
            + (self.policy_version,)
        )
        if (
            not all(_SAFE.fullmatch(value) for value in values)
            or not all(_HEX.fullmatch(value) for value in self.allowed_prompt_sha256s)
            or any(
                len(set(group)) != len(group)
                for group in (
                    self.allowed_models,
                    self.allowed_prompt_sha256s,
                    self.allowed_approval_refs,
                    self.allowed_crop_provenances,
                )
            )
        ):
            raise ValueError("invalid admission policy")
        return self


class ImageAiAdmissionSignals(_SafeModel):
    """Authenticated deterministic facts used only by admission policy."""

    schema_version: Literal["medical-image-ai-admission-signals-v1"] = (
        ADMISSION_SIGNALS_SCHEMA_VERSION
    )
    result_id: str
    candidate_count: StrictInt = Field(ge=0, le=8)
    payment_context: ContextKind
    context_evidence_sha256: str | None
    negative_context_state: Literal["clear", "veto", "unknown"]
    manual_conflict_state: Literal["none_known", "known_conflict", "unknown"]
    signals_id: str
    integrity_tag: str

    @model_validator(mode="after")
    def validate_signals(self) -> Self:
        if not all(
            _HEX.fullmatch(value)
            for value in (self.result_id, self.signals_id, self.integrity_tag)
        ):
            raise ValueError("invalid signal digest")
        if (self.payment_context == "none") != (self.context_evidence_sha256 is None):
            raise ValueError("invalid context signal")
        if self.context_evidence_sha256 is not None and not _HEX.fullmatch(
            self.context_evidence_sha256
        ):
            raise ValueError("invalid context digest")
        return self


class ImageAiAdmissionOutcome(_SafeModel):
    schema_version: Literal["medical-image-ai-admission-outcome-v1"] = (
        ADMISSION_OUTCOME_SCHEMA_VERSION
    )
    verdict: Verdict
    admission_reason: AdmissionReason
    reasons: tuple[AdmissionReason, ...] = Field(min_length=1)
    amount_yen: Annotated[StrictInt, Field(ge=1, le=999_999_999)] | None = None
    result_id: str | None = None
    provenance_status: Literal["valid", "invalid", "unknown"]
    ambiguity_status: Literal["unambiguous", "ambiguous", "unreadable", "unknown"]
    authority_boundary_result: Literal[
        "ready_for_existing_authority_evaluation", "not_presented_to_authority"
    ]
    production_authorized: Literal[False] = False
    write_authorized: Literal[False] = False
    write_plan_created: Literal[False] = False
    state_changed: Literal[False] = False

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        admitted = self.verdict == "AUTO_ADMITTED_FOR_AUTHORITY_EVALUATION"
        if admitted != (
            self.authority_boundary_result
            == "ready_for_existing_authority_evaluation"
        ):
            raise ValueError("invalid authority boundary")
        if admitted:
            if (
                self.admission_reason != "all_shadow_admission_conditions_satisfied"
                or self.reasons != ("all_shadow_admission_conditions_satisfied",)
                or self.amount_yen is None
                or self.result_id is None
                or self.provenance_status != "valid"
                or self.ambiguity_status != "unambiguous"
            ):
                raise ValueError("invalid admitted outcome")
        elif self.admission_reason != self.reasons[0]:
            raise ValueError("primary reason mismatch")
        if self.result_id is not None and not _HEX.fullmatch(self.result_id):
            raise ValueError("invalid result id")
        return self


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def _hmac(key: bytes, purpose: bytes, payload: bytes) -> str:
    return hmac.new(key, purpose + b"\0" + payload, hashlib.sha256).hexdigest()


def _valid_key(key: object) -> bool:
    return type(key) is bytes and len(key) >= 16


def _classify_context(label: str) -> tuple[ContextKind, str]:
    folded = label.casefold()
    negative = "veto" if any(value in folded for value in _NEGATIVE_CONTEXTS) else "clear"
    context: ContextKind = "none"
    for value, kind in _POSITIVE_CONTEXTS:
        if value in folded:
            context = kind  # type: ignore[assignment]
            break
    return context, negative


def _signals_payload(signals: ImageAiAdmissionSignals | Mapping[str, object]) -> dict:
    data = (
        signals.model_dump(mode="json")
        if isinstance(signals, ImageAiAdmissionSignals)
        else dict(signals)
    )
    data.pop("signals_id", None)
    data.pop("integrity_tag", None)
    return data


def build_image_ai_admission_signals(
    *,
    result: ImageAiShadowResult,
    raw_answer: object,
    observed_candidate_amounts: tuple[int, ...],
    manual_conflict_state: Literal["none_known", "known_conflict", "unknown"],
    identity_key: bytes,
) -> ImageAiAdmissionSignals:
    """Bind deterministic response/context facts without using ground truth."""
    if (
        type(result) is not ImageAiShadowResult
        or type(raw_answer) is not dict
        or set(raw_answer) != {"amount_yen", "label_quote", "status", "reason"}
        or type(observed_candidate_amounts) is not tuple
        or not _valid_key(identity_key)
        or manual_conflict_state not in {"none_known", "known_conflict", "unknown"}
    ):
        raise AdmissionShadowValidationError()
    label = raw_answer.get("label_quote")
    if result.status == "readable":
        if (
            raw_answer.get("status") != "readable"
            or raw_answer.get("amount_yen") != result.amount_yen
            or type(label) is not str
            or hashlib.sha256(label.encode("utf-8")).hexdigest()
            != result.label_evidence_sha256
        ):
            raise AdmissionShadowValidationError()
    elif raw_answer.get("status") != result.status:
        raise AdmissionShadowValidationError()
    if (
        len(observed_candidate_amounts) > 8
        or any(type(value) is not int or not 1 <= value <= 999_999_999 for value in observed_candidate_amounts)
        or (
            result.status == "readable"
            and result.amount_yen not in observed_candidate_amounts
        )
    ):
        raise AdmissionShadowValidationError()
    if type(label) is str and label:
        context, negative = _classify_context(label)
        context_digest = (
            hashlib.sha256(label.encode("utf-8")).hexdigest()
            if context != "none"
            else None
        )
    else:
        context, negative, context_digest = "none", "unknown", None
    unsigned = {
        "schema_version": ADMISSION_SIGNALS_SCHEMA_VERSION,
        "result_id": result.result_id,
        "candidate_count": len(observed_candidate_amounts),
        "payment_context": context,
        "context_evidence_sha256": context_digest,
        "negative_context_state": negative,
        "manual_conflict_state": manual_conflict_state,
    }
    payload = _canonical(unsigned)
    signals_id = _hmac(identity_key, b"medical-image-ai-admission-signals-id-v1", payload)
    integrity = _hmac(
        identity_key,
        b"medical-image-ai-admission-signals-integrity-v1",
        signals_id.encode("ascii") + payload,
    )
    return ImageAiAdmissionSignals.safe_validate(
        {**unsigned, "signals_id": signals_id, "integrity_tag": integrity}
    )


def _review(
    *reasons: AdmissionReason,
    amount_yen: int | None = None,
    result_id: str | None = None,
    provenance_status: Literal["valid", "invalid", "unknown"] = "unknown",
    ambiguity_status: Literal[
        "unambiguous", "ambiguous", "unreadable", "unknown"
    ] = "unknown",
) -> ImageAiAdmissionOutcome:
    unique = tuple(dict.fromkeys(reasons))
    return ImageAiAdmissionOutcome(
        verdict="REQUIRES_HUMAN_REVIEW",
        admission_reason=unique[0],
        reasons=unique,
        amount_yen=amount_yen,
        result_id=result_id,
        provenance_status=provenance_status,
        ambiguity_status=ambiguity_status,
        authority_boundary_result="not_presented_to_authority",
    )


def evaluate_image_ai_admission(
    result: ImageAiShadowResult,
    signals: ImageAiAdmissionSignals,
    *,
    current_binding: ImageAiCropBinding,
    current_provenance: ImageAiProvenance,
    crop_bytes: bytes,
    identity_key: bytes,
    policy: ImageAiAdmissionPolicy,
    previously_accepted_result_id: str | None = None,
) -> ImageAiAdmissionOutcome:
    """Apply policy after existing validation, stopping before authority logic."""
    boundary = evaluate_image_ai_for_authority_boundary(
        result,
        current_binding=current_binding,
        current_provenance=current_provenance,
        crop_bytes=crop_bytes,
        identity_key=identity_key,
        previously_accepted_result_id=previously_accepted_result_id,
    )
    boundary_reasons: dict[str, AdmissionReason] = {
        "ambiguous_requires_human_review": "ambiguous_amount",
        "unreadable_requires_human_review": "unreadable",
        "invalid_binding": "invalid_crop_binding",
        "invalid_schema": "integrity_failure",
        "stale_provenance": "invalid_provenance",
        "duplicate_replay": "duplicate_replay",
        "conflicting_replay": "conflicting_replay",
    }
    if boundary.status != "validated_for_authority_evaluation":
        reason = boundary_reasons.get(boundary.status, "integrity_failure")
        if boundary.status == "invalid_binding":
            try:
                binding_matches = result.binding == current_binding
                bytes_match = hmac.compare_digest(
                    hashlib.sha256(crop_bytes).hexdigest(), result.binding.crop_sha256
                )
            except (AttributeError, TypeError):
                binding_matches = bytes_match = False
            reason = (
                "integrity_failure"
                if binding_matches and bytes_match
                else "invalid_crop_binding"
            )
        elif boundary.status == "invalid_schema":
            try:
                provenance_differs = result.provenance != current_provenance
            except AttributeError:
                provenance_differs = True
            reason = "invalid_provenance" if provenance_differs else "integrity_failure"
        ambiguity = (
            "ambiguous"
            if boundary.status == "ambiguous_requires_human_review"
            else "unreadable"
            if boundary.status == "unreadable_requires_human_review"
            else "unknown"
        )
        return _review(
            reason,
            provenance_status=(
                "invalid" if boundary.status == "stale_provenance" else "unknown"
            ),
            ambiguity_status=ambiguity,
        )
    if type(policy) is not ImageAiAdmissionPolicy:
        return _review("invalid_provenance", ambiguity_status="unambiguous")
    try:
        policy = ImageAiAdmissionPolicy.safe_validate(policy.model_dump(mode="python"))
        signals = ImageAiAdmissionSignals.safe_validate(signals.model_dump(mode="python"))
    except (AdmissionShadowValidationError, AttributeError):
        return _review(
            "invalid_admission_signals",
            amount_yen=result.amount_yen,
            result_id=result.result_id,
            provenance_status="valid",
            ambiguity_status="unambiguous",
        )
    payload = _canonical(_signals_payload(signals))
    expected_id = _hmac(
        identity_key, b"medical-image-ai-admission-signals-id-v1", payload
    )
    expected_integrity = _hmac(
        identity_key,
        b"medical-image-ai-admission-signals-integrity-v1",
        expected_id.encode("ascii") + payload,
    )
    if not (
        hmac.compare_digest(signals.result_id, result.result_id)
        and hmac.compare_digest(signals.signals_id, expected_id)
        and hmac.compare_digest(signals.integrity_tag, expected_integrity)
    ):
        return _review(
            "integrity_failure",
            amount_yen=result.amount_yen,
            result_id=result.result_id,
            provenance_status="valid",
            ambiguity_status="unambiguous",
        )
    reasons: list[AdmissionReason] = []
    if result.evidence_type != "IMAGE_AI_CANDIDATE":
        reasons.append("not_image_ai_candidate")
    if result.status != "readable" or result.ambiguity != "unambiguous":
        reasons.append("ambiguous_amount")
    if signals.candidate_count != 1:
        reasons.append("multiple_payment_candidates")
    if (
        signals.payment_context == "none"
        or signals.context_evidence_sha256 != result.label_evidence_sha256
    ):
        reasons.append("missing_payment_context")
    if signals.negative_context_state != "clear":
        reasons.append("negative_context")
    if signals.manual_conflict_state == "known_conflict":
        reasons.append("human_adjudication_conflict")
    elif signals.manual_conflict_state != "none_known":
        reasons.append("unknown_manual_conflict_state")
    provenance_valid = all(
        (
            result.provenance.model in policy.allowed_models,
            result.provenance.prompt_sha256 in policy.allowed_prompt_sha256s,
            result.provenance.approval_ref in policy.allowed_approval_refs,
            result.provenance.answer_schema_version
            == policy.required_answer_schema_version,
            result.binding.crop_provenance in policy.allowed_crop_provenances,
        )
    )
    if not provenance_valid:
        reasons.append("invalid_provenance")
    if reasons:
        return _review(
            *reasons,
            amount_yen=result.amount_yen,
            result_id=result.result_id,
            provenance_status="valid" if provenance_valid else "invalid",
            ambiguity_status="unambiguous",
        )
    reasons: list[AdmissionReason] = []
    allowed_provenance = (
        result.provenance.model in policy.allowed_models
        and result.provenance.prompt_sha256 in policy.allowed_prompt_sha256s
        and result.provenance.approval_ref in policy.allowed_approval_refs
        and result.provenance.answer_schema_version
        == policy.required_answer_schema_version
        and result.binding.crop_provenance in policy.allowed_crop_provenances
    )
    if not allowed_provenance:
        reasons.append("invalid_provenance")
    if (
        result.evidence_type != "IMAGE_AI_CANDIDATE"
        or result.status != "readable"
        or result.ambiguity != "unambiguous"
        or result.amount_yen is None
    ):
        reasons.append("not_image_ai_candidate")
    if signals.candidate_count != 1:
        reasons.append(
            "multiple_payment_candidates"
            if signals.candidate_count > 1
            else "ambiguous_amount"
        )
    if (
        signals.payment_context == "none"
        or signals.context_evidence_sha256 is None
        or not hmac.compare_digest(
            signals.context_evidence_sha256, result.label_evidence_sha256 or ""
        )
    ):
        reasons.append("missing_payment_context")
    if signals.negative_context_state != "clear":
        reasons.append("negative_context")
    if signals.manual_conflict_state == "known_conflict":
        reasons.append("human_adjudication_conflict")
    elif signals.manual_conflict_state != "none_known":
        reasons.append("unknown_manual_conflict_state")
    if reasons:
        return _review(
            *reasons,
            amount_yen=result.amount_yen,
            result_id=result.result_id,
            provenance_status="invalid" if "invalid_provenance" in reasons else "valid",
            ambiguity_status="unambiguous",
        )
    return ImageAiAdmissionOutcome(
        verdict="AUTO_ADMITTED_FOR_AUTHORITY_EVALUATION",
        admission_reason="all_shadow_admission_conditions_satisfied",
        reasons=("all_shadow_admission_conditions_satisfied",),
        amount_yen=result.amount_yen,
        result_id=result.result_id,
        provenance_status="valid",
        ambiguity_status="unambiguous",
        authority_boundary_result="ready_for_existing_authority_evaluation",
    )
