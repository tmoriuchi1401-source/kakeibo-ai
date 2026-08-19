from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


Direction = Literal["debit", "credit"]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class EventBase(BaseModel):
    event_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    connector: str = Field(min_length=1)
    connector_version: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    status: str = Field(min_length=1)

    currency: str = Field(default="JPY", min_length=1)
    direction: Direction

    parent_event_id: str | None = None
    source_message_id: str | None = None
    source_provider_id: str | None = None
    source_hash: str = Field(min_length=1)
    raw_reference: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    created_at: datetime = Field(default_factory=_now_utc)
    updated_at: datetime = Field(default_factory=_now_utc)


class PurchaseEvent(EventBase):
    external_order_id: str | None = None
    external_item_id: str | None = None

    merchant: str | None = None
    ordered_at: datetime | None = None
    occurred_at: datetime | None = None

    product_name: str | None = None
    quantity: float | None = Field(default=None, ge=0)

    list_price: int | None = Field(default=None, ge=0)
    paid_amount: int | None = Field(default=None, ge=0)
    order_total: int | None = Field(default=None, ge=0)

    category: str | None = None
    payment_method: str | None = None


class PaymentEvent(EventBase):
    account_type: str | None = None
    payment_method: str | None = None
    merchant: str | None = None
    occurred_at: datetime | None = None
    amount: int = Field(ge=0)

    external_transaction_id: str | None = None
    order_reference: str | None = None


class MatchResult(BaseModel):
    status: Literal["matched", "unmatched", "ambiguous", "conflict"]
    purchase_event_ids: list[str] = Field(default_factory=list)
    payment_event_ids: list[str] = Field(default_factory=list)
    reason: str = ""
    evidence: list[str] = Field(default_factory=list)
