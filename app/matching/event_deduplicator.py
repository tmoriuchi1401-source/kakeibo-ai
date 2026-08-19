from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime, timezone
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, Field

from ..events import PaymentEvent, PurchaseEvent


Event: TypeAlias = PurchaseEvent | PaymentEvent
DedupStatus = Literal["new", "duplicate", "revision", "possible_duplicate", "conflict"]


class DedupResult(BaseModel):
    status: DedupStatus
    incoming_event_id: str
    existing_event_id: str | None = None
    candidate_event_ids: list[str] = Field(default_factory=list)
    reason: str
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    changed_fields: list[str] = Field(default_factory=list)


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.isoformat(timespec="seconds")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _canonical_values(event: Event) -> dict[str, Any]:
    common = {
        "source": _text(event.source),
        "event_type": _text(event.event_type),
        "merchant": _text(event.merchant),
        "direction": event.direction,
        "currency": event.currency.upper(),
    }
    if isinstance(event, PurchaseEvent):
        return common | {
            "external_order_id": _text(event.external_order_id),
            "external_item_id": _text(event.external_item_id),
            "occurred_at": _datetime(event.occurred_at or event.ordered_at),
            "list_price": event.list_price,
            "paid_amount": event.paid_amount,
            "order_total": event.order_total,
        }
    return common | {
        "occurred_at": _datetime(event.occurred_at),
        "amount": event.amount,
        "payment_method": _text(event.payment_method),
        "account_type": _text(event.account_type),
        "external_transaction_id": _text(event.external_transaction_id),
    }


def canonical_fingerprint(event: Event) -> str:
    """Return a stable business-content fingerprint without observation metadata."""
    payload = json.dumps(
        _canonical_values(event), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_PURCHASE_REVISION_FIELDS = (
    "merchant", "status", "ordered_at", "occurred_at", "list_price",
    "paid_amount", "order_total", "direction", "quantity", "product_name",
)
_PAYMENT_REVISION_FIELDS = (
    "merchant", "status", "occurred_at", "amount", "direction", "currency",
    "payment_method", "account_type", "order_reference",
)


def _comparable(value: Any) -> Any:
    if isinstance(value, datetime):
        return _datetime(value)
    if isinstance(value, str):
        return _text(value)
    return value


def _changed_fields(incoming: Event, existing: Event) -> list[str]:
    fields = (_PURCHASE_REVISION_FIELDS if isinstance(incoming, PurchaseEvent)
              else _PAYMENT_REVISION_FIELDS)
    return [
        name for name in fields
        if _comparable(getattr(incoming, name)) != _comparable(getattr(existing, name))
    ]


def _mail_index(event: Event) -> str | None:
    value = event.metadata.get("mail_item_index")
    return str(value) if value is not None else None


def _identity(incoming: Event, existing: Event) -> tuple[bool, list[str]]:
    evidence: list[str] = []
    if type(incoming) is not type(existing):
        return False, evidence

    if isinstance(incoming, PurchaseEvent):
        same_business = (
            incoming.source == existing.source
            and incoming.event_type == existing.event_type
            and incoming.external_order_id is not None
            and incoming.external_order_id == existing.external_order_id
            and incoming.external_item_id is not None
            and incoming.external_item_id == existing.external_item_id
        )
        if same_business:
            evidence.append("purchase_business_key")
        same_order_observation = (
            incoming.event_type == existing.event_type
            and incoming.external_order_id is not None
            and incoming.external_order_id == existing.external_order_id
            and incoming.external_item_id == existing.external_item_id
        )
        if same_order_observation and incoming.source_message_id is not None \
                and incoming.source_message_id == existing.source_message_id:
            evidence.append("source_message_id_purchase_key")
        if same_order_observation and incoming.source_provider_id is not None \
                and incoming.source_provider_id == existing.source_provider_id:
            evidence.append("source_provider_id_purchase_key")
    else:
        same_transaction = (
            incoming.external_transaction_id is not None
            and incoming.external_transaction_id == existing.external_transaction_id
        )
        if same_transaction and incoming.source == existing.source:
            evidence.append("payment_business_key")
        if same_transaction and incoming.source_message_id is not None \
                and incoming.source_message_id == existing.source_message_id:
            evidence.append("source_message_id_transaction_key")
        if same_transaction and incoming.source_provider_id is not None \
                and incoming.source_provider_id == existing.source_provider_id:
            evidence.append("source_provider_id_transaction_key")
        same_mail_item = (
            incoming.source_message_id is not None
            and incoming.source_message_id == existing.source_message_id
            and _mail_index(incoming) is not None
            and _mail_index(incoming) == _mail_index(existing)
        )
        if same_mail_item:
            evidence.append("source_message_id_mail_item_key")
    return bool(evidence), evidence


def _explicit_ids_differ(incoming: Event, existing: Event) -> bool:
    if isinstance(incoming, PurchaseEvent) and isinstance(existing, PurchaseEvent):
        if incoming.external_order_id and existing.external_order_id \
                and incoming.external_order_id != existing.external_order_id:
            return True
        return bool(
            incoming.external_order_id == existing.external_order_id
            and incoming.external_item_id and existing.external_item_id
            and incoming.external_item_id != existing.external_item_id
        )
    if isinstance(incoming, PaymentEvent) and isinstance(existing, PaymentEvent):
        return bool(
            incoming.external_transaction_id and existing.external_transaction_id
            and incoming.external_transaction_id != existing.external_transaction_id
        )
    return False


def _weak_similarity(incoming: Event, existing: Event) -> list[str]:
    evidence: list[str] = []
    if _text(incoming.merchant) and _text(incoming.merchant) == _text(existing.merchant):
        evidence.append("merchant")
    incoming_date = _datetime(incoming.occurred_at)
    existing_date = _datetime(existing.occurred_at)
    if isinstance(incoming, PurchaseEvent) and isinstance(existing, PurchaseEvent):
        incoming_date = _datetime(incoming.occurred_at or incoming.ordered_at)
        existing_date = _datetime(existing.occurred_at or existing.ordered_at)
        incoming_amounts = {incoming.list_price, incoming.paid_amount, incoming.order_total} - {None}
        existing_amounts = {existing.list_price, existing.paid_amount, existing.order_total} - {None}
        if incoming.external_order_id and incoming.external_order_id == existing.external_order_id:
            evidence.append("external_order_id")
        if incoming_amounts & existing_amounts:
            evidence.append("amount")
    elif isinstance(incoming, PaymentEvent) and isinstance(existing, PaymentEvent):
        if incoming.amount == existing.amount and incoming.currency == existing.currency:
            evidence.append("amount")
    if incoming_date and incoming_date == existing_date:
        evidence.append("occurred_at")
    return evidence


def _incompatible_event_types(incoming: Event, existing: Event) -> bool:
    return incoming.event_type != existing.event_type


def _material_amount_conflict(incoming: Event, existing: Event) -> bool:
    if not isinstance(incoming, PaymentEvent) or not isinstance(existing, PaymentEvent):
        return False
    difference = abs(incoming.amount - existing.amount)
    return difference >= max(1000, max(incoming.amount, existing.amount) * 0.5)


class EventDeduplicator:
    def compare(self, incoming: Event, existing: Event) -> DedupResult:
        base = {
            "incoming_event_id": incoming.event_id,
            "existing_event_id": existing.event_id,
            "candidate_event_ids": [existing.event_id],
        }
        if type(incoming) is not type(existing):
            return DedupResult(**base, status="new", reason="different event models",
                               confidence=1.0)

        strong, identity_evidence = _identity(incoming, existing)
        if strong:
            changed = _changed_fields(incoming, existing)
            if _incompatible_event_types(incoming, existing):
                if _material_amount_conflict(incoming, existing):
                    return DedupResult(
                        **base, status="conflict",
                        reason="strong identity has incompatible event type and amount",
                        evidence=identity_evidence, confidence=0.99,
                        changed_fields=sorted(set(changed + ["event_type"])),
                    )
                return DedupResult(
                    **base, status="possible_duplicate",
                    reason="strong transaction identifier spans different event types",
                    evidence=identity_evidence, confidence=0.65,
                    changed_fields=sorted(set(changed + ["event_type"])),
                )
            if changed:
                return DedupResult(
                    **base, status="revision", reason="strong identity with updated content",
                    evidence=identity_evidence, confidence=0.98, changed_fields=changed,
                )
            return DedupResult(
                **base, status="duplicate", reason="strong identity and content match",
                evidence=identity_evidence, confidence=1.0,
            )

        if _explicit_ids_differ(incoming, existing):
            return DedupResult(
                **base, status="new", reason="explicit business identifiers differ",
                evidence=["different_explicit_id"], confidence=0.99,
            )

        if canonical_fingerprint(incoming) == canonical_fingerprint(existing):
            return DedupResult(
                **base, status="duplicate", reason="canonical fingerprint matches",
                evidence=["canonical_fingerprint"], confidence=0.9,
            )

        weak = _weak_similarity(incoming, existing)
        if "amount" in weak and ("merchant" in weak or "external_order_id" in weak) \
                and "occurred_at" in weak:
            return DedupResult(
                **base, status="possible_duplicate",
                reason="weak business attributes match without shared strong identity",
                evidence=weak, confidence=0.55,
            )
        return DedupResult(
            **base, status="new", reason="no sufficient shared identity",
            evidence=weak, confidence=0.9,
        )

    def find_best_duplicate(self, incoming: Event, candidates: list[Event]) -> DedupResult:
        if not candidates:
            return DedupResult(
                status="new", incoming_event_id=incoming.event_id,
                reason="no candidates", confidence=1.0,
            )
        results = [self.compare(incoming, candidate) for candidate in candidates]
        rank = {"conflict": 4, "duplicate": 3, "revision": 3, "possible_duplicate": 2, "new": 1}
        best_rank = max(rank[result.status] for result in results)
        best = [result for result in results if rank[result.status] == best_rank]
        if len(best) > 1 and best_rank >= 2:
            return DedupResult(
                status="possible_duplicate", incoming_event_id=incoming.event_id,
                candidate_event_ids=[result.existing_event_id for result in best
                                     if result.existing_event_id],
                reason="multiple candidates have equally strong evidence",
                evidence=sorted({item for result in best for item in result.evidence}),
                confidence=min(result.confidence for result in best),
            )
        return best[0]
