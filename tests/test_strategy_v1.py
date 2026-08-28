"""Golden + negative fixtures for deterministic pullback-v1.

Fixture engineering:
- closes oscillate: up day +1.6, down day -0.8 (alternating) => drift +0.4/day,
  Wilder RSI converges to ~66.7 (in [45,70]), SMA20>SMA50>0 slope.
- one deliberate dip: low = SMA20 - 0.05 three bars from the end (pullback touch).
- today's intraday volume 2.0x the flat 1e6 daily volume (>=1.5x).
- intraday highs break the prior day's high within the trading window.
"""
from dataclasses import replace
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from sts.features import indicators as ta
from sts.strategy import registry
from sts.strategy.pullback_v1 import StrategyContext, evaluate, regime_rules


def build_daily(closes, volumes=None):
    closes = list(closes)
    n = len(closes)
    volumes = volumes if volumes is not None else [1_000_000.0] * n
    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) + 0.3 for o, c in zip(opens, closes)]
    lows = [min(o, c) - 0.3 for o, c in zip(opens, closes)]
    df = pd.DataFrame({
        "date": pd.date_range("2026-04-01", periods=n, freq="B").date,
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": volumes,
    })
    return df


def apply_pullback_dip(df):
    sma20 = ta.sma(df["close"], 20)
    j = len(df) - 3  # inside last 5 sessions
    df.loc[j, "low"] = float(sma20.iloc[j]) - 0.05
    return df


def golden_closes(n=80):
    closes = [100.0]
    for i in range(1, n):
        closes.append(closes[-1] + (1.6 if i % 2 == 1 else -0.8))
    return closes


def golden_daily(n=80):
    return apply_pullback_dip(build_daily(golden_closes(n)))


def golden_intraday(prev_high, total_vol=2_000_000.0, breach=True):
    nbars = 10
    vols = [total_vol / nbars] * nbars
    start = prev_high - 1.5
    step = 0.25
    rows = []
    for i in range(nbars):
        o = start + i * step
        h = o + 0.10
        l = o - 0.05
        c = o + 0.05
        rows.append((o, h, l, c))
    if not breach:
        rows = [(o, min(h, prev_high - 0.01), l, min(c, prev_high - 0.02))
                for (o, h, l, c) in rows]
    return pd.DataFrame({
        "ts": pd.date_range("2026-08-21 09:15", periods=nbars, freq="5min"),
        "o": [r[0] for r in rows], "h": [r[1] for r in rows],
        "l": [r[2] for r in rows], "c": [r[3] for r in rows], "v": vols,
    })


NOW = datetime(2026, 8, 21, 10, 0)          # inside 09:30-14:30 IST window
PREV_DAY = date(2026, 8, 20)


def golden_index():
    idx_closes = [20000.0 + 30.0 * i for i in range(80)]
    df = build_daily(idx_closes)
    df = df.rename(columns={})  # keep 'close' column name
    return df


def make_ctx(**kw) -> StrategyContext:
    base = dict(
        daily={"GOOD": golden_daily()},
        intraday={"GOOD": None},   # filled below against actual prior high
        index_daily=golden_index(),
        vix_now=15.0,
        now=NOW,
        eligible=["GOOD"],
        prev_day=PREV_DAY,
        rng_seed=None,
        params=None,
    )
    ctx_params = base.pop("params")
    intraday_kw = kw.pop("intraday_kwargs", {})
    base.update(kw)
    for sym in list(base["eligible"]) or ["GOOD"]:
        if sym not in base["daily"]:
            base["daily"][sym] = golden_daily()
        if sym not in base["intraday"] or base["intraday"][sym] is None:
            base["intraday"][sym] = golden_intraday(
                float(base["daily"][sym]["high"].iloc[-1]), **intraday_kw)
    return StrategyContext(params=ctx_params or {}, **base)


class TestGoldenFixture:
    def test_exactly_one_candidate_with_expected_trigger_stop(self):
        ctx = make_ctx()
        out = evaluate(ctx)
        assert len(out) == 1
        cand = out[0]
        assert cand.symbol == "GOOD"
        expected_trigger = float(ctx.daily["GOOD"]["high"].iloc[-1])
        expected_atr = float(ta.atr(ctx.daily["GOOD"]["high"],
                                    ctx.daily["GOOD"]["low"],
                                    ctx.daily["GOOD"]["close"], 14).iloc[-1])
        assert cand.entry_trigger_price == pytest.approx(expected_trigger)
        assert cand.atr == pytest.approx(expected_atr)
        assert cand.stop_px == pytest.approx(expected_trigger - 1.5 * expected_atr)
        assert cand.ts == NOW
        rule_ids = [r.rule_id for r in cand.rules]
        assert rule_ids[0].startswith("regime_")
        for rid in ("regime_index", "regime_vix", "trend", "pullback",
                    "momentum", "volume", "trigger"):
            assert rid in rule_ids
        assert all(r.passed for r in cand.rules)

    def test_rsi_in_band_for_fixture(self):
        rsi = float(ta.rsi(golden_daily()["close"], 14).iloc[-1])
        assert 45.0 <= rsi <= 70.0


class TestNegativeFixtures:
    def test_regime_gate_fails_closed_when_index_missing(self):
        out = evaluate(make_ctx(index_daily=None))
        assert out == []
        rules = regime_rules(make_ctx(index_daily=None))
        assert rules[0].rule_id == "regime_index" and rules[0].passed is False
        assert "FAIL CLOSED" in rules[0].observed

    def test_vix_spike_blocks_entries(self):
        assert evaluate(make_ctx(vix_now=25.0)) == []

    def test_vix_missing_passes_with_flag(self):
        out = evaluate(make_ctx(vix_now=None))
        assert len(out) == 1
        vix_rule = next(r for r in out[0].rules if r.rule_id == "regime_vix")
        assert vix_rule.passed is True and "flag" in vix_rule.observed.lower()

    def test_trend_failure(self):
        declining = build_daily([200.0 - 0.8 * i for i in range(80)])
        out = evaluate(make_ctx(daily={"BAD": declining}, eligible=["BAD"]))
        assert out == []

    def test_pullback_failure_no_touch(self):
        df = golden_daily()
        sma20 = ta.sma(df["close"], 20)
        for i in range(len(df) - 5, len(df)):
            df.loc[i, "low"] = float(sma20.iloc[i]) + 1.0
        out = evaluate(make_ctx(daily={"NOTOUCH": df}, eligible=["NOTOUCH"]))
        assert out == []

    def test_momentum_failure_rsi_too_high(self):
        # strong monotone uptrend keeps trend/pullback satisfiable but RSI ~100
        closes = [100.0 + 1.2 * i for i in range(80)]
        closes[79] -= 0.05
        df = apply_pullback_dip(build_daily(closes))
        out = evaluate(make_ctx(daily={"HOT": df}, eligible=["HOT"]))
        assert out == []

    def test_volume_failure(self):
        out = evaluate(make_ctx(intraday_kwargs={"total_vol": 500_000.0}))
        assert out == []

    def test_trigger_failure_no_breakout(self):
        out = evaluate(make_ctx(intraday_kwargs={"breach": False}))
        assert out == []

    def test_outside_trading_window(self):
        late = make_ctx(now=datetime(2026, 8, 21, 14, 31))
        assert evaluate(late) == []
        early = make_ctx(now=datetime(2026, 8, 21, 9, 29))
        assert evaluate(early) == []
        ok = make_ctx(now=datetime(2026, 8, 21, 14, 30))
        assert len(evaluate(ok)) == 1

    def test_insufficient_history(self):
        out = evaluate(make_ctx(daily={"SHORT": golden_daily(50)}, eligible=["SHORT"]))
        assert out == []


class TestRegistryAndRandomK:
    def _multi_ctx(self, syms=("A", "B", "C")):
        daily = {s: golden_daily() for s in syms}
        intraday = {s: golden_intraday(float(daily[s]["high"].iloc[-1])) for s in syms}
        return StrategyContext(
            daily=daily, intraday=intraday, index_daily=golden_index(),
            vix_now=15.0, now=NOW, eligible=list(syms), prev_day=PREV_DAY,
        )

    def test_registry_lookup(self):
        assert set(registry.STRATEGIES) >= {"pullback-v1", "random-k"}
        assert callable(registry.get_strategy("pullback-v1"))
        with pytest.raises(KeyError):
            registry.get_strategy("moonshot")

    def test_random_k_samples_subset_deterministically(self):
        ctx = self._multi_ctx()
        all_c = evaluate(ctx)
        assert len(all_c) == 3
        rk = registry.get_strategy("random-k")
        a = rk(ctx, {"k": 2, "seed": 7})
        b = rk(ctx, {"k": 2, "seed": 7})
        c = rk(ctx, {"k": 2, "seed": 8})
        assert len(a) == 2 and [x.symbol for x in a] == [x.symbol for x in b]
        assert {x.symbol for x in a} <= {x.symbol for x in all_c}
        # detection logic shared: same candidate objects qualify
        assert {x.symbol for x in c} <= {x.symbol for x in all_c}

    def test_random_k_k_ge_all_returns_all(self):
        rk = registry.get_strategy("random-k")
        assert len(rk(self._multi_ctx(), {"k": 5})) == 3
