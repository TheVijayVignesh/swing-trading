"""Deterministic pullback V1 strategy (normative spec: ARCHITECTURE.md §7,
ARCHITECTURE_V1.1.md §5). Pure functions — data in, CandidateSignals out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

import pandas as pd

from sts.contracts import CandidateSignal, RuleResult
from sts.features import indicators as ta

TRADING_WINDOW_START = time(9, 30)
TRADING_WINDOW_END = time(14, 30)

DEFAULT_PARAMS: dict[str, Any] = {
    "min_daily_rows": 60,
    "pullback_window": 5,
    "rsi_n": 14,
    "rsi_min": 45.0,
    "rsi_max": 70.0,
    "atr_n": 14,
    "vol_multiple": 1.5,
    "vol_sma_n": 20,
    "slope_n": 10,
    "stop_atr_mult": 1.5,
    "vix_max": 22.0,
}


@dataclass(slots=True)
class StrategyContext:
    daily: dict[str, pd.DataFrame]          # symbol -> [date, open, high, low, close, volume]
    intraday: dict[str, pd.DataFrame]       # symbol -> 5m bars [ts, o, h, l, c, v] for today
    index_daily: pd.DataFrame | None        # NIFTY50 daily [date, ..., close]
    vix_now: float | None
    now: datetime                           # decision time (IST-naive), never wall-clock
    eligible: list[str]
    prev_day: date
    rng_seed: int | None = None             # consumed by random-k baseline only
    params: dict[str, Any] = field(default_factory=dict)


def _p(params: dict[str, Any] | None, key: str) -> Any:
    return (params or {}).get(key, DEFAULT_PARAMS[key])


def regime_rules(ctx: StrategyContext, params: dict[str, Any] | None = None) -> list[RuleResult]:
    """Market-regime gate rules. FAILS CLOSED when index data is missing."""
    rules: list[RuleResult] = []
    idx = ctx.index_daily
    if idx is None or len(idx) == 0 or "close" not in getattr(idx, "columns", []):
        rules.append(RuleResult(
            rule_id="regime_index",
            description="NIFTY50 close>SMA50 and SMA20>SMA50",
            observed="index data missing -> FAIL CLOSED",
            threshold="trend up",
            passed=False,
        ))
        return rules

    closes = idx["close"].astype(float)
    sma20 = ta.sma(closes, 20).iloc[-1]
    sma50 = ta.sma(closes, 50).iloc[-1]
    last_close = float(closes.iloc[-1])
    ok = bool(last_close > sma50 and sma20 > sma50)
    rules.append(RuleResult(
        rule_id="regime_index",
        description="NIFTY50 close>SMA50 and SMA20>SMA50",
        observed=f"close={last_close:.2f} sma20={sma20:.2f} sma50={sma50:.2f}",
        threshold="close>sma50 AND sma20>sma50",
        passed=ok,
    ))

    if ctx.vix_now is None:
        rules.append(RuleResult(
            rule_id="regime_vix",
            description=f"India VIX < {_p(params, 'vix_max')}",
            observed="VIX not provided -> pass-with-flag",
            threshold=f"< {_p(params, 'vix_max')}",
            passed=True,
        ))
    else:
        vok = bool(float(ctx.vix_now) < float(_p(params, "vix_max")))
        rules.append(RuleResult(
            rule_id="regime_vix",
            description=f"India VIX < {_p(params, 'vix_max')}",
            observed=f"VIX={ctx.vix_now}",
            threshold=f"< {_p(params, 'vix_max')}",
            passed=vok,
        ))
    return rules


def _in_trading_window(now: datetime) -> bool:
    t = now.time()
    return TRADING_WINDOW_START <= t <= TRADING_WINDOW_END


def detect_candidates(ctx: StrategyContext, params: dict[str, Any] | None = None) -> list[CandidateSignal]:
    """All symbols passing every rule (shared by pullback-v1 and random-k)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    if not _in_trading_window(ctx.now):
        return []

    gate = regime_rules(ctx, p)
    if not all(r.passed for r in gate):
        return []

    candidates: list[CandidateSignal] = []
    for sym in ctx.eligible:
        df = ctx.daily.get(sym)
        if df is None or len(df) < int(p["min_daily_rows"]):
            continue
        df = df.reset_index(drop=True)
        o, h, l = df["open"], df["high"], df["low"]
        c, v = df["close"], df["volume"]

        sma20_s = ta.sma(c, 20)
        sma50_s = ta.sma(c, 50)
        last_close = float(c.iloc[-1])
        last_sma20 = float(sma20_s.iloc[-1])
        last_sma50 = float(sma50_s.iloc[-1])
        slope10 = float(ta.slope(sma50_s, int(p["slope_n"])).iloc[-1])

        rules: list[RuleResult] = list(gate)

        trend_ok = bool(last_close > last_sma50 and last_sma20 > last_sma50 and slope10 > 0)
        rules.append(RuleResult(
            rule_id="trend",
            description="close>SMA50 AND SMA20>SMA50 AND SMA50-slope(10d)>0",
            observed=(f"close={last_close:.2f} sma20={last_sma20:.2f} "
                      f"sma50={last_sma50:.2f} slope10={slope10:.4f}"),
            threshold="all three hold",
            passed=trend_ok,
        ))
        if not trend_ok:
            continue

        w = int(p["pullback_window"])
        touched = any(
            float(l.iloc[i]) <= float(sma20_s.iloc[i]) * 1.0
            for i in range(len(df) - w, len(df))
        )
        reclaimed = last_close > last_sma20
        pullback_ok = bool(touched and reclaimed)
        rules.append(RuleResult(
            rule_id="pullback",
            description=f"low<=SMA20 within last {w} sessions, then close reclaims SMA20",
            observed=f"touched={touched} close_vs_sma20={last_close - last_sma20:+.2f}",
            threshold="touch AND reclaim",
            passed=pullback_ok,
        ))
        if not pullback_ok:
            continue

        rsi_val = float(ta.rsi(c, int(p["rsi_n"])).iloc[-1])
        momentum_ok = bool(p["rsi_min"] <= rsi_val <= p["rsi_max"])
        rules.append(RuleResult(
            rule_id="momentum",
            description=f"RSI({p['rsi_n']}, prior close) in [{p['rsi_min']},{p['rsi_max']}]",
            observed=f"RSI={rsi_val:.2f}",
            threshold=f"[{p['rsi_min']},{p['rsi_max']}]",
            passed=momentum_ok,
        ))
        if not momentum_ok:
            continue

        intraday = ctx.intraday.get(sym)
        if intraday is None or len(intraday) == 0:
            continue
        today_vol = float(intraday["v"].sum())
        vol_sma20 = float(ta.sma(v, int(p["vol_sma_n"])).iloc[-1])
        vol_ok = bool(today_vol >= float(p["vol_multiple"]) * vol_sma20)
        rules.append(RuleResult(
            rule_id="volume",
            description=f"today's volume >= {p['vol_multiple']}x SMA{p['vol_sma_n']}(prior days)",
            observed=f"today={today_vol:.0f} sma20v={vol_sma20:.0f}",
            threshold=f">= {float(p['vol_multiple']) * vol_sma20:.0f}",
            passed=vol_ok,
        ))
        if not vol_ok:
            continue

        trigger = float(h.iloc[-1])  # previous completed session's high
        intraday_high = float(intraday["h"].max())
        breakout = intraday_high > trigger
        rules.append(RuleResult(
            rule_id="trigger",
            description="intraday high breaks above prior day's high (09:30-14:30 IST)",
            observed=f"intraday_high={intraday_high:.2f} prev_high={trigger:.2f}",
            threshold=f"> {trigger:.2f}",
            passed=breakout,
        ))
        if not breakout:
            continue

        atr14 = float(ta.atr(h, l, c, int(p["atr_n"])).iloc[-1])
        candidates.append(CandidateSignal(
            symbol=sym,
            ts=ctx.now,
            entry_trigger_price=trigger,
            atr=atr14,
            stop_px=trigger - float(p["stop_atr_mult"]) * atr14,
            rules=rules,
        ))
    return candidates


def evaluate(ctx: StrategyContext, params: dict[str, Any] | None = None) -> list[CandidateSignal]:
    """pullback-v1 entry point: every qualifying candidate, deterministic order."""
    return detect_candidates(ctx, params)


def prescreen_daily(ctx: StrategyContext, params: dict[str, Any] | None = None) -> list[dict]:
    """Closed-market diagnostic: which eligible symbols pass ALL daily
    conditions (regime, trend, pullback, momentum, volume-vs-SMA20 using the
    last COMPLETED session) and are therefore ARMED awaiting the intraday
    breakout trigger at the next open. Pure; uses daily data only — never
    fabricates intraday evidence. Returns per-symbol rule summaries."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    gate = regime_rules(ctx, p)
    gate_ok = all(r.passed for r in gate)
    armed: list[dict] = []
    for sym in ctx.eligible:
        df = ctx.daily.get(sym)
        if df is None or len(df) < int(p["min_daily_rows"]):
            continue
        df = df.reset_index(drop=True)
        h, l, c, v = df["high"], df["low"], df["close"], df["volume"]
        sma20_s, sma50_s = ta.sma(c, 20), ta.sma(c, 50)
        last_close = float(c.iloc[-1])
        slope10 = float(ta.slope(sma50_s, int(p["slope_n"])).iloc[-1])
        trend_ok = bool(last_close > float(sma50_s.iloc[-1])
                        and float(sma20_s.iloc[-1]) > float(sma50_s.iloc[-1]) and slope10 > 0)
        w = int(p["pullback_window"])
        touched = any(float(l.iloc[i]) <= float(sma20_s.iloc[i])
                      for i in range(len(df) - w, len(df)))
        pullback_ok = bool(touched and last_close > float(sma20_s.iloc[-1]))
        rsi_val = float(ta.rsi(c, int(p["rsi_n"])).iloc[-1])
        momentum_ok = bool(p["rsi_min"] <= rsi_val <= p["rsi_max"])
        # volume: last completed session vs SMA20 of the PRIOR 20 sessions
        vol_base = v.iloc[-(int(p["vol_sma_n"]) + 1):-1]
        vol_ok = bool(float(v.iloc[-1]) >= float(p["vol_multiple"]) * float(ta.sma(vol_base.reset_index(drop=True), int(p["vol_sma_n"])).iloc[-1]))
        checks = {"trend": trend_ok, "pullback": pullback_ok,
                  "momentum": momentum_ok, "volume": vol_ok}
        if all(checks.values()):
            armed.append({
                "symbol": sym,
                "trigger_level": float(h.iloc[-1]),   # prior-day high breakout
                "stop_px": float(last_close - float(p["stop_atr_mult"]) * float(ta.atr(h, l, c, int(p["atr_n"])).iloc[-1])),
                "rsi": round(rsi_val, 2),
                "regime_ok": gate_ok,
                "checks": checks,
            })
    return armed
