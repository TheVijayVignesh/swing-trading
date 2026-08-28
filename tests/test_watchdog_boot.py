"""Boot-state watchdog tests (boot-grace v3).

Scenario matrix for a barless OPEN market measured against
BOOT_INIT_GRACE_S = WATCHDOG_STALE_BAR_AFTER_S (= BAR_PERIOD_S 300 +
POLL_CADENCE_S 60 + EMISSION_LATENCY_MARGIN_S 60 = 420s):

(a) boot -> ticks < grace, zero bars   -> INITIALIZING, zero incidents
(b) boot -> zero bars >= grace         -> FEED_INIT_TIMEOUT exactly once
(c) boot -> first valid bar < grace    -> leaves INITIALIZING, zero incidents,
                                          HEALTHY
(d) init-timeout / stale episode then valid bar -> ONE FEED_RECOVERED INFO,
                                          health ok (dedup on further ticks)
(e) closed market                      -> WAITING_MARKET_OPEN heartbeats only,
                                          never stale/init incidents

The old contract ("stale within one tick at boot") was a boot artifact: a
healthy emission pipeline legally needs window completion + next poll + latency
before the FIRST completed 5m bar can land.
"""
from __future__ import annotations

import datetime as dt
import json
import zoneinfo

import pandas as pd
import pytest

from sts.config import SessionConfig
from sts.contracts import Bar
from sts.data import calendar as cal
from sts.lab.manager import LabManager
from sts.lab.runner import (
    BOOT_INIT_GRACE_S,
    WATCHDOG_STALE_BAR_AFTER_S,
    SessionRunner,
)
from sts.marketdata.service import MarketDataService
from sts.storage.db import init_db

IST = zoneinfo.ZoneInfo("Asia/Kolkata")
SUNDAY = dt.datetime(2026, 8, 23, 12, 0)           # market CLOSED
MONDAY_OPEN = dt.datetime(2026, 8, 24, 10, 0)      # Monday, market OPEN


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
    cfg = SessionConfig(name="boot", capital_initial=60_000.0, mode="paper",
                        universe="LAB1", strategy_id="pullback-v1",
                        params={"rsi_min": 99, "rsi_max": 100})  # never enters
    sid = e.mgr.create_session(cfg)
    from sts.lab.factory import build_session_graph
    graph = e.mgr.graphs.get(sid) or build_session_graph(cfg, e.conn, sid)
    runner = SessionRunner(graph, e.md, ["AAA", "BBB", "CCC"], clock=clock,
                           watchdog_interval_s=60.0)
    assert BOOT_INIT_GRACE_S == WATCHDOG_STALE_BAR_AFTER_S == 420.0
    return runner


def incident_rows(e, kind: str | None = None) -> list:
    sql = "SELECT severity, kind, detail_json FROM incidents"
    if kind:
        sql += f" WHERE kind='{kind}'"
    return e.conn.execute(sql).fetchall()


# ------------------------------------------------------------------ (a)
async def test_boot_within_grace_zero_bars_is_initializing_without_incidents(env):
    e = env
    clock = FakeClock(MONDAY_OPEN)
    runner = make_runner(e, clock)

    for waited in (0, 60, 120, 180, 240, 300):     # all strictly < grace
        act = runner.watchdog_tick()
        assert act["state"] == "INITIALIZING"
        assert act["blocker_detail"]["waited_seconds"] == waited
        clock.advance(minutes=1)
    acts = e.conn.execute(
        "SELECT detail_json FROM session_events WHERE event='ACTIVITY' "
        "ORDER BY id").fetchall()
    states = [json.loads(r["detail_json"])["activity"]["state"] for r in acts]
    assert states and set(states) == {"INITIALIZING"}
    assert incident_rows(e) == [], "zero incidents inside the boot grace"


# ------------------------------------------------------------------ (b)
async def test_boot_beyond_grace_fires_feed_init_timeout_exactly_once(env):
    e = env
    clock = FakeClock(MONDAY_OPEN)
    runner = make_runner(e, clock)

    runner.watchdog_tick()                          # waited 0s: initializing
    clock.advance(minutes=8)                        # waited 480s > 420s grace
    act = runner.watchdog_tick()
    assert act["state"] == "FEED_STALE"             # fail-closed surface state
    assert act["blocker_detail"].get("init_timeout") is True
    assert act["blocker_detail"]["seconds_since_bar"] is None
    assert runner.health == "stale"

    rows = incident_rows(e, "FEED_INIT_TIMEOUT")
    assert len(rows) == 1
    assert rows[0]["severity"] == "WARN"
    detail = json.loads(rows[0]["detail_json"])
    assert detail["detected_by"] == "initialization-watchdog"
    assert detail["waited_seconds"] >= int(BOOT_INIT_GRACE_S)

    clock.advance(minutes=1)                        # still no bars: dedupe
    runner.watchdog_tick()
    assert len(incident_rows(e, "FEED_INIT_TIMEOUT")) == 1


# ------------------------------------------------------------------ (c)
async def test_first_bar_within_grace_leaves_initializing_no_incidents(env):
    e = env
    clock = FakeClock(MONDAY_OPEN)
    runner = make_runner(e, clock)

    act = runner.watchdog_tick()
    assert act["state"] == "INITIALIZING"

    e.md._last_tick_at = MONDAY_OPEN.replace(tzinfo=IST)      # feed OPEN
    clock.advance(minutes=0.5)
    runner._process_bar(bar("AAA", bts(dt.date(2026, 8, 24), 9, 35)))
    clock.advance(minutes=0.5)

    act = runner.watchdog_tick()
    assert act["state"] != "INITIALIZING", "first valid bar ends boot state"
    assert act["state"] != "FEED_STALE"
    assert runner.health == "ok"
    assert incident_rows(e) == [], "healthy first delivery journals nothing"


# ------------------------------------------------------------------ (d)
async def test_init_timeout_episode_recovers_with_single_feed_recovered(env):
    e = env
    clock = FakeClock(MONDAY_OPEN)
    runner = make_runner(e, clock)

    clock.advance(minutes=8)                        # past grace
    runner.watchdog_tick()
    assert len(incident_rows(e, "FEED_INIT_TIMEOUT")) == 1
    assert runner.health == "stale"

    e.md._last_tick_at = MONDAY_OPEN.replace(tzinfo=IST)      # feed OPEN
    runner._process_bar(bar("AAA", bts(dt.date(2026, 8, 24), 9, 35)))
    assert runner.health == "ok", "valid bar restores health immediately"

    rec = incident_rows(e, "FEED_RECOVERED")
    assert len(rec) == 1, "exactly one FEED_RECOVERED per episode"
    assert rec[0]["severity"] == "INFO"
    detail = json.loads(rec[0]["detail_json"])
    assert detail["episode"] == "FEED_INIT_TIMEOUT"
    assert detail["duration_seconds"] is not None

    clock.advance(minutes=1)                        # further ticks: no spam
    act = runner.watchdog_tick()
    assert runner.health == "ok"
    assert act["state"] != "FEED_STALE"
    assert len(incident_rows(e, "FEED_RECOVERED")) == 1


# ------------------------------------------------------------------ (e)
async def test_closed_market_yields_only_waiting_open_never_incidents(env):
    e = env
    clock = FakeClock(SUNDAY)
    runner = make_runner(e, clock)

    for _ in range(5):                              # ~4h of closed-market ticks
        act = runner.watchdog_tick()
        assert act["state"] == "WAITING_MARKET_OPEN"
        clock.advance(minutes=61)                   # force hourly heartbeat
    beats = e.conn.execute(
        "SELECT COUNT(*) n FROM session_events WHERE event='WAITING_MARKET_OPEN'"
    ).fetchone()["n"]
    assert beats >= 2, "closed-market heartbeat keeps firing hourly"
    assert incident_rows(e) == [], "closed market must never raise stale/init incidents"
