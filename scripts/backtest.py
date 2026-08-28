"""CLI: run the daily-bar backtester over real NSE bhavcopy data.

Examples:
    uv run python scripts/backtest.py --universe NIFTY50 --years 2
    uv run python scripts/backtest.py --universe NIFTY200 --years 2 \
        --report out/backtest_nifty50_2y

Data: data/parquet/candles_1d/{SYMBOL}.parquet (source nse_bhavcopy).
Prices are RAW/UNADJUSTED closes — corporate actions inside the window are a
known caveat and every report says so.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sts.research.backtest import (  # noqa: E402
    BacktestConfig,
    Backtester,
    composite_index,
    load_symbol_frames,
)

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "parquet" / "candles_1d"
REF_DIR = ROOT / "data" / "ref"

UNIVERSE_FILES = {
    "NIFTY50": "nifty50_membership.csv",
    "NIFTY200": "nifty200_membership.csv",
}


def universe_symbols(name: str) -> list[str]:
    fname = UNIVERSE_FILES[name.upper()]
    path = REF_DIR / fname
    if not path.exists():
        raise SystemExit(f"universe file missing: {path}")
    syms: list[str] = []
    for row in csv.reader(line for line in path.read_text().splitlines()
                          if not line.startswith("#")):
        if not row or row[0].strip() == "symbol":
            continue
        syms.append(row[0].strip())
    return sorted(set(syms))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--universe", default="NIFTY50", choices=["NIFTY50", "NIFTY200"])
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--capital", type=float, default=200_000.0)
    ap.add_argument("--profile", default="small", choices=["small", "standard"])
    ap.add_argument("--strategy", default="pullback-v1")
    ap.add_argument("--min-rows", type=int, default=70,
                    help="skip symbols with fewer rows in-window")
    ap.add_argument("--report", default="", help="optional output path base; writes <base>.json/.md")
    args = ap.parse_args()

    all_syms = universe_symbols(args.universe)
    end = None  # window cut below from actual last date
    frames_all = load_symbol_frames(all_syms, DATA_DIR, min_rows=1)
    if not frames_all:
        raise SystemExit("no usable parquet data found")
    last_date = max(df.index[-1] for df in frames_all.values())
    start = (last_date - timedelta(days=int(round(args.years * 365.25)))).date() \
        if hasattr(last_date, "date") else last_date - timedelta(days=int(round(args.years * 365.25)))

    frames = load_symbol_frames(all_syms, DATA_DIR,
                                min_rows=args.min_rows, start=start)
    skipped = sorted(set(all_syms) - set(frames))
    print(f"universe={args.universe}: {len(all_syms)} symbols, "
          f"{len(frames)} loaded, {len(skipped)} skipped (<{args.min_rows} rows)")

    idx = composite_index(frames)
    cfg = BacktestConfig(
        capital_initial=args.capital,
        strategy_id=args.strategy,
        risk_profile=args.profile,
        min_rows_symbol=args.min_rows,
    )
    bt = Backtester(cfg, frames, idx)
    res = bt.run()

    m = res.metrics
    print("\n=== BACKTEST RESULT (real bhavcopy data, raw/unadjusted closes) ===")
    print(f"window          : {res.dates_covered[0]} .. {res.dates_covered[1]} "
          f"({len(res.equity_curve)} sessions)")
    print(f"symbols         : {res.n_symbols}")
    print(f"total_return_pct: {m['total_return_pct']}")
    print(f"max_dd_pct      : {m['max_dd_pct']}")
    print(f"sharpe (daily)  : {m['sharpe']}")
    print(f"win_rate        : {m['win_rate']}")
    print(f"profit_factor   : {m['profit_factor']}")
    print(f"expectancy_R    : {m['expectancy_R']}")
    print(f"avg_hold_days   : {m['avg_hold_days']}")
    print(f"n_trades        : {m['n_trades']}")
    print(f"cost_drag_pct   : {m['cost_drag_pct']}")
    print("\nfunnel:", json.dumps(res.funnel, sort_keys=True))
    if res.rejection_reasons:
        print("rejections:", json.dumps(res.rejection_reasons, sort_keys=True))

    if m["n_trades"] == 0:
        print("\nNOTE: zero trades — see funnel breakdown above "
              "(days_regime_blocked vs eligible vs setups vs risk_ok).")

    if args.report:
        base = Path(args.report)
        base.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "metrics": res.metrics,
            "funnel": res.funnel,
            "rejection_reasons": res.rejection_reasons,
            "config": {
                "universe": args.universe, "years": args.years,
                "capital": args.capital, "profile": args.profile,
                "strategy": args.strategy, "min_rows": args.min_rows,
                "symbols_loaded": res.n_symbols,
                "window": [str(res.dates_covered[0]), str(res.dates_covered[1])],
            },
            "data_caveat": "raw unadjusted NSE bhavcopy closes; no corp-action adjustment",
            "equity_curve": [[str(d), e] for d, e in res.equity_curve],
            "trades": [
                {**t, "signal_date": str(t["signal_date"]),
                 "entry_date": str(t["entry_date"]), "exit_date": str(t["exit_date"])}
                for t in res.trades
            ],
        }
        base.with_suffix(".json").write_text(json.dumps(payload, indent=2))
        lines = [
            f"# Backtest report — {args.universe} {args.years}y ({args.strategy})",
            "",
            f"- window: {res.dates_covered[0]} .. {res.dates_covered[1]} "
            f"({len(res.equity_curve)} sessions, {res.n_symbols} symbols)",
            f"- metrics: {json.dumps(res.metrics)}",
            f"- funnel: {json.dumps(res.funnel)}",
            f"- caveat: {payload['data_caveat']}",
        ]
        base.with_suffix(".md").write_text("\n".join(lines) + "\n")
        print(f"\nreport written: {base}.json / {base}.md")


if __name__ == "__main__":
    main()
