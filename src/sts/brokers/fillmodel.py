"""Normative paper fill model (ARCHITECTURE_V1.1 §1.3) — PURE functions.

Shared by BOTH the backtest engine and PaperBroker; divergence between the two
is a bug class. Rules implemented literally:

- Entry BUY LIMIT @ L: fills iff bar.low < L (strict trade-through) OR
  (bar.low == L AND cumulative volume since submission >= 3 * qty).
  Fill px pays the half-spread even on a limit.
- Exit target @ T: fills iff bar.high > T (strict); px = T - half-spread.
- Stop: if bar.open <= stop_px (gapped through) fill at open - slippage -
  half-spread (full gap loss taken); else at stop_px - slippage - half-spread.
  INVARIANT: a stop NEVER fills above stop_px (asserted here, tested as property).
- Same-bar stop + target: ADVERSE SEQUENCING — stop fires first; targets are
  credited only from bars whose low did not touch the stop level.

All inputs explicit (no globals), returns are plain frozen dataclasses.
Trailing-stop updates belong to the CALLER at bar close — this module never
sees future bars.
"""
from __future__ import annotations

from dataclasses import dataclass

from sts.brokers.costs import round_paise
from sts.contracts import Bar, Side


@dataclass(frozen=True, slots=True)
class SpreadModel:
    """Half-spread paid on every fill (5 bps default)."""

    half_spread_pct: float = 0.0005


@dataclass(frozen=True, slots=True)
class SlippageModel:
    """Adverse slippage tiers; illiquid/low-volume bars pay more."""

    base_adverse_pct: float = 0.0005
    illiquid_pct: float = 0.001
    low_vol_multiplier: float = 2.0


@dataclass(frozen=True, slots=True)
class Fill:
    """A simulated fill. `qty` is the filled quantity (caller owns sizing)."""

    px: float
    qty: int = 0


def effective_slippage(slippage: SlippageModel, *, low_vol_bar: bool) -> float:
    """Slippage fraction for a bar; low-volume bars take the multiplied tier."""
    if low_vol_bar:
        return slippage.illiquid_pct * slippage.low_vol_multiplier
    return slippage.base_adverse_pct


def entry_fill(
    order_limit: float,
    side: Side,
    bar: Bar,
    cum_volume_at_touch: float,
    req_qty: int,
    spread: SpreadModel,
) -> Fill | None:
    """LIMIT entry against one bar. Strict trade-through, or touch with volume evidence."""
    if req_qty <= 0 or order_limit <= 0:
        return None
    hs = spread.half_spread_pct
    if side is Side.BUY:
        traded_through = bar.low < order_limit
        touch_with_volume = bar.low == order_limit and cum_volume_at_touch >= 3 * req_qty
        if not (traded_through or touch_with_volume):
            return None
        return Fill(px=round_paise(order_limit * (1.0 + hs)), qty=req_qty)
    # SELL limit (exit into bid / short entry): mirrored strict penetration.
    traded_through = bar.high > order_limit
    touch_with_volume = bar.high == order_limit and cum_volume_at_touch >= 3 * req_qty
    if not (traded_through or touch_with_volume):
        return None
    return Fill(px=round_paise(order_limit * (1.0 - hs)), qty=req_qty)


def exit_target_fill(target: float, side: Side, bar: Bar, spread: SpreadModel) -> Fill | None:
    """Target exit requires STRICT penetration of the target (high > T for longs)."""
    if target <= 0:
        return None
    hs = spread.half_spread_pct
    if side is Side.SELL:  # long position taking profit
        if not bar.high > target:
            return None
        return Fill(px=round_paise(target * (1.0 - hs)))
    if not bar.low < target:  # short position covering
        return None
    return Fill(px=round_paise(target * (1.0 + hs)))


def stop_fill(
    stop_px: float,
    side: Side,
    bar: Bar,
    slippage: SlippageModel,
    spread: SpreadModel,
    *,
    low_vol_bar: bool = False,
) -> Fill:
    """Stop execution. Gap-through bars fill AT THE OPEN minus adverse costs.

    INVARIANT (asserted): the stop never fills better than stop_px.
    Price is rounded DOWN to paise so rounding cannot flip the invariant.
    """
    slip = effective_slippage(slippage, low_vol_bar=low_vol_bar)
    hs = spread.half_spread_pct
    if side is Side.SELL:  # long protection
        base = bar.open if bar.open <= stop_px else stop_px
        raw = base * (1.0 - slip - hs)
        px = round_paise(raw, down=True)
        assert px <= stop_px, f"stop fill {px} above stop {stop_px}"
        return Fill(px=px)
    base = bar.open if bar.open >= stop_px else stop_px
    raw = base * (1.0 + slip + hs)
    px = round_paise(raw)  # short cover: never below stop
    assert px >= stop_px, f"stop cover {px} below stop {stop_px}"
    return Fill(px=px)


def resolve_bar(
    position_side_long: bool,
    stop_px: float,
    bar: Bar,
    slippage: SlippageModel,
    spread: SpreadModel,
    *,
    stop_reason: str = "STOP",
    target1: float | None = None,
    target2: float | None = None,
    t2_armed: bool = False,
    low_vol_bar: bool = False,
) -> list[tuple[str, Fill]]:
    """Resolve exits for ONE completed bar. Returns [(reason, Fill), ...].

    Normative adverse sequencing: if the stop level lies inside [low, high],
    the STOP fires first and NO target is credited from that bar. Targets
    require strict penetration. target2 is only considered when `t2_armed`
    (i.e. after the T1 partial has already happened in prior state).
    """
    side = Side.SELL if position_side_long else Side.BUY
    if position_side_long:
        stop_touched = bar.low <= stop_px
    else:
        stop_touched = bar.high >= stop_px
    if stop_touched and stop_px > 0:
        return [(stop_reason, stop_fill(stop_px, side, bar, slippage, spread, low_vol_bar=low_vol_bar))]

    events: list[tuple[str, Fill]] = []
    if target1 is not None:
        f = exit_target_fill(target1, side, bar, spread)
        if f is not None:
            events.append(("TARGET1", f))
    if target2 is not None and t2_armed:
        f = exit_target_fill(target2, side, bar, spread)
        if f is not None:
            events.append(("TARGET2", f))
    return events
