"""Bootstrap daily index series: ^NSEI (NIFTY 50) and ^INDIAVIX.

Writes data/parquet/candles_1d/_NSEI.parquet and _INDIAVIX.parquet with columns
    [date, open, high, low, close, volume, adjclose, source]

Source precedence per ticker:
  1. fresh file on disk (<20h old)          -> skipped
  2. Yahoo v8 chart API (short retry — no long backoff storms)
  3. yfinance secondary flow
  4. (^NSEI only) proxy_ew20: REAL, COMPUTED equal-weight index from the
     normalized closes of the 20 largest bundled-universe symbols that have
     daily parquet on disk. Clearly labeled source='proxy_ew20' — never
     presented as the official index. ^INDIAVIX has NO honest equity-derived
     proxy; if all fetch paths fail it is left absent (fail-closed).

Usage: uv run python scripts/bootstrap_index.py [--out-dir data/parquet/candles_1d]
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sts.data.history import (  # noqa: E402
    BROWSER_UA,
    CHART_URL,
    COLUMNS,
    _is_fresh,
    _parquet_path,
    fetch_daily_yfinance,
    flag_bad_prints,
    parse_chart_payload,
)

log = logging.getLogger("bootstrap_index")

OUT_COLUMNS = COLUMNS + ["source"]
PROXY_SOURCE = "proxy_ew20"
PROXY_BASE = 100.0            # base-100 equal-weight index level
PROXY_SYMBOLS = 20


def quick_chart_fetch(symbol: str, rng: str = "5y") -> pd.DataFrame | None:
    """One-shot Yahoo chart fetch with a single short retry (script context:
    we must not sit through history.fetch_daily_chart's multi-minute backoff)."""
    s = requests.Session()
    s.headers["User-Agent"] = BROWSER_UA
    for attempt in range(2):
        host = random.choice((1, 2))
        try:
            resp = s.get(CHART_URL.format(host=host, symbol=requests.utils.quote(symbol)),
                         params={"range": rng, "interval": "1d"}, timeout=20)
            if resp.status_code == 200:
                df = parse_chart_payload(resp.json())
                if not df.empty:
                    return df
            log.warning("%s chart HTTP %s (attempt %d)", symbol, resp.status_code, attempt + 1)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s chart error: %s", symbol, exc)
        time.sleep(3.0)
    return None


def write_series(df: pd.DataFrame, path: Path, source: str) -> int:
    out = flag_bad_prints(df)[COLUMNS].copy()
    out["source"] = source
    out.to_parquet(path, index=False)
    return len(out)


def build_proxy_ew20(out_dir: Path) -> pd.DataFrame | None:
    """Equal-weight mean of normalized closes of the 20 largest universe
    symbols with existing daily parquet. A REAL series computed from REAL
    data — labeled source='proxy_ew20', NOT the official NIFTY 50."""
    try:
        from sts.data.universe import get_nifty200
        metas = get_nifty200()
    except Exception as exc:  # noqa: BLE001
        log.warning("universe unavailable for proxy (%s); trying nifty50", exc)
        try:
            from sts.data.universe import get_nifty50
            metas = get_nifty50()
        except Exception as exc2:  # noqa: BLE001
            log.error("no universe snapshot at all: %s", exc2)
            return None
    closes: dict[str, pd.Series] = {}
    for m in metas:
        if len(closes) >= PROXY_SYMBOLS:
            break
        p = _parquet_path(out_dir, m.symbol)
        if not p.exists():
            continue
        try:
            df = pd.read_parquet(p)
        except Exception:  # noqa: BLE001
            continue
        if df.empty or "close" not in df or "date" not in df:
            continue
        s = df.dropna(subset=["close"]).set_index("date")["close"].astype(float)
        first = s.iloc[0]
        if first and first > 0:
            closes[m.symbol] = s / first
    if len(closes) < 10:
        log.error("proxy_ew20 needs >=10 constituent series; found %d", len(closes))
        return None
    panel = pd.DataFrame(closes).dropna(how="any").sort_index()
    idx = panel.mean(axis=1) * PROXY_BASE
    # synthesize OHLC around the index level so downstream consumers see the
    # expected columns; volume/adjclose mirror close (documented proxy shape).
    df = pd.DataFrame({
        "date": pd.to_datetime(idx.index),
        "open": idx.shift(1).fillna(idx.iloc[0]),
        "high": idx,
        "low": idx.shift(1).fillna(idx.iloc[0]),
        "close": idx,
        "volume": 0.0,
        "adjclose": idx,
    }, columns=COLUMNS).reset_index(drop=True)
    log.info("proxy_ew20 built from %d symbols, %d sessions", len(closes), len(df))
    return df


def bootstrap(name: str, ticker: str, out_dir: Path) -> str:
    path = _parquet_path(out_dir, ticker)
    if _is_fresh(path):
        return "fresh"
    df = quick_chart_fetch(ticker)
    if df is not None:
        n = write_series(df, path, "yahoo_chart")
        print(f"{name}: wrote {n} rows to {path} (source=yahoo_chart)")
        return "yahoo_chart"
    df = fetch_daily_yfinance(ticker, years=5)
    if df is not None and not df.empty:
        n = write_series(df, path, "yfinance")
        print(f"{name}: wrote {n} rows to {path} (source=yfinance)")
        return "yfinance"
    if name == "nifty50":
        df = build_proxy_ew20(out_dir)
        if df is not None and not df.empty:
            n = write_series(df, path, PROXY_SOURCE)
            print(f"{name}: wrote {n} rows to {path} (source={PROXY_SOURCE})")
            return PROXY_SOURCE
    print(f"{name}: UNAVAILABLE — no parquet written (fail-closed)", file=sys.stderr)
    return "unavailable"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default="data/parquet/candles_1d")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    statuses = {
        "nifty50": bootstrap("nifty50", "^NSEI", out_dir),
        "indiavix": bootstrap("indiavix", "^INDIAVIX", out_dir),
    }
    print(statuses)
    return 0 if all(s != "unavailable" for s in statuses.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
