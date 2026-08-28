"""Broker-layer exceptions."""
from __future__ import annotations


class BrokerError(Exception):
    """Base class for broker-layer failures."""


class InvalidOrderError(BrokerError):
    """Order failed local validation (qty/limit/type/circuit-band/funds)."""


class OrderRejectedError(BrokerError):
    """Broker rejected the order."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnknownOrderError(BrokerError):
    """Referenced order id does not exist for this session."""


class OrderStateError(BrokerError):
    """Operation not legal for the order's current state."""


class BrokerTimeoutError(BrokerError):
    """Placement outcome unknown (simulated timeout / chaos knob)."""
