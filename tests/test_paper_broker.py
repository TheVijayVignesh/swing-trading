"""PaperBroker integration: entry -> T1 partial -> trail -> final exit.

Cash/equity arithmetic asserted to the paisa; costs per side; snapshot
bookkeeping identity balanced; restore() recovery continues identically.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from sts.brokers.costs import compute_costs, load_cost_schedule, round_paise
from sts.brokers.errors import OrderRejectedError
from sts.brokers.fillmodel import SlippageModel, SpreadModel
from sts.brokers.paper import PaperBroker
from sts.contracts import (
    Bar,
    OrderStatus,
    OrderType,
    PortfolioState,
    Side,
    TradeIntent,
)

SCHED = load_cost_schedule(Path(__file__).parents[1] / "configs" / "costs.yaml")


class FakeSink:
    def __init__(self) -> None:
        self.orders: list = []
        self.fills: list = []
        self.updates: list[PortfolioState] = []

    def on_order(self, order) -> None:
        self.orders.append(order)

    def on_fill(self, fill) -> None:
        self.fills.append(fill)

    def on_update(self, state: PortfolioState) -> None:
        self.updates.append(state)


def make_broker(starting_cash: float = 100_000.0, **kw):
    sink = FakeSink()
    counter = {"n": 0}

    def ids() -> str:
        counter["n"] += 1
        return f"o{counter['n']:04d}"

    tick = {"t": datetime(2026, 8, 3, 9, 15)}

    def clock() -> datetime:
        tick["t"] += timedelta(seconds=1)
        return tick["t"]

    broker = PaperBroker(
        SCHED, SpreadModel(), SlippageModel(), clock, sink,
        starting_cash=starting_cash, id_factory=ids, **kw,
    )
    return broker, sink


def buy_intent(corr: str = "c1", limit: float = 100.0, qty: int = 10,
               stop: float = 95.0, t1: float | None = 102.0,
               t2: float | None = 130.0) -> TradeIntent:
    return TradeIntent(
        session_id="s1", ts=datetime(2026, 8, 3, 9, 15), symbol="X",
        side=Side.BUY, order_type=OrderType.LIMIT, qty=qty, limit_price=limit,
        stop_px=stop, target1_px=t1, target2_px=t2, trail_mult_atr=1.5,
        correlation_id=corr,
    )


def bar(ts: datetime, o: float, h: float, l: float, c: float,
        v: float = 10_000.0) -> Bar:
    return Bar(symbol="X", ts=ts, open=o, high=h, low=l, close=c, volume=v,
               timeframe="5m")


D1A = datetime(2026, 8, 3, 9, 20)
D1B = datetime(2026, 8, 3, 9, 25)
D2 = datetime(2026, 8, 4, 9, 15)
D3 = datetime(2026, 8, 5, 9, 15)


def test_costs_golden_totals_documented() -> None:
    """Schedule c1.0.0: BUY 10@1000 = 1.87; SELL 10@1010 = 23.47 (see costs.py)."""
    buy = compute_costs(Side.BUY, 1000.0, 10, SCHED)
    assert buy == {"brokerage": 0.0, "stt": 0.0, "exchange_txn": 0.30,
                   "sebi": 0.01, "gst": 0.06, "stamp_duty": 1.50,
                   "dp_charge": 0.0, "total": 1.87}
    sell = compute_costs(Side.SELL, 1010.0, 10, SCHED)
    assert sell == {"brokerage": 0.0, "stt": 10.10, "exchange_txn": 0.30,
                    "sebi": 0.01, "gst": 0.06, "stamp_duty": 0.0,
                    "dp_charge": 13.00, "total": 23.47}


def test_full_lifecycle_entry_t1_trail_exit() -> None:
    broker, sink = make_broker(starting_cash=100_000.0)
    oid = broker.place_order("s1", buy_intent())

    # ---- bar 1: strict penetration => entry fill at L + half-spread
    fills = broker.on_bar("s1", bar(D1A, 100.0, 100.6, 99.5, 100.2))
    assert len(fills) == 1 and fills[0].order_id == oid
    assert fills[0].side is Side.BUY
    assert fills[0].px == round_paise(100.0 * 1.0005) == 100.05
    buy_costs = compute_costs(Side.BUY, 100.05, 10, SCHED)["total"]
    st = sink.updates[-1]
    assert st.cash == pytest.approx(100_000 - 1000.50 - buy_costs, abs=1e-9)
    assert len(st.positions) == 1

    # ---- bar 2: T1 partial (half, min 1) at T - half-spread; stop not touched
    fills = broker.on_bar("s1", bar(D1B, 100.2, 102.5, 100.0, 102.2))
    assert len(fills) == 1
    assert fills[0].order_id.startswith("dir:X:TARGET1")
    assert (fills[0].px, fills[0].qty) == (round_paise(102.0 * 0.9995), 5)
    assert fills[0].px == 101.95
    sell1_costs = compute_costs(Side.SELL, 101.95, 5, SCHED)["total"]
    cash_after_t1 = 100_000 - 1000.50 - buy_costs + (101.95 * 5 - sell1_costs)
    assert sink.updates[-1].cash == pytest.approx(cash_after_t1, abs=1e-9)

    # trail arms at bar close: hh_since_T1=102.5; ATR=(100.05-95)/1.5
    # => trail = 102.5 - 1.5*ATR = 97.45 > initial stop 95
    pos = broker.get_positions("s1")[0]
    assert pos.t1_done is True
    exp_trail = 102.5 - (100.05 - 95.0)
    assert pos.trail_px is not None and pos.stop_px == pytest.approx(exp_trail, abs=1e-9)

    # ---- bar 3 (next day): low pierces trail => TRAIL_STOP closes remainder
    fills = broker.on_bar("s1", bar(D2, 103.0, 104.0, 96.0, 97.0))
    assert len(fills) == 1
    assert fills[0].order_id.startswith("dir:X:TRAIL_STOP")
    assert fills[0].qty == 5
    assert fills[0].px <= pos.stop_px                      # never above stop
    assert fills[0].px == round_paise(pos.stop_px * (1 - 0.0005 - 0.0005), down=True)
    sell2_costs = compute_costs(Side.SELL, fills[0].px, 5, SCHED)["total"]
    final_cash = cash_after_t1 + (fills[0].px * 5 - sell2_costs)
    assert sink.updates[-1].cash == pytest.approx(final_cash, abs=1e-9)
    assert broker.get_positions("s1") == []

    # realized = sum of net proceeds minus avg_entry*qty over both sells
    exp_realized = ((101.95 * 5 - sell1_costs) + (fills[0].px * 5 - sell2_costs)
                    - 100.05 * 10)
    assert sink.updates[-1].realized == pytest.approx(exp_realized, abs=1e-9)
    # global balance identity: final cash delta == realized - buy costs
    assert (sink.updates[-1].cash - 100_000) == pytest.approx(
        exp_realized - buy_costs, abs=1e-9)


def test_snapshot_series_bookkeeping_identity_balanced() -> None:
    broker, sink = make_broker()
    broker.place_order("s1", buy_intent())
    for b in (bar(D1A, 100.0, 100.6, 99.5, 100.2),
              bar(D1B, 100.2, 102.5, 100.0, 102.2),
              bar(D2, 103.0, 104.0, 96.0, 97.0)):
        broker.on_bar("s1", b)
    for u in sink.updates:
        assert u.equity == pytest.approx(u.cash + u.invested + u.unrealized,
                                         abs=1e-9)
        assert u.drawdown_pct >= 0.0
        assert u.hwm >= u.equity


# ---------------------------------------------------------------- recovery
def test_restore_continues_identically_on_next_bars() -> None:
    a, _ = make_broker()
    a.place_order("s1", buy_intent())
    a.on_bar("s1", bar(D1A, 100.0, 100.6, 99.5, 100.2))
    a.on_bar("s1", bar(D1B, 100.2, 102.5, 100.0, 102.2))

    snap = a.export_state("s1")
    b, sink_b = make_broker()
    b.restore("s1", snap["cash"], snap["positions"], snap["open_orders"],
              realized=snap["realized"], hwm=snap["hwm"])

    crash_bar = bar(D2, 103.0, 104.0, 96.0, 97.0)
    fa = a.on_bar("s1", crash_bar)
    fb = b.on_bar("s1", crash_bar)
    sa, sb = a.get_account_state("s1"), b.get_account_state("s1")
    assert (sa.cash, sa.realized, sa.equity, sa.invested) == \
           pytest.approx((sb.cash, sb.realized, sb.equity, sb.invested), abs=1e-9)
    assert [f.px for f in fa] == [f.px for f in fb]
    assert sa.positions == [] and sb.positions == []


def test_restore_rebuilds_open_orders_that_later_fill() -> None:
    a, _ = make_broker()
    a.place_order("s1", buy_intent())
    pending = a.place_order("s1", buy_intent(corr="c2", limit=90.0))  # before any bar: no circuit ref yet
    a.on_bar("s1", bar(D1A, 100.0, 100.6, 99.5, 100.2))     # fills entry #1 only
    snap = a.export_state("s1")
    assert list(snap["open_orders"]) == [pending]

    b, _ = make_broker()
    b.restore("s1", snap["cash"], snap["positions"], snap["open_orders"],
              realized=snap["realized"], hwm=snap["hwm"])
    fills = b.on_bar("s1", bar(D2, 91.0, 92.0, 89.9, 90.5))
    # exits BEFORE entries: old position stopped out, then the 90 limit fills
    assert len(fills) == 2
    assert fills[0].order_id.startswith("dir:X:STOP") and fills[0].side is Side.SELL
    assert fills[0].px <= 95.0 and fills[0].qty == 10
    assert fills[1].order_id == pending and fills[1].side is Side.BUY
    assert fills[1].px == round_paise(90.0 * 1.0005) == 90.05
    pos = b.get_positions("s1")
    assert len(pos) == 1 and pos[0].qty == 10 and pos[0].avg_entry == 90.05


# ---------------------------------------------------------------- orders API
def test_modify_is_replace_with_link_and_new_id() -> None:
    broker, sink = make_broker()
    old = broker.place_order("s1", buy_intent())
    new = broker.modify_order("s1", old, 101.0)
    assert new != old
    vo, vn = broker.get_order("s1", old), broker.get_order("s1", new)
    assert vo.status is OrderStatus.CANCELLED and vo.replaced_by == new
    assert vn.status is OrderStatus.WORKING and vn.limit_price == 101.0
    assert broker.cancel_order("s1", new) is True
    assert broker.cancel_order("s1", new) is False            # already cancelled
    with pytest.raises(Exception):
        broker.modify_order("s1", new, 102.0)                 # non-WORKING


def test_limit_only_invariant_and_circuit_band() -> None:
    broker, _ = make_broker()
    intent = buy_intent()
    sl = TradeIntent(session_id=intent.session_id, ts=intent.ts, symbol="X",
                     side=Side.BUY, order_type=OrderType.STOP_LOSS_LIMIT,
                     qty=10, limit_price=100.0, correlation_id="sl")
    with pytest.raises(OrderRejectedError):
        broker.place_order("s1", sl)
    broker.on_bar("s1", bar(D1A, 100.0, 100.6, 99.5, 100.2))  # ref price ~100.2
    far = buy_intent(corr="c3", limit=120.0)                  # > +10% band
    with pytest.raises(OrderRejectedError):
        broker.place_order("s1", far)


def test_time_stop_exits_at_next_open_directive() -> None:
    broker, sink = make_broker(time_stop_days=1)
    broker.place_order("s1", buy_intent(t1=None, t2=None))
    broker.on_bar("s1", bar(D1A, 100.0, 100.6, 99.5, 100.2))   # day 0
    broker.on_bar("s1", bar(D2, 100.2, 101.0, 99.8, 100.5))    # held_days -> 1, armed
    fills = broker.on_bar("s1", bar(D3, 99.0, 99.4, 98.0, 98.5))
    assert len(fills) == 1
    assert fills[0].order_id.startswith("dir:X:TIME_STOP")
    expected_px = round_paise(99.0 * (1 - 0.0005 - 0.0005), down=True)
    assert fills[0].px == expected_px
    assert fills[0].qty == 10
    assert broker.get_positions("s1") == []


def test_capabilities_contract() -> None:
    broker, _ = make_broker()
    caps = broker.capabilities()
    assert caps == {"fills": "simulated", "modify": "replace",
                    "partial": "binary", "stale_guard": True}
