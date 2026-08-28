"""Bootstrap daily history from NSE official bhavcopy archives.

Used when Yahoo is rate-limited. Walks back trading days, fetches the daily
full-bhavcopy CSV (one file = ALL symbols), filters EQ series, writes the same
per-symbol parquet layout as sts.data.history. Source provenance: 'nse_bhavcopy'.

NOTE: bhavcopy CLOSE is raw (unadjusted). Yahoo (split-adjusted) remains the
preferred series; this bootstrap exists so the lab can run on real data today.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import pandas as pd
import requests

from sts.data.calendar import is_trading_day

BASE = "https://archives.nseindia.com/products/content/sec_bhavdata_full_{d}.csv"
COLUMNS = ["date", "open", "high", "low", "close", "volume", "adjclose", "source"]
OUT_DIR = Path("data/parquet/candles_1d")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"})
    try:
        s.get("https://www.nseindia.com", timeout=10)
        time.sleep(0.5)
    except requests.RequestException:
        pass
    return s


def _clean_num(x: str) -> float:
    x = str(x).replace(",", "").strip()
    try:
        return float(x)
    except ValueError:
        return float("nan")


def fetch_day(sess: requests.Session, d: dt.date, retries: int = 2) -> pd.DataFrame | None:
    url = BASE.format(d=d.strftime("%d%m%Y"))
    for a in range(retries + 1):
        try:
            r = sess.get(url, timeout=20)
        except requests.RequestException:
            time.sleep(2 * (a + 1))
            continue
        if r.status_code == 200 and len(r.text) > 1000:
            rows = []
            lines = r.text.splitlines()
            for line in lines[1:]:
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 11 or parts[1] != "EQ":
                    continue
                rows.append({
                    "symbol": parts[0],
                    "date": pd.to_datetime(parts[2], format="%d-%b-%Y"),
                    "open": _clean_num(parts[4]), "high": _clean_num(parts[5]),
                    "low": _clean_num(parts[6]), "close": _clean_num(parts[8]),
                    "volume": _clean_num(parts[10]),
                })
            df = pd.DataFrame(rows)
            return df if not df.empty else None
        if r.status_code == 404:
            return None  # holiday / not yet published
        time.sleep(3 * (a + 1))
    return None


def bootstrap(days_back: int = 420, universe: list[str] | None = None,
              out_dir: Path = OUT_DIR) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    sess = _session()
    today = dt.date.today()
    frames: dict[str, list] = {}
    fetched = 0
    d = today
    while fetched < days_back and d > today - dt.timedelta(days=days_back * 2):
        if d.weekday() < 5 and is_trading_day(d):
            df = fetch_day(sess, d)
            if df is not None:
                fetched += 1
                if universe:
                    df = df[df["symbol"].isin(universe)]
                for sym, g in df.groupby("symbol"):
                    frames.setdefault(sym, []).append(g)
                if fetched % 25 == 0:
                    print(f"  {fetched} sessions fetched ({len(frames)} symbols)", flush=True)
            time.sleep(0.8)  # polite pacing
        d -= dt.timedelta(days=1)

    written = {}
    for sym, parts in frames.items():
        df = pd.concat(parts).sort_values("date").drop_duplicates("date").reset_index(drop=True)
        df["adjclose"] = df["close"]
        df["source"] = "nse_bhavcopy"
        df[COLUMNS].to_parquet(out_dir / f"{sym}.parquet", index=False)
        written[sym] = len(df)
    meta = {"fetched_sessions": fetched, "symbols": len(written), "generated_at": dt.datetime.utcnow().isoformat()}
    (out_dir / "_bhavcopy_manifest.json").write_text(json.dumps(meta, indent=1))
    return {"sessions": fetched, "symbols_written": len(written),
            "rows": sum(written.values()) if written else 0}


if __name__ == "__main__":
    import sys
    from sts.data.universe import get_nifty200
    uni = [s.symbol for s in get_nifty200()]
    print("bootstrapping NSE bhavcopy for", len(uni), "symbols...")
    print(bootstrap(days_back=int(sys.argv[1]) if len(sys.argv) > 1 else 420, universe=uni))
