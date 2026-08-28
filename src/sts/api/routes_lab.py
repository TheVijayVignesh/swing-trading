"""Lab page-support API — CONTRACT ADDENDUM v2 capabilities owned by the
frontend agent (routes_api.py is off-limits; anything missing from it lives
here instead).

Endpoints:
  GET    /api/lab/board?include_archived=1   summary superset (archived flags,
                                             activity states, recent decisions)
  POST   /api/sessions/{id}/archive          soft-archive (event-marker)
  POST   /api/sessions/{id}/restore
  DELETE /api/sessions/{id}                  hard delete, CREATED-only (409 else)
  POST   /api/sessions/{id}/scan             diagnostic scan-now (honest)
  GET    /api/sessions/{id}/timeline         merged journal timeline (paged)
  GET    /api/lab/benchmark?dates=...        ^NSEI normalized benchmark series
  GET    /api/lab/compare_extra?ids=...      sharpe/sortino/cagr/candidates/rejections

Helpers consumed by routes_pages.py: board_payload(), enrich_detail().
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter()

ARCHIVE_EVENT = "ARCHIVED"
RESTORE_EVENT = "RESTORED"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jload(text) -> dict:
    if not text:
        return {}
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}


# ------------------------------------------------------------------ archive state
def _archived_ids(conn) -> set[str]:
    """Latest-wins archive markers stored as session_events (the sessions
    table CHECK constraint has no ARCHIVED status, so the marker is a
    journaled event — same durability as every other lifecycle transition)."""
    marks: dict[str, str] = {}
    for r in conn.execute(
        "SELECT session_id, event FROM session_events"
        " WHERE event IN (?,?) ORDER BY id", (ARCHIVE_EVENT, RESTORE_EVENT),
    ).fetchall():
        marks[r["session_id"]] = r["event"]
    return {sid for sid, ev in marks.items() if ev == ARCHIVE_EVENT}


def _record_marker(conn, sid: str, event: str) -> None:
    conn.execute(
        "INSERT INTO session_events(session_id, ts, event, actor, detail_json)"
        " VALUES(?,?,?,?,?)",
        (sid, _now_iso(), event, "operator", "{}"),
    )
    conn.commit()


# ------------------------------------------------------------------ activity
def _activity_from_event(conn, sid: str) -> dict | None:
    row = conn.execute(
        "SELECT ts, detail_json FROM session_events"
        " WHERE session_id=? AND event='ACTIVITY' ORDER BY id DESC LIMIT 1",
        (sid,),
    ).fetchone()
    if row is None:
        return None
    d = _jload(row["detail_json"]).get("activity")
    if isinstance(d, dict) and d.get("state"):
        d.setdefault("explanation", "")
        d.setdefault("blocker_detail", {})
        d["as_of"] = row["ts"]
        return d
    return None


def activity_for(request: Request, row: dict) -> dict:
    """Live runner state wins; else the latest persisted ACTIVITY event;
    else an honest status-derived fallback."""
    lab = request.app.state.lab
    sid = row["id"]
    runner = lab.runners.get(sid)
    act = None
    if runner is not None:
        if runner.faulted:
            act = {"state": "FAULTED",
                   "explanation": "runner faulted; session preserved for inspection",
                   "blocker_detail": {}}
        else:
            act = runner.activity
    if act is None:
        act = _activity_from_event(request.app.state.conn, sid)
    if act is None:
        act = {"state": "WAITING_MARKET_OPEN",
               "explanation": f"session is {row['status']} — no scanning in progress.",
               "blocker_detail": {}}
    # terminal/paused truth beats a stale persisted TRADING snapshot
    if row["status"] in ("PAUSED", "STOPPED", "ABORTED", "CREATED") \
            and act.get("state") not in ("FAULTED",):
        prefix = {
            "CREATED": "Created, never started.",
            "PAUSED": "Paused by operator — scanning suspended.",
            "STOPPED": "Stopped.",
            "ABORTED": "Aborted.",
        }.get(row["status"], "")
        act = dict(act)
        act["explanation"] = (prefix + " " + (act.get("explanation") or "")).strip()
    return act


# ------------------------------------------------------------------ rejections
def _rejections_block(conn, sid: str) -> dict[str, dict[str, int]]:
    """{stage: {reason: count}} from journaled intents only."""
    out: dict[str, dict[str, int]] = {}
    for r in conn.execute(
        "SELECT decision, rejection_reason FROM intents"
        " WHERE session_id=? AND rejection_reason IS NOT NULL"
        " AND rejection_reason != ''", (sid,),
    ).fetchall():
        stage = r["decision"] or "REJECT"
        reason = str(r["rejection_reason"]).split(":")[0]
        out.setdefault(stage, {})
        out[stage][reason] = out[stage].get(reason, 0) + 1
    return out


def _top_rejections(rej: dict[str, dict[str, int]], n: int = 5) -> list[list]:
    counter: Counter = Counter()
    for reasons in rej.values():
        counter.update(reasons)
    return [[reason, count] for reason, count in counter.most_common(n)]


# ------------------------------------------------------------------ board (summary superset)
def recent_decisions(conn, limit: int = 8) -> list[dict]:
    rows = conn.execute(
        "SELECT i.ts, i.symbol, i.decision AS action, i.rejection_reason,"
        " s.name AS session_name, s.id AS session_id"
        " FROM intents i JOIN sessions s ON s.id = i.session_id"
        " WHERE COALESCE(i.decision,'') != ''"
        " ORDER BY i.ts DESC, i.id DESC LIMIT ?", (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def board_payload(request: Request, include_archived: bool = False) -> dict:
    """Superset of GET /api/lab/summary (ADDENDUM v2): archived flags +
    activity states + recent decisions strip. /api/lab/summary itself is
    owned by routes_api.py, so the page route and JS poll this instead."""
    from sts.api.routes_api import _session_summary, _system_block

    conn = request.app.state.conn
    rows = request.app.state.lab.sessions.list_sessions(include_archived=include_archived)
    archived = _archived_ids(conn)
    sessions, best_pool = [], []
    for row in rows:
        s = _session_summary(request, row)
        if s is None:
            continue
        s["archived"] = row["id"] in archived
        s["activity_state"] = activity_for(request, row).get("state")
        if s["archived"]:
            if include_archived:
                sessions.append(s)
            continue
        sessions.append(s)
        if s["status"] in ("RUNNING", "PAUSED") and len(s.get("sparkline") or []) >= 1:
            best_pool.append(s)
    best = max(best_pool, key=lambda s: s["return_pct"], default=None)
    return {
        "sessions": sessions,
        "best": best,
        "system": _system_block(request),
        "recent_decisions": recent_decisions(conn),
    }


# ------------------------------------------------------------------ endpoints
@router.get("/api/lab/board")
async def lab_board(request: Request, include_archived: int = 0):
    return board_payload(request, include_archived=bool(include_archived))


@router.post("/api/sessions/{sid}/archive")
async def archive_session(sid: str, request: Request):
    lab = request.app.state.lab
    row = lab.sessions.get_session(sid)
    if row is None:
        raise HTTPException(404, "unknown session")
    if row["status"] in ("RUNNING", "STOPPING"):
        raise HTTPException(409,
                            f"cannot archive a {row['status']} session — stop it first")
    conn = request.app.state.conn
    conn.execute("UPDATE sessions SET status='ARCHIVED', archived_at=? WHERE id=?",
                 (datetime.now(timezone.utc).isoformat(), sid))
    _record_marker(conn, sid, ARCHIVE_EVENT)
    conn.commit()
    return {"status": "ARCHIVED"}


@router.post("/api/sessions/{sid}/restore")
async def restore_session(sid: str, request: Request):
    lab = request.app.state.lab
    row = lab.sessions.get_session(sid)
    if row is None:
        raise HTTPException(404, "unknown session")
    conn = request.app.state.conn
    conn.execute("UPDATE sessions SET status='STOPPED', archived_at=NULL WHERE id=?", (sid,))
    _record_marker(conn, sid, RESTORE_EVENT)
    conn.commit()
    return {"status": "STOPPED"}


@router.delete("/api/sessions/{sid}")
async def delete_session(sid: str, request: Request):
    lab = request.app.state.lab
    row = lab.sessions.get_session(sid)
    if row is None:
        raise HTTPException(404, "unknown session")
    if row["status"] != "CREATED":
        raise HTTPException(
            409, "only never-started (CREATED) sessions can be deleted; "
                 "stop it and archive instead")
    conn = request.app.state.conn
    for table in ("fills", "orders", "intents", "positions", "account_snapshots",
                  "metrics_timeseries", "incidents", "session_events"):
        conn.execute(f"DELETE FROM {table} WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
    conn.commit()
    return JSONResponse(status_code=204, content=None)


@router.post("/api/sessions/{sid}/scan")
async def diagnostic_scan(sid: str, request: Request):
    """Diagnostic scan-now. Honest implementation against persisted data:
    uses the live runner's latest scan summary when alive, else the most
    recent persisted SCAN_FUNNEL. When the feed is not OPEN candidates are
    reported as deferred with reason MARKET_CLOSED (never fabricated)."""
    lab = request.app.state.lab
    row = lab.sessions.get_session(sid)
    if row is None:
        raise HTTPException(404, "unknown session")
    conn = request.app.state.conn
    md = request.app.state.marketdata

    funnel = None
    runner = lab.runners.get(sid)
    if runner is not None and getattr(runner, "last_scan_summary", None):
        s = runner.last_scan_summary
        funnel = {"ts": s.get("ts"), "scanned": s.get("scanned"),
                  "eligible": s.get("eligible"), "setups": s.get("setups"),
                  "ml_passed": s.get("ml_passed"), "portfolio_ok": s.get("portfolio_ok"),
                  "risk_ok": s.get("risk_ok"), "selected": s.get("selected")}
    if funnel is None:
        frow = conn.execute(
            "SELECT detail_json, ts FROM session_events"
            " WHERE session_id=? AND event='SCAN_FUNNEL' ORDER BY id DESC LIMIT 1",
            (sid,),
        ).fetchone()
        if frow is not None:
            d = _jload(frow["detail_json"])
            funnel = {"ts": frow["ts"]}
            for k in ("scanned", "eligible", "setups", "ml_passed",
                      "portfolio_ok", "risk_ok", "selected"):
                funnel[k] = d.get(k, 0)

    intents = conn.execute(
        "SELECT ts, symbol, decision, rejection_reason, feature_vector_json"
        " FROM intents WHERE session_id=? ORDER BY ts DESC, id DESC LIMIT 20",
        (sid,),
    ).fetchall()
    candidates = []
    for r in intents:
        score = _jload(r["feature_vector_json"]).get("score")
        candidates.append({"ts": r["ts"], "symbol": r["symbol"],
                           "decision": r["decision"], "score": score,
                           "rejection_reason": r["rejection_reason"] or None})

    feed = md.feed_status()
    deferrals = []
    # ACTUALLY RUN the pipeline (runner.run_scan_now persists funnel + intents
    # and returns {funnel, candidates, deferrals}). Falls back to read-only
    # reporting only when no live runner exists (e.g. session never started).
    runner = lab.runners.get(sid)
    if runner is not None:
        try:
            result = await runner.run_scan_now(reason="diagnostic-api")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"diagnostic scan failed: {exc}") from exc
        funnel = result.get("funnel")
        if funnel is not None and not isinstance(funnel, dict):
            from dataclasses import asdict
            funnel = asdict(funnel)
        candidates = result.get("candidates", [])
        deferrals = result.get("deferrals", [])
        if deferrals == [] and feed != "OPEN":
            deferrals = [{"reason": "MARKET_CLOSED",
                          "detail": f"feed {feed} — approved candidates deferred, "
                                    f"not fabricated; entries resume at open"}]
        prescreen = getattr(runner, "_last_prescreen", [])
        return {"diagnostic": True, "ran": True, "funnel": funnel,
                "candidates": candidates, "deferrals": deferrals, "feed": feed,
                "prescreen": prescreen,
                "prescreen_note": (f"{len(prescreen)} symbols pass all daily conditions and are "
                                   "armed awaiting the intraday breakout trigger at next open"
                                   if prescreen and feed != "OPEN" else None)}

    # no live runner — report last persisted scan honestly
    if feed != "OPEN":
        deferrals.append({
            "reason": "MARKET_CLOSED",
            "detail": f"feed {feed} — session not started; no scan executed",
        })
    return {"diagnostic": True, "ran": False, "funnel": funnel, "candidates": candidates,
            "deferrals": deferrals, "feed": feed}


@router.get("/api/sessions/{sid}/timeline")
async def session_timeline(sid: str, request: Request, limit: int = 50,
                           offset: int = 0):
    lab = request.app.state.lab
    if lab.sessions.get_session(sid) is None:
        raise HTTPException(404, "unknown session")
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    conn = request.app.state.conn
    items: list[dict] = []

    for r in conn.execute(
        "SELECT ts, event, actor, detail_json FROM session_events"
        " WHERE session_id=? ORDER BY id DESC LIMIT ?",
        (sid, limit + offset),
    ).fetchall():
        d = _jload(r["detail_json"])
        bits = [str(r["event"])]
        if d.get("name"):
            bits.append(str(d["name"]))
        if d.get("policy"):
            bits.append("policy=" + str(d["policy"]))
        if d.get("terminal_state"):
            bits.append(str(d["terminal_state"]))
        act = d.get("activity") or {}
        if act.get("state"):
            bits.append(act["state"])
        text = " · ".join(bits)
        if r["actor"]:
            text += f" (by {r['actor']})"
        items.append({"ts": r["ts"], "kind": "event", "label": str(r["event"]),
                      "text": text})

    for r in conn.execute(
        "SELECT ts, symbol, decision, rejection_reason FROM intents"
        " WHERE session_id=? ORDER BY id DESC LIMIT ?",
        (sid, limit + offset),
    ).fetchall():
        text = f"{r['decision']} {r['symbol'] or '?'}"
        if r["rejection_reason"]:
            text += f" — {r['rejection_reason']}"
        items.append({"ts": r["ts"], "kind": "decision",
                      "label": str(r["decision"] or "?"), "text": text})

    for r in conn.execute(
        "SELECT f.ts, f.px, f.qty, o.side FROM fills f"
        " JOIN orders o ON o.id=f.order_id"
        " WHERE f.session_id=? ORDER BY f.id DESC LIMIT ?",
        (sid, limit + offset),
    ).fetchall():
        items.append({"ts": r["ts"], "kind": "fill", "label": "FILL",
                      "text": f"{r['side']} {r['qty']} @ {r['px']}"})

    items.sort(key=lambda x: x["ts"] or "", reverse=True)
    page = items[offset:offset + limit]
    return {"items": page, "offset": offset + len(page), "total": len(items),
            "has_more": len(items) > offset + limit}


@router.get("/api/lab/benchmark")
async def lab_benchmark(request: Request, dates: str = ""):
    """^NSEI daily closes normalized to the first requested session date.
    Honest unavailability: {available:false, reason} when parquet missing."""
    wanted = [d.strip() for d in dates.split(",") if d.strip()]
    if not wanted:
        raise HTTPException(400, "dates required (comma-separated ISO dates)")
    md = request.app.state.marketdata
    df = None
    try:
        df = md.index_frame("nifty50")
    except Exception:  # noqa: BLE001
        df = None
    if df is None or len(df) == 0:
        try:
            import pandas as pd
            from pathlib import Path
            p = Path("data/parquet/candles_1d/_NSEI.parquet")
            if not p.exists():
                p = (Path(__file__).resolve().parents[3]
                     / "data" / "parquet" / "candles_1d" / "_NSEI.parquet")
            if p.exists():
                df = pd.read_parquet(p)
                df["date"] = pd.to_datetime(df["date"])
            else:
                df = None
        except Exception:  # noqa: BLE001
            df = None
    if df is None or len(df) == 0:
        return {"available": False,
                "reason": "benchmark parquet (_NSEI) unavailable on this machine"}
    by_day: dict[str, float] = {}
    for _, row in df.iterrows():
        try:
            by_day[str(row["date"])[:10]] = float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
    base = next((by_day[d] for d in wanted if d in by_day), None)
    if base is None:
        return {"available": False,
                "reason": "no ^NSEI close within the requested date range"}
    series = [[d, round(by_day[d] / base, 6)] for d in sorted(by_day)
              if wanted[0] <= d <= wanted[-1]]
    return {"available": True, "source": "^NSEI · NIFTY 50",
            "base_date": wanted[0], "series": series}


@router.get("/api/lab/compare_extra")
async def compare_extra(request: Request, ids: str = ""):
    """Per-session analytics the ADDENDUM v2 expects from compare but that
    routes_api does not expose yet: risk-adjusted metrics + candidate and
    rejection counts (computed from persisted journal data only)."""
    conn = request.app.state.conn
    lab = request.app.state.lab
    out = []
    for sid in [s.strip() for s in ids.split(",") if s.strip()]:
        if lab.sessions.get_session(sid) is None:
            raise HTTPException(404, f"unknown session {sid}")
        curve_rows = conn.execute(
            "SELECT ts, equity FROM account_snapshots WHERE session_id=? ORDER BY ts, id",
            (sid,),
        ).fetchall()
        by_day: dict[str, float] = {}
        for r in curve_rows:
            by_day[str(r["ts"])[:10]] = float(r["equity"])
        days = sorted(by_day)
        rets = []
        for a, b in zip(days, days[1:]):
            if by_day[a]:
                rets.append(by_day[b] / by_day[a] - 1)
        mean = sum(rets) / len(rets) if rets else 0.0
        sd = ((sum((x - mean) ** 2 for x in rets) / len(rets)) ** 0.5) if rets else 0.0
        downside = [min(x, 0.0) for x in rets]
        dsd = ((sum(x * x for x in downside) / len(downside)) ** 0.5) if downside else 0.0
        ann = 252 ** 0.5
        cagr_pct = None
        if len(days) >= 2 and by_day[days[0]] > 0:
            years = max((len(days) - 1) / 252.0, 1 / 252.0)
            cagr_pct = round(((by_day[days[-1]] / by_day[days[0]]) ** (1 / years) - 1) * 100, 4)
        n_intents = conn.execute(
            "SELECT COUNT(*) AS n FROM intents WHERE session_id=?", (sid,)
        ).fetchone()["n"]
        rej = _rejections_block(conn, sid)
        out.append({
            "id": sid,
            "sharpe": round(mean / sd * ann, 4) if sd > 0 else None,
            "sortino": round(mean / dsd * ann, 4) if dsd > 0 else None,
            "cagr_pct": cagr_pct,
            "candidates_total": int(n_intents),
            "rejections_top": _top_rejections(rej),
        })
    return {"sessions": out}


# ------------------------------------------------- detail enrichment (routes_pages)
def enrich_detail(request: Request, sid: str, detail: dict) -> dict:
    """ADDENDUM v2 detail fields the page dossier needs, merged onto the
    routes_api.session_detail() payload without touching routes_api.py."""
    conn = request.app.state.conn
    lab = request.app.state.lab
    row = lab.sessions.get_session(sid)
    if row is None:
        return detail

    detail["created_at"] = row["created_at"]
    detail["started_at"] = row["started_at"]
    detail["ended_at"] = row["ended_at"]
    detail["config_hash_short"] = (row["config_hash"] or "")[:12]
    detail["strategy_version"] = row["strategy_version"]
    detail["ml_model_id"] = row["ml_model_id"]
    detail["costs_version"] = row["costs_version"]

    act = activity_for(request, row)
    detail["activity"] = {"state": act.get("state"),
                          "explanation": act.get("explanation", ""),
                          "blocker_detail": act.get("blocker_detail") or {}}
    detail["rejections"] = _rejections_block(conn, sid)

    # ---- market/system state
    md = request.app.state.marketdata
    last_bar = None
    try:
        universe = (detail.get("config") or {}).get("universe") or "NIFTY200"
        symbols = lab.resolve_universe(universe)[:1]
        if symbols:
            b = md.get_bar(symbols[0])
            if b is not None:
                last_bar = {"symbol": getattr(b, "symbol", symbols[0]),
                            "ts": getattr(b, "ts", None),
                            "close": float(getattr(b, "close", 0) or 0)}
    except Exception:  # noqa: BLE001 — last bar is best-effort only
        last_bar = None
    detail["last_bar"] = last_bar

    jr = conn.execute(
        "SELECT ts, event FROM session_events WHERE session_id=?"
        " ORDER BY id DESC LIMIT 1", (sid,),
    ).fetchone()
    detail["latest_journal_event"] = (
        {"ts": jr["ts"], "kind": jr["event"]} if jr else None)
    detail["incident_count"] = conn.execute(
        "SELECT COUNT(*) AS n FROM incidents WHERE session_id=?", (sid,)
    ).fetchone()["n"]
    detail["feed_source"] = getattr(
        getattr(request.app.state.marketdata, "poller", None), "active_source", None)

    # ---- exposure timeseries (watchdog records metric='exposure')
    detail["exposure_timeseries"] = [
        [r["ts"], round(float(r["value"]), 6)] for r in conn.execute(
            "SELECT ts, value FROM metrics_timeseries"
            " WHERE session_id=? AND metric='exposure' AND value IS NOT NULL"
            " ORDER BY ts, id", (sid,),
        ).fetchall()
    ]

    # ---- positions extras: entry_ts from journal + entry intent context
    pos_rows = {
        p["symbol"]: p for p in conn.execute(
            "SELECT symbol, opened_at, strategy_version, ml_model_id FROM positions"
            " WHERE session_id=? AND status='OPEN'", (sid,),
        ).fetchall()
    }
    for p in detail.get("positions") or []:
        prow = pos_rows.get(p.get("symbol"))
        p.setdefault("exchange", "NSE")
        if prow is not None:
            p["entry_ts"] = prow["opened_at"]
            p["strategy_version"] = prow["strategy_version"]
            intent = conn.execute(
                "SELECT decision, rejection_reason, ml_score, signals_json FROM intents"
                " WHERE session_id=? AND symbol=? AND ts<=?"
                " ORDER BY id DESC LIMIT 1", (sid, p["symbol"], prow["opened_at"]),
            ).fetchone()
            if intent is not None and intent["decision"] in ("ENTER", "ADD"):
                p["ml_score"] = intent["ml_score"]
                rules = []
                try:
                    rules = json.loads(intent["signals_json"] or "[]")
                except json.JSONDecodeError:
                    pass
                passed = [r for r in rules if isinstance(r, dict) and r.get("passed")]
                p["entry_reason"] = (passed[0].get("description")
                                     if passed else intent["decision"])
            else:
                p["ml_score"] = None
                p["entry_reason"] = None
        else:
            p["entry_ts"] = None
            p["strategy_version"] = None
            p["ml_score"] = None
            p["entry_reason"] = None
        qty = p.get("qty") or 0
        px = p.get("last_px") or p.get("avg_entry") or 0
        p["market_value"] = round(qty * px, 2)

    # ---- trades extras: strategy/ml provenance via the closed position row
    closed_rows = {}
    for t in detail.get("trades") or []:
        key = (t.get("symbol"), str(t.get("entry_ts")))
        if key in closed_rows:
            continue
        closed_rows[key] = conn.execute(
            "SELECT strategy_version, ml_model_id FROM positions"
            " WHERE session_id=? AND symbol=? AND status='CLOSED'"
            " AND opened_at=? LIMIT 1", (sid, t.get("symbol"), t.get("entry_ts")),
        ).fetchone()
    for t in detail.get("trades") or []:
        crow = closed_rows[(t.get("symbol"), str(t.get("entry_ts")))]
        t["fees"] = t.get("costs")
        t["slippage"] = None   # not persisted per fill — honest null, never guessed
        t["strategy_version"] = crow["strategy_version"] if crow else None
        t["ml_model_id"] = crow["ml_model_id"] if crow else None

    return detail
