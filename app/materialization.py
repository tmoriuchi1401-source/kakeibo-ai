"""Pure, preview-only materialization contracts.

This module deliberately has no Sheets, Drive, or domain imports.  A domain
adapter owns the decision that something is safe to materialize; this contract
only preserves that already-decided intent in an immutable form.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Mapping, TypeAlias


JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
OperationKind: TypeAlias = Literal["append_row", "create_sheet", "update_cells"]


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name}_required")
    return value.strip()


def _freeze_json(value: object) -> JsonValue:
    """Return a deeply immutable, JSON-compatible value."""

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("json_number_must_be_finite")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key in sorted(value):
            if not isinstance(key, str):
                raise TypeError("json_mapping_key_must_be_string")
            frozen[key] = _freeze_json(value[key])
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise TypeError("json_compatible_value_required")


def _jsonable(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class MaterializationSource:
    """Domain-owned source identity; content hash remains a separate field."""

    identity_kind: str
    identity_value: str
    provider: str | None = None
    content_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "identity_kind", _required_text(self.identity_kind, "identity_kind"))
        object.__setattr__(self, "identity_value", _required_text(self.identity_value, "identity_value"))
        if self.provider is not None:
            object.__setattr__(self, "provider", _required_text(self.provider, "provider"))
        if self.content_hash is not None:
            object.__setattr__(self, "content_hash", _required_text(self.content_hash, "content_hash"))

    def to_dict(self) -> dict[str, object]:
        return {
            "identity_kind": self.identity_kind,
            "identity_value": self.identity_value,
            "provider": self.provider,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class MaterializationPrecondition:
    """An expected state already decided by a domain preview."""

    kind: str
    expected: JsonValue
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _required_text(self.kind, "precondition_kind"))
        object.__setattr__(self, "expected", _freeze_json(self.expected))
        frozen = _freeze_json(self.metadata)
        if not isinstance(frozen, Mapping):
            raise TypeError("precondition_metadata_mapping_required")
        object.__setattr__(self, "metadata", frozen)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "expected": _jsonable(self.expected),
            "metadata": _jsonable(self.metadata),
        }


@dataclass(frozen=True)
class MaterializationOperation:
    """One non-executable, domain-prepared external reflection intent."""

    operation_id: str
    kind: OperationKind
    target: Mapping[str, JsonValue]
    payload: Mapping[str, JsonValue]
    preconditions: tuple[MaterializationPrecondition, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation_id", _required_text(self.operation_id, "operation_id"))
        if self.kind not in {"append_row", "create_sheet", "update_cells"}:
            raise ValueError("unsupported_operation_kind")
        for name in ("target", "payload"):
            frozen = _freeze_json(getattr(self, name))
            if not isinstance(frozen, Mapping):
                raise TypeError(f"operation_{name}_mapping_required")
            object.__setattr__(self, name, frozen)
        preconditions = tuple(self.preconditions)
        if any(not isinstance(item, MaterializationPrecondition) for item in preconditions):
            raise TypeError("materialization_precondition_required")
        object.__setattr__(self, "preconditions", preconditions)

    def to_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "kind": self.kind,
            "target": _jsonable(self.target),
            "payload": _jsonable(self.payload),
            "preconditions": [item.to_dict() for item in self.preconditions],
        }


@dataclass(frozen=True)
class MaterializationPlan:
    """A deterministic, write-free representation of domain-approved intent."""

    domain: str
    plan_version: str
    source: MaterializationSource
    operations: tuple[MaterializationOperation, ...]
    blocked: bool = False
    blocked_reason: str | None = None
    provenance: Mapping[str, JsonValue] = field(default_factory=dict)
    plan_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "domain", _required_text(self.domain, "domain"))
        object.__setattr__(self, "plan_version", _required_text(self.plan_version, "plan_version"))
        if not isinstance(self.source, MaterializationSource):
            raise TypeError("materialization_source_required")
        operations = tuple(self.operations)
        if any(not isinstance(item, MaterializationOperation) for item in operations):
            raise TypeError("materialization_operation_required")
        operation_ids = [item.operation_id for item in operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("operation_ids_must_be_unique")
        object.__setattr__(self, "operations", operations)
        if not isinstance(self.blocked, bool):
            raise TypeError("blocked_must_be_bool")
        if self.blocked:
            object.__setattr__(self, "blocked_reason", _required_text(self.blocked_reason, "blocked_reason"))
        elif self.blocked_reason is not None:
            raise ValueError("blocked_reason_requires_blocked_plan")
        frozen = _freeze_json(self.provenance)
        if not isinstance(frozen, Mapping):
            raise TypeError("provenance_mapping_required")
        object.__setattr__(self, "provenance", frozen)
        object.__setattr__(self, "plan_id", self._make_plan_id())

    def _identity_payload(self) -> dict[str, object]:
        # Operation order is intentionally retained: a create/header/append
        # sequence is not equivalent to the same operations in another order.
        # Provenance and blocked_reason are diagnostic/explanatory fields.  They
        # are serialized with the plan but must not turn one write intent into a
        # different plan merely because reporting detail changed.
        return {
            "contract_version": "materialization-plan-id-v1",
            "domain": self.domain,
            "plan_version": self.plan_version,
            "source": self.source.to_dict(),
            "operations": [item.to_dict() for item in self.operations],
            "blocked": self.blocked,
        }

    def _make_plan_id(self) -> str:
        digest = hashlib.sha256(_canonical_json(self._identity_payload()).encode("utf-8")).hexdigest()
        return f"MP-{digest[:32]}"

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "domain": self.domain,
            "plan_version": self.plan_version,
            "source": self.source.to_dict(),
            "operations": [item.to_dict() for item in self.operations],
            "blocked": self.blocked,
            "blocked_reason": self.blocked_reason,
            "provenance": _jsonable(self.provenance),
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())
