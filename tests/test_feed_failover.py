"""Feed-stack fixes: cross-thread publishing, runtime failover, batch events,
queue-overflow accounting, stale-alert branches, NSE snapshot volumes.

All tests are offline — fake pollers / fake HTTP only (Yahoo is rate-limited;
live-network checks are marked @pytest.mark.network and excluded by default).
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest
import requests

from sts.contracts import Bar
from sts.data import calendar as cal
from sts.data import live
from sts.marketdata import service as svc
from sts.marketdata.service import MarketDataService

IST = cal.IST


def mkbar(sym: str, h: int, m: int, close: float = 100.0, vol: float = 1000.0) -> Bar:
    return Bar(symbol=sym, ts=dt.datetime(2026, 8, 25, h, m), open=close - 1,
               high=close + 1, low=close - 2, close=close, volume=vol, timeframe="5m")


class Clock:
    def __init__(self, start: dt.datetime | None = None):
        self.t = start or dt.datetime(2026, 8, 25, 10, 0, tzinfo=IST)

    def __call__(self) -> dt.datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += dt.timedelta(seconds=seconds)


class StubPoller:
    """Configurable duck-typed poller."""
    def __init__(self, result: tuple[int, int] = (0, 0), bars: dict[str, Bar] | None = None):
        self.result = result
        self.bars = bars or {}
        self.calls = 0
        self.freshness_seconds = 1.0
        self.status = "FEED_OPEN"
        self.last_success_ts: dt.datetime | None = None

    def poll_once(self) -> tuple[int, int]:
        self.calls += 1
        return self.result

    def get_bars(self) -> dict[str, Bar]:
        return dict(self.bars)


class FailingPrimary(StubPoller):
    pass


class HealthySecondary(StubPoller):
    pass


# =====================================================================
# FIX 1: cross-thread event publishing must deliver events
# =====================================================================
async def test_poll_cycle_from_foreign_thread_delivers_events():
    """Regression: _publish used asyncio.get_running_loop() inside the feed
    thread -> RuntimeError swallowed -> EVERY bar dropped. poll_cycle must run
    in a plain thread and events MUST arrive on a subscriber queue living on
    THIS (separate) running loop."""
    poller = StubPoller(result=(1, 0), bars={"AAA": mkbar("AAA", 9, 35)})
    md = MarketDataService(["AAA"], poller=poller)
    q = md.subscribe()                       # captured loop = this test's loop
    await asyncio.sleep(0)                   # let subscription settle
    updated, failed = await asyncio.to_thread(md.poll_cycle)   # feed thread
    assert (updated, failed) == (1, 0)
    kind, bars = await asyncio.wait_for(q.get(), timeout=5)
    assert kind == "bars" and len(bars) == 1 and bars[0].symbol == "AAA"


# =====================================================================
# FIX 5: coalesced batch events + overflow accounting
# =====================================================================
async def test_one_batch_event_per_cycle_with_all_advanced_bars():
    bars = {s: mkbar(s, 9, 35) for s in ("AAA", "BBB", "CCC")}
    md = MarketDataService(["AAA", "BBB", "CCC"], poller=StubPoller((3, 0), bars))
    q = md.subscribe()
    await asyncio.sleep(0)
    await asyncio.to_thread(md.poll_cycle)
    item = await asyncio.wait_for(q.get(), timeout=5)
    assert item[0] == "bars" and {b.symbol for b in item[1]} == {"AAA", "BBB", "CCC"}
    # second cycle with unchanged bars -> no new events (diff-based)
    await asyncio.to_thread(md.poll_cycle)
    assert q.empty()


async def test_queue_overflow_counted_never_silent(monkeypatch):
    monkeypatch.setattr(svc, "QUEUE_MAXSIZE", 1)
    md = MarketDataService(["AAA"], poller=StubPoller())
    q = md.subscribe()
    await asyncio.sleep(0)
    b1, b2, b3 = mkbar("AAA", 9, 35), mkbar("AAA", 9, 40), mkbar("AAA", 9, 45)
    md._publish_batch([b1])
    assert q.qsize() == 1 and md.dropped_events == 0
    md._publish_batch([b2])                  # full -> newest dropped + counted
    assert md.dropped_events == 1
    md._publish_batch([b3])
    assert md.dropped_events == 2            # every drop accounted for
    item = await q.get()
    assert item[1][0].ts == b1.ts             # first batch survived


def test_queue_maxsize_raised_to_1000():
    assert svc.QUEUE_MAXSIZE == 1000


# =====================================================================
# FIX 2: runtime failover
# =====================================================================
def test_failover_after_three_consecutive_failed_cycles():
    clock = Clock()
    bad = FailingPrimary(result=(0, 1))
    good = HealthySecondary(result=(2, 0),
                            bars={"AAA": mkbar("AAA", 9, 35), "BBB": mkbar("BBB", 9, 35)})
    fp = live.FailoverPoller([bad, good], clock=clock)

    for _ in range(2):
        fp.poll_once()
    assert fp.active_source == "FailingPrimary"      # not yet
    updated, failed = fp.poll_once()                 # 3rd consecutive failure
    assert fp.active_source == "HealthySecondary"    # switched
    assert updated == 2 and failed == 0              # bars flow immediately
    assert fp.failure_counts == {"FailingPrimary": 3, "HealthySecondary": 0}
    assert set(fp.get_bars()) == {"AAA", "BBB"}      # union serves bars


def test_failover_union_survives_source_switch_blank():
    clock = Clock()
    a = FailingPrimary(result=(0, 1), bars={})
    a.bars["AAA"] = mkbar("AAA", 9, 30)
    b = HealthySecondary(result=(1, 0), bars={"BBB": mkbar("BBB", 9, 30)})
    fp = live.FailoverPoller([a, b], clock=clock)
    fp.poll_once()                                   # primary succeeds once
    for _ in range(3):
        a.result = (0, 1)
        fp.poll_once()
    assert fp.active_source == "HealthySecondary"
    assert set(fp.get_bars()) == {"AAA", "BBB"}     # switch did NOT blank feed


def test_primary_regains_eligibility_after_cooldown():
    clock = Clock()
    primary = FailingPrimary(result=(0, 1))
    secondary = HealthySecondary(result=(0, 1))      # now BOTH fail
    fp = live.FailoverPoller([primary, secondary], clock=clock)
    for _ in range(3):
        fp.poll_once()
    assert fp.active_source == "HealthySecondary"
    for _ in range(3):
        fp.poll_once()                               # secondary fails 3x too
    assert fp.active_source == "HealthySecondary"    # primary still cooling down
    clock.advance(live.FailoverPoller.COOLDOWN_SECONDS + 1)
    for _ in range(3):
        fp.poll_once()
    assert fp.active_source == "FailingPrimary"      # regained after 10 min
    # recovery: once primary succeeds its consecutive-failure streak resets
    primary.result = (1, 0)
    primary.bars = {"AAA": mkbar("AAA", 9, 35)}
    updated, failed = fp.poll_once()
    assert (updated, failed) == (1, 0)
    assert fp.failure_counts["FailingPrimary"] >= 3   # history retained
    assert fp.consecutive_failures == 0


def test_failover_status_and_freshness_delegate_to_active():
    clock = Clock()
    primary = FailingPrimary(result=(0, 1))
    primary.status = "FEED_STALE"
    primary.freshness_seconds = None                 # never ticked
    secondary = HealthySecondary(result=(1, 0))
    secondary.status = "FEED_OPEN"
    secondary.freshness_seconds = 12.0
    fp = live.FailoverPoller([primary, secondary], clock=clock)
    assert fp.active_source == "FailingPrimary"
    assert fp.status == "FEED_STALE" and fp.freshness_seconds is None
    for _ in range(3):
        fp.poll_once()
    assert fp.status == "FEED_OPEN" and fp.freshness_seconds == 12.0


def test_failover_merges_preferring_volume_known_bars():
    clock = Clock()
    nse_like = FailingPrimary(bars={"AAA": mkbar("AAA", 9, 35, vol=0.0)})
    yahoo_like = HealthySecondary(bars={"AAA": mkbar("AAA", 9, 35, vol=4200.0)})
    nse_like.get_volume_known = lambda: {"AAA": False}
    yahoo_like.get_volume_known = lambda: {"AAA": True}
    fp = live.FailoverPoller([nse_like, yahoo_like], clock=clock)
    fp.poll_once()
    assert fp.get_bars()["AAA"].volume == 0.0        # arrived first
    fp2 = live.FailoverPoller([yahoo_like, nse_like], clock=clock)
    fp2.poll_once()
    fp2._merge_union(nse_like)                       # same-ts unknown-volume print
    assert fp2.get_bars()["AAA"].volume == 4200.0    # volume-known preferred
    assert fp2.get_volume_known()["AAA"] is True


# =====================================================================
# FIX 4: NSE snapshot volume deltas + volume_known + updated-count fix
# =====================================================================
def _nse_poller(symbols: list[str]) -> live.NSEQuotePoller:
    return live.NSEQuotePoller(symbols)


def test_nse_window_volume_is_cumulative_delta():
    p = _nse_poller(["AAA", "BBB"])
    t0 = dt.datetime(2026, 8, 25, 9, 15, tzinfo=IST)
    p._ingest_snapshot("AAA", 100.0, 101.0, 99.0, t0, cum_vol=5000.0)
    p._ingest_snapshot("AAA", 102.0, 103.0, 101.0, t0 + dt.timedelta(minutes=2),
                       cum_vol=5800.0)
    assert p.get_bars() == {}                        # window still open
    t1 = t0 + dt.timedelta(minutes=5)
    p._ingest_snapshot("AAA", 105.0, 106.0, 104.0, t1, cum_vol=6500.0)  # closes window
    bar = p.get_bar("AAA")
    assert bar.volume == pytest.approx(800.0)        # 5800 - 5000 across window
    assert p.get_volume_known()["AAA"] is True
    assert bar.volume >= 0.0


def test_nse_unknown_cumulative_volume_marks_volume_known_false():
    p = _nse_poller(["BBB"])
    t0 = dt.datetime(2026, 8, 25, 9, 15, tzinfo=IST)
    p._ingest_snapshot("BBB", 50.0, 51.0, 49.0, t0, cum_vol=None)
    p._ingest_snapshot("BBB", 52.0, 53.0, 51.0, t0 + dt.timedelta(minutes=5),
                       cum_vol=None)
    bar = p.get_bar("BBB")
    assert bar.volume == 0.0                         # kept 0 ...
    assert p.get_volume_known()["BBB"] is False      # ... but flagged unknown


def test_nse_negative_delta_clamped_to_zero():
    p = _nse_poller(["CCC"])
    t0 = dt.datetime(2026, 8, 25, 9, 15, tzinfo=IST)
    p._ingest_snapshot("CCC", 10.0, 11.0, 9.0, t0, cum_vol=900.0)
    p._ingest_snapshot("CCC", 11.0, 12.0, 10.0, t0 + dt.timedelta(minutes=5),
                       cum_vol=400.0)                # feed glitch: went DOWN
    assert p.get_bar("CCC").volume == 0.0


def test_nse_poll_once_returns_newly_updated_not_cached_size(monkeypatch):
    monkeypatch.setattr(cal, "market_phase", lambda *a, **k: "OPEN")
    p = _nse_poller(["AAA", "BBB"])
    t0 = dt.datetime(2026, 8, 25, 9, 15, tzinfo=IST)
    # seed open windows at t0 so the next snapshot advances/closes them
    p._ingest_snapshot("AAA", 100.0, 101.0, 99.0, t0, cum_vol=50.0)
    p._ingest_snapshot("BBB", 200.0, 201.0, 199.0, t0, cum_vol=50.0)

    def fetch_advance_window():
        t1 = t0 + dt.timedelta(minutes=5)
        p._ingest_snapshot("AAA", 100.0, 101.0, 99.0, t1, cum_vol=100.0)
        p._ingest_snapshot("BBB", 200.0, 201.0, 199.0, t1, cum_vol=100.0)
        return {"AAA": mkbar("AAA", 9, 20), "BBB": mkbar("BBB", 9, 20)}   # snapshot rows

    monkeypatch.setattr(p, "_fetch_all", fetch_advance_window)
    updated, failed = p.poll_once()                  # both windows close -> 2 new
    assert (updated, failed) == (2, 0)
    assert len(p.get_bars()) == 2                    # cached size coincides here
    # next cycle: snapshots inside the SAME windows -> nothing NEW completes
    def fetch_same_window():
        p._ingest_snapshot("AAA", 101.0, 102.0, 100.0,
                           t0 + dt.timedelta(minutes=7), cum_vol=150.0)
        p._ingest_snapshot("BBB", 201.0, 202.0, 200.0,
                           t0 + dt.timedelta(minutes=7), cum_vol=180.0)
        return {"AAA": mkbar("AAA", 9, 22), "BBB": mkbar("BBB", 9, 22)}

    monkeypatch.setattr(p, "_fetch_all", fetch_same_window)
    updated, failed = p.poll_once()
    assert updated == 0                              # NOT cached size (2)
    assert failed == 0


def test_nse_index_url_chosen_by_universe():
    assert live.NSEQuotePoller([f"S{i}" for i in range(50)]).index_url.endswith("NIFTY%2050")
    assert live.NSEQuotePoller([f"S{i}" for i in range(51)]).index_url.endswith("NIFTY%20200")
    assert live.NSEQuotePoller([f"S{i}" for i in range(200)]).index_url.endswith("NIFTY%20200")


# =====================================================================
# FIX 6: stale alert fires for a never-ticking feed during OPEN phase
# =====================================================================
def _open_service(clock: Clock, monkeypatch) -> MarketDataService:
    monkeypatch.setattr(cal, "market_phase", lambda *a, **k: "OPEN")
    return MarketDataService(["AAA"], poller=StubPoller(), clock=clock)


def test_stale_alert_never_ticking_feed_fires_after_open_phase_threshold(monkeypatch):
    clock = Clock(dt.datetime(2026, 8, 25, 9, 15, tzinfo=IST))
    md = _open_service(clock, monkeypatch)
    assert md.last_tick_age_s is None
    # branch A: within threshold since phase-open -> STALE but NO alert yet
    clock.advance(301.0)
    assert md.feed_status() == "STALE"
    assert md._stale_alerted is False
    # branch B: past STALE_ALERT_AFTER_S since phase became OPEN -> alert latches
    clock.advance(svc.STALE_ALERT_AFTER_S + 1)       # 601s since phase-open
    assert md.feed_status() == "STALE"
    assert md._stale_alerted is True


def test_stale_alert_ticking_feed_fires_on_tick_age(monkeypatch):
    clock = Clock(dt.datetime(2026, 8, 25, 9, 15, tzinfo=IST))
    md = _open_service(clock, monkeypatch)
    poller = md.poller
    poller.bars = {"AAA": mkbar("AAA", 9, 20)}
    md.poll_cycle()                                  # feed ticks
    clock.advance(200.0)
    assert md.feed_status() == "OPEN"                # age 200s < STALE_AFTER_S
    clock.advance(svc.STALE_ALERT_AFTER_S - 190.0)   # age now 610s > 600s
    assert md.feed_status() == "STALE"
    assert md._stale_alerted is True


# =====================================================================
# Service default stack is the failover pair
# =====================================================================
def test_default_poller_is_failover_stack():
    md = MarketDataService(["RELIANCE"])
    from sts.data.live import FailoverPoller
    assert isinstance(md.poller, FailoverPoller)
    names = [type(p).__name__ for p in md.poller.pollers]
    assert names == ["NSEQuotePoller", "YahooChartPoller"]


# =====================================================================
# YAHOO/FALLBACK-HEALTH: source_health contract, warning visibility,
# validation-before-healthy
# =====================================================================
class SelfStamping(StubPoller):
    """Stamps last_success_ts on successful polls like the real pollers do."""
    def poll_once(self) -> tuple[int, int]:
        self.calls += 1
        upd, fail = self.result
        if upd >= 1:
            self.last_success_ts = dt.datetime.now(tz=IST)
            self.last_error = None
        return upd, fail


class FlakyPrimary(SelfStamping):
    pass


class SoundBackup(SelfStamping):
    pass


class LonerSource(SelfStamping):
    pass


def test_source_health_shape_and_idle_states():
    clock = Clock()
    fp = live.FailoverPoller([FlakyPrimary(result=(0, 1)),
                              SoundBackup(result=(0, 0))], clock=clock)
    h = fp.source_health()
    assert set(h) == {"primary", "fallback"}
    for entry in h.values():
        assert set(entry) == {"name", "status", "consecutive_failures",
                              "last_error", "last_success_ts"}
        assert entry["status"] == "IDLE"          # never tried, never succeeded
        assert entry["consecutive_failures"] == 0
        assert entry["last_error"] is None
        assert entry["last_success_ts"] is None
    assert h["primary"]["name"] == "FlakyPrimary"
    assert h["fallback"]["name"] == "SoundBackup"
    assert fp.active_source_name == "FlakyPrimary"
    assert fp.active_source_name == fp.active_source


def test_source_health_degraded_then_cooldown_and_fallback_healthy():
    clock = Clock()
    primary = FlakyPrimary(result=(0, 1))
    backup = SoundBackup(result=(2, 0), bars={"AAA": mkbar("AAA", 9, 35)})
    fp = live.FailoverPoller([primary, backup], clock=clock)

    primary.last_error = "HTTPError status=429: too many requests"
    fp.poll_once()
    h = fp.source_health()
    assert h["primary"]["status"] == "DEGRADED"
    assert h["primary"]["consecutive_failures"] == 1
    assert h["primary"]["last_error"] == "HTTPError status=429: too many requests"
    assert h["fallback"]["status"] == "IDLE"

    fp.poll_once()
    assert fp.source_health()["primary"]["consecutive_failures"] == 2

    fp.poll_once()                                # third strike -> switch + trial
    h = fp.source_health()
    assert h["primary"]["status"] == "COOLDOWN"
    assert h["primary"]["consecutive_failures"] == 3
    assert h["fallback"]["status"] == "HEALTHY"   # standby succeeded on trial poll
    assert h["fallback"]["name"] == "SoundBackup"
    assert h["fallback"]["last_error"] is None
    ts = dt.datetime.fromisoformat(h["fallback"]["last_success_ts"])
    assert ts.tzinfo is not None                  # ISO string with offset
    assert fp.active_source_name == "SoundBackup"


def test_source_health_failed_after_cooldown_expiry():
    clock = Clock()
    primary = FlakyPrimary(result=(0, 1))
    backup = SoundBackup(result=(2, 0), bars={"AAA": mkbar("AAA", 9, 35)})
    fp = live.FailoverPoller([primary, backup], clock=clock)
    for _ in range(3):
        fp.poll_once()
    assert fp.source_health()["primary"]["status"] == "COOLDOWN"
    clock.advance(live.FailoverPoller.COOLDOWN_SECONDS + 1)
    h = fp.source_health()                        # read WITHOUT polling
    assert h["primary"]["status"] == "FAILED"     # terminal record, streak unrepaired
    assert h["primary"]["consecutive_failures"] == 3
    assert h["fallback"]["status"] == "HEALTHY"


def test_source_health_single_source_failed_then_recovers_to_healthy():
    clock = Clock()
    solo = LonerSource(result=(0, 1))             # nobody to fail over to
    solo.last_error = "boom"
    fp = live.FailoverPoller([solo], clock=clock)
    for _ in range(3):
        fp.poll_once()
    h = fp.source_health()["primary"]
    assert h["status"] == "FAILED"
    assert h["consecutive_failures"] == 3
    solo.result = (1, 0)
    solo.bars = {"AAA": mkbar("AAA", 9, 35)}
    fp.poll_once()
    h = fp.source_health()["primary"]
    assert h["status"] == "HEALTHY"
    assert h["consecutive_failures"] == 0
    assert h["last_error"] is None
    assert h["last_success_ts"] is not None


# --------------------------------------------------------------- HTTP-200-no-bars
class _Fake200Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._payload


def _forming_only_payload() -> dict:
    """Valid JSON, every bar still forming (stamps in the future) => zero
    parseable bars despite HTTP 200."""
    now_epoch = int(dt.datetime.now(tz=dt.timezone.utc).timestamp()) + 3600
    return {"chart": {"result": [{
        "meta": {"symbol": "X"},
        "timestamp": [now_epoch, now_epoch + 300],
        "indicators": {"quote": [{
            "open": [1.0, 1.0], "high": [1.0, 1.0],
            "low": [1.0, 1.0], "close": [1.0, 1.0], "volume": [0, 0],
        }]},
    }]}}


def test_http_200_zero_valid_bars_does_not_reset_consecutive_failures(monkeypatch):
    monkeypatch.setattr(cal, "market_phase", lambda *a, **k: "OPEN")
    sess = requests.Session()
    monkeypatch.setattr(sess, "get",
                        lambda url, **kw: _Fake200Resp(_forming_only_payload()))
    yahoo = live.YahooChartPoller(["RELIANCE.NS"], session=sess)
    fp = live.FailoverPoller([yahoo], clock=Clock())
    u1, f1 = fp.poll_once()
    assert (u1, f1) == (0, 1)                     # every request was HTTP 200...
    assert fp.consecutive_failures == 1           # ...but the streak still counts
    u2, f2 = fp.poll_once()
    assert (u2, f2) == (0, 1)
    assert fp.consecutive_failures == 2           # NOT reset by empty 200s
    h = fp.source_health()["primary"]
    assert h["status"] == "DEGRADED"
    assert h["last_error"] == "YahooChartPoller cycle failed"
