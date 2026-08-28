"""Backtest engine + purged walk-forward splitter tests.

Synthetic universe: 130 sessions, 3 symbols with deterministic cyclic paths
engineered to fire pullback-v1 (choppy rise -> SMA20-piercing dip -> reclaim
bar with breakout high + 5x volume). All properties asserted are the
anti-leakage / fill-invariant contracts from ARCHITECTURE_V1.1 §8.
"""
from __future__ import annotations

import math
from datetime import date

import pandas as pd
import pytest

from sts.research.backtest import (
    Backtester,
    BacktestConfig,
    composite_index,
    compute_metrics,
    load_symbol_frames,
)
from sts.research.walkforward import purged_split

N_SESSIONS = 130


# ---------------------------------------------------------------- fixtures
def _bdates(n: int) -> list[date]:
    return list(pd.bdate_range("2025-01-01", periods=n).date)


def make_index(n: int) -> pd.DataFrame:
    rows = []
    for i, d in enumerate(_bdates(n)):
        c = 100.0 * (1 + 0.002 * i)
        rows.append({"date": d, "open": c * 0.999, "high": c * 1.001,
                     "low": c * 0.998, "close": c, "volume": 1e7})
    return pd.DataFrame(rows).set_index("date")[["open", "high", "low", "close", "volume"]]


CYCLE = 25
DIP_DAYS = (15, 16)
SIGNAL_DAY = 17
CRASH_SESSION = 108


def _close_for(j: int, b: float, prev: float | None) -> float:
    if not prev:
        return b
    if j <= 13:
        return prev * (1.007 if j % 2 == 0 else 0.9985)
    if j == 14:
        return prev * 1.008
    if j in DIP_DAYS:
        return prev * 0.99
    if j == SIGNAL_DAY:
        return prev * 1.016
    return prev * 1.004


def make_symbol(n: int, phase_shift: int, base: float = 100.0) -> pd.DataFrame:
    closes: list[float] = []
    prev: float | None = None
    b = base
    for i in range(n):
        j = (i + phase_shift) % CYCLE
        if j == 0 and closes:
            b = closes[-1] * 1.004
        closes.append(_close_for(j, b, prev))
        prev = closes[-1]

    rows = []
    prev_c = None
    for idx_, d in enumerate(_bdates(n)):
        c = closes[idx_]
        o = prev_c if prev_c is not None else c * 0.995
        j = (idx_ + phase_shift) % CYCLE
        w = 0.045 if (j in DIP_DAYS or j == SIGNAL_DAY) else 0.02
        hi = max(o, c) * (1 + w)
        lo = min(o, c) * (1 - w)
        v = 1_000_000.0 * (5.0 if j == SIGNAL_DAY else 1.0)
        rows.append({"date": d, "open": o, "high": hi, "low": lo,
                     "close": c, "volume": v})
        prev_c = c
    return pd.DataFrame(rows).set_index("date")[["open", "high", "low", "close", "volume"]]


@pytest.fixture()
def synth_universe():
    idx = make_index(N_SESSIONS)
    daily = {s: make_symbol(N_SESSIONS, p)
             for s, p in [("AAA", 0), ("BBB", 8), ("CCC", 16)]}
    # engineered gap-down crash so open positions stop out via the
    # gap-through branch of the shared fill model (open-based fill)
    for df in daily.values():
        i = CRASH_SESSION
        d = df.index[i]
        prev_close = float(df["close"].iloc[i - 1])
        o = round(prev_close * 0.82, 2)
        df.loc[d] = {"open": o, "high": o * 1.01, "low": o * 0.97,
                     "close": o * 0.99, "volume": 3_000_000.0}
    return daily, idx


def _run(daily, idx) -> "object":
    cfg = BacktestConfig(capital_initial=200_000.0, risk_profile="small")
    bt = Backtester(cfg, daily, idx, corr_fn=lambda a, b_: 0.1)
    return bt.run()


# ---------------------------------------------------------------- walk-forward
class TestPurgedSplit:
    def test_shapes_and_gaps(self):
        dates = _bdates(120)
        folds = purged_split(dates, train_days=40, test_days=10, embargo_days=5)
        assert folds, "expected at least one fold"
        idx = {d: i for i, d in enumerate(dates)}
        for (tr_a, tr_b), (te_a, te_b) in folds:
            # ordering inside each window
            assert idx[tr_a] < idx[tr_b]
            assert idx[te_a] <= idx[te_b]
            # train window has exactly train_days sessions
            assert idx[tr_b] - idx[tr_a] + 1 == 40
            # EMBARGO: minimum gap between train end and test start >= 5 sessions
            gap = idx[te_a] - idx[tr_b] - 1
            assert gap >= 5, f"leakage: embargo gap {gap} < 5"
        # test segments are non-overlapping and advancing
        starts = [idx[f[1][0]] for f in folds]
        assert starts == sorted(set(starts))
        for (_, trb1), (tea1, _) in zip(folds, folds[1:]):
            pass  # per-fold checks above cover adjacency
        for f1, f2 in zip(folds, folds[1:]):
            assert idx[f2[1][0]] > idx[f1[1][1]], "test segments overlap"

    def test_no_overlap_between_train_and_test(self):
        dates = _bdates(90)
        folds = purged_split(dates, 30, 15, 7)
        for (tr_a, tr_b), (te_a, te_b) in folds:
            assert idx_range_ok(tr_a, tr_b, te_a, te_b, dates, embargo=7)

    def test_determinism(self):
        dates = _bdates(80)
        a = purged_split(dates, 30, 10, 5)
        b = purged_split(dates, 30, 10, 5)
        assert a == b

    def test_invalid_args_raise(self):
        dates = _bdates(50)
        with pytest.raises(ValueError):
            purged_split(dates, 0, 10, 5)
        with pytest.raises(ValueError):
            purged_split(dates, 10, 10, -1)

    def test_short_history_yields_empty(self):
        dates = _bdates(20)
        assert purged_split(dates, 40, 10, 5) == []


def idx_range_ok(tr_a, tr_b, te_a, te_b, dates, embargo: int) -> bool:
    idx = {d: i for i, d in enumerate(dates)}
    if not (idx[tr_a] < idx[tr_b] < idx[te_a] <= idx[te_b]):
        return False
    return (idx[te_a] - idx[tr_b] - 1) >= embargo


# ---------------------------------------------------------------- backtester
class TestSyntheticBacktest:
    def test_produces_trades(self, synth_universe):
        daily, idx = synth_universe
        res = _run(daily, idx)
        assert res.metrics["n_trades"] >= 2, (
            f"synthetic universe must engineer >=2 trades; funnel={res.funnel}"
        )

    def test_no_lookahead_entry_after_signal(self, synth_universe):
        """Every entry must happen on a session STRICTLY AFTER its signal date."""
        daily, idx = synth_universe
        res = _run(daily, idx)
        session_idx = {d: i for i, d in enumerate(sorted({d for df in daily.values() for d in df.index}))}
        for t in res.trades:
            assert t["entry_date"] > t["signal_date"], t
            assert session_idx[t["entry_date"]] > session_idx[t["signal_date"]]

    def test_stop_fills_never_above_stop(self, synth_universe):
        daily, idx = synth_universe
        res = _run(daily, idx)
        stop_exits = [t for t in res.trades if t["exit_reason"] == "STOP"]
        assert stop_exits, "expected at least one hard-stop exit in synthetic run"
        for t in stop_exits:
            assert t["exit_px"] <= t["stop_px"], (
                f"stop invariant violated: exit {t['exit_px']} > stop {t['stop_px']}"
            )

    def test_equity_identity_cash_plus_invested(self, synth_universe):
        daily, idx = synth_universe
        res = _run(daily, idx)
        assert len(res.accounting) == len(res.equity_curve)
        for (d, cash, invested, equity), (d2, eq2) in zip(res.accounting, res.equity_curve):
            assert d == d2
            assert math.isclose(equity, cash + invested, rel_tol=1e-9, abs_tol=1e-6)
            assert math.isclose(eq2, equity, rel_tol=1e-12)

    def test_metrics_consistent(self, synth_universe):
        daily, idx = synth_universe
        res = _run(daily, idx)
        m = res.metrics
        for key in ("total_return_pct", "max_dd_pct", "sharpe", "win_rate",
                    "profit_factor", "expectancy_R", "avg_hold_days",
                    "n_trades", "cost_drag_pct"):
            assert key in m, key
        assert m["n_trades"] == len(res.trades)
        assert 0.0 <= m["win_rate"] <= 1.0
        assert m["max_dd_pct"] >= 0.0
        if res.trades:
            avg_r = sum(t["r_multiple"] for t in res.trades) / len(res.trades)
            assert math.isclose(m["expectancy_R"], round(avg_r, 4), abs_tol=1e-9)

    def test_determinism_run_twice_identical(self, synth_universe):
        daily, idx = synth_universe
        r1 = _run(daily, idx)
        r2 = _run(daily, idx)
        assert r1.trades == r2.trades
        assert r1.metrics == r2.metrics
        assert r1.equity_curve == r2.equity_curve
        assert r1.funnel == r2.funnel

    def test_funnel_counters_coherent(self, synth_universe):
        daily, idx = synth_universe
        res = _run(daily, idx)
        f = res.funnel
        assert f["scanned"] >= f["eligible"]
        assert f["selected"] <= f["risk_ok"] <= f["setups"]


# ---------------------------------------------------------------- metrics util
class TestComputeMetrics:
    def test_basic_math(self):
        cap = 100_000.0
        curve = list(zip(_bdates(3), [cap, 101_000.0, 100_500.0]))
        trades = [
            {"pnl": 500.0, "r_multiple": 2.0, "hold_days": 3, "costs": 40.0},
            {"pnl": -250.0, "r_multiple": -1.0, "hold_days": 5, "costs": 38.0},
        ]
        m = compute_metrics(trades, curve, cap)
        assert m["n_trades"] == 2
        assert m["win_rate"] == 0.5
        assert m["profit_factor"] == 2.0
        assert m["expectancy_R"] == 0.5
        assert m["total_return_pct"] == pytest.approx(0.5, abs=1e-6)
        assert m["max_dd_pct"] > 0.0
        assert m["cost_drag_pct"] == pytest.approx((78.0 / cap) * 100.0, abs=1e-6)

    def test_empty_inputs(self):
        m = compute_metrics([], [], 100_000.0)
        assert m["n_trades"] == 0
        assert m["total_return_pct"] == 0.0
        assert m["profit_factor"] == 0.0


# ---------------------------------------------------------------- loader
class TestLoader:
    def test_skip_symbols_below_min_rows(self, tmp_path):
        good = make_symbol(80, 0).reset_index()
        short = make_symbol(50, 8).reset_index()
        good.to_parquet(tmp_path / "GOOD.parquet")
        short.to_parquet(tmp_path / "SHORT.parquet")
        out = load_symbol_frames(["GOOD", "SHORT", "MISSING"], tmp_path, min_rows=70)
        assert set(out) == {"GOOD"}
        assert list(out["GOOD"].columns) == ["open", "high", "low", "close", "volume"]

    def test_composite_index_deterministic(self):
        daily = {s: make_symbol(60, p) for s, p in [("A", 0), ("B", 10)]}
        c1 = composite_index(daily)
        c2 = composite_index(daily)
        assert c1.equals(c2)
        assert (c1["close"] > 0).all()
