"""`python -m sts.main` — boot the Swing Lab host process.

- argparse: --db (default data/sqlite/journal.db), --port 8787, --host 127.0.0.1
- init dirs, init_db, LabManager + boot recovery, MarketDataService thread,
- uvicorn serving create_app(...) and a startup banner.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from sts.api.app import create_app
from sts.lab.manager import LabManager
from sts.marketdata.service import MarketDataService
from sts.observability.logs import get_logger, setup_logging
from sts.storage.db import init_db


def _universe_symbols() -> list[str]:
    from sts.lab.manager import _bundled_universe
    try:
        return _bundled_universe("NIFTY200")
    except Exception:  # noqa: BLE001 — degraded start with whatever we have
        from sts.lab.manager import _bundled_universe as b
        try:
            return b("NIFTY50")
        except Exception:
            return []


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sts", description="Swing Lab host process")
    parser.add_argument("--db", default="data/sqlite/journal.db")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)

    setup_logging()
    log = get_logger("sts.main")

    for d in ("data/sqlite", "logs", "data/parquet/candles_1d", "data/ref"):
        Path(d).mkdir(parents=True, exist_ok=True)

    conn = init_db(args.db)
    symbols = _universe_symbols()
    marketdata = MarketDataService(symbols)
    marketdata.start_thread()          # shared feed thread (V1.2 §2)

    lab = LabManager(conn, marketdata)
    app = create_app(lab, marketdata, conn)   # startup hook runs recover_on_boot

    url = f"http://{args.host}:{args.port}"
    log.info("swing lab starting", extra={"url": url, "db": args.db, "symbols": len(symbols)})
    print("=" * 56)
    print("  SWING LAB — multi-session paper trading lab")
    print(f"  Dashboard/API : {url}")
    print(f"  Journal DB    : {args.db}")
    print(f"  Universe      : {len(symbols)} symbols")
    print("=" * 56)

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
