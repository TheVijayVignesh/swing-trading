"""Health-surface tests: MarketDataService.feed_health(), the feed_degraded
truth-table helper, /api/system/health + /api/lab/board passthrough, and the
dashboard health-strip rendering hooks.

Teammate contract shapes consumed defensively:
  - poller.active_source_name -> str         (@property on FailoverPoller;
                                              plain methods also tolerated)
  - poller.source_health() -> {"primary": {...}, "fallback": {...}}
  - md.daily_data_status() -> {...}          (not surfaced here; must not break)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import zoneinfo
from types import SimpleNamespace

import httpx
import pytest

from sts.api.app import create_app
from sts.api.routes_api import _system_block, feed_degraded
from sts.config import SessionConfig
from sts.contracts import Bar
from sts.lab.manager import LabManager
from sts.marketdata.service import MarketDataService
from sts.storage.db import init_db
from sts.storage.repos import SessionRepo, TradingRepo

IST = zoneinfo.ZoneInfo("Asia/Kolkata")
SAT_NOON = dt.datetime(2026, 8, 22, 12, 0, tzinfo=IST)    # weekend -> phase CLOSED
TUE_OPEN = dt.datetime(2026, 8, 25, 10, 30, tzinfo=IST)   # Tuesday -> phase OPEN


def bar(sym: str, ts: dt.datetime) -> Bar:
    return Bar(symbol=sym, ts=ts, open=100.0, high=101.0, low=99.0,
               close=100.5, volume=1000, timeframe="5m")


class FakePoller:
    def __init__(self) -> None:
        self.bars: dict[str, Bar] = {}

    def poll_once(self):
        return len(self.bars), 0

    def get_bars(self) -> dict[str, Bar]:
        return dict(self.bars)


class HealthPoller(FakePoller):
    """Teammate-B shape: FailoverPoller gains source_health/active_source_name."""

    def active_source_name(self) -> str:
        return "YahooChartPoller"

    def source_health(self) -> dict:
        return {
            "primary": {"name": "NSEQuotePoller", "status": "FAILED",
                        "consecutive_failures": 4, "last_error": "HTTP 429",
                        "last_success_ts": None},
            "fallback": {"name": "YahooChartPoller", "status": "OK",
                         "consecutive_failures": 0, "last_error": None,
                         "last_success_ts": "2026-08-25T10:00:00+05:30"},
        }


class GarbagePoller(FakePoller):
    """source_health present but malformed — must normalize, never raise."""

    def active_source_name(self) -> str:
        return "YahooChartPoller"

    def source_health(self) -> dict:
        return {"primary": "not-a-dict"}


def make_service(tmp_path, poller=None, clock=lambda: SAT_NOON) -> MarketDataService:
    return MarketDataService(["AAA", "BBB"], poller=poller or FakePoller(),
                             clock=clock, daily_dir=tmp_path / "daily",
                             poll_seconds=60)


def make_app(tmp_path, md=None, clock=lambda: SAT_NOON):
    conn = init_db(str(tmp_path / "journal.db"))
    (tmp_path / "daily").mkdir(exist_ok=True)
    md = md or make_service(tmp_path, clock=clock)
    mgr = LabManager(conn, md, universe_resolver=lambda name: ["AAA", "BBB"])
    return create_app(mgr, md, conn, recover_on_startup=False), conn


# ------------------------------------------------------------- feed_health()
def test_feed_health_shape_idle_weekend(tmp_path):
    svc = make_service(tmp_path)
    fh = svc.feed_health()
    assert fh["state"] == "CLOSED"           # weekend -> market phase CLOSED
    assert fh["phase"] == "CLOSED"
    assert fh["source"] == "NSEQuotePoller"  # poller lacks active_source_name
    assert fh["dropped_events"] == 0
    assert fh["last_bar"] is None            # no bars yet
    assert fh["last_tick_age_s"] is None
    for side in ("primary", "fallback"):
        assert fh[side] == {"name": side.upper(), "status": "UNKNOWN",
                            "consecutive_failures": 0, "last_error": None,
                            "last_success_ts": None}


def test_feed_health_last_bar_symbol_ts_age(tmp_path):
    svc = make_service(tmp_path)
    tick = SAT_NOON - dt.timedelta(seconds=70)
    with svc._lock:
        svc._latest["AAA"] = bar("AAA", SAT_NOON.replace(tzinfo=None)
                                 - dt.timedelta(minutes=5))
        svc._last_tick_at = tick
    fh = svc.feed_health()
    lb = fh["last_bar"]
    assert lb is not None
    assert lb["symbol"] == "AAA"
    assert lb["ts"].startswith("2026-08-22T11:")   # iso, naive bar kept as-is
    assert lb["age_s"] == 300                      # bar 11:55 vs clock 12:00 IST
    assert isinstance(lb["age_s"], int)
    assert fh["last_tick_age_s"] == 70


def test_feed_health_consumes_poller_source_health(tmp_path):
    svc = make_service(tmp_path, poller=HealthPoller())
    fh = svc.feed_health()
    assert fh["source"] == "YahooChartPoller"
    assert fh["primary"]["status"] == "FAILED"
    assert fh["primary"]["consecutive_failures"] == 4
    assert fh["primary"]["last_error"] == "HTTP 429"
    assert fh["fallback"]["status"] == "OK"
    assert fh["fallback"]["last_success_ts"] == "2026-08-25T10:00:00+05:30"


def test_feed_health_defends_against_malformed_source_health(tmp_path):
    svc = make_service(tmp_path, poller=GarbagePoller())
    fh = svc.feed_health()
    assert fh["source"] == "YahooChartPoller"
    assert fh["primary"]["status"] == "UNKNOWN"     # non-dict -> normalized
    assert fh["fallback"]["status"] == "UNKNOWN"    # absent -> defaulted


def test_feed_health_reflects_dropped_events(tmp_path):
    svc = make_service(tmp_path)
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    item = ("bars", [bar("AAA", SAT_NOON)])
    q.put_nowait(item)                               # fill the queue
    svc._enqueue_or_count_drop(svc, q, item)         # overflow -> counted drop
    assert svc.dropped_events == 1
    assert svc.feed_health()["dropped_events"] == 1


def test_feed_health_state_open_when_ticking(tmp_path):
    svc = make_service(tmp_path, clock=lambda: TUE_OPEN)   # Tue 10:30 IST -> OPEN
    with svc._lock:
        svc._last_tick_at = TUE_OPEN - dt.timedelta(seconds=30)
    fh = svc.feed_health()
    assert fh["state"] == "OPEN"
    assert fh["phase"] == "OPEN"


# ------------------------------------------------------------ feed_degraded()
@pytest.mark.parametrize("payload,expected", [
    (None, False),                                   # unknown != unhealthy
    ({}, False),
    ({"state": "OPEN", "dropped_events": 0,
      "fallback": {"status": "OK"}}, False),         # fully healthy
    ({"state": "OPEN", "dropped_events": 1,
      "fallback": {"status": "OK"}}, True),          # drops => degraded
    ({"state": "OPEN", "dropped_events": 0,
      "fallback": {"status": "FAILED"}}, True),      # fallback down
    ({"state": "OPEN", "dropped_events": 0,
      "fallback": {"status": "DEGRADED"}}, True),
    ({"state": "OPEN", "dropped_events": 0,
      "fallback": {"status": "failed"}}, True),      # case-insensitive
    ({"state": "STALE", "dropped_events": 0,
      "fallback": {"status": "OK"}}, True),          # stale during OPEN phase
    ({"state": "CLOSED", "dropped_events": 0,
      "fallback": {"status": "OK"}}, False),         # closed & clean is neutral
    ({"state": "OPEN", "dropped_events": "3",
      "fallback": {"status": "OK"}}, True),          # numeric strings tolerated
    ({"state": "OPEN", "fallback": "garbage"}, False),  # non-dict side ignored
])
def test_feed_degraded_truth_table(payload, expected):
    assert feed_degraded(payload) is expected


# ------------------------------------------------- /api/system/health surface
async def test_system_health_endpoint_contains_feed_health(tmp_path):
    app, _ = make_app(tmp_path, make_service(tmp_path, poller=HealthPoller()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        r = await client.get("/api/system/health")
    assert r.status_code == 200
    body = r.json()
    # legacy keys preserved (regression guard)
    assert {"feed", "last_tick_age_s", "sessions_running", "db_ok",
            "incidents_24h", "heartbeat"} <= set(body)
    fh = body["feed_health"]
    assert fh["dropped_events"] == 0
    assert fh["source"] == "YahooChartPoller"
    assert set(fh["primary"]) >= {"name", "status", "consecutive_failures",
                                  "last_error", "last_success_ts"}
    assert set(fh["fallback"]) >= {"name", "status", "consecutive_failures",
                                   "last_error", "last_success_ts"}
    assert body["feed_degraded"] is feed_degraded(fh)


async def test_system_health_flags_degraded_when_stale(tmp_path):
    md = make_service(tmp_path, clock=lambda: TUE_OPEN)   # OPEN phase, no ticks
    md.force_stale(True)
    app, _ = make_app(tmp_path, md=md)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        body = (await client.get("/api/system/health")).json()
    assert body["feed_health"]["state"] == "STALE"
    assert body["feed_degraded"] is True


async def test_system_health_survives_md_without_feed_health(tmp_path):
    """A marketdata stub lacking feed_health must NOT 500 the endpoint."""
    conn = init_db(str(tmp_path / "j.db"))
    srepo = SessionRepo(conn)
    now = dt.datetime.now(dt.timezone.utc)
    for name in ("s1", "s2"):
        sid = srepo.create_session(SessionConfig(name=name, capital_initial=1000.0))
        TradingRepo(conn, sid).record_incident("WARN", "FEED_STALE",
                                               ts=now - dt.timedelta(hours=2))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        marketdata=SimpleNamespace(feed_status=lambda: "CLOSED",
                                   last_tick_age_s=None),
        conn=conn,
        lab=SimpleNamespace(sessions=srepo),
    )))
    block = _system_block(request)
    assert block["feed_health"] is None
    assert block["feed_degraded"] is False
    assert block["incidents_24h"] == 2                     # window semantics intact
    assert dt.datetime.fromisoformat(block["heartbeat"]).tzinfo is not None


# ------------------------------------------------------- board passthrough
async def test_board_payload_includes_system_feed_health(tmp_path):
    app, _ = make_app(tmp_path, make_service(tmp_path, poller=HealthPoller()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        r = await client.get("/api/lab/board")
    assert r.status_code == 200
    sysd = r.json()["system"]
    assert sysd["feed_health"]["source"] == "YahooChartPoller"
    assert sysd["feed_health"]["primary"]["status"] == "FAILED"
    assert "incidents_24h" in sysd


# --------------------------------------------------------- dashboard render
async def test_overview_page_renders_feed_health_hooks(tmp_path):
    app, _ = make_app(tmp_path, make_service(tmp_path, poller=HealthPoller()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        r = await client.get("/")
        new_page = await client.get("/sessions/new")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    for hook in ('id="feed-health"', "data-feed-source", "data-feed-state",
                 "data-feed-last-bar", "data-feed-age-h", "data-feed-dropped",
                 "data-feed-primary", "data-feed-fallback"):
        assert hook in r.text, f"missing dashboard hook {hook}"
    assert new_page.status_code == 200
