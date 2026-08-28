"""History module tests — offline via mocked HTTP + fixture payloads; network-marked live."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import requests

from sts.data import history

cal_ist = ZoneInfo("Asia/Kolkata")


# --------------------------------------------------------------- fixtures
def chart_payload(symbol: str, stamps: list[int], closes: list[float], adj: list[float] | None = None) -> dict:
    return {
        "chart": {
            "result": [{
                "meta": {"symbol": symbol},
                "timestamp": stamps,
                "indicators": {
                    "quote": [{
                        "open": [c - 1 for c in closes],
                        "high": [c + 2 for c in closes],
                        "low": [c - 3 for c in closes],
                        "close": closes,
                        "volume": [1000] * len(closes),
                    }],
                    "adjclose": [{"adjclose": adj or closes}],
                },
            }],
            "error": None,
        }
    }


def ist_stamp(day: dt.date, hour: int = 9, minute: int = 15) -> int:
    d = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo("Asia/Kolkata"))
    return int(d.timestamp())


class FakeResp:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


# --------------------------------------------------------------- parse tests
def test_parse_chart_payload_maps_ist_dates():
    p = chart_payload(
        "RELIANCE.NS",
        stamps=[ist_stamp(dt.date(2025, 8, 18)), ist_stamp(dt.date(2025, 8, 19))],
        closes=[100.0, 101.0],
    )
    df = history.parse_chart_payload(p)
    assert list(df.columns) == history.COLUMNS
    assert len(df) == 2
    assert df["date"].iloc[0] == pd.Timestamp(dt.date(2025, 8, 18))
    assert df["close"].iloc[1] == 101.0
    assert (df["date"] == pd.to_datetime(df["date"])).all()


def test_parse_chart_payload_handles_nulls_and_empty():
    # null close row must be dropped, not fabricated
    p = chart_payload("X.NS", stamps=[ist_stamp(dt.date(2025, 8, 18)), ist_stamp(dt.date(2025, 8, 19))],
                      closes=[10.0])
    p["chart"]["result"][0]["indicators"]["quote"][0]["close"].insert(1, None)
    df = history.parse_chart_payload(p)
    assert len(df) == 1
    empty = history.parse_chart_payload({"chart": {"result": [], "error": None}})
    assert empty.empty and list(empty.columns) == history.COLUMNS


# --------------------------------------------------------------- fetch tests
def test_fetch_daily_chart_retries_on_429_then_succeeds(monkeypatch):
    calls = {"n": 0}
    sleeps: list[float] = []
    monkeypatch.setattr(history.time, "sleep", lambda s: sleeps.append(s))

    class FlakySession(requests.Session):
        def get(self, *a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                return FakeResp(status_code=429)
            return FakeResp(payload=chart_payload("R.NS", [ist_stamp(dt.date(2025, 8, 18))], [50.0]))
    monkeypatch.setattr(history.random, "choice", lambda seq: 1)
    df = history.fetch_daily_chart("R.NS", session=FlakySession())
    assert calls["n"] == 3
    assert len(df) == 1 and df["close"].iloc[0] == 50.0
    assert len(sleeps) >= 2  # exponential backoff happened


def test_fetch_daily_chart_gives_none_on_persistent_failure(monkeypatch):
    monkeypatch.setattr(history.time, "sleep", lambda s: None)
    monkeypatch.setattr(history.random, "choice", lambda seq: 1)
    sess = requests.Session()
    def boom(*a, **k):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(sess, "get", boom)
    assert history.fetch_daily_chart("R.NS", session=sess) is None


def test_fetch_daily_degrades_to_yfinance_secondary(monkeypatch):
    monkeypatch.setattr(history.random, "choice", lambda seq: 1)
    monkeypatch.setattr(history.time, "sleep", lambda s: None)
    monkeypatch.setattr(history, "fetch_daily_chart", lambda *a, **k: None)
    good = pd.DataFrame({
        "date": [pd.Timestamp("2025-08-18")], "open": [1.0], "high": [1.0],
        "low": [1.0], "close": [1.0], "volume": [1.0], "adjclose": [1.0],
    })
    monkeypatch.setattr(history, "fetch_daily_yfinance", lambda s, years=5: good)
    df, source = history.fetch_daily("RELIANCE.NS")
    assert source == "yfinance" and len(df) == 1


def test_fetch_daily_explicit_unavailable(monkeypatch):
    monkeypatch.setattr(history, "fetch_daily_chart", lambda *a, **k: None)
    monkeypatch.setattr(history, "fetch_daily_yfinance", lambda s, years=5: None)
    df, source = history.fetch_daily("NOPE.NS")
    assert source == "unavailable" and df.empty


def test_bad_prints_flagged_not_deleted():
    df = pd.DataFrame({
        "date": pd.to_datetime(["2025-08-18", "2025-08-19"]),
        "open": [10.0, 20.0], "high": [11.0, 20.0], "low": [9.0, 20.0], "close": [10.0, 20.0],
        "volume": [1000.0, 0.0],
        "adjclose": [10.0, 20.0],
    })
    flagged = history.flag_bad_prints(df)
    assert "bad_print" in flagged.columns
    assert flagged["bad_print"].tolist() == [False, True]
    assert len(flagged) == 2  # flagged, NOT deleted


def test_bootstrap_daily_writes_parquet_and_skips_fresh(tmp_path, monkeypatch):
    calls = {"n": 0}
    def fake_fetch(sym, years=5, session=None):
        calls["n"] += 1
        df = pd.DataFrame({
            "date": pd.to_datetime([dt.date(2025, 8, 18), dt.date(2025, 8, 19)]),
            "open": [1.0, 2.0], "high": [1.5, 2.5], "low": [0.5, 1.5],
            "close": [1.2, 2.2], "volume": [10.0, 20.0], "adjclose": [1.2, 2.2],
        })
        return df, "chart"
    monkeypatch.setattr(history, "fetch_daily", fake_fetch)
    monkeypatch.setattr(history.time, "sleep", lambda s: None)

    out = tmp_path / "candles"
    st = history.bootstrap_daily(["AAA.NS", "BBB.NS"], years=5, out_dir=out)
    assert st == {"AAA.NS": "chart", "BBB.NS": "chart"}
    assert (out / "AAA.NS.parquet").exists()
    stored = pd.read_parquet(out / "AAA.NS.parquet")
    assert stored["close"].tolist() == [1.2, 2.2]

    st2 = history.bootstrap_daily(["AAA.NS"], years=5, out_dir=out)  # fresh -> no refetch
    assert st2 == {"AAA.NS": "fresh"}
    assert calls["n"] == 2


def test_update_daily_appends_without_dupes(tmp_path, monkeypatch):
    base = tmp_path / "candles"
    old = pd.DataFrame({
        "date": pd.to_datetime([dt.date(2025, 8, 18)]),
        "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
        "volume": [5.0], "adjclose": [1.0],
    })
    base.mkdir()
    old.to_parquet(base / "AAA.NS.parquet", index=False)

    new = pd.DataFrame({
        "date": pd.to_datetime([dt.date(2025, 8, 18), dt.date(2025, 8, 19)]),
        "open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0],
        "close": [1.0, 2.0], "volume": [5.0, 9.0], "adjclose": [1.0, 2.0],
    })
    monkeypatch.setattr(history, "fetch_daily", lambda sym, years=1, session=None: (new, "chart"))
    st = history.update_daily(["AAA.NS"], out_dir=base)
    assert st == {"AAA.NS": "chart+appended"}
    merged = pd.read_parquet(base / "AAA.NS.parquet")
    assert merged["date"].tolist() == [pd.Timestamp("2025-08-18"), pd.Timestamp("2025-08-19")]
    assert merged["volume"].iloc[0] == 5.0  # keep last on dupe date


def test_index_series_unavailable_returns_empty(monkeypatch):
    monkeypatch.setattr(history, "_is_fresh", lambda path, max_age_seconds=0: False)
    monkeypatch.setattr(history, "fetch_daily", lambda t, years=5: (
        pd.DataFrame(columns=history.COLUMNS), "unavailable"))
    df = history.index_series("indiavix", out_dir="data/parquet/candles_1d_it_test")
    assert df.empty  # documented graceful degradation
    import pathlib, shutil
    shutil.rmtree("data/parquet/candles_1d_it_test", ignore_errors=True)


def test_index_series_unknown_name_raises():
    with pytest.raises(KeyError):
        history.index_series("dowjones")


# --------------------------------------------------------------- integrity
def test_detect_gaps_counts_missing_trading_days_only():
    from sts.data import integrity
    # Fri 17 present, Sat/Sun skipped, Mon 20 MISSING, Tue 21 = Diwali holiday (not a gap)
    df = pd.DataFrame({"date": pd.to_datetime(["2025-10-17", "2025-10-22"])})
    missing = integrity.detect_gaps(df)
    assert missing == [dt.date(2025, 10, 20)]
    # 17 Fri + 20 Mon + 23 Wed present; 21 & 22 are Diwali holidays, 18/19 weekend
    summary = integrity.gap_summary(pd.DataFrame({
        "date": pd.to_datetime(["2025-10-17", "2025-10-20", "2025-10-23"])}))
    assert summary == {"expected_trading_days": 3, "present_days": 3, "missing": 0}
    assert integrity.gap_summary(pd.DataFrame()) == {"expected_trading_days": 0, "present_days": 0, "missing": 0}


def test_staleness_seconds_tz_naive_and_aware():
    from sts.data import integrity
    now = dt.datetime(2025, 8, 18, 12, 0, tzinfo=cal_ist)
    last_naive = dt.datetime(2025, 8, 18, 11, 0)
    assert integrity.staleness_seconds(last_naive, now) == 3600.0
    last_aware = dt.datetime(2025, 8, 18, 11, 0, tzinfo=cal_ist)
    assert integrity.staleness_seconds(last_aware, now) == 3600.0


@pytest.mark.network
def test_live_reliance_real_history():
    """REAL DATA: RELIANCE.NS daily from the primary chart API."""
    df, source = history.fetch_daily("RELIANCE.NS", years=2)
    assert source != "unavailable"
    assert len(df) > 400  # ~2 years of sessions
    assert df["date"].is_monotonic_increasing
    assert (df["close"] > 100).all()  # sanity: real prices, never fabricated
