from __future__ import annotations

from typing import Any, Protocol, TypeAlias, runtime_checkable

from ..events import PaymentEvent, PurchaseEvent


RawMessage: TypeAlias = dict[str, Any]


@runtime_checkable
class CommerceConnector(Protocol):
    @property
    def name(self) -> str: ...

    def supports(self, message: RawMessage) -> bool: ...

    def parse(self, message: RawMessage) -> list[PurchaseEvent]: ...


@runtime_checkable
class PaymentConnector(Protocol):
    @property
    def name(self) -> str: ...

    def supports(self, message: RawMessage) -> bool: ...

    def parse(self, message: RawMessage) -> list[PaymentEvent]: ...


Connector: TypeAlias = CommerceConnector | PaymentConnector
