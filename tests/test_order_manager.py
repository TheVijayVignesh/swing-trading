"""OrderManager: idempotency, replace chain, resubmit-after-UNKNOWN chaos flow."""
from __future__ import annotations

from datetime import datetime

import pytest

from sts.brokers.errors import BrokerTimeoutError, OrderRejectedError, OrderStateError
from sts.execution.order_manager import OrderManager
from sts.contracts import OrderType, Side, TradeIntent


class FakeBroker:
    """Minimal Broker double: counts calls, tracks order status."""

    def __init__(self) -> None:
        self.place_calls = 0
        self.modify_calls = 0
        self.cancel_calls = 0
        self.statuses: dict[str, str] = {}
        self.reject_next = False

    def place_order(self, session_id: str, intent: TradeIntent) -> str:
        self.place_calls += 1
        if self.reject_next:
            self.reject_next = False
            raise OrderRejectedError("TEST_REJECT")
        oid = f"fake-{self.place_calls}"
        self.statuses[oid] = "WORKING"
        return oid

    def cancel_order(self, session_id: str, order_id: str) -> bool:
        self.cancel_calls += 1
        if self.statuses.get(order_id) == "WORKING":
            self.statuses[order_id] = "CANCELLED"
            return True
        return False

    def modify_order(self, session_id: str, order_id: str, new_limit: float) -> str:
        self.modify_calls += 1
        assert self.statuses.get(order_id) == "WORKING"
        self.statuses[order_id] = "CANCELLED"
        new = f"{order_id}-r{self.modify_calls}"
        self.statuses[new] = "WORKING"
        return new

    # unused protocol surface
    def get_account_state(self, session_id): ...   # type: ignore[empty-body]
    def get_positions(self, session_id): ...       # type: ignore[empty-body]
    def on_bar(self, session_id, bar): ...         # type: ignore[empty-body]
    def capabilities(self): ...                    # type: ignore[empty-body]


def intent(corr: str = "c1") -> TradeIntent:
    return TradeIntent(
        session_id="s1", ts=datetime(2026, 8, 3, 9, 15), symbol="X",
        side=Side.BUY, order_type=OrderType.LIMIT, qty=10, limit_price=100.0,
        correlation_id=corr,
    )


def test_duplicate_suppression_calls_broker_once() -> None:
    fake = FakeBroker()
    om = OrderManager(fake)
    id1 = om.place_order("s1", intent("c1"))
    id2 = om.place_order("s1", intent("c1"))     # duplicate correlation id
    assert id1 == id2
    assert fake.place_calls == 1                 # broker NOT called again
    assert om.counters == {"placed": 1, "cancelled": 0,
                           "rejected": 0, "duplicates": 1}
    assert om.active_order_id("c1") == id1


def test_replace_chain_tracked_and_active_resolved() -> None:
    fake = FakeBroker()
    om = OrderManager(fake)
    first = om.place_order("s1", intent("c1"))
    second = om.modify_order("s1", first, 101.5)
    third = om.modify_order("s1", second, 102.0)
    assert first != second != third
    assert om.active_order_id("c1") == third
    assert fake.statuses[first] == "CANCELLED"
    assert fake.statuses[third] == "WORKING"
    assert om.counters["cancelled"] == 2 and om.counters["placed"] == 3


def test_chaos_timeout_then_resubmit_from_unknown() -> None:
    fake = FakeBroker()
    om = OrderManager(fake)
    om.fail_next_place = True
    with pytest.raises(BrokerTimeoutError):
        om.place_order("s1", intent("c1"))
    assert fake.place_calls == 0                 # simulated timeout before broker
    assert om.state_of("c1") == "UNKNOWN"

    oid = om.resubmit("s1", "c1")                # query-before-retry path
    assert fake.place_calls == 1
    assert om.state_of("c1") == "PLACED"
    assert om.active_order_id("c1") == oid

    with pytest.raises(OrderStateError):         # resubmit-after-KNOWN rejected
        om.resubmit("s1", "c1")


def test_duplicate_after_unknown_blocked_until_resubmit() -> None:
    fake = FakeBroker()
    om = OrderManager(fake)
    om.fail_next_place = True
    with pytest.raises(BrokerTimeoutError):
        om.place_order("s1", intent("c1"))
    with pytest.raises(OrderStateError):         # blind resend forbidden
        om.place_order("s1", intent("c1"))
    assert fake.place_calls == 0


def test_resubmit_after_known_rejected() -> None:
    om = OrderManager(FakeBroker())
    om.place_order("s1", intent("c1"))
    with pytest.raises(OrderStateError):
        om.resubmit("s1", "c1")
    with pytest.raises(OrderStateError):
        om.resubmit("s1", "never-submitted")


def test_rejection_counted() -> None:
    fake = FakeBroker()
    om = OrderManager(fake)
    fake.reject_next = True
    with pytest.raises(OrderRejectedError):
        om.place_order("s1", intent("c1"))
    assert om.counters["rejected"] == 1
    assert om.state_of("c1") == "REJECTED"
