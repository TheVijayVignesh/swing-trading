# Swing Lab — Runbook

## Setup (once)
```bash
uv venv --python 3.12 .venv
uv pip install -e '.[dev]'
```

## Start the lab (dashboard + paper-trading daemon in one process)
```bash
uv run python -m sts.main --port 8787
```
- Dashboard: **http://127.0.0.1:8787**
- Boot performs crash recovery automatically (RUNNING sessions resume, PAUSED stay paused).
- Keep alive for long sessions: `caffeinate -dis uv run python -m sts.main` (macOS).

## Run the test suite
```bash
uv run pytest -q                 # full offline suite (190 tests)
uv run pytest -m network -q      # live-data tests (needs network)
```

## Browser QA
```bash
G=$(npm root -g); NODE_PATH=$G node scripts/qa_browser.mjs   # screenshots → qa/
```

## Research backtest on real NSE data
```bash
uv run python scripts/backtest.py --universe NIFTY50 --years 2   # → out/backtest_*.md
```

## Data
- Historical: NSE bhavcopy archive bootstrap (`scripts/bootstrap_bhavcopy.py`) + Yahoo chart/yfinance (`sts.data.history`) → `data/parquet/candles_1d/*.parquet`
- Live: NSE quotes API (primary, 1 request/index) with Yahoo fallback, polled only during market hours → fail-closed on staleness
- Reference: `data/ref/nifty200_membership.csv` (real constituents), `data/ref/holidays.yaml`
- Journal: `data/sqlite/journal.db` (WAL; sessions/orders/fills/intents/snapshots)

## Using the Lab
1. Open the dashboard → **＋ New Session** (or *Start Recommended Experiment*).
2. Configure: name, capital, strategy, risk profile, stop policy. **You never pick stocks** — the session scans the universe autonomously.
3. Start. During market hours (09:15–15:30 IST) sessions scan every 5-minute bar, manage exits, and journal every decision.
4. Session Detail → decision funnel, positions, trade history, decision replay (click any decision).
5. Compare → select sessions → equity curves by calendar time or trade number.
6. Pause = no new entries (exits still managed). Stop = FLATTEN (exit everything) or HOLD (freeze).

## Troubleshooting
- **Feed shows CLOSED** — outside market hours or NSE unreachable; entries fail-closed.
- **Feed STALE** — no successful poll for >5 min during market hours; entries blocked, incident logged.
- **Session FAULTED** — runner exception; inspect `logs/sts.log` and the incidents panel; session state preserved.
- **Recovery** — kill the process any time; restart resumes from the journal; orphaned working orders are cancelled fail-closed.

## Modes
`PAPER` is the only enabled mode. `LIVE` is interlocked off at the API layer (403) and disabled in the UI.
