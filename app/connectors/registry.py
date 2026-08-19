from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..events import PaymentEvent, PurchaseEvent
from .base import Connector, RawMessage


@dataclass(frozen=True)
class DispatchResult:
    status: Literal["processed", "skipped", "ambiguous"]
    connector_name: str | None = None
    events: tuple[PurchaseEvent | PaymentEvent, ...] = ()
    matched_connectors: tuple[str, ...] = ()


class ConnectorRegistry:
    def __init__(self, connectors: list[Connector] | None = None):
        self._connectors = tuple(connectors or [])

    @property
    def connectors(self) -> tuple[Connector, ...]:
        return self._connectors

    def dispatch(self, message: RawMessage) -> DispatchResult:
        matches = [connector for connector in self._connectors if connector.supports(message)]
        names = tuple(connector.name for connector in matches)
        if not matches:
            return DispatchResult(status="skipped")
        if len(matches) > 1:
            return DispatchResult(status="ambiguous", matched_connectors=names)

        connector = matches[0]
        events = tuple(connector.parse(message))
        return DispatchResult(
            status="processed",
            connector_name=connector.name,
            events=events,
            matched_connectors=names,
        )
