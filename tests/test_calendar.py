"""Offline tests for the IST trading calendar."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
import yaml

from sts.data import calendar as cal


def test_ist_constant():
    assert str(cal.IST) == "Asia/Kolkata"
    assert dt.datetime(2026, 8, 24, 10, 0, tzinfo=ZoneInfo("UTC")).astimezone(cal.IST).hour == 15


def test_weekend_not_trading_day():
    assert cal.is_trading_day(dt.date(2025, 8, 16)) is False  # Saturday
    assert cal.is_trading_day(dt.date(2025, 8, 17)) is False  # Sunday


def test_known_2025_holidays():
    # Diwali Laxmi Pujan and Holi are official 2025 NSE holidays
    assert cal.is_trading_day(dt.date(2025, 10, 21)) is False
    assert cal.is_trading_day(dt.date(2025, 3, 14)) is False
    assert cal.is_trading_day(dt.date(2025, 1, 27)) is True   # ordinary Monday


def test_republic_day_2026_monday():
    assert cal.is_trading_day(dt.date(2026, 1, 26)) is False


def test_session_windows():
    win = cal.session_windows(dt.date(2025, 8, 18))
    assert win["open"].time() == dt.time(9, 15)
    assert win["close"].time() == dt.time(15, 30)
    assert win["pre_open"].time() == dt.time(9, 0)
    assert all(w.tzinfo is not None for w in win.values())


@pytest.mark.parametrize("hh_mm,expected", [
    ("08:59", "CLOSED"),
    ("09:00", "PRE_OPEN"),
    ("09:14", "PRE_OPEN"),
    ("09:15", "OPEN"),
    ("12:00", "OPEN"),
    ("15:30", "OPEN"),
    ("15:31", "AFTER_HOURS"),
    ("23:00", "AFTER_HOURS"),
])
def test_market_phase_boundaries(hh_mm: str, expected: str):
    now = dt.datetime.combine(dt.date(2025, 8, 18), dt.time.fromisoformat(hh_mm), tzinfo=cal.IST)
    assert cal.market_phase(now) == expected


def test_market_phase_closed_on_holiday():
    now = dt.datetime(2025, 10, 21, 11, 0, tzinfo=cal.IST)  # Diwali Tuesday
    assert cal.market_phase(now) == "CLOSED"


def test_next_bar_close_grid():
    # bars close at :20,:25,:30,... anchored on the 09:15 open
    now = dt.datetime(2025, 8, 18, 10, 2, tzinfo=cal.IST)
    nxt = cal.next_bar_close(now)
    assert nxt.time() == dt.time(10, 5)


def test_next_bar_close_exactly_at_close_goes_next_day():
    now = dt.datetime(2025, 8, 18, 15, 30, tzinfo=cal.IST)
    nxt = cal.next_bar_close(now)
    assert nxt.date() == dt.date(2025, 8, 19)
    assert nxt.time() == dt.time(9, 20)


def test_next_bar_close_skips_holiday_and_weekend():
    now = dt.datetime(2025, 10, 17, 15, 40, tzinfo=cal.IST)  # Friday after hours
    nxt = cal.next_bar_close(now)
    # Sat 18, Sun 19, Mon 20 trading day -> first bar closes Tue? No: Monday is fine.
    assert nxt.date() == dt.date(2025, 10, 20)
    assert nxt.time() == dt.time(9, 20)

    now2 = dt.datetime(2025, 10, 20, 15, 40, tzinfo=cal.IST)  # Monday after hours
    nxt2 = cal.next_bar_close(now2)
    # Diwali Laxmi Pujan (21) AND Balipratipada (22) both closed -> Wednesday
    assert nxt2.date() == dt.date(2025, 10, 23)
    assert nxt2.time() == dt.time(9, 20)


def test_bars_between_counts_full_session():
    day = dt.date(2025, 8, 18)
    win = cal.session_windows(day)
    start = win["pre_open"]
    end = win["close"] + dt.timedelta(hours=1)
    bars = cal.bars_between(start, end)
    # 09:15->15:30 = 375 min / 5 = 75 bars
    assert len(bars) == 75
    assert all(t.date() == day for t in bars)


def test_yaml_override_extends_holidays(tmp_path, monkeypatch):
    override = tmp_path / "holidays.yaml"
    override.write_text(yaml.safe_dump({"holidays": {2027: ["2027-01-26"]}}))
    monkeypatch.setattr(cal, "_OVERRIDE_PATHS", (override,))
    monkeypatch.setattr(cal, "_holiday_cache", None)
    try:
        assert cal.nse_holidays(2027) == frozenset({dt.date(2027, 1, 26)})
        assert cal.is_trading_day(dt.date(2027, 1, 26)) is False
        # embedded years still present (union semantics)
        assert dt.date(2025, 10, 21) in cal.nse_holidays(2025)
    finally:
        monkeypatch.setattr(cal, "_holiday_cache", None)
