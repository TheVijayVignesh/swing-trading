"""Daily-refresh wiring (diagnostic finding #1): refresh_daily_if_stale had NO
caller. These tests cover the new session-aware lifecycle hook:

- poll_cycle calls maybe_refresh_daily() (lightweight, 30-min spacing);
- DUE logic is holiday/session aware against the ^NSEI parquet;
- execution reuses the existing update_daily path, never re-downloads fresh
  symbols, records status, and survives downloader failures;
- repeated/restart calls within cooldown do not double-refresh.

Clock and calendar are injectable: tests pass an explicit `now` / injectable
clock and write _NSEI.parquet fixtures; the downloader is monkeypatched.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import time
from pathlib import Path

import pandas as pd
import pytest

from sts.data import calendar as cal
from sts.data import history
from sts.marketdata.service import MarketDataService
from sts.strategy.pullback_v1 import StrategyContext, prescreen_daily

COLS = ["date", "open", "high", "low", "close", "volume", "adjclose", "source"]


class NoopPoller:
    def poll_once(self):
        return 0, 0

    def get_bars(self):
        return {}


def ist(y, m, d, hh=0, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=cal.IST)


def make_service(tmp_path, now):
    return MarketDataService(["AAA"], poller=NoopPoller(), daily_dir=tmp_path,
                             clock=lambda: now)


def write_index(tmp_path, end_date: dt.date, periods: int = 6):
    dates = pd.bdate_range(end=pd.Timestamp(end_date), periods=periods)
    frame = pd.DataFrame({"date": dates, "open": 1.0, "high": 1.0, "low": 1.0,
                          "close": 1.0, "volume": 0.0, "adjclose": 1.0,
                          "source": "yfinance"})
    path = tmp_path / "_NSEI.parquet"
    frame.to_parquet(path, index=False)
    # simulate an OLD file (data ends before the expected session => mtime is old)
    old = time.time() - 25 * 3600
    os.utime(path, (old, old))


def append_session(out_dir, date_iso: str) -> None:
    """Simulate what a real download does: add one session row to _NSEI."""
    path = out_dir / "_NSEI.parquet"
    df = pd.read_parquet(path)
    extra = pd.DataFrame([{"date": pd.Timestamp(date_iso), "open": 1.0,
                           "high": 1.0, "low": 1.0, "close": 1.0,
                           "volume": 0.0, "adjclose": 1.0, "source": "chart"}])
    pd.concat([df, extra], ignore_index=True).to_parquet(path, index=False)


@pytest.fixture()
def fake_downloader(monkeypatch):
    """Patch history.update_daily (the EXISTING download path); returns recorder."""
    calls: list[list[str]] = []

    def fake(symbols, out_dir):
        calls.append(list(symbols))
        return {s: "chart+appended" for s in symbols}

    monkeypatch.setattr(history, "update_daily", fake)
    return calls


# ============================================================ due logic
def test_stale_index_missing_latest_session_is_due_and_invokes_downloader(tmp_path, fake_downloader):
    # Wed 2026-08-26 after bhavcopy window -> expected session = today (26th)
    now = ist(2026, 8, 26, 17, 0)
    write_index(tmp_path, dt.date(2026, 8, 24))          # Mon only — Tue+Wed missing
    md = make_service(tmp_path, now)
    assert md.daily_refresh_due(now) is True
    result = md.maybe_refresh_daily(sync=True)
    assert result and result["ok"] is True
    assert len(fake_downloader) == 1
    assert "^NSEI" in fake_downloader[0] and "^INDIAVIX" in fake_downloader[0]


def test_current_data_not_due_and_downloader_not_called(tmp_path, fake_downloader):
    # Wed 2026-08-26 morning (before 16:30) -> expected session = prior day (Tue 25)
    now = ist(2026, 8, 26, 10, 0)
    write_index(tmp_path, dt.date(2026, 8, 25))
    md = make_service(tmp_path, now)
    assert md.daily_refresh_due() is False
    assert md.maybe_refresh_daily(sync=True) is None
    assert fake_downloader == []


def test_before_cutoff_missing_yesterday_still_due(tmp_path, fake_downloader):
    # Even before Wednesday's close, Tuesday's missing session makes it DUE
    now = ist(2026, 8, 26, 10, 0)
    write_index(tmp_path, dt.date(2026, 8, 24))          # Tuesday missing
    md = make_service(tmp_path, now)
    assert md.daily_refresh_due() is True


def test_weekend_not_due_when_friday_present(tmp_path, fake_downloader):
    write_index(tmp_path, dt.date(2026, 8, 28))          # Friday
    md = make_service(tmp_path, ist(2026, 8, 29, 11, 0))  # Saturday
    assert md.expected_latest_session() == dt.date(2026, 8, 28)
    assert md.daily_refresh_due() is False


def test_holiday_not_due_when_prior_session_present(tmp_path, fake_downloader):
    # Christmas Day 2025 (Thursday, NSE holiday): expected session = Wed 24th
    write_index(tmp_path, dt.date(2025, 12, 24))
    md = make_service(tmp_path, ist(2025, 12, 25, 18, 0))
    assert md.expected_latest_session() == dt.date(2025, 12, 24)
    assert md.daily_refresh_due() is False


# ============================================================ execution + status
def test_downloader_failure_sets_error_no_crash_retries_next_cycle(tmp_path, monkeypatch):
    now = ist(2026, 8, 26, 17, 0)
    write_index(tmp_path, dt.date(2026, 8, 24))
    attempts: list[list[str]] = []

    def flaky(symbols, out_dir):
        attempts.append(list(symbols))
        if len(attempts) == 1:
            raise RuntimeError("yahoo down")
        return {s: "chart+appended" for s in symbols}

    monkeypatch.setattr(history, "update_daily", flaky)
    md = make_service(tmp_path, now)

    result = md.maybe_refresh_daily(sync=True)           # must NOT raise
    assert result["ok"] is False
    st = md.daily_refresh_status()
    assert st["last_success"] is None
    assert "yahoo down" in st["last_error"]
    assert st["in_flight"] is False                      # latch released for retry

    # next cycle (31 min later): cooldown elapsed -> retried automatically
    r2 = md.maybe_refresh_daily(now=ist(2026, 8, 26, 17, 31), sync=True)
    assert r2["ok"] is True
    assert md.daily_refresh_status()["last_success"] is not None
    assert len(attempts) == 2


def test_successful_refresh_updates_parquet_and_status(tmp_path, monkeypatch):
    now = ist(2026, 8, 26, 17, 0)
    write_index(tmp_path, dt.date(2026, 8, 24))

    def updater(symbols, out_dir):
        append_session(out_dir, "2026-08-26")
        return {s: "chart+appended" for s in symbols}

    monkeypatch.setattr(history, "update_daily", updater)
    md = make_service(tmp_path, now)

    result = md.maybe_refresh_daily(sync=True)
    assert result["ok"] is True and result["sessions_added"] == ["2026-08-26"]
    st = md.daily_refresh_status()
    assert st["last_attempt"] and st["last_success"]
    assert st["sessions_added"] == 1 and st["due"] is False
    dates = set(pd.to_datetime(pd.read_parquet(tmp_path / "_NSEI.parquet")["date"]).dt.date)
    assert dt.date(2026, 8, 26) in dates


def test_cooldown_blocks_immediate_second_call(tmp_path, fake_downloader):
    now = ist(2026, 8, 26, 17, 0)
    write_index(tmp_path, dt.date(2026, 8, 24))
    md = make_service(tmp_path, now)

    assert md.maybe_refresh_daily(sync=True)["ok"] is True
    assert len(fake_downloader) == 1
    # immediate repeat on the same clock: 30-min cooldown -> no double-refresh
    assert md.maybe_refresh_daily(sync=True) is None
    assert len(fake_downloader) == 1


def test_restart_idempotency_current_data_no_refetch(tmp_path, monkeypatch):
    """After a successful refresh, a RESTARTED service sees current parquet
    state and never re-downloads."""
    calls: list[list[str]] = []

    def updating_once(symbols, out_dir):
        calls.append(list(symbols))
        append_session(out_dir, "2026-08-25")
        return {s: "chart+appended" for s in symbols}

    monkeypatch.setattr(history, "update_daily", updating_once)

    now_morning = ist(2026, 8, 26, 10, 0)                # Tue 25 expected
    write_index(tmp_path, dt.date(2026, 8, 24))
    md1 = make_service(tmp_path, now_morning)
    assert md1.maybe_refresh_daily(sync=True)["ok"] is True

    # 'restart': brand-new service object over the same data dir
    md2 = make_service(tmp_path, now_morning)
    assert md2.daily_refresh_due() is False
    assert md2.maybe_refresh_daily(sync=True) is None
    assert len(calls) == 1


def test_poll_cycle_schedules_refresh_check(tmp_path, fake_downloader, monkeypatch):
    md = make_service(tmp_path, ist(2026, 8, 26, 17, 0))
    seen: list = []
    monkeypatch.setattr(md, "maybe_refresh_daily",
                        lambda *a, **k: seen.append(a or k.get("now")))
    md.poll_cycle()
    assert len(seen) == 1                                # wired into the tick


def test_fresh_symbols_not_redownloaded(tmp_path, fake_downloader):
    # Universe parquet file-fresh on disk -> excluded from the download batch;
    # only the stale index series are fetched.
    now = ist(2026, 8, 26, 17, 0)
    write_index(tmp_path, dt.date(2026, 8, 24))
    pd.DataFrame({c: [1] for c in COLS}).to_parquet(tmp_path / "AAA.parquet",
                                                    index=False)  # just written
    md = make_service(tmp_path, now)
    result = md.maybe_refresh_daily(sync=True)
    assert result["ok"] is True
    assert fake_downloader[0] == ["^NSEI", "^INDIAVIX"]


# ============================================================ refresh outcome EXPOSED
def test_successful_refresh_updates_recorded_status_fields(tmp_path, monkeypatch):
    """(a) A successful refresh records ok=True / symbols_updated>0 on the
    daily_data_status() observability surface."""
    now = ist(2026, 8, 26, 17, 0)
    write_index(tmp_path, dt.date(2026, 8, 24))

    def updater(symbols, out_dir):
        append_session(out_dir, "2026-08-25")
        return {s: "chart+appended" for s in symbols}

    monkeypatch.setattr(history, "update_daily", updater)
    md = make_service(tmp_path, now)

    md.maybe_refresh_daily(sync=True)
    lr = md.daily_data_status()["last_refresh"]
    assert lr["ok"] is True
    assert lr["at"] is not None
    assert lr["error"] is None
    assert lr["symbols_updated"] > 0


def test_failed_refresh_updates_recorded_status_fields(tmp_path, monkeypatch):
    """(a) A failed attempt records ok=False + the error (never silently lost)."""
    now = ist(2026, 8, 26, 17, 0)
    write_index(tmp_path, dt.date(2026, 8, 24))

    def boom(symbols, out_dir):
        raise RuntimeError("yahoo down")

    monkeypatch.setattr(history, "update_daily", boom)
    md = make_service(tmp_path, now)

    md.maybe_refresh_daily(sync=True)                    # must not raise
    lr = md.daily_data_status()["last_refresh"]
    assert lr["ok"] is False
    assert "yahoo down" in lr["error"]
    assert lr["symbols_updated"] == 0


# ============================================================ stale WARN during market hours
def test_stale_warning_during_open_hours_at_most_once_per_hour(tmp_path, caplog, fake_downloader):
    """Gap: stale daily data during OPEN phase must WARN — rate-limited to
    once per hour, never once per cycle."""
    write_index(tmp_path, dt.date(2026, 8, 24))          # Tue missing => due all day Wed
    md = make_service(tmp_path, ist(2026, 8, 26, 10, 0))
    with caplog.at_level(logging.WARNING, logger="sts.marketdata"):
        md.maybe_refresh_daily(sync=True)                # 10:00 -> WARN #1
        md.maybe_refresh_daily(now=ist(2026, 8, 26, 10, 30), sync=True)   # latch blocks
        md.maybe_refresh_daily(now=ist(2026, 8, 26, 11, 0), sync=True)    # >=1h -> WARN #2
    warns = [r for r in caplog.records
             if r.levelno == logging.WARNING and "STALE during market hours" in r.getMessage()]
    assert len(warns) == 2


def test_no_stale_warning_when_market_closed_or_data_current(tmp_path, caplog, fake_downloader):
    # current data during OPEN hours -> no warning at all
    write_index(tmp_path, dt.date(2026, 8, 25))          # Tue present; Wed pre-cutoff
    md = make_service(tmp_path, ist(2026, 8, 26, 10, 0))
    with caplog.at_level(logging.WARNING, logger="sts.marketdata"):
        md.maybe_refresh_daily(sync=True)
    # stale data but Saturday (CLOSED) -> no warning either
    write_index(tmp_path, dt.date(2026, 8, 24))
    md2 = make_service(tmp_path, ist(2026, 8, 29, 11, 0))
    md2.maybe_refresh_daily(sync=True)
    warns = [r for r in caplog.records
             if r.levelno == logging.WARNING and "STALE during market hours" in r.getMessage()]
    assert warns == []


# ============================================================ real-data integration
def test_real_repo_daily_context_prior_day_high_agreement():
    """(b) REAL DATA, no network: repo RELIANCE parquet -> service data layer
    (get_daily_frame) -> StrategyContext -> prior-day-high cell the strategy
    reads as breakout trigger — all agree on the true last row's high."""
    repo_dir = Path("data/parquet/candles_1d")
    if not (repo_dir / "RELIANCE.parquet").exists():
        pytest.skip("repo dataset not present")
    raw = pd.read_parquet(repo_dir / "RELIANCE.parquet")  # ground truth, read-only
    true_last = raw.sort_values("date").iloc[-1]

    md = MarketDataService(["RELIANCE"], poller=NoopPoller(), daily_dir=repo_dir)
    frame = md.get_daily_frame("RELIANCE")               # actual data layer

    # data layer hands over the full, date-sorted frame ending at the true row
    assert len(frame) > 60
    assert frame["date"].is_monotonic_increasing
    last = frame.iloc[-1]
    assert pd.Timestamp(last["date"]) == pd.Timestamp(true_last["date"])
    assert float(last["high"]) == float(true_last["high"])
    assert float(last["close"]) == float(true_last["close"])

    prev_day = pd.Timestamp(true_last["date"]).date()
    ctx = StrategyContext(
        daily={"RELIANCE": frame}, intraday={},
        index_daily=md.index_frame("nifty50"), vix_now=md.vix_now(),
        now=dt.datetime.combine(prev_day + dt.timedelta(days=1), dt.time(10, 0)),
        eligible=["RELIANCE"], prev_day=prev_day,
    )
    # the exact cell pullback-v1 uses as the breakout trigger (h.iloc[-1]):
    assert float(ctx.daily["RELIANCE"]["high"].iloc[-1]) == float(true_last["high"])

    armed = prescreen_daily(ctx)                         # real regime gate, real frames
    for entry in armed:
        assert entry["trigger_level"] == float(ctx.daily[entry["symbol"]]["high"].iloc[-1])
    reliance = [e for e in armed if e["symbol"] == "RELIANCE"]
    if reliance:
        assert reliance[0]["trigger_level"] == float(true_last["high"])
