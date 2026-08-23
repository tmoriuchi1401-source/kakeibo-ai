from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias


ORDER_STATUSES = (
    "ordered",
    "partially_shipped",
    "shipped",
    "delivered",
    "cancelled",
)
REFUND_STATUSES = ("none", "partial", "full")
AMAZON_ORDER_SOURCES = ("gmail", "csv", "mixed")

OrderStatus: TypeAlias = Literal[
    "ordered", "partially_shipped", "shipped", "delivered", "cancelled",
]
RefundStatus: TypeAlias = Literal["none", "partial", "full"]
AmazonOrderSource: TypeAlias = Literal["gmail", "csv", "mixed"]


@dataclass(frozen=True)
class AmazonOrderHeader:
    """Current Amazon order state, with one row per Order ID."""

    order_id: str
    order_date: str | None
    order_amount: int | None
    payment_method: str | None
    item_count: int | None
    order_status: OrderStatus
    charged_amount: int | None
    refund_status: RefundStatus
    refund_amount: int | None
    shipment_amount: int | None
    gift_card_amount: int | None
    points_amount: int | None
    discount_amount: int | None
    source: AmazonOrderSource
    last_updated_at: str

    def to_row(self) -> list[str | int]:
        """Return values in the exact order of HEADERS["Amazon注文ヘッダ"]."""

        values = (
            self.order_id,
            self.order_date,
            self.order_amount,
            self.payment_method,
            self.item_count,
            self.order_status,
            self.charged_amount,
            self.refund_status,
            self.refund_amount,
            self.shipment_amount,
            self.gift_card_amount,
            self.points_amount,
            self.discount_amount,
            self.source,
            self.last_updated_at,
        )
        return ["" if value is None else value for value in values]
