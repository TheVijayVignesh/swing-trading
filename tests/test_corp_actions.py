"""Corp-action adjustment tests — synthetic offline + REAL RELIANCE bonus validation."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from sts.data import corp_actions as ca


def make_daily(dates: list[str], closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.to_datetime(dates),
        "open": closes, "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes], "close": closes,
        "volume": [1000.0] * len(closes), "adjclose": list(closes),
    })


# --------------------------------------------------------------- adjustment math
def test_adjustment_factor_and_ohlc_split():
    # 2:1 split: raw close halves on day 2; adjclose back-adjusts the past by 0.5
    df = make_daily(["2025-01-01", "2025-01-02"], [200.0, 100.0])
    df.loc[0, "adjclose"] = 100.0   # pre-split price restated post-split terms
    adj = ca.adjusted_ohlc(df)
    assert np.allclose(adj["factor"], [0.5, 1.0])
    assert np.allclose(adj["close_adj"], [100.0, 100.0])       # no false gap
    assert np.allclose(adj["open_adj"], [100.0, 100.0])
    # raw columns untouched (provenance kept alongside raw)
    assert np.allclose(adj["close"], [200.0, 100.0])


def test_validate_no_false_gaps_passes_on_clean_series():
    df = ca.adjusted_ohlc(make_daily(["2025-01-01", "2025-01-02", "2025-01-03"],
                                     [100.0, 101.0, 99.0]))
    assert ca.validate_no_false_gaps(df, threshold=0.25) == []


def test_validate_no_false_gaps_detects_unexplained_jump():
    df = make_daily(["2025-01-01", "2025-01-02"], [100.0, 140.0])
    df["adjclose"] = df["close"]
    offenders = ca.validate_no_false_gaps(ca.adjusted_ohlc(df), threshold=0.25)
    assert len(offenders) == 1 and abs(offenders[0][1] - 0.40) < 1e-9


def test_parse_events_from_chart_payload():
    payload = {
        "chart": {"result": [{
            "meta": {}, "timestamp": [],
            "events": {
                "splits": {"0": {"date": 1730000000, "numerator": 2, "denominator": 1}},
                "dividends": {"1": {"date": 1720000000, "amount": 8.0}},
            },
            "indicators": {},
        }]},
    }
    rows = ca._parse_events("X.NS", payload)
    types = {r["type"]: r["amount"] for r in rows}
    assert types == {"split": 2.0, "dividend": 8.0}
    assert all(r["source"] == "yahoo_chart" for r in rows)


def test_fetch_actions_degrades_explicitly(monkeypatch):
    class BadResp:
        status_code = 503
        def raise_for_status(self):
            raise RuntimeError("down")
    import requests as _rq
    sess = _rq.Session()
    monkeypatch.setattr(sess, "get", lambda *a, **k: BadResp())
    monkeypatch.setattr(ca, "fetch_actions_yfinance", lambda s: [])
    assert ca.fetch_actions("NOPE.NS", session=sess) == []  # explicit empty, never fabricated


@pytest.mark.network
def test_live_reliance_bonus_2024_adjustment():
    """REAL DATA TEST: RELIANCE 1:1 bonus, ex-date 2024-10-28.

    PROVIDER FINDING (verified live 2026-08-25): Yahoo's daily `close` is ALREADY
    split-back-adjusted — the raw close series shows NO ~50% gap at the bonus
    (the spec's "close is split-unadjusted" premise does not hold for Yahoo).
    Therefore factor = adjclose/close captures dividend adjustment only
    (~0.99..1.0 over recent years), and the invariant we can honestly test is:
      (a) the split event exists in real corp-action data,
      (b) the ADJUSTED series shows no unexplained |overnight ret| > 0.25
          anywhere near the bonus or in the whole window.
    """
    from sts.data.history import fetch_daily
    df, source = fetch_daily("RELIANCE.NS", years=3)
    assert source != "unavailable"
    assert len(df) > 400

    # (a) the 1:1 bonus split must be present with provenance (real event data)
    actions = ca.fetch_actions("RELIANCE.NS")
    splits = [a for a in actions if a["type"] == "split"]
    assert any(
        abs(a["amount"] - 2.0) < 1e-6 and dt.date(2024, 10, 20) <= a["date"] <= dt.date(2024, 11, 5)
        for a in splits
    ), f"expected 2024 bonus 1:1 split (ratio 2.0) in {splits}"

    # (b) no unexplained gaps survive adjustment — anywhere in 3y window
    adj = ca.adjusted_ohlc(df)
    around = adj[(adj["date"] >= "2024-10-24") & (adj["date"] <= "2024-10-29")]
    max_raw_ret = around["close"].pct_change().abs().max()
    offenders = ca.validate_no_false_gaps(adj, threshold=0.25)
    assert offenders == [], f"adjusted series shows unexplained gaps: {offenders}"
    # document reality: provider already smoothed the split, so no huge raw gap either
    assert max_raw_ret < 0.30, f"provider behavior changed? raw gap {max_raw_ret}"
