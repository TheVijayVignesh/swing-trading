"""Pause/stop semantics (ARCHITECTURE_V1.2 §1, normative).

- pause(): no NEW entries; open positions continue to be managed normally
  (stops/targets/time-stops still fire).
- stop(FLATTEN): cancel working orders, exit all positions at the next
  actionable prices, then terminal STOPPED/FLATTENED.
- stop(HOLD): freeze decisions entirely; positions remain; STOPPED/HELD.
"""
from __future__ import annotations

from sts.brokers.paper import OrderStatus, PaperBroker
from sts.contracts import Bar, ExitReason, Side, TradeIntent, OrderType


def working_orders(broker: PaperBroker, session_id: str) -> dict[str, TradeIntent]:
    """Broker order ids of all WORKING orders for the session."""
    state = broker.export_state(session_id)
    return dict(state.get("open_orders") or {})


def cancel_all_working(broker: PaperBroker, session_id: str) -> int:
    n = 0
    for oid in working_orders(broker, session_id):
        if broker.cancel_order(session_id, oid):
            n += 1
    return n


def flatten_intents(
    broker: PaperBroker,
    session_id: str,
    now_ts,
    *,
    reason: ExitReason = ExitReason.SESSION_FLATTEN,
    limit_frac: float = 0.995,
) -> list[TradeIntent]:
    """Build SELL intents closing every open position (SESSION_FLATTEN)."""
    out: list[TradeIntent] = []
    for pos in broker.get_positions(session_id):
        cid = f"{session_id}:EXIT:{reason.value}:{pos.symbol}:{now_ts.isoformat()}"
        out.append(TradeIntent(
            session_id=session_id,
            ts=now_ts,
            symbol=pos.symbol,
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            qty=pos.qty,
            limit_price=round(pos.last_px * limit_frac, 2),
            correlation_id=cid,
            features_json='{"flatten": true}',
            versions_json="{}",
        ))
    return out


def bar_closes_in_window(bar: Bar, *, start: str = "09:30", end: str = "15:25") -> bool:
    """True when a 5m bar's CLOSE time falls inside the decision window."""
    from datetime import time as _t, timedelta
    close_t = (bar.ts + timedelta(minutes=5)).time()
    h1, m1 = (int(x) for x in start.split(":"))
    h2, m2 = (int(x) for x in end.split(":"))
    return _t(h1, m1) <= close_t <= _t(h2, m2)


__all__ = ["working_orders", "cancel_all_working", "flatten_intents",
           "bar_closes_in_window", "OrderStatus"]
