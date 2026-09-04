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
ResultStatus: TypeAlias = Literal["applied", "skipped", "blocked", "failed"]
OperationResultStatus: TypeAlias = Literal["applied", "skipped", "failed"]


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
    """A deterministic, write-free representation of domain-approved intent.

    ``source`` is optional only for domains whose existing preview plan has no
    source identity, such as a code-master reconciliation plan.  Adapters must
    not invent an identity merely to satisfy this common contract.
    """

    domain: str
    plan_version: str
    source: MaterializationSource | None
    operations: tuple[MaterializationOperation, ...]
    blocked: bool = False
    blocked_reason: str | None = None
    provenance: Mapping[str, JsonValue] = field(default_factory=dict)
    plan_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "domain", _required_text(self.domain, "domain"))
        object.__setattr__(self, "plan_version", _required_text(self.plan_version, "plan_version"))
        if self.source is not None and not isinstance(self.source, MaterializationSource):
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
            "source": self.source.to_dict() if self.source is not None else None,
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
            "source": self.source.to_dict() if self.source is not None else None,
            "operations": [item.to_dict() for item in self.operations],
            "blocked": self.blocked,
            "blocked_reason": self.blocked_reason,
            "provenance": _jsonable(self.provenance),
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())


@dataclass(frozen=True)
class MaterializationOperationResult:
    """Observed outcome for one operation already present in a plan."""

    operation_id: str
    status: OperationResultStatus
    external_write: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation_id", _required_text(self.operation_id, "operation_id"))
        if self.status not in {"applied", "skipped", "failed"}:
            raise ValueError("unsupported_operation_result_status")
        if not isinstance(self.external_write, bool):
            raise TypeError("external_write_must_be_bool")
        if self.reason is not None:
            object.__setattr__(self, "reason", _required_text(self.reason, "reason"))

    def to_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "status": self.status,
            "external_write": self.external_write,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MaterializationResult:
    """Pure, privacy-safe observation of one existing materialization apply."""

    plan_id: str
    status: ResultStatus
    external_write: bool
    action_requested: str
    actions_performed: tuple[str, ...]
    operations: tuple[MaterializationOperationResult, ...]
    reason: str
    observed_before: Mapping[str, JsonValue] | None = None
    observed_after: Mapping[str, JsonValue] | None = None
    occurred_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", _required_text(self.plan_id, "plan_id"))
        if self.status not in {"applied", "skipped", "blocked", "failed"}:
            raise ValueError("unsupported_materialization_result_status")
        if not isinstance(self.external_write, bool):
            raise TypeError("external_write_must_be_bool")
        object.__setattr__(self, "action_requested", _required_text(self.action_requested, "action_requested"))
        actions = tuple(_required_text(action, "performed_action") for action in self.actions_performed)
        object.__setattr__(self, "actions_performed", actions)
        operations = tuple(self.operations)
        if any(not isinstance(item, MaterializationOperationResult) for item in operations):
            raise TypeError("materialization_operation_result_required")
        operation_ids = [item.operation_id for item in operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("operation_result_ids_must_be_unique")
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "reason", _required_text(self.reason, "reason"))
        for name in ("observed_before", "observed_after"):
            value = getattr(self, name)
            if value is None:
                continue
            frozen = _freeze_json(value)
            if not isinstance(frozen, Mapping):
                raise TypeError(f"{name}_mapping_required")
            object.__setattr__(self, name, frozen)
        if self.occurred_at is not None:
            object.__setattr__(self, "occurred_at", _required_text(self.occurred_at, "occurred_at"))

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "status": self.status,
            "external_write": self.external_write,
            "action_requested": self.action_requested,
            "actions_performed": list(self.actions_performed),
            "operations": [item.to_dict() for item in self.operations],
            "reason": self.reason,
            "observed_before": (
                _jsonable(self.observed_before) if self.observed_before is not None else None
            ),
            "observed_after": (
                _jsonable(self.observed_after) if self.observed_after is not None else None
            ),
            "occurred_at": self.occurred_at,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())


@dataclass(frozen=True)
class MaterializationOperationAudit:
    """Privacy-safe join of a planned operation and its optional outcome.

    Operation payload is deliberately excluded.  Targets and preconditions are
    enough to identify the intended boundary without copying row values into a
    common audit record.
    """

    operation_id: str
    kind: OperationKind
    target: Mapping[str, JsonValue]
    preconditions: tuple[MaterializationPrecondition, ...]
    result_status: OperationResultStatus | None = None
    external_write: bool | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation_id", _required_text(self.operation_id, "operation_id"))
        if self.kind not in {"append_row", "create_sheet", "update_cells"}:
            raise ValueError("unsupported_operation_kind")
        target = _freeze_json(self.target)
        if not isinstance(target, Mapping):
            raise TypeError("operation_audit_target_mapping_required")
        object.__setattr__(self, "target", target)
        preconditions = tuple(self.preconditions)
        if any(not isinstance(item, MaterializationPrecondition) for item in preconditions):
            raise TypeError("materialization_precondition_required")
        object.__setattr__(self, "preconditions", preconditions)
        if self.result_status is None:
            if self.external_write is not None or self.reason is not None:
                raise ValueError("operation_audit_result_fields_require_status")
        else:
            if self.result_status not in {"applied", "skipped", "failed"}:
                raise ValueError("unsupported_operation_result_status")
            if not isinstance(self.external_write, bool):
                raise TypeError("operation_audit_external_write_bool_required")
            if self.reason is not None:
                object.__setattr__(self, "reason", _required_text(self.reason, "reason"))

    def to_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "kind": self.kind,
            "target": _jsonable(self.target),
            "preconditions": [item.to_dict() for item in self.preconditions],
            "result_status": self.result_status,
            "external_write": self.external_write,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MaterializationAuditRecord:
    """Immutable fact record built only from an existing plan and result."""

    audit_version: str
    plan_id: str
    domain: str
    plan_version: str
    source: MaterializationSource | None
    plan_blocked: bool
    plan_blocked_reason: str | None
    operations: tuple[MaterializationOperationAudit, ...]
    result_status: ResultStatus
    external_write: bool
    requested_action: str
    performed_actions: tuple[str, ...]
    reason: str
    observed_before: Mapping[str, JsonValue] | None = None
    observed_after: Mapping[str, JsonValue] | None = None
    occurred_at: str | None = None

    def __post_init__(self) -> None:
        for name in ("audit_version", "plan_id", "domain", "plan_version", "requested_action", "reason"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        if self.source is not None and not isinstance(self.source, MaterializationSource):
            raise TypeError("materialization_source_required")
        if not isinstance(self.plan_blocked, bool):
            raise TypeError("plan_blocked_must_be_bool")
        if self.plan_blocked_reason is not None:
            object.__setattr__(
                self, "plan_blocked_reason",
                _required_text(self.plan_blocked_reason, "plan_blocked_reason"),
            )
        operations = tuple(self.operations)
        if any(not isinstance(item, MaterializationOperationAudit) for item in operations):
            raise TypeError("materialization_operation_audit_required")
        object.__setattr__(self, "operations", operations)
        if self.result_status not in {"applied", "skipped", "blocked", "failed"}:
            raise ValueError("unsupported_materialization_result_status")
        if not isinstance(self.external_write, bool):
            raise TypeError("external_write_must_be_bool")
        performed = tuple(_required_text(item, "performed_action") for item in self.performed_actions)
        object.__setattr__(self, "performed_actions", performed)
        for name in ("observed_before", "observed_after"):
            value = getattr(self, name)
            if value is None:
                continue
            frozen = _freeze_json(value)
            if not isinstance(frozen, Mapping):
                raise TypeError(f"{name}_mapping_required")
            object.__setattr__(self, name, frozen)
        if self.occurred_at is not None:
            object.__setattr__(self, "occurred_at", _required_text(self.occurred_at, "occurred_at"))

    def to_dict(self) -> dict[str, object]:
        return {
            "audit_version": self.audit_version,
            "plan_id": self.plan_id,
            "domain": self.domain,
            "plan_version": self.plan_version,
            "source": self.source.to_dict() if self.source is not None else None,
            "plan_blocked": self.plan_blocked,
            "plan_blocked_reason": self.plan_blocked_reason,
            "operations": [item.to_dict() for item in self.operations],
            "result_status": self.result_status,
            "external_write": self.external_write,
            "requested_action": self.requested_action,
            "performed_actions": list(self.performed_actions),
            "reason": self.reason,
            "observed_before": (
                _jsonable(self.observed_before) if self.observed_before is not None else None
            ),
            "observed_after": (
                _jsonable(self.observed_after) if self.observed_after is not None else None
            ),
            "occurred_at": self.occurred_at,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())


def build_materialization_audit_record(
    plan: MaterializationPlan,
    result: MaterializationResult,
) -> MaterializationAuditRecord:
    """Join common contracts without applying, revalidating, or inventing facts."""

    if not isinstance(plan, MaterializationPlan):
        raise TypeError("materialization_plan_required")
    if not isinstance(result, MaterializationResult):
        raise TypeError("materialization_result_required")
    if result.plan_id != plan.plan_id:
        raise ValueError("materialization_audit_plan_id_mismatch")
    planned = {operation.operation_id: operation for operation in plan.operations}
    unknown = [item.operation_id for item in result.operations if item.operation_id not in planned]
    if unknown:
        raise ValueError("materialization_audit_unknown_operation_id")
    outcomes = {item.operation_id: item for item in result.operations}
    operations = tuple(
        MaterializationOperationAudit(
            operation_id=operation.operation_id,
            kind=operation.kind,
            target=operation.target,
            preconditions=operation.preconditions,
            result_status=(outcomes[operation.operation_id].status
                           if operation.operation_id in outcomes else None),
            external_write=(outcomes[operation.operation_id].external_write
                            if operation.operation_id in outcomes else None),
            reason=(outcomes[operation.operation_id].reason
                    if operation.operation_id in outcomes else None),
        )
        for operation in plan.operations
    )
    return MaterializationAuditRecord(
        audit_version="materialization-audit-v1",
        plan_id=plan.plan_id,
        domain=plan.domain,
        plan_version=plan.plan_version,
        source=plan.source,
        plan_blocked=plan.blocked,
        plan_blocked_reason=plan.blocked_reason,
        operations=operations,
        result_status=result.status,
        external_write=result.external_write,
        requested_action=result.action_requested,
        performed_actions=result.actions_performed,
        reason=result.reason,
        observed_before=result.observed_before,
        observed_after=result.observed_after,
        occurred_at=result.occurred_at,
    )
