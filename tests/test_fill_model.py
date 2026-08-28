"""Golden + property tests for the normative fill model (ARCHITECTURE_V1.1 §1.3)."""
from __future__ import annotations

import random
from datetime import datetime

import pytest

from sts.brokers.fillmodel import (
    Fill,
    SlippageModel,
    SpreadModel,
    entry_fill,
    exit_target_fill,
    resolve_bar,
    stop_fill,
)
from sts.contracts import Bar, Side

SPREAD = SpreadModel(half_spread_pct=0.0005)
SLIP = SlippageModel()


def bar(o: float, h: float, l: float, c: float, v: float = 10_000.0) -> Bar:
    return Bar(symbol="X", ts=datetime(2026, 8, 3, 9, 15), open=o, high=h,
               low=l, close=c, volume=v, timeframe="5m")


# ---------------------------------------------------------------- entries
def test_entry_touch_without_volume_is_no_fill() -> None:
    """(a) low == L with insufficient cumulative volume => NO fill."""
    f = entry_fill(100.0, Side.BUY, bar(100.5, 100.8, 100.0, 100.2),
                   cum_volume_at_touch=299.0, req_qty=100, spread=SPREAD)
    assert f is None


def test_entry_touch_with_volume_evidence_fills_at_limit_plus_half_spread() -> None:
    f = entry_fill(100.0, Side.BUY, bar(100.5, 100.8, 100.0, 100.2),
                   cum_volume_at_touch=300.0, req_qty=100, spread=SPREAD)
    assert f is not None
    assert f.px == pytest.approx(100.05)


def test_entry_strict_penetration_fills_even_on_thin_volume() -> None:
    """(b) low < L => fill at L + half_spread regardless of volume."""
    f = entry_fill(100.0, Side.BUY, bar(100.4, 100.6, 99.9, 100.1),
                   cum_volume_at_touch=1.0, req_qty=100, spread=SPREAD)
    assert f is not None
    assert f.px == pytest.approx(100.05)
    assert f.qty == 100


def test_entry_never_fills_above_limit_touch_only_high() -> None:
    f = entry_fill(99.0, Side.BUY, bar(100.0, 100.5, 99.0, 100.2),
                   cum_volume_at_touch=10_000.0, req_qty=100, spread=SPREAD)
    assert f.px == pytest.approx(round(99.0 * 1.0005, 2))


# ---------------------------------------------------------------- targets
def test_target_requires_strict_penetration() -> None:
    """(f) high == T grants nothing; high > T fills at T - half_spread."""
    assert exit_target_fill(102.0, Side.SELL, bar(101, 102.0, 100, 101.5), SPREAD) is None
    f = exit_target_fill(102.0, Side.SELL, bar(101, 102.4, 100, 102.1), SPREAD)
    assert f is not None
    assert f.px == pytest.approx(round(102.0 * 0.9995, 2))


# ---------------------------------------------------------------- stops
def test_gap_through_stop_fills_below_stop_at_open_based_price() -> None:
    """(d) open <= stop => full gap loss: px = open*(1-slip-halfspread)."""
    b = bar(93.0, 94.0, 92.5, 93.5)
    f = stop_fill(95.0, Side.SELL, b, SLIP, SPREAD)
    raw = 93.0 * (1 - SLIP.base_adverse_pct - SPREAD.half_spread_pct)
    assert f.px == pytest.approx(raw, abs=0.01)   # rounded DOWN to paise
    assert f.px < 95.0


def test_normal_stop_fill_at_stop_minus_costs() -> None:
    b = bar(98.0, 98.5, 94.0, 95.5)   # open above stop, low pierced it
    f = stop_fill(95.0, Side.SELL, b, SLIP, SPREAD)
    raw = 95.0 * (1 - SLIP.base_adverse_pct - SPREAD.half_spread_pct)
    assert f.px == pytest.approx(raw, abs=0.01)
    assert f.px <= 95.0


@pytest.mark.parametrize("low_vol", [False, True])
def test_stop_fill_never_above_stop_property(low_vol: bool) -> None:
    b = bar(98.0, 98.5, 94.0, 95.5)
    f = stop_fill(95.0, Side.SELL, b, SlippageModel(), SPREAD, low_vol_bar=low_vol)
    assert f.px <= 95.0


def test_stop_invariant_property_over_random_bars() -> None:
    """(e) seeded property: over 500 random bars the stop NEVER fills above stop."""
    rng = random.Random(42)
    for _ in range(500):
        o = rng.uniform(80, 120)
        lo = o - rng.uniform(0, 8)
        hi = o + rng.uniform(0, 8)
        b = bar(o, hi, lo, rng.uniform(lo, hi), v=rng.uniform(1, 50_000))
        f = stop_fill(100.0, Side.SELL, b, SLIP, SPREAD,
                      low_vol_bar=rng.random() < 0.3)
        assert f.px <= 100.0, f"stop violated at open={o} low={lo} high={hi}"


# ---------------------------------------------------------------- sequencing
def test_same_bar_stop_and_target_adverse_sequencing() -> None:
    """(c) stop AND target inside one bar => STOP fires, target NOT credited."""
    b = bar(101.0, 104.0, 94.0, 100.0)   # range covers both 95 stop and 102 target
    events = resolve_bar(True, 95.0, b, SLIP, SPREAD,
                         target1=102.0, target2=None, t2_armed=False)
    reasons = [r for r, _ in events]
    assert reasons == ["STOP"]
    assert events[0][1].px <= 95.0


def test_resolve_bar_credits_targets_when_stop_untouched() -> None:
    b = bar(100.0, 107.0, 99.5, 106.0)
    # T2 not armed yet -> only T1
    ev = resolve_bar(True, 95.0, b, SLIP, SPREAD, target1=102.0, target2=106.0,
                     t2_armed=False)
    assert [r for r, _ in ev] == ["TARGET1"]
    # armed -> T2 also credited
    ev = resolve_bar(True, 95.0, b, SLIP, SPREAD, target1=None, target2=106.0,
                     t2_armed=True)
    assert [r for r, _ in ev] == ["TARGET2"]


def test_resolve_bar_no_events_quiet_bar() -> None:
    b = bar(100.0, 100.6, 99.5, 100.2)
    assert resolve_bar(True, 95.0, b, SLIP, SPREAD, target1=102.0) == []


def test_trailing_stop_is_callers_concern() -> None:
    """Fill model must resolve whatever stop level it is given — no future data."""
    b = bar(100.0, 100.5, 98.0, 98.5)
    ev = resolve_bar(True, 99.0, b, SLIP, SPREAD, stop_reason="TRAIL_STOP")
    assert [r for r, _ in ev] == ["TRAIL_STOP"]
