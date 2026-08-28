"""Control-plane REST API — implements docs/API_CONTRACT.md EXACTLY (binding).

All endpoints are async so SQLite access stays on the uvicorn event-loop
thread (the journal connection is created there).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from sts.lab.manager import LabFullError, LifecycleError
from sts.strategy.registry import STRATEGIES

router = APIRouter()

VALID_RISK_PROFILES = ("micro", "small", "standard")
VALID_STOP_POLICIES = ("FLATTEN", "HOLD")


# ------------------------------------------------------------------ models
class CreateSessionBody(BaseModel):
    model_config = ConfigDict(extra="ignore")   # unknown fields (e.g. symbol) ignored
    name: str
    capital_initial: int
    mode: str = "paper"
    universe: str = "NIFTY200"
    strategy_id: str = "pullback-v1"
    risk_profile: str | None = None            # None => auto-tier by capital (micro < ₹30k)
    ml_enabled: bool = False
    on_stop_policy: str = "FLATTEN"
    params: dict = Field(default_factory=dict)          # strategy parameter overrides
    risk_overrides: dict = Field(default_factory=dict)  # risk hyperparameter overrides


class CloneBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str | None = None
    overrides: dict | None = None


class StopBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    policy: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ helpers
def _last_snapshot(conn: sqlite3.Connection, sid: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM account_snapshots WHERE session_id=? ORDER BY ts DESC, id DESC LIMIT 1",
        (sid,),
    ).fetchone()
    return dict(row) if row else None


def _trades(conn: sqlite3.Connection, sid: str) -> list[dict]:
    """Closed trades joined from positions + fills (persisted data only)."""
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
        buys = [(float(r["px"]), int(r["qty"]), json.loads(r["cost_breakdown_json"] or "{}")) for r in legs if r["side"] == "BUY"]
        sells = [(float(r["px"]), int(r["qty"]), json.loads(r["cost_breakdown_json"] or "{}"), r["ts"]) for r in legs if r["side"] == "SELL"]
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
        held_days = 0
        try:
            d0 = datetime.fromisoformat(str(p["opened_at"])).date()
            d1 = datetime.fromisoformat(str(p["closed_at"])).date()
            held_days = (d1 - d0).days
        except ValueError:
            pass
        out.append({
            "symbol": p["symbol"], "side": "LONG", "qty": qty,
            "entry_px": round(entry_px, 2), "exit_px": round(exit_px, 2),
            "entry_ts": p["opened_at"], "exit_ts": p["closed_at"],
            "pnl": round(pnl, 2),
            "r_multiple": round(pnl / risk_amt, 3) if risk_amt > 0 else None,
            "hold_days": held_days,
            "entry_reason": str(p["strategy_version"] or ""),
            "exit_reason": p["exit_reason"],
            "costs": round(buy_cost + sell_cost, 2),
        })
    return out


def _metrics(curve: list[tuple[str, float]], trades: list[dict],
             conn: sqlite3.Connection, sid: str) -> dict:
    ret_pct = 0.0
    max_dd = 0.0
    if curve:
        eq = [v for _, v in curve]
        ret_pct = (eq[-1] / eq[0] - 1) * 100 if eq[0] else 0.0
        hwm = eq[0]
        for v in eq:
            hwm = max(hwm, v)
            if hwm > 0:
                max_dd = max(max_dd, (hwm - v) / hwm * 100)
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    gp = sum(wins)
    gl = abs(sum(losses))
    n = len(trades)
    row = conn.execute(
        "SELECT AVG(CASE WHEN ? > 0 THEN invested / NULLIF(equity,0) END)*100 AS expo,"
        " SUM(invested) AS inv FROM account_snapshots WHERE session_id=?",
        (1, sid),
    ).fetchone()
    cost_row = conn.execute(
        "SELECT COALESCE(SUM(CAST(json_extract(cost_breakdown_json,'$.total') AS REAL)),0) AS c"
        " FROM fills WHERE session_id=?", (sid,)
    ).fetchone()
    cap_row = conn.execute("SELECT capital_initial FROM sessions WHERE id=?", (sid,)).fetchone()
    cap = float(cap_row["capital_initial"]) if cap_row and cap_row["capital_initial"] else 1.0
    turnover = float(row["inv"] or 0.0) / cap if row else 0.0
    return {
        "return_pct": round(ret_pct, 4),
        "max_dd_pct": round(max_dd, 4),
        "win_rate": round(len(wins) / n, 4) if n else None,
        "pf": round(gp / gl, 4) if gl > 0 else (None if not wins else round(gp, 2)),
        "expectancy": round((gp - gl) / n, 2) if n else None,
        "avg_win": round(gp / len(wins), 2) if wins else None,
        "avg_loss": round(-gl / len(losses), 2) if losses else None,
        "avg_hold_days": round(sum(t["hold_days"] for t in trades) / n, 2) if n else None,
        "turnover": round(turnover, 4),
        "exposure_pct": round(float(row["expo"] or 0.0), 4) if row else 0.0,
        "cost_drag": round(float(cost_row["c"] if cost_row else 0.0) / cap * 100, 6),
    }


def _session_summary(request: Request, row: dict) -> dict | None:
    conn = request.app.state.conn
    lab = request.app.state.lab
    sid = row["id"]
    snap = _last_snapshot(conn, sid)
    trades = _trades(conn, sid)
    equity = float(snap["equity"]) if snap else float(row["capital_initial"])
    curve_rows = conn.execute(
        "SELECT ts, equity FROM account_snapshots WHERE session_id=? ORDER BY ts, id",
        (sid,),
    ).fetchall()
    sparkline = [round(float(r["equity"]), 2) for r in curve_rows[-60:]]
    max_dd = 0.0
    hwm = None
    for r in curve_rows:
        v = float(r["equity"])
        hwm = v if hwm is None else max(hwm, v)
        if hwm:
            max_dd = max(max_dd, (hwm - v) / hwm * 100)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    last_dec = conn.execute(
        "SELECT MAX(ts) AS t FROM intents WHERE session_id=?", (sid,)
    ).fetchone()["t"]
    runner = lab.runners.get(sid)
    faulted = bool(runner and runner.faulted) if runner else False
    stale_ep = bool(runner and runner.health == "stale") if runner else False
    open_positions = conn.execute(
        "SELECT COUNT(*) AS n FROM positions WHERE session_id=? AND status='OPEN'", (sid,)
    ).fetchone()["n"]
    return {
        "id": sid,
        "name": row["name"],
        "status": row["status"],
        "terminal_state": row["terminal_state"],
        "capital_initial": float(row["capital_initial"]),
        "equity": round(equity, 2),
        "pnl_abs": round(equity - float(row["capital_initial"]), 2),
        "return_pct": round((equity / float(row["capital_initial"]) - 1) * 100, 4)
        if row["capital_initial"] else 0.0,
        "max_dd_pct": round(max_dd, 4),
        "trades": len(trades),
        "wins": wins,
        "win_rate": round(wins / len(trades), 4) if trades else None,
        "open_positions": open_positions,
        "strategy_id": row["strategy_id"],
        "ml_enabled": bool(row["ml_model_id"] and row["ml_model_id"] != "deterministic"),
        "last_decision_at": last_dec,
        "health": "faulted" if faulted else ("stale" if stale_ep else "ok"),
        "sparkline": sparkline,
    }


def _count_incidents_since(conn: sqlite3.Connection, cutoff_iso: str) -> int:
    """Incidents in [cutoff, ∞) — ts column holds UTC ISO strings, so string
    comparison is chronological (canonical storage standard, see
    docs/API_CONTRACT.md)."""
    return int(conn.execute(
        "SELECT COUNT(*) AS n FROM incidents WHERE ts >= ?", (cutoff_iso,)
    ).fetchone()["n"])


def feed_degraded(feed_health: dict | None) -> bool:
    """Pure degraded-flag for the health strip: True iff any of
      - dropped_events > 0 (queue overflow lost bar events),
      - fallback.status in {FAILED, DEGRADED},
      - state == STALE (STALE only occurs while market phase is OPEN).
    Deliberate non-degraded treatment: fallback.status in {COOLDOWN, IDLE,
    UNKNOWN} and feed CLOSED outside market hours are NOT degraded — a
    benched fallback is not an active outage, and "market is closed" is the
    normal end-of-day state. Missing/None input is NOT degraded
    (unknown ≠ unhealthy)."""
    if not isinstance(feed_health, dict):
        return False
    try:
        if int(feed_health.get("dropped_events") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    fallback = feed_health.get("fallback")
    if isinstance(fallback, dict) and \
            str(fallback.get("status") or "").upper() in {"FAILED", "DEGRADED"}:
        return True
    return str(feed_health.get("state") or "").upper() == "STALE"


def _system_block(request: Request) -> dict:
    md = request.app.state.marketdata
    conn = request.app.state.conn
    lab = request.app.state.lab
    db_ok = True
    try:
        conn.execute("SELECT 1").fetchone()
    except sqlite3.Error:
        db_ok = False
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    incidents = _count_incidents_since(conn, cutoff)
    running = sum(1 for s in lab.sessions.list_sessions() if s["status"] == "RUNNING")
    feed_health = None
    md_feed_health = getattr(md, "feed_health", None)
    if callable(md_feed_health):
        try:
            feed_health = md_feed_health()
        except Exception:  # noqa: BLE001 — health endpoint must never 500
            feed_health = None
    return {
        "feed": md.feed_status(),
        "last_tick_age_s": md.last_tick_age_s,
        "sessions_running": running,
        "db_ok": db_ok,
        "incidents_24h": int(incidents),
        "heartbeat": _now_iso(),
        "feed_health": feed_health,
        "feed_degraded": feed_degraded(feed_health),
    }


# ------------------------------------------------------------------ endpoints
@router.get("/api/lab/summary")
async def lab_summary(request: Request):
    conn = request.app.state.conn
    rows = request.app.state.lab.sessions.list_sessions()
    sessions = []
    for row in rows:
        s = _session_summary(request, row)
        if s is not None:
            sessions.append(s)
    best = None
    candidates = [
        s for s in sessions
        if s["status"] in ("RUNNING", "PAUSED") and len(s["sparkline"]) >= 1
    ]
    if candidates:
        best = max(candidates, key=lambda s: s["return_pct"])
    return {"sessions": sessions, "best": best, "system": _system_block(request)}


@router.post("/api/sessions", status_code=201)
async def create_session(body: CreateSessionBody, request: Request):
    if not isinstance(body.capital_initial, int) or body.capital_initial < 1000:
        raise HTTPException(400, "capital_initial must be an integer >= 1000")
    if body.mode != "paper":
        raise HTTPException(403, "LIVE_INTERLOCKED")
    if body.strategy_id not in STRATEGIES:
        raise HTTPException(400, f"unknown strategy_id {body.strategy_id!r}")
    risk_profile = body.risk_profile
    if risk_profile is None:
        # auto-tier: micro for small accounts unless the caller chose explicitly
        risk_profile = "micro" if body.capital_initial < 30000 else "standard"
    if risk_profile not in VALID_RISK_PROFILES:
        raise HTTPException(400, f"risk_profile must be one of {VALID_RISK_PROFILES}")
    if body.on_stop_policy not in VALID_STOP_POLICIES:
        raise HTTPException(400, f"on_stop_policy must be one of {VALID_STOP_POLICIES}")
    from sts.config import SessionConfig
    # merge addendum-v2 risk_overrides into params (explicit values win)
    params = dict(body.params or {})
    for k, v in (body.risk_overrides or {}).items():
        params[k] = v
    cfg = SessionConfig(
        name=body.name, capital_initial=float(body.capital_initial), mode=body.mode,
        universe=body.universe, strategy_id=body.strategy_id,
        risk_profile=risk_profile, ml_enabled=body.ml_enabled,
        on_stop_policy=body.on_stop_policy, params=params,
    )
    try:
        sid = request.app.state.lab.create_session(cfg)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"cannot create session: {exc}") from exc
    row = request.app.state.lab.sessions.get_session(sid)
    return {"id": sid, **{k: row[k] for k in ("name", "status", "mode", "capital_initial")}}


@router.get("/api/sessions/{sid}")
async def session_detail(sid: str, request: Request):
    conn = request.app.state.conn
    lab = request.app.state.lab
    row = lab.sessions.get_session(sid)
    if row is None:
        raise HTTPException(404, "unknown session")
    from sts.config import from_yaml
    cfg = from_yaml(row["config_yaml"])

    graph = lab.graphs.get(sid)
    snap = _last_snapshot(conn, sid)
    positions_out: list[dict] = []
    portfolio = {}
    if graph is not None:
        st = graph.broker.get_account_state(sid)
        portfolio = {
            "cash": round(st.cash, 2), "invested": round(st.invested, 2),
            "unrealized": round(st.unrealized, 2), "realized": round(st.realized, 2),
            "equity": round(st.equity, 2), "hwm": round(st.hwm, 2) if st.hwm else None,
            "drawdown_pct": round(st.drawdown_pct, 4),
            "gross_exposure": round(st.gross_exposure, 2),
            "total_open_risk": round(st.total_open_risk, 2),
        }
        for p in st.positions:
            positions_out.append({
                "symbol": p.symbol, "qty": p.qty, "avg_entry": round(p.avg_entry, 2),
                "last_px": round(p.last_px, 2), "stop_px": round(p.stop_px, 2),
                "target1_px": round(p.target1_px, 2) if p.target1_px else None,
                "target2_px": round(p.target2_px, 2) if p.target2_px else None,
                "trail_px": round(p.trail_px, 2) if p.trail_px else None,
                "unrealized_pnl": round(p.unrealized_pnl, 2),
                "pnl_pct": round(p.pnl_pct, 4),
                "held_days": p.held_days,
                "risk_amount": round(p.risk_amount, 2),
                "t1_done": p.t1_done,
            })
    elif snap is not None:
        portfolio = {
            "cash": round(float(snap["cash"]), 2), "invested": round(float(snap["invested"]), 2),
            "unrealized": round(float(snap["unrealized"]), 2),
            "realized": round(float(snap["realized"]), 2), "equity": round(float(snap["equity"]), 2),
            "hwm": round(float(snap["hwm"]), 2), "drawdown_pct": round(float(snap["drawdown"]), 4),
            "gross_exposure": None, "total_open_risk": None,
        }

    curve_rows = conn.execute(
        "SELECT ts, equity, drawdown FROM account_snapshots WHERE session_id=? ORDER BY ts, id",
        (sid,),
    ).fetchall()
    equity_curve = [[r["ts"], round(float(r["equity"]), 2)] for r in curve_rows]
    drawdown_curve = [[r["ts"], round(float(r["drawdown"]), 4)] for r in curve_rows]

    funnel = conn.execute(
        "SELECT detail_json, ts FROM session_events WHERE session_id=? AND event='SCAN_FUNNEL'"
        " ORDER BY id DESC LIMIT 1", (sid,)
    ).fetchone()
    funnel_latest = None
    if funnel is not None:
        d = json.loads(funnel["detail_json"])
        funnel_latest = {k: d.get(k) for k in
                         ("ts", "scanned", "eligible", "setups", "ml_passed",
                          "portfolio_ok", "risk_ok", "selected")}
        funnel_latest["ts"] = funnel["ts"]

    intents = conn.execute(
        "SELECT id, ts, symbol, decision, rejection_reason, feature_vector_json FROM intents"
        " WHERE session_id=? ORDER BY ts DESC, id DESC LIMIT 50", (sid,)
    ).fetchall()
    decisions = []
    for r in intents:
        score = None
        try:
            score = json.loads(r["feature_vector_json"]).get("score")
        except (json.JSONDecodeError, TypeError):
            pass
        decisions.append({
            "intent_id": r["id"], "ts": r["ts"], "symbol": r["symbol"],
            "action": r["decision"], "score": score,
            "rejection_reason": r["rejection_reason"] or None,
        })

    runner = lab.runners.get(sid)
    health = "ok"
    if runner is not None:
        health = "faulted" if runner.faulted else ("stale" if runner.health == "stale" else "ok")

    last_dec = decisions[0]["ts"] if decisions else None
    return {
        "id": sid, "name": row["name"], "status": row["status"],
        "terminal_state": row["terminal_state"],
        "capital_initial": float(row["capital_initial"]),
        "config": cfg.model_dump(),
        "portfolio": portfolio,
        "positions": positions_out,
        "trades": _trades(conn, sid),
        "equity_curve": equity_curve,
        "drawdown_curve": drawdown_curve,
        "funnel_latest": funnel_latest,
        "decisions": decisions,
        "last_decision_at": last_dec,
        "feed_status": request.app.state.marketdata.feed_status(),
        "health": health,
    }


@router.post("/api/sessions/{sid}/start")
async def start_session(sid: str, request: Request):
    return await _lifecycle(request, sid, "start")


@router.post("/api/sessions/{sid}/pause")
async def pause_session(sid: str, request: Request):
    return await _lifecycle(request, sid, "pause")


@router.post("/api/sessions/{sid}/resume")
async def resume_session(sid: str, request: Request):
    return await _lifecycle(request, sid, "resume")


@router.post("/api/sessions/{sid}/stop")
async def stop_session(sid: str, request: Request, body: StopBody | None = None):
    policy = body.policy if body else None
    return await _lifecycle(request, sid, "stop", policy)


async def _lifecycle(request: Request, sid: str, action: str, policy: str | None = None):
    lab = request.app.state.lab
    try:
        if action == "start":
            status = lab.start(sid)
        elif action == "pause":
            status = lab.pause(sid)
        elif action == "resume":
            status = lab.resume(sid)
        else:
            status = await lab.stop(sid, policy_override=policy)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except LifecycleError as exc:
        raise HTTPException(400, str(exc)) from exc
    except LabFullError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": sid, "status": status}


@router.post("/api/sessions/{sid}/clone", status_code=201)
async def clone_session(sid: str, body: CloneBody, request: Request):
    try:
        new_id = request.app.state.lab.clone(sid, new_name=body.name, overrides=body.overrides)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"clone failed: {exc}") from exc
    return {"id": new_id}


@router.get("/api/sessions/{sid}/decisions/{intent_id}")
async def decision_replay(sid: str, intent_id: int, request: Request):
    conn = request.app.state.conn
    lab = request.app.state.lab
    if lab.sessions.get_session(sid) is None:
        raise HTTPException(404, "unknown session")
    row = conn.execute("SELECT * FROM intents WHERE id=? AND session_id=?",
                       (intent_id, sid)).fetchone()
    if row is None:
        raise HTTPException(404, "unknown intent for this session")
    order = conn.execute(
        "SELECT id, status, filled_qty, avg_fill_px FROM orders WHERE intent_id=?"
        " AND session_id=? ORDER BY id LIMIT 1", (intent_id, sid)
    ).fetchone()
    sess = lab.sessions.get_session(sid)
    ml_enabled = bool(sess and sess["ml_model_id"] and sess["ml_model_id"] != "deterministic")
    order_payload = None
    if order is not None:
        order_payload = {
            "order_id": order["id"], "status": order["status"],
            "filled_qty": order["filled_qty"], "avg_fill_px": order["avg_fill_px"],
        }
    return {
        "ts": row["ts"], "symbol": row["symbol"], "action": row["decision"],
        "market_state_ref": _jload(row["market_state_ref"]),
        "features": _jload(row["feature_vector_json"]),
        "rules": _jload(row["signals_json"]),
        "ml": {"enabled": ml_enabled, "model_id": sess["ml_model_id"] if sess else None,
               "score": row["ml_score"], "prob": row["ml_prob"]} if ml_enabled or row["ml_score"] is not None else None,
        "portfolio": _jload(row["portfolio_snapshot_json"]),
        "risk_checks": _jload(row["risk_checks_json"]),
        "rejection_reason": row["rejection_reason"] or None,
        "order": order_payload,
    }


def _jload(text: str | None):
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


@router.get("/api/lab/compare")
async def compare(request: Request, ids: str):
    conn = request.app.state.conn
    lab = request.app.state.lab
    out = []
    for sid in [s.strip() for s in ids.split(",") if s.strip()]:
        row = lab.sessions.get_session(sid)
        if row is None:
            raise HTTPException(404, f"unknown session {sid}")
        curve_rows = conn.execute(
            "SELECT ts, equity FROM account_snapshots WHERE session_id=? ORDER BY ts, id",
            (sid,),
        ).fetchall()
        by_date: dict[str, float] = {}
        for r in curve_rows:
            day = str(r["ts"])[:10]
            by_date[day] = round(float(r["equity"]), 2)
        equity_curve = sorted(by_date.items())
        trades = _trades(conn, sid)
        cum = 0.0
        by_trade = [[0, 0.0]]
        for i, t in enumerate(trades, start=1):
            cum += t["pnl"]
            by_trade.append([i, round(cum, 2)])
        out.append({
            "id": sid, "name": row["name"],
            "equity_curve": equity_curve,
            "by_trade": by_trade,
            "metrics": _metrics(equity_curve, trades, conn, sid),
        })
    return {"sessions": out}


@router.get("/api/system/health")
async def system_health(request: Request):
    return _system_block(request)


@router.get("/healthz")
async def healthz():
    return {"ok": True}
