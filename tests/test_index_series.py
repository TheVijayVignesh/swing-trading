"""Index series fixes: bootstrapped ^NSEI / ^INDIAVIX parquet must be loaded
by the service (index_frame / vix_frame), refresh must include index tickers,
and history.index_series must read the bootstrapped files (no live fetch).

The first two tests read the REAL repo data produced by
`uv run python scripts/bootstrap_index.py` — they guarantee the regime gate
receives real values. Sources are honest: 'yahoo_chart'/'yfinance' for real
Yahoo data, 'proxy_ew20' for the documented equal-weight proxy fallback.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from sts.data import history
from sts.marketdata.service import INDEX_TICKERS, MarketDataService

VALID_SOURCES = {"yahoo_chart", "yfinance", "proxy_ew20"}


class NoopPoller:
    def poll_once(self):
        return 0, 0

    def get_bars(self):
        return {}


# =====================================================================
# Bootstrap guarantees (real files in the repo)
# =====================================================================
def test_nifty_index_frame_non_empty_after_bootstrap():
    md = MarketDataService(["RELIANCE"])         # default daily_dir
    df = md.index_frame("nifty50")
    assert not df.empty, "_NSEI.parquet missing — run scripts/bootstrap_index.py"
    assert {"date", "open", "high", "low", "close", "volume", "adjclose",
            "source"} <= set(df.columns)
    assert set(df["source"].unique()) <= VALID_SOURCES
    assert pd.to_datetime(df["date"]).is_monotonic_increasing


def test_vix_frame_non_empty_and_vix_now_real():
    md = MarketDataService(["RELIANCE"])
    df = md.vix_frame()
    assert not df.empty, "_INDIAVIX.parquet missing — run scripts/bootstrap_index.py"
    assert set(df["source"].unique()) <= VALID_SOURCES
    vix = md.vix_now()
    assert vix is not None and 0.0 < float(vix) < 200.0   # sane INDIA VIX band


def test_unknown_index_name_raises():
    md = MarketDataService(["RELIANCE"])
    with pytest.raises(KeyError):
        md.index_frame("dowjones")


# =====================================================================
# Loading path (isolated tmp dir)
# =====================================================================
def test_index_frame_loads_bootstrapped_parquet_by_mapped_name(tmp_path):
    dates = pd.date_range("2026-07-01", periods=10)
    frame = pd.DataFrame({
        "date": dates, "open": 100.0, "high": 101.0, "low": 99.0,
        "close": 100.5, "volume": 1.0, "adjclose": 100.5,
        "source": "yfinance",
    })
    frame.to_parquet(tmp_path / "_NSEI.parquet", index=False)     # '^' -> '_' mapping
    md = MarketDataService(["AAA"], poller=NoopPoller(), daily_dir=tmp_path)
    got = md.index_frame("nifty50")
    assert len(got) == 10 and (got["source"] == "yfinance").all()


def test_history_index_series_reads_existing_parquet_without_network(tmp_path):
    dates = pd.date_range("2026-07-01", periods=5)
    frame = pd.DataFrame({
        "date": dates, "open": 1.0, "high": 2.0, "low": 0.5,
        "close": 1.5, "volume": 0.0, "adjclose": 1.5, "source": "proxy_ew20",
    })
    frame.to_parquet(tmp_path / "_INDIAVIX.parquet", index=False)
    df = history.index_series("indiavix", out_dir=tmp_path)
    assert len(df) == 5                            # read straight off disk


def test_index_series_empty_when_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "fetch_daily",
                        lambda sym, years=5, session=None:
                        (pd.DataFrame(columns=history.COLUMNS), "unavailable"))
    df = history.index_series("nifty50", out_dir=tmp_path)
    assert df.empty                                # fail-closed, never fabricated


# =====================================================================
# refresh_daily_if_stale includes index series
# =====================================================================
def test_refresh_daily_includes_index_tickers(tmp_path, monkeypatch):
    seen: dict[str, list[str]] = {}

    def fake_update_daily(symbols, out_dir):
        seen["symbols"] = list(symbols)
        return {s: "skipped" for s in symbols}

    monkeypatch.setattr(history, "update_daily", fake_update_daily)
    md = MarketDataService(["AAA"], poller=NoopPoller(), daily_dir=tmp_path)
    md.refresh_daily_if_stale(["AAA"])
    assert "^NSEI" in seen["symbols"] and "^INDIAVIX" in seen["symbols"]
    assert INDEX_TICKERS["nifty50"] == "^NSEI" and INDEX_TICKERS["indiavix"] == "^INDIAVIX"
