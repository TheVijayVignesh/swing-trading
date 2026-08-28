"""Event-driven daily-bar backtester (research; ARCHITECTURE_V1.1 §8 discipline).

Reuses — never reimplements — the shared machinery:
    - sts.brokers.fillmodel  : entries / stops / targets / adverse sequencing
    - sts.brokers.costs      : India delivery cost schedule (configs/costs.yaml)
    - sts.risk.engine        : hard-authority pre-order vetoes per candidate
    - sts.portfolio.selector : correlation / sector / cap-constrained admission
    - strategy registry      : pure evaluate(StrategyContext) -> CandidateSignals

NO LOOK-AHEAD, per decision day t:
    - completed daily history passed to the strategy ends at t-1;
    - day t's bar is presented as the "intraday" frame exactly like a live
      09:30-14:30 decision window would see it;
    - a candidate detected at end of day t becomes a BUY LIMIT at the signal's
      entry_trigger_price working ONLY during session t+1 (day order);
    - fill/exit prices come exclusively from fillmodel against real bars.

Position lifecycle mirrors PaperBroker.on_bar sequence (the normative loop):
    pending time-stop directive at OPEN -> resolve_bar (stop-before-target,
    strict-penetration targets) -> working entry limits -> bar-close
    maintenance (trail = max(hh_since_T1 - mult*ATR, stop); time-stop arming
    for NEXT open).

Determinism: no randomness anywhere (rng_seed accepted but unused except by
random-k strategies); all iteration over sorted dates/symbols.

Data note: candles_1d `close` is RAW NSE bhavcopy close (unadjusted). Corporate
actions inside the window would distort indicator levels; this engine treats
prices as-is and every result carries that caveat.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from sts.brokers.costs import CostSchedule, compute_costs, load_cost_schedule, round_paise
from sts.brokers.fillmodel import (
    SpreadModel,
    SlippageModel,
    effective_slippage,
    entry_fill,
    resolve_bar,
)
from sts.config import SessionConfig
from sts.contracts import (
    Bar,
    ExitReason,
    PortfolioState,
    PositionView,
    Side,
)
from sts.portfolio.selector import ScoredCandidate, select
from sts.risk.engine import ADV_MAX_FRACTION, evaluate as risk_evaluate
from sts.strategy.pullback_v1 import StrategyContext, regime_rules
from sts.strategy.registry import get_strategy

DEFAULT_DATA_DIR = Path("data/parquet/candles_1d")
DECISION_TIME = time(10, 0)  # inside pullback-v1 trading window (09:30-14:30)


# ---------------------------------------------------------------- config
@dataclass
class BacktestConfig:
    capital_initial: float = 200_000.0
    strategy_id: str = "pullback-v1"
    params: dict[str, Any] = field(default_factory=dict)
    risk_profile: str = "small"          # V1.1 small tier: 1.5% risk, 33% cap
    costs_path: Path = Path("configs/costs.yaml")
    spread: SpreadModel = field(default_factory=SpreadModel)
    slippage: SlippageModel = field(default_factory=SlippageModel)
    min_rows_symbol: int = 70            # loader-level skip threshold
    corr_window: int = 60                # sessions of returns for selector corr
    seed: int | None = None              # determinism knob; pullback-v1 ignores

    def session_config(self) -> SessionConfig:
        return SessionConfig(
            name="backtest",
            capital_initial=self.capital_initial,
            universe="BACKTEST",
            strategy_id=self.strategy_id,
            risk_profile=self.risk_profile,  # type: ignore[arg-type]
            params=dict(self.params),
        )


# ---------------------------------------------------------------- data loading
def load_symbol_frames(
    symbols: list[str],
    data_dir: Path = DEFAULT_DATA_DIR,
    *,
    min_rows: int = 70,
    start: date | None = None,
) -> dict[str, pd.DataFrame]:
    """Load per-symbol daily parquet frames indexed by date, sorted, filtered.

    Symbols with < min_rows rows in the requested window are SKIPPED explicitly
    (never fabricated). Prices stay raw/unadjusted exactly as stored.
    """
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        p = Path(data_dir) / f"{sym}.parquet"
        if not p.exists():
            continue
        df = pd.read_parquet(p)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df[(df["open"] > 0) & (df["high"] >= df["low"])].sort_values("date")
        df = df.drop_duplicates(subset="date", keep="last").set_index("date")
        if start is not None:
            df = df[df.index >= start]
        if len(df) < min_rows:
            continue
        out[sym] = df[["open", "high", "low", "close", "volume"]].astype(float)
    return out


def composite_index(daily: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Equal-weight rebased composite of loaded universe closes.

    Real-data-derived regime proxy, needed because no NIFTY index parquet
    exists on disk while the regime gate fails closed without one. Each symbol
    rebased to first-available close = 100, averaged per date. Deterministic.
    """
    rebased: list[pd.Series] = []
    for sym in sorted(daily):
        df = daily[sym]
        base = float(df["close"].iloc[0])
        if base <= 0:
            continue
        rebased.append((df["close"] / base * 100.0).rename(sym))
    if not rebased:
        return pd.DataFrame(columns=["close"])
    panel = pd.concat(rebased, axis=1)
    return pd.DataFrame({"close": panel.mean(axis=1, skipna=True)})


def make_corr_fn(daily: dict[str, pd.DataFrame], window: int = 60) -> Callable[[str, str], float]:
    """Pearson corr of the last `window` overlapping daily returns; NaN => fail closed."""
    rets = {s: d["close"].pct_change() for s, d in daily.items()}

    def corr(a: str, b: str) -> float:
        ra, rb = rets.get(a), rets.get(b)
        if ra is None or rb is None:
            return float("nan")
        joined = pd.concat([ra, rb], axis=1, join="inner").dropna()
        if len(joined) < window:
            return float("nan")
        sa, sb = joined.iloc[:, 0].std(), joined.iloc[:, 1].std()
        if sa == 0 or sb == 0:
            return float("nan")
        return float(joined.corr().iloc[0, 1])

    return corr


# ---------------------------------------------------------------- internals
@dataclass(slots=True)
class _Order:
    """Day-order queued from decision day t; works only during session t+1."""

    symbol: str
    limit_price: float
    stop_px: float
    atr: float
    qty: int
    risk_amount: float
    signal_date: date


@dataclass(slots=True)
class _Position:
    symbol: str
    qty: int
    avg_entry: float
    stop_px: float
    target1_px: float | None
    target2_px: float | None
    trail_mult_atr: float
    atr: float
    hh_since_t1: float
    opened_date: date
    signal_date: date
    initial_risk: float
    last_seen_date: date
    initial_qty: int = 0
    initial_stop_px: float = 0.0
    held_days: int = 0
    t1_done: bool = False
    trail_active: bool = False
    pending_time_exit: bool = False
    buy_costs: float = 0.0
    sell_costs: float = 0.0
    realized_pnl: float = 0.0      # net proceeds minus basis, gross of buy costs
    sold_qty: int = 0
    sell_notional: float = 0.0
    last_exit_reason: str = ""


# ---------------------------------------------------------------- engine
class Backtester:
    def __init__(
        self,
        cfg: BacktestConfig,
        daily: dict[str, pd.DataFrame],
        index_daily: pd.DataFrame | None = None,
        *,
        sector_fn: Callable[[str], str] | None = None,
        corr_fn: Callable[[str, str], float] | None = None,
        sched: CostSchedule | None = None,
    ):
        self.cfg = cfg
        self.scfg = cfg.session_config()
        self.strategy = get_strategy(cfg.strategy_id)
        self.params = cfg.params
        self.sched = sched or load_cost_schedule(cfg.costs_path)
        self.daily = dict(sorted(daily.items()))
        self.index_daily = index_daily
        # Unknown sectors: symbol-level pseudo-sector keeps selector caps inert
        # instead of artificially bucketing everything under one unknown tag.
        self.sector_fn = sector_fn or (lambda s: f"UNK:{s}")
        self.corr_fn = corr_fn or make_corr_fn(self.daily, cfg.corr_window)

        all_dates: set[date] = set()
        for df in self.daily.values():
            all_dates.update(df.index)
        self.dates: list[date] = sorted(all_dates)
        self._reset()

    def _reset(self) -> None:
        self.cash = float(self.cfg.capital_initial)
        self.realized = 0.0
        self.positions: dict[str, _Position] = {}
        self.orders: list[_Order] = []
        self.trades: list[dict] = []
        self.equity_curve: list[tuple[date, float]] = []
        self.accounting: list[tuple[date, float, float, float]] = []
        self.hwm = self.cfg.capital_initial
        self._cursor: date | None = None
        self.funnel: dict[str, int] = {
            "days_total": 0,
            "days_regime_blocked": 0,
            "scanned": 0,
            "eligible": 0,
            "setups": 0,
            "risk_ok": 0,
            "selected": 0,
            "rejections": 0,
            "orders_expired": 0,
            "insufficient_funds": 0,
        }
        self.rejection_reasons: dict[str, int] = {}

    # -------------------------------------------------- portfolio snapshot
    def _mark_px(self, sym: str) -> float:
        """Last close AT OR BEFORE the cursor — never future data."""
        df = self.daily[sym]
        if self._cursor is not None:
            upto = df.loc[: self._cursor]
        else:
            upto = df
        return float(upto["close"].iloc[-1]) if len(upto) else 0.0

    def _portfolio_state(self) -> PortfolioState:
        views: list[PositionView] = []
        invested = gross = total_risk = unrealized = 0.0
        for sym in sorted(self.positions):
            pos = self.positions[sym]
            px = self._mark_px(sym) or pos.avg_entry
            invested += pos.qty * px
            gross += pos.qty * px
            risk_amt = max(pos.avg_entry - pos.stop_px, 0.0) * pos.qty
            total_risk += risk_amt
            unrealized += (px - pos.avg_entry) * pos.qty
            views.append(PositionView(
                symbol=sym, qty=pos.qty, avg_entry=pos.avg_entry, last_px=px,
                stop_px=pos.stop_px, target1_px=pos.target1_px,
                target2_px=pos.target2_px,
                trail_px=pos.stop_px if pos.trail_active else None,
                unrealized_pnl=(px - pos.avg_entry) * pos.qty,
                pnl_pct=(px / pos.avg_entry - 1.0) * 100.0 if pos.avg_entry else 0.0,
                held_days=pos.held_days, risk_amount=risk_amt, t1_done=pos.t1_done,
            ))
        equity = self.cash + invested
        dd = 0.0 if self.hwm <= 0 else (self.hwm - equity) / self.hwm * 100.0
        return PortfolioState(
            cash=self.cash, invested=invested, unrealized=unrealized,
            realized=self.realized, equity=equity, hwm=self.hwm,
            drawdown_pct=dd, gross_exposure=gross, total_open_risk=total_risk,
            positions=views,
        )

    # -------------------------------------------------- booking helpers
    def _buy(self, od: _Order, px: float, bar: Bar) -> None:
        costs = compute_costs(Side.BUY, px, od.qty, self.sched)
        outflow = px * od.qty + costs["total"]
        if outflow > self.cash + 1e-6:
            self.funnel["insufficient_funds"] += 1
            return
        self.cash -= outflow
        r = px - od.stop_px
        mult = float(self.scfg.effective("trail_mult_atr", 1.5))
        t1m = float(self.scfg.effective("t1_multiple", 1.0))
        t2m = float(self.scfg.effective("t2_multiple", 3.0))
        pos = _Position(
            symbol=od.symbol, qty=od.qty, avg_entry=px, stop_px=od.stop_px,
            target1_px=round_paise(px + t1m * r), target2_px=round_paise(px + t2m * r),
            trail_mult_atr=mult, atr=od.atr, hh_since_t1=px,
            opened_date=bar.ts.date(), signal_date=od.signal_date,
            initial_risk=r * od.qty, last_seen_date=bar.ts.date(),
            initial_qty=od.qty, initial_stop_px=od.stop_px,
            buy_costs=costs["total"],
        )
        self.positions[pos.symbol] = pos

    def _sell(self, pos: _Position, px: float, qty: int, reason: str) -> None:
        qty = min(qty, pos.qty)
        costs = compute_costs(Side.SELL, px, qty, self.sched)
        proceeds = px * qty - costs["total"]
        self.cash += proceeds
        pnl = proceeds - pos.avg_entry * qty
        self.realized += pnl
        pos.realized_pnl += pnl
        pos.sell_costs += costs["total"]
        pos.qty -= qty
        pos.sold_qty += qty
        pos.sell_notional += px * qty
        pos.last_exit_reason = reason
        if pos.qty == 0:
            del self.positions[pos.symbol]

    def _close_trade_record(self, pos: _Position, exit_date: date, fallback_px: float) -> None:
        pnl = pos.realized_pnl - pos.buy_costs
        vwap_exit = pos.sell_notional / pos.sold_qty if pos.sold_qty else fallback_px
        self.trades.append({
            "symbol": pos.symbol,
            "signal_date": pos.signal_date,
            "entry_date": pos.opened_date,
            "exit_date": exit_date,
            "entry_px": round_paise(pos.avg_entry),
            "exit_px": round_paise(vwap_exit),
            "qty": pos.sold_qty,
            "stop_px": round_paise(pos.initial_stop_px),
            "pnl": round(pnl, 2),
            "r_multiple": round(pnl / pos.initial_risk, 4) if pos.initial_risk > 0 else 0.0,
            "hold_days": pos.held_days,
            "exit_reason": pos.last_exit_reason or ExitReason.SESSION_FLATTEN.value,
            "costs": round(pos.buy_costs + pos.sell_costs, 2),
        })

    # -------------------------------------------------- main loop
    def run(self) -> "BacktestResult":
        self._reset()
        time_stop_days = int(self.scfg.effective("time_stop_days", 10))

        for i, t in enumerate(self.dates):
            self._cursor = t
            self.funnel["days_total"] += 1
            prev_t = self.dates[i - 1] if i > 0 else None

            bars: dict[str, Bar] = {}
            for sym, df in self.daily.items():
                if t in df.index:
                    row = df.loc[t]
                    bars[sym] = Bar(sym, datetime.combine(t, DECISION_TIME),
                                    float(row["open"]), float(row["high"]),
                                    float(row["low"]), float(row["close"]),
                                    float(row["volume"]), "1d")

            # ---- per-symbol broker sequence (mirrors PaperBroker.on_bar)
            for sym in sorted(bars):
                bar = bars[sym]
                pos = self.positions.get(sym)

                # a1) pending time-stop directive: exit AT THE OPEN
                if pos is not None and pos.pending_time_exit:
                    slip = effective_slippage(self.cfg.slippage, low_vol_bar=False)
                    hs = self.cfg.spread.half_spread_pct
                    px = round_paise(bar.open * (1.0 - slip - hs), down=True)
                    self._sell(pos, px, pos.qty, ExitReason.TIME_STOP.value)
                    if pos.qty == 0:
                        self._close_trade_record(pos, t, px)
                    pos = self.positions.get(sym)

                # a2) protective exits BEFORE entries (adverse sequencing inside)
                if pos is not None:
                    pre = pos
                    events = resolve_bar(
                        position_side_long=True,
                        stop_px=pos.stop_px,
                        bar=bar,
                        slippage=self.cfg.slippage,
                        spread=self.cfg.spread,
                        stop_reason=(ExitReason.TRAIL_STOP.value if pos.trail_active
                                     else ExitReason.STOP.value),
                        target1=None if pos.t1_done else pos.target1_px,
                        target2=pos.target2_px,
                        t2_armed=pos.t1_done,
                    )
                    for reason, fill in events:
                        cur = self.positions.get(sym)
                        if cur is None or cur.qty <= 0:
                            break
                        if reason == "TARGET1":
                            self._sell(cur, fill.px, max(1, cur.qty // 2),
                                       ExitReason.TARGET1.value)
                            if sym in self.positions:
                                cur = self.positions[sym]
                                cur.t1_done = True
                                cur.hh_since_t1 = bar.high
                        else:
                            self._sell(cur, fill.px, cur.qty, reason)
                    if sym not in self.positions:
                        self._close_trade_record(pre, t, bar.close)
                    pos = self.positions.get(sym)

                # b) working ENTRY limits (day order: this session only)
                matched = [o for o in self.orders if o.symbol == sym]
                self.orders = [o for o in self.orders if o.symbol != sym]
                for od in matched:
                    fill = entry_fill(od.limit_price, Side.BUY, bar,
                                      bar.volume, od.qty, self.cfg.spread)
                    if fill is None:
                        self.funnel["orders_expired"] += 1
                        continue
                    self._buy(od, fill.px, bar)

                # c+d) bar-close maintenance (trail arms NEXT bar)
                pos = self.positions.get(sym)
                if pos is not None:
                    if pos.last_seen_date != t:
                        pos.held_days += 1
                        pos.last_seen_date = t
                    if pos.t1_done:
                        pos.hh_since_t1 = max(pos.hh_since_t1, bar.high)
                        trail = pos.hh_since_t1 - pos.trail_mult_atr * pos.atr
                        pos.stop_px = max(pos.stop_px, trail)
                        pos.trail_active = True
                    if pos.held_days >= time_stop_days:
                        pos.pending_time_exit = True

            # ---- e) mark-to-market snapshot
            state = self._portfolio_state()
            self.hwm = max(self.hwm, state.equity)
            self.equity_curve.append((t, state.equity))
            self.accounting.append((t, state.cash, state.invested, state.equity))

            # ---- signals for session t+1 (decided with data through t)
            if i + 1 < len(self.dates):
                self._generate_orders(t, prev_t)

        # research convention: flatten leftovers at the final close
        if self.dates:
            final_t = self.dates[-1]
            for sym in sorted(list(self.positions)):
                pos = self.positions[sym]
                px = self._mark_px(sym)
                self._sell(pos, px, pos.qty, ExitReason.SESSION_FLATTEN.value)
                self._close_trade_record(pos, final_t, px)

        return BacktestResult(
            trades=list(self.trades),
            equity_curve=list(self.equity_curve),
            accounting=list(self.accounting),
            metrics=compute_metrics(self.trades, self.equity_curve,
                                    self.cfg.capital_initial),
            funnel=dict(self.funnel),
            rejection_reasons=dict(self.rejection_reasons),
            config=self.cfg,
            n_symbols=len(self.daily),
            dates_covered=(self.dates[0], self.dates[-1]) if self.dates else None,
        )

    # -------------------------------------------------- signal generation
    def _generate_orders(self, t: date, prev_t: date | None) -> None:
        if prev_t is None:
            return
        self.funnel["scanned"] += len(self.daily)
        min_rows = int(self.params.get("min_daily_rows", 60))
        eligible = [s for s in sorted(self.daily)
                    if t in self.daily[s].index
                    and len(self.daily[s].loc[:prev_t]) >= min_rows]
        self.funnel["eligible"] += len(eligible)
        if not eligible:
            return

        hist = {s: self.daily[s].loc[:prev_t] for s in eligible}
        intraday: dict[str, pd.DataFrame] = {}
        for s in eligible:
            row = self.daily[s].loc[t]
            intraday[s] = pd.DataFrame([{
                "ts": datetime.combine(t, DECISION_TIME),
                "o": float(row["open"]), "h": float(row["high"]),
                "l": float(row["low"]), "c": float(row["close"]),
                "v": float(row["volume"]),
            }])
        idx_hist = None
        if self.index_daily is not None and len(self.index_daily) > 0:
            idx_hist = self.index_daily[self.index_daily.index <= prev_t]
            if len(idx_hist) == 0:
                idx_hist = None

        ctx = StrategyContext(
            daily=hist, intraday=intraday, index_daily=idx_hist, vix_now=None,
            now=datetime.combine(t, DECISION_TIME), eligible=eligible,
            prev_day=prev_t, params=dict(self.params),
        )

        gate = regime_rules(ctx, self.params)
        if not all(r.passed for r in gate):
            self.funnel["days_regime_blocked"] += 1

        candidates = self.strategy(ctx, self.params)
        self.funnel["setups"] += len(candidates)
        if not candidates:
            return

        state = self._portfolio_state()
        equity = state.equity
        scored: list[ScoredCandidate] = []
        sig_by_sym = {s.symbol: s for s in candidates}
        for sig in candidates:
            past = hist[sig.symbol]
            adv20 = float(past["volume"].tail(20).mean())
            per_share = sig.entry_trigger_price - sig.stop_px
            if per_share <= 0 or adv20 <= 0:
                self._rej(sig.symbol, "BAD_GEOMETRY")
                continue
            risk_amt = self.scfg.risk_per_trade * equity
            qty = int(math.floor(risk_amt / per_share))
            qty = min(qty, int(math.floor(ADV_MAX_FRACTION * adv20)))
            if qty < 1:
                self._rej(sig.symbol, "QTY_ZERO")
                continue
            intent = TradeIntentLite(sig.entry_trigger_price, sig.stop_px)
            verdict = risk_evaluate(intent, state, self.scfg,
                                    day_pnl=0.0, hwm=self.hwm,
                                    avg_daily_volume=adv20)
            if not verdict.approved:
                self._rej(sig.symbol, verdict.rejection_reason)
                continue
            self.funnel["risk_ok"] += 1
            vol_sma20 = float(past["volume"].tail(20).mean())
            score = (float(intraday[sig.symbol]["v"].iloc[0]) / vol_sma20) if vol_sma20 > 0 else 0.0
            scored.append(ScoredCandidate(
                symbol=sig.symbol, score=score, entry_price=sig.entry_trigger_price,
                stop_px=sig.stop_px, qty=qty, risk_amount=risk_amt,
                notional=sig.entry_trigger_price * qty,
            ))

        if not scored:
            return
        scored.sort(key=lambda c: (-c.score, c.symbol))  # deterministic order
        selected, rejections = select(scored, state.positions, self.corr_fn,
                                      self.sector_fn, equity, self.scfg)
        for sym, why in rejections:
            self._rej(sym, why)
        for cand in selected:
            self.funnel["selected"] += 1
            sig = sig_by_sym[cand.symbol]
            self.orders.append(_Order(
                symbol=cand.symbol, limit_price=cand.entry_price,
                stop_px=cand.stop_px, atr=float(sig.atr), qty=cand.qty,
                risk_amount=cand.risk_amount, signal_date=t,
            ))

    def _rej(self, sym: str, why: str) -> None:
        self.funnel["rejections"] += 1
        self.rejection_reasons[why] = self.rejection_reasons.get(why, 0) + 1


class TradeIntentLite:
    """Minimal risk-engine intent surface (limit_price + stop_px only)."""

    __slots__ = ("limit_price", "stop_px")

    def __init__(self, limit_price: float, stop_px: float):
        self.limit_price = limit_price
        self.stop_px = stop_px


# ---------------------------------------------------------------- metrics
def compute_metrics(trades: list[dict], equity_curve: list[tuple[date, float]],
                    capital_initial: float) -> dict:
    n = len(trades)
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total_costs = sum(t["costs"] for t in trades)

    final_eq = equity_curve[-1][1] if equity_curve else capital_initial
    total_return_pct = (final_eq / capital_initial - 1.0) * 100.0 if capital_initial else 0.0

    peak = capital_initial
    max_dd = 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak * 100.0)

    sharpe = 0.0
    if len(equity_curve) >= 3:
        rets = pd.Series([e for _, e in equity_curve]).pct_change().dropna()
        sd = float(rets.std(ddof=1))
        if sd > 0:
            sharpe = float(rets.mean()) / sd * math.sqrt(252.0)

    gross_win, gross_loss = sum(wins), abs(sum(losses))
    pf: float | str
    if gross_loss > 0:
        pf = round(gross_win / gross_loss, 4)
    elif gross_win > 0:
        pf = "inf"
    else:
        pf = 0.0
    return {
        "total_return_pct": round(total_return_pct, 4),
        "max_dd_pct": round(max_dd, 4),
        "sharpe": round(sharpe, 4),
        "win_rate": round(len(wins) / n, 4) if n else 0.0,
        "profit_factor": pf,
        "expectancy_R": round(sum(t["r_multiple"] for t in trades) / n, 4) if n else 0.0,
        "avg_hold_days": round(sum(t["hold_days"] for t in trades) / n, 2) if n else 0.0,
        "n_trades": n,
        "cost_drag_pct": round(total_costs / capital_initial * 100.0, 4) if capital_initial else 0.0,
    }


# ---------------------------------------------------------------- result
@dataclass
class BacktestResult:
    trades: list[dict]
    equity_curve: list[tuple[date, float]]
    accounting: list[tuple[date, float, float, float]]
    metrics: dict
    funnel: dict
    rejection_reasons: dict
    config: BacktestConfig
    n_symbols: int
    dates_covered: tuple[date, date] | None
