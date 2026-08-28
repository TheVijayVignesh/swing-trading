"""Lab integration tests — full graph, synthetic data, injected clock/poller.

Drives ~60 synthetic 5m bars across 3 engineered symbols through the REAL
MarketDataService -> SessionRunner -> PaperBroker -> RepoSink pipeline against
a tmp SQLite journal, then asserts isolation, funnel/intent journaling,
lifecycle semantics (pause-blocks-entries-but-exits-fire, FLATTEN/HOLD
terminals), duplicate-correlation suppression, crash recovery with equity
continuity, and fail-closed staleness handling.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import zoneinfo

import httpx
import pandas as pd
import pytest

from sts.api.app import create_app
from sts.config import SessionConfig
from sts.contracts import Bar, OrderType, Side, TradeIntent
from sts.data import calendar as cal
from sts.lab.manager import LabFullError, LabManager, LifecycleError
from sts.lab.manager import MAX_SESSIONS
from sts.marketdata.service import MarketDataService
from sts.storage.db import init_db
from sts.storage.repos import TradingRepo

IST = zoneinfo.ZoneInfo("Asia/Kolkata")
CLOCK_NOW = dt.datetime(2026, 8, 25, 10, 0, tzinfo=IST)
DAY1 = dt.date(2026, 8, 21)      # Friday, trading day
DAY2 = dt.date(2026, 8, 24)      # Monday, trading day


def bts(day: dt.date, h: int, m: int) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(h, m))   # bar OPEN time, IST-naive


def bar(sym: str, ts: dt.datetime, o, h, l, c, v=130_000) -> Bar:
    return Bar(symbol=sym, ts=ts, open=o, high=h, low=l, close=c, volume=v, timeframe="5m")


# ---------------------------------------------------------------- fixtures data
def weekday_dates(end: dt.date, n: int) -> list[dt.date]:
    out: list[dt.date] = []
    d = end - dt.timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5 and d not in cal.nse_holidays(d.year):
            out.append(d)
        d -= dt.timedelta(days=1)
    return sorted(out)


def uptrend_frame(end: dt.date, n: int, base: float, drift: float) -> pd.DataFrame:
    dates = weekday_dates(end, n)
    closes = [base + drift * i for i in range(n)]
    # engineered pullback: dip below SMA20 within last 5 sessions, final-day reclaim
    for k in (-4, -3, -2):
        closes[k] -= 50.0
    rows = []
    for d, c in zip(dates, closes):
        rows.append({"date": d, "open": c, "high": c + 10.0, "low": c - 10.0,
                     "close": c, "volume": 200_000.0, "adjclose": c})
    return pd.DataFrame(rows)


class FakePoller:
    def __init__(self) -> None:
        self.bars: dict[str, Bar] = {}
        self.calls = 0

    def poll_once(self):
        self.calls += 1
        return len(self.bars), 0

    def get_bars(self) -> dict[str, Bar]:
        return dict(self.bars)

    def feed(self, b: Bar) -> None:
        self.bars[b.symbol] = b


@pytest.fixture()
def env(tmp_path):
    conn = init_db(str(tmp_path / "journal.db"))
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()

    # AAA/BBB identical uptrends (corr=1 exercises CORRELATION rejection);
    # CCC short history -> ineligible; ^NSEI rising regime.
    for sym in ("AAA", "BBB"):
        uptrend_frame(DAY1, 80, 500.0, 0.6).to_parquet(daily_dir / f"{sym}.parquet", index=False)
    uptrend_frame(DAY1, 40, 300.0, 0.5).to_parquet(daily_dir / "CCC.parquet", index=False)
    idx = uptrend_frame(DAY1, 80, 100.0, 0.5)
    idx.to_parquet(daily_dir / "_NSEI.parquet", index=False)

    poller = FakePoller()
    md = MarketDataService(
        ["AAA", "BBB", "CCC"], poller=poller, clock=lambda: CLOCK_NOW,
        daily_dir=daily_dir, poll_seconds=60,
    )
    universes = {"LAB1": ["AAA", "BBB", "CCC"]}
    mgr = LabManager(conn, md, universe_resolver=lambda name: universes[name])

    class Env:
        pass

    e = Env()
    e.conn = conn
    e.md = md
    e.poller = poller
    e.mgr = mgr
    e.daily_dir = daily_dir
    e.tmp_path = tmp_path
    yield e
    conn.close()


ENTER_PARAMS = {"rsi_min": 10, "rsi_max": 100}   # widen momentum band; other rules strict


async def make_session(e, name, *, capital=60_000, profile="standard",
                       policy="FLATTEN", ml=False, universe="LAB1", params=None):
    cfg = SessionConfig(name=name, capital_initial=float(capital), mode="paper",
                        universe=universe, strategy_id="pullback-v1",
                        risk_profile=profile, ml_enabled=ml, on_stop_policy=policy,
                        params=params if params is not None else dict(ENTER_PARAMS))
    sid = e.mgr.create_session(cfg)
    return sid


async def start(e, sid):
    e.mgr.start(sid)
    await asyncio.sleep(0.02)


async def drive(e, bars, settle: float = 0.05):
    """Feed bars one by one through the service fan-out."""
    for b in bars:
        e.poller.feed(b)
        e.md.poll_cycle()
        await asyncio.sleep(0)
    await asyncio.sleep(settle)


def day1_bars():
    t = lambda h, m: bts(DAY1, h, m)  # noqa: E731
    aaa = [
        bar("AAA", t(9, 35), 550, 552, 548, 551),
        bar("AAA", t(9, 40), 551, 554, 550, 553),
        bar("AAA", t(9, 45), 553, 560, 552, 558, v=180_000),  # intraday high 560 > trigger 557.4
        bar("AAA", t(9, 50), 558, 562, 556, 560),            # low < limit -> BUY fills
        bar("AAA", t(9, 55), 557, 559, 555, 556),
        bar("AAA", t(10, 0), 558, 560, 556, 557),            # flatten sell fills here
    ]
    bbb = [
        bar("BBB", t(9, 35), 550, 552, 548, 551),
        bar("BBB", t(9, 40), 551, 554, 550, 553),
        bar("BBB", t(9, 45), 553, 560, 552, 558, v=180_000),
        bar("BBB", t(9, 50), 558, 562, 556, 560),
        bar("BBB", t(9, 55), 557, 559, 555, 556),
        bar("BBB", t(10, 0), 558, 560, 556, 557),
    ]
    ccc = [bar("CCC", t(9, 35 + i), 100, 101, 99, 100) for i in range(6)]
    return aaa, bbb, ccc


def interleave(*seqs):
    """Round-robin merge so each timestamp group ends on the entry-capable symbol."""
    out = []
    for tup in zip(*seqs):
        out.extend(tup)
    return out


CRASH_BAR = bar("AAA", bts(DAY1, 10, 5), 510, 516, 500, 505)


# ===================================================================== tests
async def test_entry_funnel_journaling_and_isolation(env):
    e = env
    sid_a = await make_session(e, "alpha", capital=60_000)
    sid_b = await make_session(e, "beta", capital=30_000, profile="small", policy="HOLD",
                               params={"rsi_min": 99, "rsi_max": 100})  # momentum gate => never enters
    await start(e, sid_a)
    await start(e, sid_b)

    aaa, bbb, ccc = day1_bars()
    # Coalescing v2: ONE scan per unique bar-close ts (scheduled by whichever
    # symbol's frame lands first). CCC's off-grid bars create extra ts groups,
    # so the LATEST journaled funnel can be a duplicate-NOOP replay; actual
    # placement is asserted through intents/orders below.
    await drive(e, interleave(bbb[:3], ccc[:3], aaa[:3]))

    repo_a = TradingRepo(e.conn, sid_a)
    funnel = repo_a.latest_funnel()
    assert funnel is not None
    assert funnel["scanned"] == 3
    assert funnel["eligible"] == 2          # AAA+BBB pass; CCC <60 rows
    assert funnel["setups"] >= 1
    assert funnel["portfolio_ok"] == 1      # twin rejected via CORRELATION
    n_orders = e.conn.execute(
        "SELECT COUNT(*) n FROM orders WHERE session_id=?", (sid_a,)).fetchone()["n"]
    assert n_orders == 1                    # exactly one order placed

    intents = e.conn.execute(
        "SELECT * FROM intents WHERE session_id=? ORDER BY id", (sid_a,)).fetchall()
    enter = [r for r in intents if r["decision"] == "ENTER"]
    assert enter, "expected journaled ENTER intent"
    rules = json.loads(enter[-1]["signals_json"])
    assert rules and all({"rule_id", "observed", "threshold", "passed"} <= set(r) for r in rules)
    checks = json.loads(enter[-1]["risk_checks_json"])
    assert len(checks) == 10                 # 9 normative CHECK_ORDER + sizing_math
    assert checks[-1]["check"] == "sizing_math"
    sizing = json.loads(checks[-1]["observed"])
    assert {"qty", "notional", "cap_notional", "atr_frac"} <= set(sizing)
    versions = json.loads(enter[-1]["versions_json"])
    assert versions["strategy_version"] == "v1.0.0"
    assert versions["costs_version"] == "c1.0.0"

    await drive(e, interleave(aaa[3:], bbb[3:], ccc[3:]))
    # a coalesced scan that saw BOTH twins must journal the CORRELATION veto
    all_intents = e.conn.execute(
        "SELECT rejection_reason FROM intents WHERE session_id=?", (sid_a,)).fetchall()
    assert any(r["rejection_reason"] == "CORRELATION" for r in all_intents)
    pos_a = repo_a.open_positions()
    # 1..2 entries: intra-scan CORRELATION rejects the twin symbol, but a
    # scan-to-scan entry before the first fill is legal per selector semantics.
    assert 1 <= len(pos_a) <= 2
    assert {p["symbol"] for p in pos_a} <= {"AAA", "BBB"}
    assert all(p["qty"] > 0 for p in pos_a)
    fills_a = e.conn.execute("SELECT COUNT(*) n FROM fills WHERE session_id=?", (sid_a,)).fetchone()["n"]
    assert fills_a >= 1                      # BUY fill journaled
    snaps = repo_a.equity_curve()
    assert snaps and snaps[-1]["equity"] < 60_000     # invested
    metrics = e.conn.execute(
        "SELECT DISTINCT metric FROM metrics_timeseries WHERE session_id=?", (sid_a,)).fetchall()
    assert {"equity", "drawdown_pct", "exposure"} <= {m["metric"] for m in metrics}

    # ---- isolation: B never sees A's rows or trades
    repo_b = TradingRepo(e.conn, sid_b)
    assert repo_b.open_positions() == []
    assert repo_b.trades_closed() == []
    st_b = e.mgr.graphs[sid_b].broker.get_account_state(sid_b)
    assert st_b.equity == pytest.approx(30_000, abs=0.01)   # untouched ledger
    from sts.storage.repos import IsolationError
    with pytest.raises(IsolationError):
        repo_b.get_intent(intents[0]["id"])

    # duplicate correlation_id suppressed at OrderManager level
    om = e.mgr.graphs[sid_a].order_manager
    intent = TradeIntent(session_id=sid_a, ts=bts(DAY1, 10, 0), symbol="AAA",
                         side=Side.BUY, order_type=OrderType.LIMIT, qty=10,
                         limit_price=515.0, correlation_id="dup-probe-1")
    oid1 = om.place_order(sid_a, intent)
    oid2 = om.place_order(sid_a, intent)
    assert oid1 == oid2
    assert om.counters["duplicates"] == 1

    await e.mgr.stop(sid_b, policy_override="HOLD")


async def test_flatten_terminal_with_open_position(env):
    e = env
    sid = await make_session(e, "flatten-me", policy="FLATTEN")
    await start(e, sid)
    aaa, bbb, _ = day1_bars()
    await drive(e, interleave(aaa[:5], bbb[:5]))
    repo = TradingRepo(e.conn, sid)
    assert len(repo.open_positions()) == 1

    stop_task = asyncio.create_task(e.mgr.stop(sid))     # enters STOPPING, waits flat
    await asyncio.sleep(0.1)
    assert e.conn.execute("SELECT status FROM sessions WHERE id=?",
                          (sid,)).fetchone()["status"] == "STOPPING"
    await drive(e, [aaa[5], bbb[5],                       # bars that PLACE flatten sells
                    bar("AAA", bts(DAY1, 10, 5), 557, 559, 555, 556)])  # fills them
    await asyncio.wait_for(stop_task, timeout=5)

    row = e.conn.execute("SELECT status, terminal_state FROM sessions WHERE id=?",
                         (sid,)).fetchone()
    assert row["status"] == "STOPPED" and row["terminal_state"] == "FLATTENED"
    closed = repo.trades_closed()
    assert closed and closed[-1]["exit_reason"] == "SESSION_FLATTEN"


async def test_hold_freezes_positions(env):
    e = env
    sid_src = await make_session(e, "hold-src", policy="FLATTEN")
    sid_c = e.mgr.clone(sid_src, new_name="hold-clone",
                        overrides={"on_stop_policy": "HOLD"})
    row = e.conn.execute("SELECT config_yaml, status FROM sessions WHERE id=?",
                         (sid_c,)).fetchone()
    assert row["status"] == "CREATED"
    assert "on_stop_policy: HOLD" in row["config_yaml"]
    await start(e, sid_src)
    await start(e, sid_c)

    aaa, bbb, _ = day1_bars()
    await drive(e, interleave(aaa[:5], bbb[:5]))
    repo_c = TradingRepo(e.conn, sid_c)
    assert len(repo_c.open_positions()) == 1

    res = await e.mgr.stop(sid_c)                          # HOLD per cloned config
    assert res == "STOPPED"
    row = e.conn.execute("SELECT terminal_state FROM sessions WHERE id=?",
                         (sid_c,)).fetchone()
    assert row["terminal_state"] == "HELD"
    assert len(repo_c.open_positions()) == 1               # frozen, not flattened
    await e.mgr.stop(sid_src, policy_override="HOLD")      # clean up src


async def test_pause_blocks_entries_but_exits_fire(env):
    e = env
    sid = await make_session(e, "paused-stopper", ml=True, policy="HOLD")
    await start(e, sid)
    aaa, bbb, _ = day1_bars()
    await drive(e, interleave(aaa[:4], bbb[:4]))           # entry placed & filled

    # ML fallback incident (ml_enabled without a trained model)
    inc = e.conn.execute(
        "SELECT kind FROM incidents WHERE session_id=? AND kind='ML_NOT_AVAILABLE_DETERMINISTIC_FALLBACK'",
        (sid,)).fetchall()
    assert inc, "expected deterministic-fallback incident"

    e.mgr.pause(sid)
    assert e.conn.execute("SELECT status FROM sessions WHERE id=?",
                          (sid,)).fetchone()["status"] == "PAUSED"
    n_intents_before = e.conn.execute(
        "SELECT COUNT(*) n FROM intents WHERE session_id=? AND decision='ENTER'",
        (sid,)).fetchone()["n"]

    await drive(e, [aaa[4], bbb[4]])                       # more bars while paused
    n_after = e.conn.execute(
        "SELECT COUNT(*) n FROM intents WHERE session_id=? AND decision='ENTER'",
        (sid,)).fetchone()["n"]
    assert n_after == n_intents_before, "PAUSED must block NEW entries"

    await drive(e, [CRASH_BAR])                            # low 500 <= stop ~502
    pos = TradingRepo(e.conn, sid).trades_closed()
    assert pos and pos[-1]["exit_reason"] == "STOP", "stop must fire while PAUSED"
    fills = e.conn.execute("SELECT COUNT(*) n FROM fills WHERE session_id=?",
                           (sid,)).fetchone()["n"]
    assert fills >= 2                                      # buy + sell journaled
    await e.mgr.stop(sid, policy_override="HOLD")


async def test_illegal_transitions_and_cap(env):
    e = env
    sid = await make_session(e, "lifecycle")
    with pytest.raises(LifecycleError):                    # pause before start
        e.mgr.pause(sid)
    with pytest.raises(LifecycleError):                    # resume before start
        e.mgr.resume(sid)
    await start(e, sid)
    with pytest.raises(LifecycleError):                    # double start
        e.mgr.start(sid)
    e.mgr.pause(sid)
    with pytest.raises(LifecycleError):                    # pause twice
        e.mgr.pause(sid)
    e.mgr.resume(sid)
    with pytest.raises(LifecycleError):                    # resume while RUNNING
        e.mgr.resume(sid)
    await e.mgr.stop(sid, policy_override="HOLD")          # RUNNING -> STOPPED
    with pytest.raises(LifecycleError):                    # start after STOPPED
        e.mgr.start(sid)

    sid2 = await make_session(e, "abort-created")
    assert await e.mgr.stop(sid2) == "ABORTED"             # CREATED -> ABORTED

    # concurrency cap
    sids = []
    for i in range(MAX_SESSIONS):
        s = await make_session(e, f"cap-{i}")
        sids.append(s)
        e.mgr.start(s)
        await asyncio.sleep(0)
    with pytest.raises(LabFullError):
        extra = await make_session(e, "cap-over")
        e.mgr.start(extra)
    for s in sids:
        await e.mgr.stop(s, policy_override="HOLD")


async def test_crash_recovery_equity_continuity(env):
    e = env
    sid = await make_session(e, "crashy")
    await start(e, sid)

    # Day 1: enter, stop out (clean slate), so recovery test owns day 2 cleanly.
    aaa, bbb, _ = day1_bars()
    await drive(e, aaa + bbb + [CRASH_BAR])
    repo = TradingRepo(e.conn, sid)
    assert repo.trades_closed()

    # Day 2 bars: re-entry on AAA.
    t = lambda h, m: bts(DAY2, h, m)  # noqa: E731
    e2 = [
        bar("AAA", t(9, 35), 551, 553, 549, 552),
        bar("AAA", t(9, 40), 552, 559, 551, 558, v=180_000),
        bar("AAA", t(9, 45), 557, 560, 555, 556),
    ]
    await drive(e, e2)
    assert len(repo.open_positions()) == 1

    last_snap_before = repo.equity_curve()[-1]["equity"]
    qty_before = repo.open_positions()[0]["qty"]

    # ---- CRASH: kill the runner task and drop all in-memory state
    task = e.mgr.tasks.pop(sid)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    e.mgr.runners.pop(sid, None)
    e.mgr.graphs.pop(sid, None)
    e.mgr.sessions.set_status(sid, "RUNNING")              # journal says RUNNING

    recovered = e.mgr.recover_on_boot()
    assert sid in recovered
    ev = e.conn.execute(
        "SELECT COUNT(*) n FROM session_events WHERE session_id=? AND event='RECOVERED'",
        (sid,)).fetchone()["n"]
    assert ev == 1
    assert len(e.mgr.graphs[sid].broker.get_positions(sid)) == 1

    # cash formula: capital - SUM(buy px*qty+costs) + SUM(sell px*qty-costs)
    rows = e.conn.execute(
        "SELECT f.px, f.qty, f.cost_breakdown_json, o.side FROM fills f"
        " JOIN orders o ON o.id=f.order_id WHERE f.session_id=? ORDER BY f.ts", (sid,)
    ).fetchall()
    expected_cash = 60_000.0
    for r in rows:
        cost = float(json.loads(r["cost_breakdown_json"]).get("total", 0.0))
        if r["side"] == "BUY":
            expected_cash -= float(r["px"]) * int(r["qty"]) + cost
        else:
            expected_cash += float(r["px"]) * int(r["qty"]) - cost
    acct_cash = e.mgr.graphs[sid].broker._account(sid).cash
    assert acct_cash == pytest.approx(expected_cash, abs=0.02)

    # equity continuity: feed one more bar; restored runner reprices the position
    await drive(e, [bar("AAA", t(9, 50), 556, 558, 554, 555)])
    snap_after = TradingRepo(e.conn, sid).equity_curve()[-1]["equity"]
    assert abs(snap_after - last_snap_before) <= qty_before * 3.0

    await e.mgr.stop(sid, policy_override="HOLD")


async def test_fail_closed_stale_feed(env):
    e = env
    sid = await make_session(e, "stale-guard")
    await start(e, sid)

    e.md.force_stale(True)
    assert e.md.feed_status() == "STALE"
    t = lambda h, m: bts(DAY1, h, m)  # noqa: E731
    stale_bar = bar("AAA", t(11, 0), 557, 559, 556, 558, v=180_000)
    await drive(e, [stale_bar, bar("BBB", t(11, 0), 557, 559, 556, 558)])

    runner = e.mgr.runners[sid]
    assert runner.health == "stale"
    inc = e.conn.execute(
        "SELECT COUNT(*) n FROM incidents WHERE session_id=? AND kind='FEED_STALE_ENTRIES_BLOCKED'",
        (sid,)).fetchone()["n"]
    assert inc == 1
    n_enter = e.conn.execute(
        "SELECT COUNT(*) n FROM intents WHERE session_id=? AND decision='ENTER'",
        (sid,)).fetchone()["n"]
    assert n_enter == 0, "entries must be blocked when feed is stale"

    e.md.force_stale(False)
    await drive(e, [bar("AAA", t(11, 5), 558, 560, 557, 559)])
    assert e.mgr.runners[sid].health == "ok"
    await e.mgr.stop(sid, policy_override="HOLD")


# ------------------------------------------------------------------ API layer
async def test_api_contract(env):
    e = env
    app = create_app(e.mgr, e.md, e.conn, recover_on_startup=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        r = await client.get("/healthz")
        assert r.status_code == 200 and r.json() == {"ok": True}

        # validation: bad capital
        r = await client.post("/api/sessions", json={"name": "x", "capital_initial": 500})
        assert r.status_code == 400
        # validation: non-paper mode interlocked
        r = await client.post("/api/sessions",
                              json={"name": "x", "capital_initial": 5000, "mode": "live"})
        assert r.status_code == 403 and "LIVE_INTERLOCKED" in r.json()["detail"]
        # symbol field NOT accepted — ignored silently
        r = await client.post("/api/sessions", json={
            "name": "api-sess", "capital_initial": 60000, "mode": "paper",
            "universe": "LAB1", "strategy_id": "pullback-v1", "risk_profile": "standard",
            "ml_enabled": False, "on_stop_policy": "FLATTEN", "symbol": "RELIANCE"})
        assert r.status_code == 201
        sid = r.json()["id"]
        assert e.conn.execute(
            "SELECT COUNT(*) n FROM sessions WHERE config_yaml LIKE '%symbol%'").fetchone()["n"] == 0

        # lifecycle over API
        assert (await client.post(f"/api/sessions/{sid}/start")).json()["status"] == "RUNNING"
        assert (await client.post(f"/api/sessions/{sid}/pause")).json()["status"] == "PAUSED"
        assert (await client.post(f"/api/sessions/{sid}/resume")).json()["status"] == "RUNNING"
        r = await client.post(f"/api/sessions/{sid}/start")
        assert r.status_code == 400                        # illegal double start
        r = await client.post(f"/api/sessions/{sid}/stop", json={"policy": "HOLD"})
        assert r.json()["status"] == "STOPPED"
        r = await client.post(f"/api/sessions/{sid}/start")
        assert r.status_code == 400                        # illegal: STOPPED -> RUNNING

        # clone endpoint
        r = await client.post(f"/api/sessions/{sid}/clone",
                              json={"name": "clone-api", "overrides": {"capital_initial": 45000}})
        assert r.status_code == 201
        clone_id = r.json()["id"]

        # summary
        r = await client.get("/api/lab/summary")
        body = r.json()
        assert set(body) >= {"sessions", "best", "system"}
        s0 = body["sessions"][0]
        assert {"id", "name", "status", "terminal_state", "capital_initial", "equity",
                "pnl_abs", "return_pct", "max_dd_pct", "trades", "wins", "win_rate",
                "open_positions", "strategy_id", "ml_enabled", "last_decision_at",
                "health", "sparkline"} <= set(s0)
        assert {"feed", "last_tick_age_s", "sessions_running",
                "db_ok", "incidents_24h", "heartbeat"} <= set(body["system"])
        # health-surface extension: feed_health + derived degraded flag
        assert set(body["system"]) == {"feed", "last_tick_age_s", "sessions_running",
                                       "db_ok", "incidents_24h", "heartbeat",
                                       "feed_health", "feed_degraded"}

        # detail
        r = await client.get(f"/api/sessions/{sid}")
        d = r.json()
        for key in ("id", "name", "status", "terminal_state", "capital_initial", "config",
                    "portfolio", "positions", "trades", "equity_curve", "drawdown_curve",
                    "funnel_latest", "decisions", "last_decision_at", "feed_status"):
            assert key in d, key
        pf = d["portfolio"]
        assert {"cash", "invested", "unrealized", "realized", "equity", "hwm",
                "drawdown_pct", "gross_exposure", "total_open_risk"} <= set(pf)

        # compare
        r = await client.get(f"/api/lab/compare?ids={sid},{clone_id}")
        cmp = r.json()["sessions"]
        assert len(cmp) == 2
        m = cmp[0]["metrics"]
        assert {"return_pct", "max_dd_pct", "win_rate", "pf", "expectancy", "avg_win",
                "avg_loss", "avg_hold_days", "turnover", "exposure_pct",
                "cost_drag"} <= set(m)
        assert isinstance(cmp[0]["equity_curve"], list)
        assert isinstance(cmp[0]["by_trade"], list)

        r = await client.get("/api/system/health")
        assert r.status_code == 200 and "feed" in r.json()

        r = await client.get(f"/api/sessions/{sid}/decisions/999999")
        assert r.status_code == 404


async def test_decision_replay_from_persisted_records(env):
    e = env
    sid = await make_session(e, "replay")
    await start(e, sid)
    aaa, bbb, _ = day1_bars()
    await drive(e, interleave(aaa[:3], bbb[:3]))
    iid = e.conn.execute(
        "SELECT id FROM intents WHERE session_id=? AND decision='ENTER' LIMIT 1",
        (sid,)).fetchone()["id"]
    await e.mgr.stop(sid, policy_override="HOLD")

    app = create_app(e.mgr, e.md, e.conn, recover_on_startup=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        r = await client.get(f"/api/sessions/{sid}/decisions/{iid}")
        replay = r.json()
        assert replay["action"] == "ENTER"
        assert replay["rules"] and isinstance(replay["rules"], list)
        assert len(replay["risk_checks"]) == 10   # 9 normative + sizing_math
        assert replay["order"] is not None
        assert {"order_id", "status", "filled_qty", "avg_fill_px"} <= set(replay["order"])
        assert replay["features"]["action"] == "ENTER"


async def test_pages_template_fallback(env):
    """Templates now ship at src/sts/api/templates -> pages must RENDER (200 HTML).
    The JSON fallback only fires if the template dir is genuinely absent."""
    e = env
    app = create_app(e.mgr, e.md, e.conn, recover_on_startup=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        r = await client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "SWING LAB" in r.text or "swing" in r.text.lower()
        r = await client.get("/sessions/new")
        assert r.status_code == 200
