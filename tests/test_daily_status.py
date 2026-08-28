"""daily_data_status() contract (health-API surface; key names are FIXED):

    {"as_of": iso-date|None,          # latest date present in ^NSEI parquet
     "expected_session": iso|None,    # latest session we should already have
     "stale": bool,                   # fail-closed: unknown == stale
     "last_refresh": {"at", "ok", "error", "symbols_updated"}}

Fake clock throughout; _NSEI.parquet fixtures written locally. No network.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from sts.data import calendar as cal
from sts.marketdata.service import MarketDataService

REQUIRED_KEYS = {"as_of", "expected_session", "stale", "last_refresh"}
LAST_REFRESH_KEYS = {"at", "ok", "error", "symbols_updated"}


class NoopPoller:
    def poll_once(self):
        return 0, 0

    def get_bars(self):
        return {}


def ist(y, m, d, hh=0, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=cal.IST)


def make_service(daily_dir, now):
    return MarketDataService(["AAA"], poller=NoopPoller(), daily_dir=daily_dir,
                             clock=lambda: now)


def write_index(tmp_path, end_date: dt.date, periods: int = 6):
    dates = pd.bdate_range(end=pd.Timestamp(end_date), periods=periods)
    frame = pd.DataFrame({"date": dates, "open": 1.0, "high": 1.0, "low": 1.0,
                          "close": 1.0, "volume": 0.0, "adjclose": 1.0,
                          "source": "yfinance"})
    frame.to_parquet(tmp_path / "_NSEI.parquet", index=False)


# ============================================================ shape
def test_shape_matches_fixed_contract(tmp_path):
    md = make_service(tmp_path, ist(2026, 8, 26, 17, 0))
    write_index(tmp_path, dt.date(2026, 8, 26))
    status = md.daily_data_status()
    assert set(status) == REQUIRED_KEYS
    assert set(status["last_refresh"]) == LAST_REFRESH_KEYS


def test_last_refresh_defaults_before_any_attempt(tmp_path):
    write_index(tmp_path, dt.date(2026, 8, 26))
    md = make_service(tmp_path, ist(2026, 8, 26, 17, 0))
    assert md.daily_data_status()["last_refresh"] == {
        "at": None, "ok": None, "error": None, "symbols_updated": 0,
    }


def test_as_of_and_expected_are_iso_date_strings(tmp_path):
    write_index(tmp_path, dt.date(2026, 8, 24))
    md = make_service(tmp_path, ist(2026, 8, 26, 17, 0))
    status = md.daily_data_status()
    dt.date.fromisoformat(status["as_of"])                  # parseable iso date
    dt.date.fromisoformat(status["expected_session"])
    assert isinstance(status["stale"], bool)
    assert isinstance(status["last_refresh"]["symbols_updated"], int)


# ============================================================ stale-flag logic (fake clock)
def test_current_after_cutoff_not_stale(tmp_path):
    # Wed 26th 17:00 IST, data through Wed -> as_of == expected, not stale
    write_index(tmp_path, dt.date(2026, 8, 26))
    md = make_service(tmp_path, ist(2026, 8, 26, 17, 0))
    status = md.daily_data_status()
    assert status["as_of"] == "2026-08-26"
    assert status["expected_session"] == "2026-08-26"
    assert status["stale"] is False


def test_behind_expected_is_stale(tmp_path):
    # data ends Monday; Wednesday evening expects the 26th -> stale
    write_index(tmp_path, dt.date(2026, 8, 24))
    md = make_service(tmp_path, ist(2026, 8, 26, 17, 0))
    status = md.daily_data_status()
    assert status["as_of"] == "2026-08-24"
    assert status["expected_session"] == "2026-08-26"
    assert status["stale"] is True


def test_missing_index_parquet_fails_closed_stale(tmp_path):
    md = make_service(tmp_path, ist(2026, 8, 26, 17, 0))
    status = md.daily_data_status()
    assert status["as_of"] is None
    assert status["expected_session"] == "2026-08-26"
    assert status["stale"] is True                          # never silently current


def test_weekend_with_friday_present_not_stale(tmp_path):
    write_index(tmp_path, dt.date(2026, 8, 28))             # Friday
    md = make_service(tmp_path, ist(2026, 8, 29, 11, 0))    # Saturday
    status = md.daily_data_status()
    assert status["as_of"] == "2026-08-28"
    assert status["expected_session"] == "2026-08-28"
    assert status["stale"] is False


def test_morning_before_cutoff_expects_prior_session(tmp_path):
    # Wed 10:00 (pre-bhavcopy): Tuesday is the expected latest session
    write_index(tmp_path, dt.date(2026, 8, 25))
    md = make_service(tmp_path, ist(2026, 8, 26, 10, 0))
    status = md.daily_data_status()
    assert status["as_of"] == "2026-08-25"
    assert status["expected_session"] == "2026-08-25"
    assert status["stale"] is False


def test_as_of_reflects_refreshed_data(tmp_path, monkeypatch):
    """After a refresh adds a session, the cached as_of must catch up."""
    from sts.data import history
    write_index(tmp_path, dt.date(2026, 8, 24))
    md = make_service(tmp_path, ist(2026, 8, 26, 17, 0))
    assert md.daily_data_status()["as_of"] == "2026-08-24"

    def updater(symbols, out_dir):
        path = out_dir / "_NSEI.parquet"
        df = pd.read_parquet(path)
        extra = pd.DataFrame([{"date": pd.Timestamp("2026-08-25"), "open": 1.0,
                               "high": 1.0, "low": 1.0, "close": 1.0,
                               "volume": 0.0, "adjclose": 1.0, "source": "chart"}])
        pd.concat([df, extra], ignore_index=True).to_parquet(path, index=False)
        return {s: "chart+appended" for s in symbols}

    monkeypatch.setattr(history, "update_daily", updater)
    md.maybe_refresh_daily(sync=True)
    status = md.daily_data_status()
    assert status["as_of"] == "2026-08-25"                  # cache caught up post-refresh
    assert status["expected_session"] == "2026-08-26"       # 17:00 -> Wed expected
    assert status["stale"] is True                          # still one session behind
