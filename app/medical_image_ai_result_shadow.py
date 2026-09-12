"""Fail-closed, shadow-only result contract for Medical image-AI evidence.

The models in this module are deliberately separate from OCR observations and
human review assertions.  A valid result can reach only the input boundary of
the existing authority evaluation.  It cannot authorize or plan a write.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_validator


RESULT_SCHEMA_VERSION = "medical-image-ai-shadow-result-v1"
PROVENANCE_SCHEMA_VERSION = "medical-image-ai-provenance-v1"
ANSWER_SCHEMA_VERSION = "medical-image-ai-answer-v1"

_HEX = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")
_RAW_ANSWER_FIELDS = frozenset({"amount_yen", "label_quote", "status", "reason"})
_ABSTENTION_REASONS = frozenset(
    {"multiple_payment_amounts", "insufficient_visual_evidence", "illegible"}
)

AmountYen = Annotated[StrictInt, Field(ge=1, le=999_999_999)]
ResultStatus = Literal["readable", "ambiguous", "unreadable"]
EvidenceType = Literal["IMAGE_AI_CANDIDATE", "IMAGE_AI_ABSTENTION"]
HandoffStatus = Literal[
    "validated_for_authority_evaluation",
    "ambiguous_requires_human_review",
    "unreadable_requires_human_review",
    "invalid_binding",
    "invalid_schema",
    "stale_provenance",
    "duplicate_replay",
    "conflicting_replay",
]


class ImageAiShadowValidationError(ValueError):
    """Fixed error that never reflects rejected medical data."""

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("image AI shadow validation failed")


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
            raise ImageAiShadowValidationError() from None

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        if update is not None:
            raise ImageAiShadowValidationError()
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
            raise ImageAiShadowValidationError()
        return self.model_copy(deep=deep)


class ImageAiProvenance(_SafeModel):
    schema_version: Literal["medical-image-ai-provenance-v1"] = (
        PROVENANCE_SCHEMA_VERSION
    )
    answer_schema_version: Literal["medical-image-ai-answer-v1"] = (
        ANSWER_SCHEMA_VERSION
    )
    model: str
    prompt_sha256: str
    input_mode: Literal["fresh_codex_exec_image"]
    approval_ref: str

    @model_validator(mode="after")
    def validate_provenance(self) -> Self:
        if (
            not _MODEL.fullmatch(self.model)
            or not _HEX.fullmatch(self.prompt_sha256)
            or not _VERSION.fullmatch(self.approval_ref)
        ):
            raise ValueError("invalid provenance")
        return self


class ImageAiCropBinding(_SafeModel):
    source_sha256: str
    source_image_sha256: str
    unit: StrictInt = Field(ge=1, le=10_000)
    page: StrictInt = Field(ge=1, le=1_000)
    crop_sha256: str
    crop_coordinates_original: tuple[StrictInt, StrictInt, StrictInt, StrictInt]
    rotation_clockwise_degrees: Literal[0, 90, 180, 270]
    crop_provenance: str
    manifest_sha256: str

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if not all(
            _HEX.fullmatch(value)
            for value in (
                self.source_sha256,
                self.source_image_sha256,
                self.crop_sha256,
                self.manifest_sha256,
            )
        ):
            raise ValueError("invalid digest")
        left, top, right, bottom = self.crop_coordinates_original
        if min(left, top) < 0 or right <= left or bottom <= top:
            raise ValueError("invalid crop coordinates")
        if not _VERSION.fullmatch(self.crop_provenance):
            raise ValueError("invalid crop provenance")
        return self


class ImageAiShadowResult(_SafeModel):
    """Authenticated image-AI observation; never OCR or human provenance."""

    schema_version: Literal["medical-image-ai-shadow-result-v1"] = (
        RESULT_SCHEMA_VERSION
    )
    evidence_type: EvidenceType
    status: ResultStatus
    ambiguity: Literal["unambiguous", "ambiguous", "unreadable"]
    amount_yen: AmountYen | None
    label_evidence_sha256: str | None
    abstention_reason: Literal[
        "multiple_payment_amounts", "insufficient_visual_evidence", "illegible"
    ] | None
    binding: ImageAiCropBinding
    provenance: ImageAiProvenance
    result_id: str
    integrity_tag: str
    production_authorized: Literal[False] = False
    write_authorized: Literal[False] = False

    @model_validator(mode="after")
    def validate_semantics(self) -> Self:
        digests = (self.result_id, self.integrity_tag)
        if not all(_HEX.fullmatch(value) for value in digests):
            raise ValueError("invalid result digest")
        readable = self.status == "readable"
        if readable:
            if (
                self.evidence_type != "IMAGE_AI_CANDIDATE"
                or self.ambiguity != "unambiguous"
                or self.amount_yen is None
                or self.label_evidence_sha256 is None
                or not _HEX.fullmatch(self.label_evidence_sha256)
                or self.abstention_reason is not None
            ):
                raise ValueError("invalid readable result")
        elif (
            self.evidence_type != "IMAGE_AI_ABSTENTION"
            or self.ambiguity != self.status
            or self.amount_yen is not None
            or self.label_evidence_sha256 is not None
            or self.abstention_reason not in _ABSTENTION_REASONS
        ):
            raise ValueError("invalid abstention")
        return self


class ImageAiAuthorityBoundary(_SafeModel):
    """Read-only handoff outcome at, but never through, authority evaluation."""

    status: HandoffStatus
    result_id: str | None = None
    receipt_unit_ref: str | None = None
    evidence_type: EvidenceType | None = None
    amount_yen: AmountYen | None = None
    binding: ImageAiCropBinding | None = None
    provenance: ImageAiProvenance | None = None
    next_boundary: Literal[
        "requires_existing_production_authority", "requires_human_review", "none"
    ]
    replay_detected: bool = False
    production_authorized: Literal[False] = False
    write_authorized: Literal[False] = False
    write_plan_created: Literal[False] = False
    state_changed: Literal[False] = False

    @model_validator(mode="after")
    def validate_boundary(self) -> Self:
        accepted = self.status == "validated_for_authority_evaluation"
        needs_human = self.status in {
            "ambiguous_requires_human_review",
            "unreadable_requires_human_review",
        }
        if accepted != (self.next_boundary == "requires_existing_production_authority"):
            raise ValueError("invalid authority boundary")
        if needs_human != (self.next_boundary == "requires_human_review"):
            raise ValueError("invalid review boundary")
        if accepted and (
            self.result_id is None
            or self.receipt_unit_ref is None
            or self.evidence_type != "IMAGE_AI_CANDIDATE"
            or self.amount_yen is None
            or self.binding is None
            or self.provenance is None
        ):
            raise ValueError("accepted boundary requires candidate")
        if not accepted and any(
            value is not None
            for value in (self.amount_yen, self.binding, self.provenance)
        ):
            raise ValueError("rejected boundary carries candidate data")
        if self.result_id is not None and not _HEX.fullmatch(self.result_id):
            raise ValueError("invalid result id")
        if self.receipt_unit_ref is not None and not _HEX.fullmatch(self.receipt_unit_ref):
            raise ValueError("invalid unit ref")
        if self.status in {"duplicate_replay", "conflicting_replay"} and not self.replay_detected:
            raise ValueError("replay status requires marker")
        return self


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def _hmac(key: bytes, purpose: bytes, payload: bytes) -> str:
    return hmac.new(key, purpose + b"\0" + payload, hashlib.sha256).hexdigest()


def _valid_key(identity_key: object) -> bool:
    return type(identity_key) is bytes and len(identity_key) >= 16


def _unsigned_payload(result: ImageAiShadowResult | Mapping[str, object]) -> dict[str, object]:
    if isinstance(result, ImageAiShadowResult):
        data = result.model_dump(mode="python")
    else:
        data = dict(result)
    for name in ("result_id", "integrity_tag", "production_authorized", "write_authorized"):
        data.pop(name, None)
    return data


def build_image_ai_shadow_result(
    *,
    raw_answer: object,
    binding: ImageAiCropBinding,
    provenance: ImageAiProvenance,
    identity_key: bytes,
) -> ImageAiShadowResult:
    """Strictly parse one stored response and seal it to source/crop/provenance."""
    if (
        type(raw_answer) is not dict
        or set(raw_answer) != _RAW_ANSWER_FIELDS
        or type(binding) is not ImageAiCropBinding
        or type(provenance) is not ImageAiProvenance
        or not _valid_key(identity_key)
    ):
        raise ImageAiShadowValidationError()
    status = raw_answer.get("status")
    amount = raw_answer.get("amount_yen")
    label = raw_answer.get("label_quote")
    reason = raw_answer.get("reason")
    if status == "readable":
        if (
            type(amount) is not int
            or not 1 <= amount <= 999_999_999
            or type(label) is not str
            or not 1 <= len(label) <= 128
            or type(reason) is not str
            or reason != ""
        ):
            raise ImageAiShadowValidationError()
        evidence_type: EvidenceType = "IMAGE_AI_CANDIDATE"
        ambiguity: Literal["unambiguous", "ambiguous", "unreadable"] = "unambiguous"
        label_digest = hashlib.sha256(label.encode("utf-8")).hexdigest()
        abstention_reason = None
    elif status in {"ambiguous", "unreadable"}:
        if (
            amount is not None
            or label not in {None, ""}
            or type(reason) is not str
            or reason not in _ABSTENTION_REASONS
        ):
            raise ImageAiShadowValidationError()
        evidence_type = "IMAGE_AI_ABSTENTION"
        ambiguity = status
        label_digest = None
        abstention_reason = reason
    else:
        raise ImageAiShadowValidationError()
    unsigned = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "evidence_type": evidence_type,
        "status": status,
        "ambiguity": ambiguity,
        "amount_yen": amount,
        "label_evidence_sha256": label_digest,
        "abstention_reason": abstention_reason,
        "binding": binding.model_dump(mode="python"),
        "provenance": provenance.model_dump(mode="python"),
    }
    payload = _canonical(unsigned)
    result_id = _hmac(identity_key, b"medical-image-ai-result-id-v1", payload)
    integrity = _hmac(
        identity_key,
        b"medical-image-ai-result-integrity-v1",
        result_id.encode("ascii") + payload,
    )
    return ImageAiShadowResult.safe_validate(
        {
            **unsigned,
            "result_id": result_id,
            "integrity_tag": integrity,
            "production_authorized": False,
            "write_authorized": False,
        }
    )


def _outcome(
    status: HandoffStatus,
    *,
    next_boundary: Literal["requires_human_review", "none"] = "none",
    replay: bool = False,
) -> ImageAiAuthorityBoundary:
    return ImageAiAuthorityBoundary(
        status=status,
        next_boundary=next_boundary,
        replay_detected=replay,
    )


def evaluate_image_ai_for_authority_boundary(
    result: ImageAiShadowResult,
    *,
    current_binding: ImageAiCropBinding,
    current_provenance: ImageAiProvenance,
    crop_bytes: bytes,
    identity_key: bytes,
    previously_accepted_result_id: str | None = None,
) -> ImageAiAuthorityBoundary:
    """Validate stored evidence and stop at the existing authority boundary."""
    if not _valid_key(identity_key):
        return _outcome("invalid_binding")
    if (
        type(result) is not ImageAiShadowResult
        or type(current_binding) is not ImageAiCropBinding
        or type(current_provenance) is not ImageAiProvenance
        or type(crop_bytes) is not bytes
    ):
        return _outcome("invalid_schema")
    try:
        result = ImageAiShadowResult.safe_validate(result.model_dump(mode="python"))
        current_binding = ImageAiCropBinding.safe_validate(
            current_binding.model_dump(mode="python")
        )
        current_provenance = ImageAiProvenance.safe_validate(
            current_provenance.model_dump(mode="python")
        )
    except ImageAiShadowValidationError:
        return _outcome("invalid_schema")
    if result.provenance != current_provenance:
        return _outcome("stale_provenance")
    if result.binding != current_binding:
        return _outcome("invalid_binding")
    if not hmac.compare_digest(hashlib.sha256(crop_bytes).hexdigest(), result.binding.crop_sha256):
        return _outcome("invalid_binding")
    unsigned = _unsigned_payload(result)
    payload = _canonical(unsigned)
    expected_id = _hmac(identity_key, b"medical-image-ai-result-id-v1", payload)
    expected_integrity = _hmac(
        identity_key,
        b"medical-image-ai-result-integrity-v1",
        expected_id.encode("ascii") + payload,
    )
    if not (
        hmac.compare_digest(result.result_id, expected_id)
        and hmac.compare_digest(result.integrity_tag, expected_integrity)
    ):
        return _outcome("invalid_binding")
    if previously_accepted_result_id is not None:
        if type(previously_accepted_result_id) is not str or not _HEX.fullmatch(
            previously_accepted_result_id
        ):
            return _outcome("invalid_binding")
        if hmac.compare_digest(result.result_id, previously_accepted_result_id):
            return _outcome("duplicate_replay", replay=True)
        return _outcome("conflicting_replay", replay=True)
    if result.status == "ambiguous":
        return _outcome(
            "ambiguous_requires_human_review", next_boundary="requires_human_review"
        )
    if result.status == "unreadable":
        return _outcome(
            "unreadable_requires_human_review", next_boundary="requires_human_review"
        )
    source_unit_identity = (
        f"{result.binding.source_sha256}:{result.binding.unit}:{result.binding.page}"
    ).encode("ascii")
    unit_ref = _hmac(identity_key, b"medical-receipt-unit", source_unit_identity)
    return ImageAiAuthorityBoundary(
        status="validated_for_authority_evaluation",
        result_id=result.result_id,
        receipt_unit_ref=unit_ref,
        evidence_type="IMAGE_AI_CANDIDATE",
        amount_yen=result.amount_yen,
        binding=result.binding,
        provenance=result.provenance,
        next_boundary="requires_existing_production_authority",
    )
