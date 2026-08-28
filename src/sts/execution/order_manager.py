"""OrderManager — idempotency, replace-chain tracking, resubmit-after-UNKNOWN.

Wraps ANY contracts.Broker (paper today, live adapters later) adding the §14
failure-playbook semantics:

- Idempotency: submissions keyed by intent.correlation_id; a duplicate returns
  the ORIGINAL order id WITHOUT calling the broker again.
- Chaos knob `fail_next_place`: the next place_order raises BrokerTimeoutError
  and records the attempt as UNKNOWN (outcome unknowable). Query-before-retry:
  `resubmit(correlation_id)` is legal ONLY from UNKNOWN state.
- Replace chains: modify_order records old->new links (Alpaca-style replace);
  active_order_id(correlation_id) resolves to the currently-live order.
- Counters for observability: placed / cancelled / rejected / duplicates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sts.brokers.errors import BrokerTimeoutError, OrderRejectedError, OrderStateError
from sts.contracts import Broker, TradeIntent


@dataclass(slots=True)
class _Attempt:
    correlation_id: str
    intent: Optional[TradeIntent]   # None for attempts adopted via modify/cancel only
    state: str = "PLACED"           # PLACED | UNKNOWN | REJECTED
    order_ids: list[str] = field(default_factory=list)


class OrderManager:
    """The ONLY surface the lab should use to trade through a broker."""

    def __init__(self, broker: Broker) -> None:
        self._broker = broker
        self.fail_next_place = False   # chaos test knob (simulated timeout)
        self.counters = {"placed": 0, "cancelled": 0, "rejected": 0, "duplicates": 0}
        self._attempts: dict[str, _Attempt] = {}
        self._order_to_cid: dict[str, str] = {}

    # -------------------------------------------------- placement
    def place_order(self, session_id: str, intent: TradeIntent) -> str:
        cid = intent.correlation_id
        existing = self._attempts.get(cid)
        if existing is not None and existing.state != "REJECTED":
            # Duplicate submission intercepted locally — broker NOT called again.
            self.counters["duplicates"] += 1
            if not existing.order_ids:
                raise OrderStateError("DUPLICATE_OF_UNKNOWN_ATTEMPT")
            return existing.order_ids[-1]

        if self.fail_next_place:
            self.fail_next_place = False
            self._attempts[cid] = _Attempt(correlation_id=cid, intent=intent,
                                           state="UNKNOWN")
            raise BrokerTimeoutError(f"place outcome unknown for {cid}")

        try:
            oid = self._broker.place_order(session_id, intent)
        except OrderRejectedError:
            self.counters["rejected"] += 1
            self._attempts[cid] = _Attempt(correlation_id=cid, intent=intent,
                                           state="REJECTED")
            raise
        self.counters["placed"] += 1
        attempt = _Attempt(correlation_id=cid, intent=intent, state="PLACED",
                           order_ids=[oid])
        self._attempts[cid] = attempt
        self._order_to_cid[oid] = cid
        return oid

    def resubmit(self, session_id: str, correlation_id: str) -> str:
        """Query-before-retry: only an UNKNOWN attempt may be retried."""
        attempt = self._attempts.get(correlation_id)
        if attempt is None or attempt.state != "UNKNOWN":
            raise OrderStateError(
                f"resubmit allowed only from UNKNOWN, attempt is "
                f"{attempt.state if attempt else 'ABSENT'}")
        oid = self._broker.place_order(session_id, attempt.intent)
        attempt.state = "PLACED"
        attempt.order_ids.append(oid)
        self._order_to_cid[oid] = correlation_id
        self.counters["placed"] += 1
        return oid

    def state_of(self, correlation_id: str) -> str:
        attempt = self._attempts.get(correlation_id)
        return attempt.state if attempt else "ABSENT"

    # -------------------------------------------------- lifecycle
    def cancel_order(self, session_id: str, order_id: str) -> bool:
        ok = self._broker.cancel_order(session_id, order_id)
        if ok:
            self.counters["cancelled"] += 1
        return ok

    def modify_order(self, session_id: str, order_id: str, new_limit: float) -> str:
        """Replace semantics: old CANCELLED, NEW id returned and chained."""
        new_id = self._broker.modify_order(session_id, order_id, new_limit)
        cid = self._order_to_cid.get(order_id, order_id)
        attempt = self._attempts.setdefault(cid, _Attempt(correlation_id=cid,
                                                          intent=None))
        attempt.order_ids.append(new_id)
        self._order_to_cid[new_id] = cid
        self.counters["cancelled"] += 1
        self.counters["placed"] += 1
        return new_id

    def active_order_id(self, correlation_id: str) -> str | None:
        attempt = self._attempts.get(correlation_id)
        if attempt is None or not attempt.order_ids:
            return None
        return attempt.order_ids[-1]
