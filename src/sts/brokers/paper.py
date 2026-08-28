"""PaperBroker — simulated broker implementing contracts.Broker.

Safety rules (ARCHITECTURE_V1.1 §1.3, normative):
- LIMIT-only: any non-LIMIT TradeIntent is rejected (hard invariant).
- Entries never fill at signal-time price; the shared pure fill model decides.
- Stops never fill above the stop price; gap-through losses taken in full.
- Exits are resolved BEFORE entries each bar (adverse sequencing).
- Trailing stops update ONLY at bar close and arm from the NEXT bar.
- No wall-clock inside decisions: the injected clock stamps records only.

Ledgers are per-session, in-memory, and rebuildable via restore().
"""
from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any, Protocol

from sts.brokers.costs import CostSchedule, compute_costs, round_paise
from sts.brokers.errors import (
    InvalidOrderError,
    OrderRejectedError,
    OrderStateError,
    UnknownOrderError,
)
from sts.brokers.fillmodel import (
    SlippageModel,
    SpreadModel,
    effective_slippage,
    entry_fill,
    resolve_bar,
)
from sts.contracts import (
    Bar,
    FillRecord,
    OrderStatus,
    OrderType,
    PortfolioState,
    PositionView,
    Side,
    TradeIntent,
)


# ---------------------------------------------------------------- sink protocol
@dataclass(slots=True)
class OrderView:
    """Audit payload emitted to the sink on every order state change."""

    order_id: str
    session_id: str
    symbol: str
    side: Side
    order_type: OrderType
    qty: int
    limit_price: float
    status: OrderStatus
    correlation_id: str
    created_ts: datetime
    replaced_by: str | None = None


class OrderSink(Protocol):
    """The lab wires these to storage; PaperBroker only requires these callbacks."""

    def on_order(self, order: OrderView) -> None: ...
    def on_fill(self, fill: FillRecord) -> None: ...
    def on_update(self, state: PortfolioState) -> None: ...


# ---------------------------------------------------------------- internal state
@dataclass(slots=True)
class PositionState:
    symbol: str
    qty: int
    avg_entry: float
    stop_px: float
    target1_px: float | None
    target2_px: float | None
    trail_mult_atr: float
    atr: float                      # ATR estimate carried from entry (proxy)
    hh_since_t1: float              # highest high observed since T1 filled
    t1_done: bool = False
    trail_active: bool = False
    pending_time_exit: bool = False
    opened_ts: datetime | None = None
    last_seen_date: date | None = None
    held_days: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "qty": self.qty, "avg_entry": self.avg_entry,
            "stop_px": self.stop_px, "target1_px": self.target1_px,
            "target2_px": self.target2_px, "trail_mult_atr": self.trail_mult_atr,
            "atr": self.atr, "hh_since_t1": self.hh_since_t1,
            "t1_done": self.t1_done, "trail_active": self.trail_active,
            "pending_time_exit": self.pending_time_exit, "opened_ts": self.opened_ts,
            "last_seen_date": self.last_seen_date, "held_days": self.held_days,
        }

    @classmethod
    def from_snapshot(cls, snap: Mapping[str, Any]) -> PositionState:
        return cls(**dict(snap))


@dataclass(slots=True)
class WorkingOrder:
    order_id: str
    intent: TradeIntent
    status: OrderStatus
    created_ts: datetime
    cum_volume: float = 0.0     # volume accumulated while working (queue evidence)
    bars_alive: int = 0
    replaced_by: str | None = None


@dataclass(slots=True)
class _Account:
    session_id: str
    cash: float
    realized: float = 0.0
    hwm: float | None = None
    positions: dict[str, PositionState] = field(default_factory=dict)
    orders: dict[str, WorkingOrder] = field(default_factory=dict)
    last_prices: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------- broker
class PaperBroker:
    """Deterministic simulated broker; one code path with the backtest engine."""

    def __init__(
        self,
        cost_schedule: CostSchedule,
        spread_model: SpreadModel,
        slippage_model: SlippageModel,
        clock: Callable[[], datetime],
        sink: OrderSink,
        *,
        starting_cash: float = 25_000.0,
        time_stop_days: int = 10,
        max_bars_working: int = 6,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._sched = cost_schedule
        self._spread = spread_model
        self._slip = slippage_model
        self._clock = clock
        self._sink = sink
        self._starting_cash = starting_cash
        self._time_stop_days = time_stop_days
        self._max_bars_working = max_bars_working
        self._id = id_factory or (lambda: uuid.uuid4().hex)
        self._accounts: dict[str, _Account] = {}

    # -------------------------------------------------- session lifecycle
    def export_state(self, session_id: str) -> dict[str, Any]:
        """Serialize a ledger for crash recovery (feed into restore())."""
        acct = self._account(session_id)
        return {
            "cash": acct.cash,
            "realized": acct.realized,
            "hwm": acct.hwm,
            "positions": [p.snapshot() for p in acct.positions.values()],
            "open_orders": {oid: o.intent for oid, o in acct.orders.items()
                            if o.status is OrderStatus.WORKING},
        }

    def _account(self, session_id: str) -> _Account:
        acct = self._accounts.get(session_id)
        if acct is None:
            acct = _Account(session_id=session_id, cash=self._starting_cash)
            self._accounts[session_id] = acct
        return acct

    def restore(
        self,
        session_id: str,
        cash: float,
        positions_list: Iterable[Mapping[str, Any]],
        open_orders: Mapping[str, TradeIntent],
        *,
        realized: float = 0.0,
        hwm: float | None = None,
    ) -> None:
        """Crash recovery: rebuild a session ledger from serialized state."""
        acct = _Account(session_id=session_id, cash=float(cash),
                        realized=float(realized), hwm=hwm)
        for snap in positions_list:
            pos = PositionState.from_snapshot(snap)
            acct.positions[pos.symbol] = pos
        now = self._clock()
        for oid, intent in open_orders.items():
            order = WorkingOrder(order_id=oid, intent=intent,
                                 status=OrderStatus.WORKING, created_ts=now)
            acct.orders[oid] = order
            self._emit_order(order)
        self._accounts[session_id] = acct

    # -------------------------------------------------- Broker protocol
    def get_account_state(self, session_id: str) -> PortfolioState:
        return self._portfolio_state(self._account(session_id))

    def get_positions(self, session_id: str) -> list[PositionView]:
        return self._portfolio_state(self._account(session_id)).positions

    def get_order(self, session_id: str, order_id: str) -> OrderView:
        order = self._account(session_id).orders.get(order_id)
        if order is None:
            raise UnknownOrderError(order_id)
        return self._order_view(order)

    def place_order(self, session_id: str, intent: TradeIntent) -> str:
        """Validate and register a WORKING order. Rejects raise OrderRejectedError."""
        acct = self._account(session_id)

        def reject(reason: str) -> OrderRejectedError:
            ghost = WorkingOrder(order_id=self._id(), intent=intent,
                                 status=OrderStatus.REJECTED, created_ts=self._clock())
            self._emit_order(ghost)
            return OrderRejectedError(reason)

        # HARD INVARIANT: LIMIT-only (addendum §18.4).
        if intent.order_type is not OrderType.LIMIT:
            raise reject(f"NON_LIMIT_ORDER:{intent.order_type}")
        if intent.qty <= 0:
            raise reject("QTY_NOT_POSITIVE")
        if intent.limit_price <= 0:
            raise reject("LIMIT_NOT_POSITIVE")
        ref = acct.last_prices.get(intent.symbol)
        if ref is not None and not (0.9 * ref <= intent.limit_price <= 1.1 * ref):
            raise reject("CIRCUIT_BAND")
        worst_cost = intent.limit_price * intent.qty * 1.01  # spread+slip+costs buffer
        if intent.side is Side.BUY and worst_cost > acct.cash:
            raise reject("INSUFFICIENT_FUNDS")

        order = WorkingOrder(order_id=self._id(), intent=intent,
                             status=OrderStatus.WORKING, created_ts=self._clock())
        acct.orders[order.order_id] = order
        self._emit_order(order)
        return order.order_id

    def cancel_order(self, session_id: str, order_id: str) -> bool:
        order = self._account(session_id).orders.get(order_id)
        if order is None:
            raise UnknownOrderError(order_id)
        if order.status is not OrderStatus.WORKING:
            return False
        order.status = OrderStatus.CANCELLED
        self._emit_order(order)
        return True

    def modify_order(self, session_id: str, order_id: str, new_limit: float) -> str:
        """Replace semantics: old becomes CANCELLED with replaced_by link; NEW id."""
        acct = self._account(session_id)
        old = acct.orders.get(order_id)
        if old is None:
            raise UnknownOrderError(order_id)
        if old.status is not OrderStatus.WORKING:
            raise OrderStateError("MODIFY_NON_WORKING")
        if new_limit <= 0:
            raise InvalidOrderError("LIMIT_NOT_POSITIVE")

        new_intent = replace(old.intent, limit_price=new_limit)
        new_order = WorkingOrder(order_id=self._id(), intent=new_intent,
                                 status=OrderStatus.WORKING, created_ts=self._clock())
        old.status = OrderStatus.CANCELLED
        old.replaced_by = new_order.order_id
        acct.orders[new_order.order_id] = new_order
        self._emit_order(old)
        self._emit_order(new_order)
        return new_order.order_id

    # -------------------------------------------------- THE engine
    def on_bar(self, session_id: str, bar: Bar) -> list[FillRecord]:
        """Drive all fills for one completed bar. Sequence (normative):

        a) pending time-stop directive at the OPEN, then stops/targets via
           fillmodel.resolve_bar (adverse sequencing inside);
        b) working ENTRY limits via fillmodel.entry_fill;
        c) bar-close state update (trail = max(hh_since_T1 - mult*ATR, stop));
        d) time-stop arming (executes at NEXT bar open);
        e) portfolio snapshot to sink.on_update.
        """
        acct = self._account(session_id)
        fills: list[FillRecord] = []
        pos = acct.positions.get(bar.symbol)

        # --- a1) time-stop directive queued on a previous bar: exit at OPEN
        if pos is not None and pos.pending_time_exit:
            slip = effective_slippage(self._slip, low_vol_bar=False)
            px = round_paise(
                bar.open * (1.0 - slip - self._spread.half_spread_pct), down=True)
            self._sell(acct, pos, px, pos.qty, "TIME_STOP", bar, fills)
            pos = acct.positions.get(bar.symbol)

        # --- a2) protective exits BEFORE entries (adverse sequencing)
        if pos is not None:
            events = resolve_bar(
                position_side_long=True,
                stop_px=pos.stop_px,
                bar=bar,
                slippage=self._slip,
                spread=self._spread,
                stop_reason="TRAIL_STOP" if pos.trail_active else "STOP",
                target1=None if pos.t1_done else pos.target1_px,
                target2=pos.target2_px,
                t2_armed=pos.t1_done,
            )
            for reason, fill in events:
                pos = acct.positions.get(bar.symbol)
                if pos is None or pos.qty <= 0:
                    break
                if reason == "TARGET1":
                    sell_qty = max(1, pos.qty // 2)   # half, round down, min 1
                    self._sell(acct, pos, fill.px, sell_qty, reason, bar, fills)
                    if pos.qty > 0:
                        pos.t1_done = True
                        pos.hh_since_t1 = bar.high
                else:  # STOP / TRAIL_STOP / TARGET2 close the remainder
                    self._sell(acct, pos, fill.px, pos.qty, reason, bar, fills)
            pos = acct.positions.get(bar.symbol)

        # --- b) working ENTRY limits (strict penetration / volume evidence)
        symbol_orders = sorted(
            (o for o in acct.orders.values()
             if o.status is OrderStatus.WORKING and o.intent.symbol == bar.symbol),
            key=lambda o: o.created_ts,
        )
        for order in symbol_orders:
            order.bars_alive += 1
            cum_at_touch = order.cum_volume + bar.volume
            fill = entry_fill(order.intent.limit_price, order.intent.side, bar,
                              cum_at_touch, order.intent.qty, self._spread)
            order.cum_volume += bar.volume
            if fill is None:
                if order.bars_alive >= self._max_bars_working:
                    order.status = OrderStatus.EXPIRED
                    self._emit_order(order)
                continue
            if order.intent.side is Side.BUY:
                costs = self._book_buy(acct, order.intent, fill.px, bar)
                rec = self._fill_record(order.order_id, acct.session_id, bar,
                                        fill.px, fill.qty, Side.BUY, costs)
                self._sink.on_fill(rec)
                fills.append(rec)
            else:
                # Working SELL limit (manual/flatten exit): settle against the
                # long position exactly like an internal directive exit.
                pos_now = acct.positions.get(order.intent.symbol)
                if pos_now is None:
                    order.status = OrderStatus.EXPIRED
                    self._emit_order(order)
                    continue
                costs = compute_costs(Side.SELL, fill.px, fill.qty, self._sched)
                proceeds = fill.px * fill.qty - costs["total"]
                acct.cash += proceeds
                acct.realized += proceeds - pos_now.avg_entry * fill.qty
                pos_now.qty -= fill.qty
                rec = self._fill_record(order.order_id, acct.session_id, bar,
                                        fill.px, fill.qty, Side.SELL, costs)
                self._sink.on_fill(rec)
                fills.append(rec)
                if pos_now.qty <= 0:
                    del acct.positions[pos_now.symbol]
            order.status = OrderStatus.FILLED
            self._emit_order(order)
            pos = acct.positions.get(bar.symbol) or pos

        # --- c+d) bar-close position maintenance (trail arms NEXT bar)
        acct.last_prices[bar.symbol] = bar.close
        pos = acct.positions.get(bar.symbol)
        if pos is not None:
            today = bar.ts.date()
            if pos.last_seen_date != today:
                pos.held_days += 1
                pos.last_seen_date = today
            if pos.t1_done:
                pos.hh_since_t1 = max(pos.hh_since_t1, bar.high)
                trail = pos.hh_since_t1 - pos.trail_mult_atr * pos.atr
                pos.stop_px = max(pos.stop_px, trail)
                pos.trail_active = True
            if pos.held_days >= self._time_stop_days:
                pos.pending_time_exit = True

        # --- e) snapshot
        state = self._portfolio_state(acct)
        acct.hwm = max(acct.hwm if acct.hwm is not None else state.equity, state.equity)
        state.hwm = acct.hwm
        state.drawdown_pct = (
            0.0 if acct.hwm in (None, 0.0) else (acct.hwm - state.equity) / acct.hwm * 100.0
        )
        self._sink.on_update(state)
        return fills

    # -------------------------------------------------- booking helpers
    def _sell(
        self,
        acct: _Account,
        pos: PositionState,
        px: float,
        qty: int,
        reason: str,
        bar: Bar,
        fills: list[FillRecord],
    ) -> None:
        qty = min(qty, pos.qty)
        costs = compute_costs(Side.SELL, px, qty, self._sched)
        proceeds = px * qty - costs["total"]
        acct.cash += proceeds
        acct.realized += proceeds - pos.avg_entry * qty
        pos.qty -= qty
        if pos.qty == 0:
            del acct.positions[pos.symbol]
        dir_id = f"dir:{bar.symbol}:{reason}:{bar.ts.isoformat()}"
        rec = self._fill_record(dir_id, acct.session_id, bar, px, qty, Side.SELL, costs)
        self._sink.on_fill(rec)
        fills.append(rec)

    def _book_buy(
        self, acct: _Account, intent: TradeIntent, px: float, bar: Bar,
    ) -> dict[str, float]:
        qty = intent.qty
        costs = compute_costs(Side.BUY, px, qty, self._sched)
        outflow = px * qty + costs["total"]
        if outflow > acct.cash + 1e-6:  # validated at placement; belt-and-braces
            raise OrderRejectedError("INSUFFICIENT_FUNDS")
        acct.cash -= outflow
        pos = acct.positions.get(intent.symbol)
        if pos is None:
            stop = intent.stop_px if intent.stop_px is not None else px * 0.97
            mult = intent.trail_mult_atr or 1.5
            atr = (px - stop) / mult if mult > 0 else px * 0.02
            pos = PositionState(
                symbol=intent.symbol, qty=qty, avg_entry=px, stop_px=stop,
                target1_px=intent.target1_px, target2_px=intent.target2_px,
                trail_mult_atr=mult, atr=atr, hh_since_t1=px,
                opened_ts=self._clock(), last_seen_date=bar.ts.date(),
            )
            acct.positions[intent.symbol] = pos
        else:  # add-on: volume-weighted average entry, levels stay as-is
            total = pos.qty + qty
            pos.avg_entry = (pos.avg_entry * pos.qty + px * qty) / total
            pos.qty = total
        return costs

    def _fill_record(
        self, order_id: str, session_id: str, bar: Bar, px: float, qty: int,
        side: Side, costs: dict[str, float],
    ) -> FillRecord:
        return FillRecord(
            order_id=order_id, session_id=session_id, symbol=bar.symbol, side=side,
            px=px, qty=qty, ts=self._clock(), cost_breakdown=dict(costs),
        )

    def _order_view(self, order: WorkingOrder) -> OrderView:
        i = order.intent
        return OrderView(
            order_id=order.order_id, session_id=i.session_id, symbol=i.symbol,
            side=i.side, order_type=i.order_type, qty=i.qty,
            limit_price=i.limit_price, status=order.status,
            correlation_id=i.correlation_id, created_ts=order.created_ts,
            replaced_by=order.replaced_by,
        )

    def _emit_order(self, order: WorkingOrder) -> None:
        self._sink.on_order(self._order_view(order))

    # -------------------------------------------------- portfolio view
    def _position_views(self, acct: _Account) -> list[PositionView]:
        views: list[PositionView] = []
        for p in acct.positions.values():
            last = acct.last_prices.get(p.symbol, p.avg_entry)
            unreal = (last - p.avg_entry) * p.qty
            views.append(PositionView(
                symbol=p.symbol, qty=p.qty, avg_entry=p.avg_entry, last_px=last,
                stop_px=p.stop_px, target1_px=p.target1_px, target2_px=p.target2_px,
                trail_px=p.stop_px if p.trail_active else None,
                unrealized_pnl=unreal,
                pnl_pct=(unreal / (p.avg_entry * p.qty)) * 100.0 if p.qty else 0.0,
                held_days=p.held_days,
                risk_amount=(p.avg_entry - p.stop_px) * p.qty,
                t1_done=p.t1_done,
            ))
        return views

    def _portfolio_state(self, acct: _Account) -> PortfolioState:
        views = self._position_views(acct)
        invested = sum(p.qty * p.avg_entry for p in acct.positions.values())
        unrealized = sum(v.unrealized_pnl for v in views)
        equity = acct.cash + invested + unrealized
        gross = sum(v.qty * v.last_px for v in views)
        risk = sum(v.risk_amount for v in views)
        hwm = acct.hwm if acct.hwm is not None else equity
        dd = 0.0 if hwm <= 0 else (hwm - equity) / hwm * 100.0
        return PortfolioState(
            cash=acct.cash, invested=invested, unrealized=unrealized,
            realized=acct.realized, equity=equity, hwm=hwm, drawdown_pct=dd,
            gross_exposure=gross, total_open_risk=risk, positions=views,
        )

    def capabilities(self) -> dict:
        return {"fills": "simulated", "modify": "replace",
                "partial": "binary", "stale_guard": True}
