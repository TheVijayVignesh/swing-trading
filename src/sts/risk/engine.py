"""Hard-authority pre-order risk engine (ARCHITECTURE.md §11, V1.1 small tier).

Checks run in a FIXED order; ML scores are structurally ignored — nothing in
this module reads ml_score, so no model output can flip any verdict.
"""
from __future__ import annotations

import math
from typing import Any

from sts.config import SessionConfig
from sts.contracts import PortfolioState, RiskCheck, RiskVerdict

CHECK_ORDER = [
    "qty_sizing",
    "min_notional",
    "max_positions",
    "total_open_risk",
    "position_cap",
    "gross_exposure",
    "daily_loss_limit",
    "drawdown_kill",
    "adv_size",
]

ADV_MAX_FRACTION = 0.005  # qty <= 0.5% of avg daily volume


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def evaluate(
    intent: Any,
    portfolio: PortfolioState,
    cfg: SessionConfig,
    day_pnl: float,
    hwm: float,
    *,
    avg_daily_volume: float | None = None,
) -> RiskVerdict:
    """Veto a trade intent against hard constraints.

    `intent` is TradeIntent-like: needs limit_price (entry), stop_px.
    `portfolio` is the current PortfolioState BEFORE the trade.
    """
    equity = float(portfolio.equity)
    entry = float(_get(intent, "limit_price", _get(intent, "entry_price", 0.0)))
    stop = _get(intent, "stop_px")
    stop = float(stop) if stop is not None else float("nan")

    risk_amt = cfg.risk_per_trade * equity
    per_share = entry - stop

    checks: list[RiskCheck] = []
    reasons: list[str] = []

    # 1. qty_sizing ---------------------------------------------------------
    if math.isnan(stop) or per_share <= 0:
        qty = 0
        checks.append(RiskCheck("qty_sizing", f"entry-stop>0 (risk/trade={cfg.risk_per_trade:.3%})",
                                f"entry={entry:.2f} stop={stop}", False))
        reasons.append("qty_sizing")
    else:
        qty = int(math.floor(risk_amt / per_share))
        ok = qty >= 1
        checks.append(RiskCheck("qty_sizing", f"qty>=1 (risk_amt={risk_amt:.2f}, per_share={per_share:.2f})",
                                f"qty={qty}", ok))
        if not ok:
            reasons.append("qty_sizing")

    # 2. min_notional -------------------------------------------------------
    notional = qty * entry
    mn_ok = qty >= 1 and notional >= cfg.min_notional
    checks.append(RiskCheck("min_notional", f"qty*entry >= {cfg.min_notional:.0f}",
                            f"{notional:.2f}", bool(mn_ok)))
    if not mn_ok:
        reasons.append("min_notional")

    # 3. max_positions ------------------------------------------------------
    open_count = len(portfolio.positions)
    mp_ok = open_count < cfg.max_positions
    checks.append(RiskCheck("max_positions", f"open < {cfg.max_positions}",
                            f"open={open_count}", bool(mp_ok)))
    if not mp_ok:
        reasons.append("max_positions")

    # 4. total_open_risk ----------------------------------------------------
    existing_risk = float(portfolio.total_open_risk)
    tor_ok = existing_risk + risk_amt <= cfg.max_total_open_risk * equity + 1e-9
    checks.append(RiskCheck("total_open_risk", f"existing+new <= {cfg.max_total_open_risk:.2%} of equity",
                            f"{existing_risk + risk_amt:.2f} vs {cfg.max_total_open_risk * equity:.2f}",
                            bool(tor_ok)))
    if not tor_ok:
        reasons.append("total_open_risk")

    # 5. position_cap -------------------------------------------------------
    cap_notional = cfg.max_position_pct * equity
    pc_ok = notional <= cap_notional + 1e-9
    checks.append(RiskCheck("position_cap", f"qty*entry <= {cfg.max_position_pct:.2%} of equity",
                            f"{notional:.2f} vs {cap_notional:.2f}", bool(pc_ok)))
    if not pc_ok:
        reasons.append("position_cap")

    # 6. gross_exposure -----------------------------------------------------
    gross_after = float(portfolio.gross_exposure) + notional
    ge_ok = gross_after <= cfg.max_gross_exposure * equity + 1e-9
    checks.append(RiskCheck("gross_exposure", f"gross+new <= {cfg.max_gross_exposure:.2%} of equity",
                            f"{gross_after:.2f} vs {cfg.max_gross_exposure * equity:.2f}", bool(ge_ok)))
    if not ge_ok:
        reasons.append("gross_exposure")

    # 7. daily_loss_limit ---------------------------------------------------
    dl_ok = day_pnl > -cfg.daily_loss_limit * equity
    checks.append(RiskCheck("daily_loss_limit", f"day_pnl > -{cfg.daily_loss_limit:.2%} of equity",
                            f"day_pnl={day_pnl:.2f}", bool(dl_ok)))
    if not dl_ok:
        reasons.append("daily_loss_limit")

    # 8. drawdown_kill ------------------------------------------------------
    dd_ok = True
    if hwm > 0:
        dd = (hwm - equity) / hwm
        dd_ok = dd <= cfg.drawdown_kill
        dd_obs = f"drawdown={dd:.4f}"
    else:
        dd_obs = "hwm<=0 -> fail closed"
    checks.append(RiskCheck("drawdown_kill", f"(hwm-equity)/hwm <= {cfg.drawdown_kill:.2%}",
                            dd_obs, bool(dd_ok)))
    if not dd_ok:
        reasons.insert(0, "DRAWDOWN_KILL")  # special flag takes precedence

    # 9. adv_size -----------------------------------------------------------
    if avg_daily_volume is None or avg_daily_volume <= 0:
        adv_ok = False
        adv_obs = "ADV missing/invalid -> fail closed"
        max_qty_adv = 0.0
    else:
        max_qty_adv = ADV_MAX_FRACTION * float(avg_daily_volume)
        adv_ok = qty <= max_qty_adv
        adv_obs = f"qty={qty} vs {max_qty_adv:.1f}"
    checks.append(RiskCheck("adv_size", f"qty <= {ADV_MAX_FRACTION:.1%} of ADV",
                            adv_obs, bool(adv_ok)))
    if not adv_ok:
        reasons.append("adv_size")

    assert [c.check for c in checks] == CHECK_ORDER, "check order is normative"
    approved = all(c.passed for c in checks)
    rejection_reason = "" if approved else (reasons[0] if reasons else "REJECTED")
    return RiskVerdict(approved=approved, checks=checks, rejection_reason=rejection_reason)
