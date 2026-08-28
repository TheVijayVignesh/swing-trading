"""Liveness watchdog + bar-close coalescing tests (audit v2 + boot-grace v3).

Covers:
- closed market: WAITING_MARKET_OPEN heartbeat max hourly, ACTIVITY persisted,
  never any stale/init incident
- open market + zero bars: INITIALIZING inside the boot grace
  (BOOT_INIT_GRACE_S = WATCHDOG_STALE_BAR_AFTER_S = BAR_PERIOD 300 + POLL 60 +
  LATENCY 60 = 420s), FEED_INIT_TIMEOUT exactly once beyond it, and ONE
  FEED_RECOVERED INFO when a valid bar closes the episode
- open market + bars stopped: emission-aware staleness — legal gaps (<420s)
  stay healthy; >=420s opens exactly one FEED_STALE_ENTRIES_BLOCKED episode
- liveness floors: account snapshot + SCAN_FUNNEL (scanned=0, 'no data') on
  15min/30min cadences even with zero bars
- coalescing: a whole batch of symbol-bars sharing one ts yields EXACTLY one
  SCAN_FUNNEL and one account_snapshot for that ts (200-bar amplification fix)
- scan-now: DEFERRED / MARKET_CLOSED decisions persisted when market is closed
- atomic decision writes: crash between intent and order persists neither
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import zoneinfo

import pandas as pd
import pytest

from sts.config import SessionConfig
from sts.contracts import Bar, ExitReason, Side, OrderType, TradeIntent
from sts.data import calendar as cal
from sts.lab.manager import LabManager
from sts.lab.runner import SessionRunner
from sts.marketdata.service import MarketDataService
from sts.storage.db import init_db
from sts.storage.repos import TradingRepo

IST = zoneinfo.ZoneInfo("Asia/Kolkata")
SUNDAY = dt.datetime(2026, 8, 23, 12, 0)            # market CLOSED
MONDAY_OPEN = dt.datetime(2026, 8, 24, 10, 0)       # Monday, market OPEN


class FakeClock:
    def __init__(self, start: dt.datetime) -> None:
        self.now = start

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, minutes: float = 0.0) -> None:
        self.now = self.now + dt.timedelta(minutes=minutes)


def bts(day: dt.date, h: int, m: int) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(h, m))   # IST-naive bar OPEN time


def bar(sym: str, ts: dt.datetime, c=100.0, v=130_000) -> Bar:
    return Bar(symbol=sym, ts=ts, open=c, high=c + 2, low=c - 2, close=c,
               volume=v, timeframe="5m")


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
    for k in (-4, -3, -2):
        closes[k] -= 50.0
    rows = [{"date": d, "open": c, "high": c + 10.0, "low": c - 10.0,
             "close": c, "volume": 200_000.0, "adjclose": c}
            for d, c in zip(dates, closes)]
    return pd.DataFrame(rows)


class FakePoller:
    def __init__(self) -> None:
        self.bars: dict[str, Bar] = {}

    def poll_once(self):
        return len(self.bars), 0

    def get_bars(self) -> dict[str, Bar]:
        return dict(self.bars)


@pytest.fixture()
def env(tmp_path):
    conn = init_db(str(tmp_path / "journal.db"))
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    for sym in ("AAA", "BBB"):
        uptrend_frame(dt.date(2026, 8, 21), 80, 500.0, 0.6).to_parquet(
            daily_dir / f"{sym}.parquet", index=False)
    idx = uptrend_frame(dt.date(2026, 8, 21), 80, 100.0, 0.5)
    idx.to_parquet(daily_dir / "_NSEI.parquet", index=False)

    poller = FakePoller()
    md = MarketDataService(["AAA", "BBB"], poller=poller,
                           clock=lambda: MONDAY_OPEN.replace(tzinfo=IST),
                           daily_dir=daily_dir)
    universes = {"LAB1": ["AAA", "BBB", "CCC"]}
    mgr = LabManager(conn, md, universe_resolver=lambda name: universes[name])

    class Env:
        pass
    e = Env()
    e.conn = conn
    e.md = md
    e.poller = poller
    e.mgr = mgr
    yield e
    conn.close()


def make_runner(e, clock: FakeClock) -> SessionRunner:
    cfg = SessionConfig(name="liveness", capital_initial=60_000.0, mode="paper",
                        universe="LAB1", strategy_id="pullback-v1",
                        params={"rsi_min": 99, "rsi_max": 100})  # never enters
    sid = e.mgr.create_session(cfg)
    from sts.lab.factory import build_session_graph
    graph = e.mgr.graphs.get(sid) or build_session_graph(cfg, e.conn, sid)
    runner = SessionRunner(graph, e.md, ["AAA", "BBB", "CCC"], clock=clock,
                           watchdog_interval_s=60.0)
    return runner


# ------------------------------------------------------------------ watchdog
async def test_closed_market_hourly_heartbeat(env):
    e = env
    clock = FakeClock(SUNDAY)
    runner = make_runner(e, clock)

    runner.watchdog_tick()
    runner.watchdog_tick()                       # same hour -> dedupe
    n1 = e.conn.execute("SELECT COUNT(*) n FROM session_events WHERE event='WAITING_MARKET_OPEN'"
                        ).fetchone()["n"]
    assert n1 == 1

    clock.advance(minutes=61)
    runner.watchdog_tick()
    n2 = e.conn.execute("SELECT COUNT(*) n FROM session_events WHERE event='WAITING_MARKET_OPEN'"
                        ).fetchone()["n"]
    assert n2 == 2, "closed-market heartbeat at most once per hour"

    acts = e.conn.execute("SELECT detail_json FROM session_events WHERE event='ACTIVITY'"
                          " ORDER BY id").fetchall()
    assert acts, "ACTIVITY state must be persisted every tick"
    for r in acts:
        assert json.loads(r["detail_json"])["activity"]["state"] == "WAITING_MARKET_OPEN"


async def test_boot_grace_init_timeout_then_single_recovery(env):
    """Boot contract (v3): barless OPEN market inside BOOT_INIT_GRACE_S is
    INITIALIZING with zero incidents; at/after the grace FEED_INIT_TIMEOUT
    fires exactly once; a valid bar closes the episode with exactly ONE
    FEED_RECOVERED INFO and restores health."""
    e = env
    clock = FakeClock(MONDAY_OPEN)
    assert cal.market_phase(clock()) == "OPEN"
    runner = make_runner(e, clock)

    # ticks at waited = 0..360s: inside grace -> INITIALIZING, no incidents
    for _ in range(7):
        act = runner.watchdog_tick()
        assert act["state"] == "INITIALIZING"
        assert act["blocker_detail"]["waited_seconds"] < 420
        clock.advance(minutes=1)
    inc = e.conn.execute("SELECT COUNT(*) n FROM incidents").fetchone()["n"]
    assert inc == 0, "no incident may fire inside the boot init grace"

    # beyond grace (waited 420s >= threshold): FEED_INIT_TIMEOUT, once
    clock.advance(minutes=1)
    act = runner.watchdog_tick()
    assert act["state"] == "FEED_STALE"
    assert act["blocker_detail"].get("init_timeout") is True
    assert runner.health == "stale"
    rows = e.conn.execute(
        "SELECT severity, detail_json FROM incidents WHERE kind='FEED_INIT_TIMEOUT'"
    ).fetchall()
    assert len(rows) == 1, "FEED_INIT_TIMEOUT must fire exactly once"
    assert rows[0]["severity"] == "WARN"
    detail = json.loads(rows[0]["detail_json"])
    assert detail["detected_by"] == "initialization-watchdog"
    assert detail["waited_seconds"] >= int(420)

    runner.watchdog_tick()                       # same episode -> no duplicate
    n = e.conn.execute("SELECT COUNT(*) n FROM incidents "
                       "WHERE kind='FEED_INIT_TIMEOUT'").fetchone()["n"]
    assert n == 1

    # first valid bar arrives -> ONE FEED_RECOVERED INFO + health restored
    e.md._last_tick_at = MONDAY_OPEN.replace(tzinfo=IST)   # feed OPEN
    runner._process_bar(bar("AAA", bts(dt.date(2026, 8, 24), 9, 35)))
    assert runner.health == "ok"
    rec = e.conn.execute(
        "SELECT severity, detail_json FROM incidents WHERE kind='FEED_RECOVERED'"
    ).fetchall()
    assert len(rec) == 1, "recovery must journal exactly one FEED_RECOVERED"
    assert rec[0]["severity"] == "INFO"
    assert json.loads(rec[0]["detail_json"])["duration_seconds"] is not None

    # steady state afterwards: still exactly one recovery, health stays ok
    clock.advance(minutes=1)
    act = runner.watchdog_tick()
    assert runner.health == "ok"
    assert act["state"] != "FEED_STALE"
    n = e.conn.execute("SELECT COUNT(*) n FROM incidents "
                       "WHERE kind='FEED_RECOVERED'").fetchone()["n"]
    assert n == 1


@pytest.mark.parametrize("age_s", [299, 300, 359, 360, 419])
async def test_bar_gap_below_emission_threshold_stays_healthy(env, age_s):
    """Boundary sweep, bars previously flowing then stopped, phase OPEN:
    any silence below the emission-aware threshold (420s = window 300 +
    poll 60 + latency margin 60) must NOT trip staleness."""
    e = env
    clock = FakeClock(MONDAY_OPEN)
    runner = make_runner(e, clock)
    e.md._last_tick_at = MONDAY_OPEN.replace(tzinfo=IST)   # feed OPEN
    runner._process_bar(bar("AAA", bts(dt.date(2026, 8, 24), 9, 35)))

    clock.advance(minutes=age_s / 60.0)
    act = runner.watchdog_tick()
    assert runner.health == "ok"
    inc = e.conn.execute(
        "SELECT COUNT(*) n FROM incidents "
        "WHERE kind IN ('FEED_STALE_ENTRIES_BLOCKED','FEED_INIT_TIMEOUT')"
    ).fetchone()["n"]
    assert inc == 0, f"gap of {age_s}s is legal; no incident allowed"
    assert act["state"] in {"TRADING", "SCANNING", "RISK_BLOCKED", "NO_SETUPS"}


async def test_bar_gap_at_emission_threshold_opens_single_stale_episode(env):
    """At exactly 420s of silence the emission-aware watchdog opens ONE stale
    episode; continued barless ticks never duplicate it."""
    e = env
    clock = FakeClock(MONDAY_OPEN)
    runner = make_runner(e, clock)
    e.md._last_tick_at = MONDAY_OPEN.replace(tzinfo=IST)   # feed OPEN
    runner._process_bar(bar("AAA", bts(dt.date(2026, 8, 24), 9, 35)))

    clock.advance(minutes=7)                     # age == 420s
    act = runner.watchdog_tick()
    assert act["state"] == "FEED_STALE"
    assert runner.health == "stale"

    def stale_count():
        return e.conn.execute(
            "SELECT COUNT(*) n FROM incidents "
            "WHERE kind='FEED_STALE_ENTRIES_BLOCKED'").fetchone()["n"]

    assert stale_count() == 1, ">=420s silence trips exactly one stale incident"
    clock.advance(minutes=1)                     # same episode continues
    runner.watchdog_tick()
    assert stale_count() == 1, "continued ticks dedupe to one episode"


async def test_liveness_floors_snapshot_and_funnel(env):
    e = env
    clock = FakeClock(MONDAY_OPEN)
    runner = make_runner(e, clock)

    runner.watchdog_tick()
    snaps = e.conn.execute("SELECT COUNT(*) n FROM account_snapshots").fetchone()["n"]
    funnels = e.conn.execute(
        "SELECT detail_json FROM session_events WHERE event='SCAN_FUNNEL'").fetchall()
    assert snaps >= 1, "snapshot liveness floor must write with ZERO bars"
    assert any(json.loads(r["detail_json"]).get("scanned") == 0 and
               json.loads(r["detail_json"]).get("explanation") == "no data"
               for r in funnels), "SCAN_FUNNEL floor needs scanned=0 + explanation"

    clock.advance(minutes=5)
    runner.watchdog_tick()
    snaps = e.conn.execute("SELECT COUNT(*) n FROM account_snapshots").fetchone()["n"]
    assert snaps == 1, "floors are cadence-limited"

    clock.advance(minutes=16)
    runner.watchdog_tick()
    snaps = e.conn.execute("SELECT COUNT(*) n FROM account_snapshots").fetchone()["n"]
    assert snaps >= 2, "snapshot floor re-fires after >=15min without data"


# ------------------------------------------------------- coalesced scanning
async def test_batch_of_200_symbol_bars_one_ts_yields_single_scan_and_snapshot(env):
    e = env
    sid = e.mgr.create_session(SessionConfig(
        name="coalesce", capital_initial=60_000.0, mode="paper", universe="LAB1",
        strategy_id="pullback-v1"))
    e.mgr.start(sid)
    await asyncio.sleep(0.02)
    runner = e.mgr.runners[sid]

    t = bts(dt.date(2026, 8, 21), 9, 35)         # single bar-close ts group
    batch = [bar(f"F{i}", t, c=90 + i % 7) for i in range(198)]
    batch += [bar("AAA", t, c=550), bar("BBB", t, c=550)]

    # stamp a fresh feed tick so feed_status() == OPEN (as after a real poll)
    for b in batch:
        e.poller.bars[b.symbol] = b
    e.md._last_tick_at = MONDAY_OPEN.replace(tzinfo=IST)

    runner._process_event(("bars", batch))       # documented v2 batch interface
    runner._drain_pending_scans()

    funnels = e.conn.execute(
        "SELECT COUNT(*) n FROM session_events WHERE session_id=? AND event='SCAN_FUNNEL'",
        (sid,)).fetchone()["n"]
    snaps = e.conn.execute(
        "SELECT COUNT(*) n FROM account_snapshots WHERE session_id=?",
        (sid,)).fetchone()["n"]
    assert funnels == 1, "exactly ONE scan per unique 5m bar-close ts"
    assert snaps == 1, "exactly ONE account snapshot per unique bar-close ts"

    # a second ts triggers exactly one more of each
    t2 = bts(dt.date(2026, 8, 21), 9, 40)
    runner._process_event(("bars", [bar("AAA", t2, c=551)]))
    runner._drain_pending_scans()
    funnels = e.conn.execute(
        "SELECT COUNT(*) n FROM session_events WHERE session_id=? AND event='SCAN_FUNNEL'",
        (sid,)).fetchone()["n"]
    assert funnels == 2

    await e.mgr.stop(sid, policy_override="HOLD")


# ------------------------------------------------------------------ scan-now
async def test_scan_now_defers_when_market_closed(env):
    e = env
    clock = FakeClock(SUNDAY.replace(hour=11))   # time-of-day inside 09:30-14:30
    runner = make_runner(e, clock)
    sid = runner.session_id

    # widen the momentum band and seed today's intraday bars so the
    # deterministic strategy finds real setups (intraday high breaks
    # prior-day high, volume >= multiple x SMA20)
    runner.cfg.params["rsi_min"] = 10
    runner.cfg.params["rsi_max"] = 100
    t0 = bts(dt.date(2026, 8, 21), 9, 35)
    runner._intraday["AAA"] = [
        Bar(symbol="AAA", ts=t0, open=550, high=560, low=548, close=558,
            volume=200_000, timeframe="5m"),
        Bar(symbol="AAA", ts=dt.datetime(2026, 8, 21, 9, 40), open=558, high=562,
            low=556, close=560, volume=220_000, timeframe="5m"),
    ]
    result = await runner.run_scan_now("diagnostic")
    assert result["candidates"], "expected candidates from engineered daily data"
    assert result["deferrals"], "closed market => approved candidates deferred"
    rows = e.conn.execute(
        "SELECT decision, rejection_reason FROM intents WHERE session_id=?", (sid,)
    ).fetchall()
    deferred = [r for r in rows if r["decision"] == "DEFERRED"]
    assert deferred, "DEFERRED decisions must be REAL persisted intents (replayable)"
    assert all(r["rejection_reason"] == "MARKET_CLOSED" for r in deferred)
    orders = e.conn.execute(
        "SELECT COUNT(*) n FROM orders WHERE session_id=?", (sid,)).fetchone()["n"]
    assert orders == 0, "scan-now on a closed market must place NO orders"


# ------------------------------------------------------------ atomic writes
async def test_crash_between_intent_and_order_persists_neither(env):
    e = env
    clock = FakeClock(MONDAY_OPEN)
    runner = make_runner(e, clock)
    repo = runner.graph.repo
    sink = runner.graph.sink

    intent = TradeIntent(session_id=runner.session_id, ts=MONDAY_OPEN, symbol="AAA",
                         side=Side.SELL, order_type=OrderType.LIMIT, qty=5,
                         limit_price=500.0,
                         correlation_id=f"{runner.session_id}:EXIT:TEST:{MONDAY_OPEN.isoformat()}")

    def exploding_on_order(ov):
        raise KeyboardInterrupt   # simulates a hard crash mid-placement

    sink.on_order = exploding_on_order
    with pytest.raises(KeyboardInterrupt):
        runner._place_exit(intent, reason=ExitReason.REGIME_EXIT,
                           stale_note=False, ts=MONDAY_OPEN)
    n_intents = e.conn.execute("SELECT COUNT(*) n FROM intents").fetchone()["n"]
    n_orders = e.conn.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]
    assert n_intents == 0 and n_orders == 0, \
        "crash between intent and order inserts must commit NEITHER row"

    # repo-level atomic primitive: both rows commit together...
    iid, oid = repo.record_decision(
        {"ts": MONDAY_OPEN.isoformat(), "symbol": "AAA",
         "decision": "ENTER", "rejection_reason": ""},
        {"broker_order_id": "o-1", "side": "BUY", "type": "LIMIT", "qty": 5,
         "limit_px": 500.0, "status": "WORKING", "submitted_at": MONDAY_OPEN.isoformat(),
         "idempotency_key": "k1"})
    assert iid and oid
    # ...and a failure in the ORDER insert rolls the INTENT back too.
    with pytest.raises(Exception):
        repo.record_decision(
            {"ts": MONDAY_OPEN.isoformat(), "symbol": "BBB",
             "decision": "ENTER", "rejection_reason": ""},
            {"no_such_column": 1})   # OperationalError -> full rollback
    orphan = e.conn.execute(
        "SELECT COUNT(*) n FROM intents WHERE symbol='BBB' AND decision='ENTER'"
    ).fetchone()["n"]
    assert orphan == 0, "no orphaned intent without its order"
