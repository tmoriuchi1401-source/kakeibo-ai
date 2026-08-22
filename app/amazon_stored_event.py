from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias


AMAZON_EVENT_TYPES = (
    "order",
    "payment",
    "shipment",
    "delivery",
    "cancellation",
    "return",
    "refund",
    "unknown",
)
PARSE_STATUSES = ("parsed", "needs_review", "unusable")
MATCH_STATUSES = (
    "unmatched",
    "matched",
    "order_not_found",
    "missing_order_id",
    "item_unresolved",
)
APPLY_STATUSES = ("pending", "no_action", "review")

AmazonEventType: TypeAlias = Literal[
    "order", "payment", "shipment", "delivery", "cancellation", "return",
    "refund", "unknown",
]
ParseStatus: TypeAlias = Literal["parsed", "needs_review", "unusable"]
MatchStatus: TypeAlias = Literal[
    "unmatched", "matched", "order_not_found", "missing_order_id",
    "item_unresolved",
]
ApplyStatus: TypeAlias = Literal["pending", "no_action", "review"]


@dataclass(frozen=True)
class AmazonStoredEvent:
    """One persisted Amazon Gmail event, independent of parser output."""

    event_id: str
    gmail_message_id: str
    rfc_message_id: str | None
    thread_id: str
    source_hash: str
    event_type: AmazonEventType
    order_id: str | None
    event_date: str | None
    charged_amount: int | None
    order_amount: int | None
    refund_amount: int | None
    shipment_amount: int | None
    gift_card_amount: int | None
    points_amount: int | None
    coupon_amount: int | None
    discount_amount: int | None
    payment_method: str | None
    item_count: int | None
    parse_status: ParseStatus
    match_status: MatchStatus
    apply_status: ApplyStatus
    parser_version: str
    imported_at: str
    last_parsed_at: str

    def to_row(self) -> list[str | int]:
        """Return values in the exact order of HEADERS["Amazonイベント"]."""

        values = (
            self.event_id,
            self.gmail_message_id,
            self.rfc_message_id,
            self.thread_id,
            self.source_hash,
            self.event_type,
            self.order_id,
            self.event_date,
            self.charged_amount,
            self.order_amount,
            self.refund_amount,
            self.shipment_amount,
            self.gift_card_amount,
            self.points_amount,
            self.coupon_amount,
            self.discount_amount,
            self.payment_method,
            self.item_count,
            self.parse_status,
            self.match_status,
            self.apply_status,
            self.parser_version,
            self.imported_at,
            self.last_parsed_at,
        )
        return ["" if value is None else value for value in values]
