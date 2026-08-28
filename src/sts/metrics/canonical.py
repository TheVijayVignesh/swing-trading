"""Canonical session metrics — the SINGLE source of truth (CONTRACT ADDENDUM
v2). Every consumer (API summary/detail/compare, lab UI, reports) must
delegate here. Pure functions of (conn, session_id); no app state, no I/O
beyond the journal DB.

Definitions:
- equity curve: full-resolution account_snapshots anchored at capital_initial.
- current_equity: last snapshot's equity, else capital_initial.
- trades: closed positions joined to their fills inside [opened_at, closed_at].
- expectancy_r: mean R-multiple per trade (risk-normalized).
- sharpe/sortino: daily last-equity pct returns, rf=0, annualized sqrt(252).
- cagr_pct: (end/start)^(365.25/days) - 1, 0 when span < 1 day or start<=0.
- exposure_pct_avg: mean(invested/equity)*100 across snapshots.
- turnover: SUM(invested)/capital_initial over all snapshots.
- cost_drag: total fill costs / capital_initial * 100.

Shared keys are rounded exactly as the API contract specifies so parity is
bit-for-bit; extra fields (expectancy_r, sharpe, sortino, cagr_pct) are raw.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from typing import Any


# ------------------------------------------------------------------ internals
def capital(conn: sqlite3.Connection, sid: str) -> float:
    row = conn.execute(
        "SELECT capital_initial FROM sessions WHERE id=?", (sid,)).fetchone()
    cap = float(row["capital_initial"]) if row and row["capital_initial"] else 0.0
    return cap or 1.0  # never divide by zero


def equity_curve(conn: sqlite3.Connection, sid: str) -> list[tuple[str, float]]:
    rows = conn.execute(
        "SELECT ts, equity FROM account_snapshots WHERE session_id=? ORDER BY ts, id",
        (sid,),
    ).fetchall()
    return [(str(r["ts"]), float(r["equity"])) for r in rows]


def current_equity(conn: sqlite3.Connection, sid: str) -> float:
    row = conn.execute(
        "SELECT equity FROM account_snapshots WHERE session_id=?"
        " ORDER BY ts DESC, id DESC LIMIT 1", (sid,)).fetchone()
    if row is not None:
        return float(row["equity"])
    return capital(conn, sid)


def total_return_pct(conn: sqlite3.Connection, sid: str) -> float:
    return round((current_equity(conn, sid) / capital(conn, sid) - 1) * 100, 4)


def max_drawdown_pct(conn: sqlite3.Connection, sid: str) -> float:
    curve = equity_curve(conn, sid)
    if not curve:
        return 0.0
    hwm: float | None = None
    dd = 0.0
    for _, v in curve:
        hwm = v if hwm is None else max(hwm, v)
        if hwm > 0:
            dd = max(dd, (hwm - v) / hwm * 100)
    return round(dd, 4)


def closed_trades(conn: sqlite3.Connection, sid: str) -> list[dict[str, Any]]:
    """Trades from persisted data only: closed positions x their fills.

    NOTE: mirrors routes_api._trades semantics (entry/exit px are fill-weighted
    averages; costs prorated by quantity share).
    """
    positions = conn.execute(
        "SELECT * FROM positions WHERE session_id=? AND status='CLOSED' ORDER BY closed_at",
        (sid,),
    ).fetchall()
    out: list[dict] = []
    for p in positions:
        legs = conn.execute(
            "SELECT f.px, f.qty, f.cost_breakdown_json, f.ts, o.side FROM fills f"
            " JOIN orders o ON o.id=f.order_id WHERE f.session_id=? AND o.symbol=?"
            " AND f.ts >= ? AND f.ts <= ? ORDER BY f.ts",
            (sid, p["symbol"], p["opened_at"], p["closed_at"]),
        ).fetchall()
        buys = [(float(r["px"]), int(r["qty"]), json.loads(r["cost_breakdown_json"] or "{}"))
                for r in legs if r["side"] == "BUY"]
        sells = [(float(r["px"]), int(r["qty"]), json.loads(r["cost_breakdown_json"] or "{}"), r["ts"])
                 for r in legs if r["side"] == "SELL"]
        if not buys or not sells:
            continue
        bq = sum(q for _, q, _ in buys)
        sq = sum(q for _, q, _, _ in sells)
        qty = min(bq, sq)
        if qty == 0:
            continue
        entry_px = sum(px * q for px, q, _ in buys) / bq
        exit_px = sum(px * q for px, q, _, _ in sells) / sq
        buy_cost = sum(c.get("total", 0.0) * q / bq for px, q, c in buys)
        sell_cost = sum(c.get("total", 0.0) * q / sq for px, q, c, _ in sells)
        pnl = (exit_px - entry_px) * qty - buy_cost - sell_cost
        stop = float(p["stop"] or 0.0)
        risk_amt = (entry_px - stop) * qty if stop > 0 else 0.0
        held_days = _hold_days(p["opened_at"], p["closed_at"])
        out.append({
            "symbol": p["symbol"], "side": "LONG", "qty": qty,
            "entry_px": entry_px, "exit_px": exit_px,
            "entry_ts": p["opened_at"], "exit_ts": p["closed_at"],
            "pnl": pnl,
            "r_multiple": (pnl / risk_amt) if risk_amt > 0 else None,
            "hold_days": held_days,
            "exit_reason": p["exit_reason"],
            "costs": buy_cost + sell_cost,
        })
    return out


def _hold_days(opened: Any, closed: Any) -> int:
    try:
        d0 = datetime.fromisoformat(str(opened)).date()
        d1 = datetime.fromisoformat(str(closed)).date()
        return (d1 - d0).days
    except (ValueError, TypeError):
        return 0


def win_rate(conn: sqlite3.Connection, sid: str) -> float | None:
    trades = closed_trades(conn, sid)
    if not trades:
        return None
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return round(wins / len(trades), 4)


def profit_factor(conn: sqlite3.Connection, sid: str) -> float | None:
    """gp/gl; gl==0 -> gp if any wins else None (API-compatible)."""
    wins, losses = _win_loss(conn, sid)
    gp, gl = sum(wins), abs(sum(losses))
    if gl > 0:
        return round(gp / gl, 4)
    return None if not wins else round(gp, 2)


def _win_loss(conn, sid) -> tuple[list[float], list[float]]:
    pnls = [t["pnl"] for t in closed_trades(conn, sid)]
    return ([p for p in pnls if p > 0], [p for p in pnls if p <= 0])


def expectancy_r(conn: sqlite3.Connection, sid: str) -> float | None:
    """Mean R-multiple across closed trades (None without trades/R data)."""
    rs = [t["r_multiple"] for t in closed_trades(conn, sid) if t["r_multiple"] is not None]
    if not rs:
        return None
    return sum(rs) / len(rs)


def avg_win(conn: sqlite3.Connection, sid: str) -> float | None:
    wins, _ = _win_loss(conn, sid)
    return round(sum(wins) / len(wins), 2) if wins else None


def avg_loss(conn: sqlite3.Connection, sid: str) -> float | None:
    """Average LOSS as a NEGATIVE number (API convention: -gl/n)."""
    _, losses = _win_loss(conn, sid)
    return round(sum(losses) / len(losses), 2) if losses else None


def avg_hold_days(conn: sqlite3.Connection, sid: str) -> float | None:
    trades = closed_trades(conn, sid)
    if not trades:
        return None
    return round(sum(t["hold_days"] for t in trades) / len(trades), 2)


def _daily_returns(conn: sqlite3.Connection, sid: str) -> list[float]:
    by_day: dict[str, float] = {}
    for ts, eq in equity_curve(conn, sid):
        by_day[ts[:10]] = eq  # last snapshot of each calendar day wins
    eqs = [by_day[d] for d in sorted(by_day)]
    out = []
    for a, b in zip(eqs, eqs[1:]):
        if a > 0:
            out.append(b / a - 1)
    return out


def sharpe(conn: sqlite3.Connection, sid: str, rf: float = 0.0) -> float | None:
    rets = _daily_returns(conn, sid)
    n = len(rets)
    if n < 2:
        return None
    excess = [r - rf / 252.0 for r in rets]
    mean = sum(excess) / n
    var = sum((r - mean) ** 2 for r in excess) / (n - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return None
    return round(mean / sd * math.sqrt(252), 4)


def sortino(conn: sqlite3.Connection, sid: str, rf: float = 0.0) -> float | None:
    rets = _daily_returns(conn, sid)
    n = len(rets)
    if n < 2:
        return None
    excess = [r - rf / 252.0 for r in rets]
    mean = sum(excess) / n
    downside = [r for r in excess if r < 0]
    if not downside:
        return None
    dd = math.sqrt(sum(r ** 2 for r in downside) / len(downside))
    if dd == 0:
        return None
    return round(mean / dd * math.sqrt(252), 4)


def cagr_pct(conn: sqlite3.Connection, sid: str) -> float | None:
    curve = equity_curve(conn, sid)
    if not curve:
        return None
    start_v, end_v = curve[0][1], curve[-1][1]
    try:
        d0 = datetime.fromisoformat(curve[0][0])
        d1 = datetime.fromisoformat(curve[-1][0])
    except ValueError:
        return None
    days = (d1 - d0).total_seconds() / 86400.0
    if days < 1.0 or start_v <= 0 or end_v <= 0:
        return None
    return round(((end_v / start_v) ** (365.25 / days) - 1) * 100, 4)


def exposure_pct_avg(conn: sqlite3.Connection, sid: str) -> float:
    row = conn.execute(
        "SELECT AVG(CASE WHEN ? > 0 THEN invested / NULLIF(equity,0) END)*100 AS expo"
        " FROM account_snapshots WHERE session_id=?",
        (1, sid),
    ).fetchone()
    return round(float(row["expo"] or 0.0), 4) if row else 0.0


def turnover(conn: sqlite3.Connection, sid: str) -> float:
    row = conn.execute(
        "SELECT SUM(invested) AS inv FROM account_snapshots WHERE session_id=?",
        (sid,),
    ).fetchone()
    inv = float(row["inv"] or 0.0) if row else 0.0
    return round(inv / capital(conn, sid), 4)


def cost_drag(conn: sqlite3.Connection, sid: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(CAST(json_extract(cost_breakdown_json,'$.total') AS REAL)),0)"
        " AS c FROM fills WHERE session_id=?", (sid,)
    ).fetchone()
    return round(float(row["c"] if row else 0.0) / capital(conn, sid) * 100, 6)


def compare_metrics(conn: sqlite3.Connection, sid: str) -> dict[str, Any]:
    """The /api/lab/compare metrics block (contract v1 + addendum v2 adds)."""
    return {
        "return_pct": total_return_pct(conn, sid),
        "max_dd_pct": max_drawdown_pct(conn, sid),
        "win_rate": win_rate(conn, sid),
        "pf": profit_factor(conn, sid),
        "expectancy": _expectancy_currency(conn, sid),
        "avg_win": avg_win(conn, sid),
        "avg_loss": avg_loss(conn, sid),
        "avg_hold_days": avg_hold_days(conn, sid),
        "turnover": turnover(conn, sid),
        "exposure_pct": exposure_pct_avg(conn, sid),
        "cost_drag": cost_drag(conn, sid),
        # addendum v2 additions
        "sharpe": sharpe(conn, sid),
        "sortino": sortino(conn, sid),
        "cagr_pct": cagr_pct(conn, sid),
    }


def _expectancy_currency(conn, sid) -> float | None:
    trades = closed_trades(conn, sid)
    if not trades:
        return None
    gp = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
    return round((gp - gl) / len(trades), 2)


def summary_metrics(conn: sqlite3.Connection, sid: str) -> dict[str, Any]:
    """Per-session card metrics for /api/lab/summary."""
    eq = current_equity(conn, sid)
    cap = capital(conn, sid)
    trades = closed_trades(conn, sid)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return {
        "equity": round(eq, 2),
        "pnl_abs": round(eq - cap, 2),
        "return_pct": round((eq / cap - 1) * 100, 4) if cap else 0.0,
        "max_dd_pct": max_drawdown_pct(conn, sid),
        "trades": len(trades),
        "wins": wins,
        "win_rate": round(wins / len(trades), 4) if trades else None,
    }
