"""Operator CLI: run the daily-parquet refresh NOW (no scheduler needed).

Usage:
  uv run python scripts/refresh_daily.py           # refresh only if DUE
  uv run python scripts/refresh_daily.py --force   # bypass due-check + cooldown

Refreshes stale daily parquets for the universe + ^NSEI/^INDIAVIX via the
EXISTING service path (MarketDataService.refresh_daily_if_stale, which itself
skips file-fresh symbols). Prints daily_refresh_status() as JSON.
Exit code: 0 on success/no-op, 1 when a refresh attempt failed.
"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="refresh_daily", description="Daily parquet refresh (universe + index series)")
    parser.add_argument("--force", action="store_true",
                        help="run even if not DUE; bypasses the 30-min cooldown "
                             "(per-symbol skip-if-fresh still applies)")
    args = parser.parse_args(argv)

    from sts.main import _universe_symbols
    from sts.marketdata.service import MarketDataService

    symbols = _universe_symbols()
    md = MarketDataService(symbols)
    result = md.maybe_refresh_daily(force=args.force, sync=True)
    status = md.daily_refresh_status()
    print(json.dumps({"symbols": len(symbols), "result": result, "status": status},
                     indent=2, default=str))
    return 0 if result is None or result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
