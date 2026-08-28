"""Greedy constrained portfolio selection (ARCHITECTURE.md §10).

Candidates must arrive scored descending. Selection re-applies the risk caps
plus diversification constraints; every skip yields a machine-readable reason.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from sts.config import SessionConfig


@dataclass(slots=True)
class ScoredCandidate:
    symbol: str
    score: float
    entry_price: float
    stop_px: float
    qty: int
    risk_amount: float
    notional: float


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def select(
    candidates: list[ScoredCandidate],
    open_positions: list[Any],
    corr_matrix_fn: Callable[[str, str], float],
    sector_fn: Callable[[str], str],
    equity: float,
    cfg: SessionConfig,
) -> tuple[list[ScoredCandidate], list[tuple[str, str]]]:
    """Greedily admit candidates. Returns (selected, rejections).

    corr_matrix_fn(a, b) -> Pearson corr of 60d daily returns (NaN => fail closed).
    open_positions items need .symbol, .risk_amount, .notional (PositionView adapter).
    """
    selected: list[ScoredCandidate] = []
    rejections: list[tuple[str, str]] = []

    held_symbols = [str(_get(p, "symbol")) for p in open_positions]
    total_risk = sum(float(_get(p, "risk_amount", 0.0)) for p in open_positions)
    sector_counts: dict[str, int] = {}
    sector_notional: dict[str, float] = {}
    for p in open_positions:
        sec = str(sector_fn(str(_get(p, "symbol"))))
        sector_counts[sec] = sector_counts.get(sec, 0) + 1
        sector_notional[sec] = sector_notional.get(sec, 0.0) + float(_get(p, "notional", 0.0))

    admitted_symbols = set(held_symbols)

    for cand in candidates:
        sym = cand.symbol
        sec = str(sector_fn(sym))

        if any(sym == s for s in [*held_symbols, *(c.symbol for c in selected)]):
            rejections.append((sym, "DUPLICATE"))
            continue
        if len(open_positions) + len(selected) >= cfg.max_positions:
            rejections.append((sym, "MAX_POSITIONS"))
            continue
        if total_risk + cand.risk_amount > cfg.max_total_open_risk * equity:
            rejections.append((sym, "TOTAL_OPEN_RISK"))
            continue
        if cand.notional > cfg.max_position_pct * equity:
            rejections.append((sym, "POSITION_CAP"))
            continue

        corr_fail = False
        for other in admitted_symbols:
            rho = float(corr_matrix_fn(sym, other))
            if rho != rho or rho > 0.7:  # NaN or too correlated
                corr_fail = True
                break
        if corr_fail:
            rejections.append((sym, "CORRELATION"))
            continue

        if sector_counts.get(sec, 0) >= 2:
            rejections.append((sym, "SECTOR_COUNT"))
            continue
        if sector_notional.get(sec, 0.0) + cand.notional > 0.40 * equity:
            rejections.append((sym, "SECTOR_EXPOSURE"))
            continue

        selected.append(cand)
        admitted_symbols.add(sym)
        total_risk += cand.risk_amount
        sector_counts[sec] = sector_counts.get(sec, 0) + 1
        sector_notional[sec] = sector_notional.get(sec, 0.0) + cand.notional

    return selected, rejections
