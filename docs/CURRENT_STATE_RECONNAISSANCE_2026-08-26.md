# CURRENT STATE RECONNAISSANCE — 2026-08-26

**Method:** Seven parallel read-only investigations (architecture, data pipeline, session/runtime, database, strategy/broker, API/UI, tests) followed by personal cross-checks of every critical claim against the live repository, the live SQLite database, running processes, and current logs. Nothing was modified.

**Critical framing fact discovered during verification:** the previous diagnostic (`MULTI_SESSION_DIAGNOSTIC_2026-08-26.md`, written 11:41 IST today) was followed by a **fix wave at 12:44–12:51 IST today**, but the **running server predates that wave** (booted 09:49:17 IST). Therefore most "is it fixed?" answers differ between *disk* (current truth) and the *running process* (stale code in memory). This report distinguishes the two everywhere it matters.

---

## 1. Executive Summary

What exists right now:

- A working, self-healing multi-session paper-trading lab for NSE ("swing-lab", package `sts`, Python ≥3.12, FastAPI + SQLite + parquet), launched as `uv run python -m sts.main --port 8787`.
- **One server process is alive right now**: PID 41585, booted 2026-08-26 09:49:17 IST, listening on 127.0.0.1:8787; a browser is actively polling the dashboard.
- **10 sessions exist** (3 RUNNING: `hybrid-main`, `det-only`, `random-k` @ ₹25,000 each; 7 ARCHIVED incl. the `lineup-*` trio and four QA sessions). Sessions are created only via API/UI; nothing auto-seeds at boot.
- All three RUNNING sessions are scanning NIFTY200 every 5 minutes (`scanned=200, eligible=197, setups=0`) and **have never traded**: `intents`, `orders`, `fills`, `positions` are all empty; every equity snapshot is exactly ₹25,000. This is confirmed strategy-specification behavior (volume rule), not an execution bug.
- **281 tests pass, 0 failures** (5 network-marked deselected).
- **Five of the six previously-diagnosed defects are already fixed on disk** — mostly in today's 12:44–12:51 fix wave with regression tests and DB migration 5 applied at 12:52 IST. However, the running process still executes pre-fix code for three of them (timezone funnel writes, incidents_24h display, intent→order commit deferral). A restart is required to converge runtime with disk.
- The largest genuine capability gap for the project's stated purpose ("explain why a trade did or did not happen"): **per-symbol rejections at the setup stage are never persisted** — exactly the stage where all 200 symbols currently die. `top_rejections_json` exists in schema but has zero non-null rows.

## 2. Current Architecture

**Entrypoint & launch chain** (verified): `src/sts/main.py` → argparse defaults (`--db data/sqlite/journal.db --port 8787 --host 127.0.0.1`) → `setup_logging()` → mkdirs → `init_db()` (schema + migrations) → bundled NIFTY200 universe from `data/ref/nifty200_membership.csv` → `MarketDataService(symbols)` + `start_thread()` (60 s daemon poll thread) → `LabManager(conn, marketdata)` → `create_app(...)` registers `@app.on_event("startup")` → uvicorn run. On startup event: `lab_manager.recover_on_boot()` (manager.py:316–343) rebuilds ledgers for RUNNING sessions from fills replay, cancels orphaned WORKING orders fail-closed, respawns runners. No shutdown hook exists; crash-safety strategy is recovery-on-boot. Confirmed in `logs/server.log`: boot 09:49 IST → "boot recovery done" → 3 runner starts.

**Module map:** `main → api.app {routes_api, routes_lab, routes_pages} → lab.manager → lab.factory → {brokers.paper+costs, execution.order_manager, portfolio.selector, risk.engine, strategy.registry, storage.repos}`; `lab.runner ← marketdata.service ← data.{live, history, calendar, corp_actions, universe, integrity}`; `lab.policies`; shared leaf `contracts.py`; research path `research.{backtest, walkforward}` reuses production strategy/risk/fill code.

**Notably absent vs architecture docs:** no git repository (`.gitignore` prepared, never `git init`); no launchd plist / Makefile / cron (supervision is manual `caffeinate`); no ML package at all (only an `ml_enabled` flag); no broker adapters beyond PaperBroker; no scheduler library (poller thread does cron-like work).

**Packaging:** pyproject `swing-lab` v0.1.0, hatchling, no `[project.scripts]`. Deps: fastapi, uvicorn, jinja2, pydantic, pandas, numpy, yfinance, requests, python-multipart, pyarrow, pyyaml.

## 3. Runtime Flow — one real bar, end to end

Verified flow (all steps have file:line evidence):

1. **Feed thread** `MarketDataService._run_forever` (marketdata/service.py:130–149) runs `poll_cycle()` every 60 s.
2. **Failover layer** `FailoverPoller.poll_once()` (data/live.py:525–550): active source poll; ≥3 consecutive failed cycles → promote next source (immediate poll), loser gets 600 s cooldown; union-merge keeps newest bar.
3. **Primary source** `NSEQuotePoller._fetch_all()` (live.py:349–378): cookie warmup on nseindia.com homepage → single GET `https://www.nseindia.com/api/equity-stock-indices?index=NIFTY%20200` (renamed endpoint; old name caused the historical HTTP 404 storm) → normalize rows (lastPrice/dayHigh/dayLow/open, cumulative totalTradedVolume) → `_ingest_snapshot()` aggregates into 5-minute windows keyed on window-open IST time → `_emit_bar()` computes window volume as **cumulative-day-volume delta** → emission on first snapshot of next window OR proactive `_flush_open_windows()` when window age ≥300 s (max legit latency ≈ window end + 60 s cadence).
4. **Fallback source** `YahooChartPoller._fetch_batch()` (live.py:158–174): ≤100-symbol batches of `.NS`, v8 chart API `range=1d&interval=5m`, walks stamps back to newest fully-closed bar.
5. **Bus** `poll_cycle` diffs vs `_latest` (advance-only) → ONE batch event per subscriber via `loop.call_soon_threadsafe` (service.py:181–207); overflow drops NEWEST batch, counts `dropped_events`, alerts QUEUE_OVERFLOW_DROP (never silent).
6. **Per-session queue** asyncio.Queue(maxsize=1000) created at `subscribe()`.
7. **Runner consume** `_consume → _process_event → _process_bar` (lab/runner.py:374–433): filter non-5m bars → day rollover → append to `_intraday[symbol]` → stamp sink coalescing key → **`broker.on_bar(session_id, bar)`** (fills/exits run even while paused) → decision-window gate (bar close times 09:30–15:25 IST) → feed-staleness gate (fail-closed) → schedule scan for close_ts.
8. **Scan** `_drain_pending_scans → _scan_entries` (runner.py:569–786): daily frames from LRU cache → eligibility filter (≥60 daily rows, px ≥ ₹50, 20-day median turnover ≥ ₹5 cr) → `StrategyContext` (rng_seed = sha256(session_id)[:8]) → `strategy(ctx, params)` pure call → portfolio selector → risk engine per candidate → atomic placement.
9. **Decision persistence & order** inside `repo.transaction()`: `insert_intent(commit=False)` → `sink.current_intent_id` → `order_manager.place_order` → `PaperBroker.place_order` (LIMIT-only, circuit-band ±10%, funds check) → `sink.on_order` mints order row (commit deferred by transaction depth guard) → funnel row via `record_funnel`.
10. **Fill** on a later bar: PaperBroker.on_bar resolves protective exits first (adverse sequencing), then working entry limits (strict trade-through, half-spread paid, expire after 6 bars) → sink persists fills, positions, account snapshots, metrics.
11. **API/UI**: dashboard polls `/api/lab/board` (5 s) and htmx-polls `/sessions/{sid}` (5 s); charts hydrate from `account_snapshots`/`metrics_timeseries`/closed-position queries. No fabricated values anywhere (verified by UI agent grep + qa2 three-way integrity checks).

## 4. Data Architecture

**Historical (daily):** one parquet per symbol in `data/parquet/candles_1d/` (203 files = 200 equities + `_NSEI` + `_INDIAVIX` + manifest). Producers: NSE full-bhavcopy bootstrap (primary provenance on disk, raw unadjusted closes) and Yahoo v8 chart/yfinance fallback. Runtime loading via `get_daily_frame` LRU-256; index/VIX frames from `_NSEI`/`_INDIAVIX` parquet. **Current dates:** equities end 2026-08-24, index ends 2026-08-25 (1-day skew is expected pre-cutoff; refresh due-check keys off `_NSEI` after 16:30 IST).

**Daily refresh (Defect 1):** `poll_cycle()` now calls `self.maybe_refresh_daily(now)` (service.py:244) — throttled due-check every 30 min, executes `refresh_daily_if_stale()` in a background daemon thread after the 16:30 IST cutoff on trading days, skipping files fresher than 20 h; operator CLI `scripts/refresh_daily.py`. Eleven-test regression suite explicitly named for this finding. **Caveat:** service.py mtime is today 12:45 (after server boot), so the *running* process may not have this wiring in memory; no refresh has been due yet today anyway. First real proof point: after 16:30 IST today.

**Live:** NSE quote polling primary, Yahoo chart fallback (service.py:94–101). Bars are IST-naive 5-minute bars anchored to window opens.

**Freshness model (three layers):**
- Poller: status CLOSED/FEED_STALE/FEED_OPEN at 300 s threshold (STALE_AFTER_SECONDS, live.py:60).
- Service: `feed_status()` STALE iff forced-stale or tick-age >300 s; ERROR alert latches only past 600 s, measured from `_open_phase_since` for never-ticking feeds (service.py:264–281 — the alert-grace fix; mtime caveat as above).
- Runner watchdog: incident FEED_STALE_ENTRIES_BLOCKED when no bar for any symbol >300 s while OPEN (runner.py:67, 253–269); entries double-gated fail-closed; **exits are never blocked** (stops/targets/trails/time-stops/regime exits always run, widened slippage noted when stale).

The original Defect-2 premise (~360 s inter-delivery) no longer holds because of proactive window flushing + the global any-symbol bar clock + immediate failover promotion. Constants remain 300/600 s and are covered by tests.

**Universe:** NIFTY200 bundled CSV (fetched_at 2026-08-24), weekly-TTL NSE archive fetch with fail-closed degradation to snapshot; per-session universes supported (`resolve_universe`, ≥3 symbols); sha256 universe_snapshot_id pinned per session.

**Calendar/corp actions:** embedded NSE holidays 2025/2026 + editable YAML override; market_phase PRE_OPEN/OPEN/AFTER_HOURS; corp actions from Yahoo events with adjustment model preserving raw OHLC and false-gap validation.

## 5. Session Architecture

**Creation:** `POST /api/sessions` validates capital ≥1000 int, mode must be `paper` (else 403 LIVE_INTERLOCKED), strategy_id ∈ registry, risk_profile auto-tiers to micro <₹30k → `SessionConfig` → `SessionRepo.create_session` persists full config as YAML + content_hash + version stamps. Clone endpoint reads frozen source config. Start is separate (`POST .../start`). **No auto-created sessions**: startup only runs recover_on_boot; the `hybrid-main/det-only/random-k` names exist as (a) the three currently-RUNNING sessions created 2026-08-25 02:19 UTC via API and (b) a UI "recommended lineup" button that creates `lineup-*` copies only after explicit modal confirmation. Not random, not server-seeded — but see §20 Q3.

**Isolation (verdict: CONFIRMED, matches previous diagnostic):** every trading-relevant object is instantiated per session in `build_session_graph` (factory.py:236–264) and `SessionRunner.__init__`: TradingRepo (session-id-bound, cross-session access raises IsolationError, repos.py:616–624), RepoSink, SpreadModel/SlippageModel, PaperBroker, OrderManager, RiskEngine wrapper, strategy invocation context, fan-out queue, consume+watchdog tasks (3 asyncio tasks per RUNNING session on the single loop). Shared surface: one sqlite connection (loop-thread-confined), one lock-guarded MarketDataService, immutable STRATEGIES registry, pure selector/risk functions. Module-level mutable state audit found only the thread-local txn-depth registry and MarketDataService fields (lock-guarded). Identical `scanned=200/eligible=197` across sessions is legitimate same-universe behavior — do not "fix".

**Lifecycle:** CREATED→RUNNING⇄PAUSED→STOPPING→STOPPED(HELD|FLATTENED)/ABORTED(+HELD on flatten timeout), plus ARCHIVED. Pause blocks new entries only; exits keep firing. Runner exceptions set internal `faulted` without mutating lifecycle status. Resume handles reboot-zombies by spawning fresh runners. MAX_SESSIONS=10 concurrent RUNNING.

**Recovery (proven by tests):** RUNNING at boot → ledger rebuilt from fills replay (cash formula, FIFO realized, hwm from snapshots), orphaned WORKING orders cancelled fail-closed, RECOVERED event; PAUSED stay dormant; broker state exportable/restorable.

## 6. Strategy (exact, unchanged — read-only documentation)

**Id:** `pullback-v1` (registry.py:12), runtime version v1.0.0. Pure-function module `strategy/pullback_v1.py`.

Parameters (defaults pullback_v1.py:18–30; overridable via session `params`, persisted in config_yaml):
min_daily_rows=60, pullback_window=5, rsi_n=14, rsi_min=45, rsi_max=70, atr_n=14, **vol_multiple=1.5**, vol_sma_n=20, slope_n=10, stop_atr_mult=1.5, vix_max=22. Hardcoded: trading window 09:30–14:30 IST.

Pipeline in evaluation order:
0. Time gate 09:30–14:30 else [].
1. Regime gate (fails closed if index missing): last_close>SMA50 ∧ SMA20>SMA50 on ^NSEI; VIX<VIX_max (missing VIX passes-with-flag).
2. Trend: close>SMA50 ∧ SMA20>SMA50 ∧ slope10(SMA50)>0 (daily).
3. Pullback: low touched SMA20 within last 5 days ∧ close reclaimed SMA20.
4. RSI(14): 45≤RSI≤70.
5. **Volume rule — exact code (pullback_v1.py:174–176):** `today_vol = intraday["v"].sum()` (cumulative partial-day 5m volume so far) must be ≥ `1.5 × SMA20(daily volume)`. **No time-of-day normalization.** Morning breakouts are structurally near-impossible (~2 % of day volume at 09:35 vs a 150 %-of-full-day hurdle). This is the confirmed cause of zero trades. The backtester masks this (it presents the full day as one "intraday" bar), so backtests overstate live triggerability.
6. Breakout: intraday_high > previous completed daily high (strict >).
7. Stop: `trigger − 1.5×ATR14`. Targets T1=+1R / T2=+3R assigned by runner (config t1_multiple/t2_multiple). Trailing after T1 (hh_since_t1 − 1.5×ATR), arms next bar. Time-stop 10 days → exit next open. REGIME_EXIT limit at 0.995×last when regime rules break.
8. Portfolio caps (selector): max_positions=4, total open risk ≤2 % equity, position cap by profile (small .33/micro .60/std .20), correlation ≤0.7 vs admitted (NaN fails closed), sector count ≤2, sector exposure ≤40 % — **sector rules are inert** (pseudo-sector per symbol).
9. Risk engine (fixed CHECK_ORDER, pre-trade, entries only): qty=floor(risk_per_trade×E/(entry−stop)) [small 1.5 %], min_notional, max_positions, total_open_risk ≤2 %, position_cap, gross_exposure ≤80 %, daily_loss_limit −3 %, drawdown_kill −10 % (fail-closed hwm), ADV size ≤0.5 %. Exits bypass risk by design.

**random-k** remains registered/selectable: identical detection then seeded uniform sample k=3 — ablation baseline. **ML:** flag default False; implementation does not exist anywhere; enabling only journals an ML_NOT_AVAILABLE_DETERMINISTIC_FALLBACK incident. Risk engine structurally ignores ml_score.

## 7. PaperBroker

LIMIT-only hard invariant; ±10 % circuit band vs last price; funds check with 1 % buffer; modify = cancel+replace chain; binary fills (no partials; T1 sells half). Fill model: half-spread (5 bps) paid on every fill; strict penetration for entries (`low < limit`, or touch with volume ≥3× order) and targets; stops honor adverse sequencing (stop before target same bar), gap-through fills at open-based price, invariant stop-fill ≤ stop_px asserted by property test; slippage tiers base 5 bps / illiquid 10 bps; working orders expire after 6 bars; time-stop executes next bar open. Cash ledger with exact-paisa India delivery costs (c1.0.0: zero brokerage, STT 0.1 % sell, exchange txn, GST, SEBI, stamp buy, DP ₹13/sell — golden-tested). Long-only in practice. Crash recovery via export_state/restore.

## 8. Database

SQLite WAL at `data/sqlite/journal.db` (+wal 4.1 MB / main 635 KB). Nine tables + `scan_funnels_tz_audit`; append-only migration list in `_schema_migrations` (v1–v4 applied 2026-08-25 14:17 UTC; **v5 timezone repair applied 2026-08-26 07:22 UTC = 12:52 IST**, after the running server booted). Schema and migrations verified in sync with code.

Current volumes (verified live, growing during market hours): sessions 10, session_events ~1456+, account_snapshots 220, metrics_timeseries 660, incidents 19, scan_funnels 198, **intents 0, orders 0, fills 0, positions 0**.

**Decision-replay capability:** lifecycle events, ACTIVITY states, 5-min funnel aggregates, watchdog liveness floors, incidents, equity/drawdown/exposure series, and (once trades exist) full intent→order→fill→position chains with risk_checks JSON — all persisted atomically. **Blind spot:** setup-stage per-symbol outcomes are lost (`setups=0` with no names); `top_rejections_json` never populated; funnel payload verified as `{ts, scanned, eligible, setups, ml_passed, portfolio_ok, risk_ok, selected}` only. "Why didn't KOTAKBANK trade at 09:30?" is currently unanswerable from persisted data alone.

Funnel timestamp convention post-fix: canonical aware-UTC ISO strings everywhere; migration 5 corrected 27 historical corrupt rows with full audit trail.

## 9. API

Control plane (routes_api.py, implements docs/API_CONTRACT.md): GET /api/lab/summary; POST/GET /api/sessions; start/pause/resume/stop/clone; GET session detail (portfolio, positions, trades, equity+drawdown curves, decisions, funnel_latest, feed_status); GET /api/sessions/{sid}/decisions/{intent_id} (replay chain); GET /api/lab/compare; GET /api/system/health; /healthz.

Addendum v2 (routes_lab.py): GET /api/lab/board (activity states, recent decisions); archive/restore; protected DELETE (CREATED-only); POST .../scan (honest diagnostic scan; MARKET_CLOSED deferrals); GET .../timeline (paged merged events/intents/fills); GET /api/lab/benchmark (^NSEI normalized, honest unavailable); GET /api/lab/compare_extra (sharpe/sortino/cagr/top rejections).

Pages: `/` overview, `/sessions/new` (full hyperparameter form + live effective-config preview + clone prefill), `/sessions/{sid}` dossier, `/compare`. LIVE mode 403-interlocked at both manager and API layers.

## 10. Dashboard

Aesthetic implemented as specified: Japanese editorial/watercolor system (washi-paper grain via feTurbulence, sumi-ink tokens, vermilion accent, ink-brush hero SVG, rising-disc wash, falling sakura petals capped 0.35 opacity, vertical 自動売買研究所 label, ensō empty states), dark mode with localStorage persistence and chart redraw, reduced-motion respected. Dependency-free SVG chart lib (SLCharts) — sparklines, equity curve with NIFTY50 benchmark overlay, drawdown curve, trade P&L bars, R-multiple and hold-days histograms, exposure-through-time area, compare overlays + small multiples. Every numeric is hydrated from API payloads backed by DB queries; grep found no lorem/dummy/demo data; empty states honest; slippage rendered as explicit "not journaled". Dossier includes positions (16 cols), trade history (15 cols), decision journal with replay buttons, funnel + rejection breakdown, capital dossier, config snapshot, timeline, diagnostic pane. Refresh: 5 s board poll + htmx detail poll + 30 s heartbeat; no WS/SSE.

Functional-vs-decorative verdict: **functional**. Two QA rounds pass (qa: clean; qa2: 23/24 — the single failure is a stale test-selector `[data-field="equity"]`, not an app defect). Known cosmetic: `/favicon.ico` 404 probe while SVG favicon is properly linked.

## 11. Current Sessions (verified from DB, unaltered)

| Name | Status | Capital | Strategy | ML | Created (UTC) |
|---|---|---|---|---|---|
| hybrid-main | **RUNNING** | 25000 | pullback-v1 | deterministic | 08-25T02:19:58 |
| det-only | **RUNNING** | 25000 | pullback-v1 | deterministic | 08-25T02:19:58 |
| random-k | **RUNNING** | 25000 | random-k | deterministic | 08-25T02:19:58 |
| lineup-hybrid-main | ARCHIVED | 25000 | pullback-v1 | pinned-at-start (ml_enabled=true) | 08-25T12:37:38 |
| lineup-det-only | ARCHIVED | 25000 | pullback-v1 | deterministic | 08-25T12:37:38 |
| lineup-random-k | ARCHIVED | 25000 | random-k | deterministic | 08-25T12:37:38 |
| qa-archive-me ×4 | ARCHIVED | 12000 | pullback-v1 | deterministic | 08-25→08-26 |

All share: universe NIFTY200, FLATTEN stop policy, params `{max_positions:4, max_total_open_risk:0.02, max_gross_exposure:0.8, daily_loss_limit:0.03, drawdown_kill:0.1, time_stop_days:10, trail_mult_atr:1.5, t1_multiple:1.0, t2_multiple:3.0}`. Positions/trades/P&L: none anywhere; latest snapshots 09:25:37Z equity ₹25,000 flat; latest funnels (09:20Z & 09:25Z) scanned=200 eligible=197 setups=0 for all three.

## 12. Live Data Status (as of ~15:00 IST, Wed 2026-08-26)

- Market OPEN; bars flowing; scans executing every 5 min per session; feed recovered from a 14:00 IST episode (NSE curl timeout → FEED_STALE alerts all sessions → FEED_STALE_ENTRIES_BLOCKED incidents → recovered; scans normal again by 14:50).
- Earlier today: transient curl timeouts 10:20 IST (FEED_STALE_OVER_10MIN ERROR latch fired once); quiet 10:58→13:23; the Aug-25-era "NSE quotes HTTP 404" storm is gone post-endpoint-rename.
- Historical: equities parquet end 2026-08-24 (bhavcopy, 420 sessions × 200 symbols, 80,525 rows); index ends 08-25. Refresh becomes due after 16:30 IST today.
- Fallback health: failover executed twice correctly this morning during NSE degradation (09:17/09:20 IST per sts.log era logs); Yahoo usable via curl_cffi impersonation but per-symbol errors still DEBUG-level (see §13/Defect 3).
- Queue drops: mechanism counted/alerted (QUEUE_OVERFLOW_DROP); no current evidence of active dropping.

## 13. Previous Diagnostic Findings — current status (code = authority)

Timeline established from file mtimes + DB + process start: diagnostic written 11:41 IST → fix wave 12:44–12:51 IST (repos/service/migrations/routes_api + five test files) → migration 5 applied 12:52 IST → **server NOT restarted since 09:49 IST**.

| # | Defect | Disk | Running process | Evidence |
|---|---|---|---|---|
| 1 | Daily refresh no caller | **ALREADY FIXED** | UNKNOWN/LIKELY ABSENT (service.py edited 09:49+; untestable until due) | service.py:244 `maybe_refresh_daily` wired into poll_cycle; :380–477 due logic/cutoff/thread; scripts/refresh_daily.py; tests/test_daily_refresh.py (11 tests, docstring names "diagnostic finding #1"); mtime service.py 12:45 today |
| 2 | Watchdog 300 s false-stale | **ALREADY FIXED IN EFFECT** | FIXED (live.py+runner.py predate boot) | Proactive `_flush_open_windows` (live.py:339–346, called :377) bounds latency ≈ window+cadence; global any-symbol `_last_bar_at` clock (runner.py:392, checked :255); boot-aware 600 s alert branch (service.py:264–281); constants still 300/600 (runner.py:67, service.py:51–52); tests pin semantics |
| 3 | Yahoo 429 silent DEBUG failure | **PARTIALLY FIXED** | SAME | Root cause addressed via curl_cffi chrome impersonation (live.py:34–42, MEASURED comment); retries/backoff at WARNING, final at ERROR (history.py:97–113); source-health counters + WARNING failover logs (live.py:444–505). Residual: per-symbol Yahoo live-fetch errors STILL `log.debug` (**live.py:173 — personally verified today**); history bootstrap/update use plain requests session (history.py:191,220) |
| 4 | scan_funnels naive-IST-as-UTC | **ALREADY FIXED** (code + migration 5 repaired 27 rows w/ audit) | **STILL BROKEN on watchdog path** | Disk: `utc_iso` treats naive as IST→UTC (repos.py:43–53, mtime 12:44); migration v5 + `scan_funnels_tz_audit` applied 12:52 IST; 15-test suite. Live: personally verified skew continuing minutes ago — watchdog "no data" funnel id=195 ts=`14:48:26` (naive IST) vs paired bar-close rows id=199–201 correct `09:25:00+00:00`; bar-close path was always clean (aware inputs converted correctly pre-fix). Converges on restart |
| 5 | incidents_24h `day_ago = now` | **ALREADY FIXED** | **STILL BROKEN (displays ~0)** | Disk: routes_api.py:228 proper `now(timezone.utc)−timedelta(hours=24)`, string-compare valid vs aware-UTC storage (:210–216); 5-test boundary suite. Running process loaded routes_api pre-fix (mtime 12:47 > boot) → health strip undercounts until restart. Personally verified code on disk |
| 6a | Boot false-STALE (no INITIALIZING) | **PARTIALLY FIXED** | MIXED | No literal INITIALIZING state exists (grep: zero hits). Closed-market boots emit WAITING_MARKET_OPEN heartbeats, not stale (runner liveness floor, live in memory); never-ticking-feed alert grace branch is on disk in service.py (12:45, stale in memory). Semantics covered by tests (feed_failover:297–308, runner_liveness:132–153) |
| 6b | RepoSink.on_order commit-inside-txn | **ALREADY FIXED** | **PRE-FIX BEHAVIOR ACTIVE** (latent — zero orders ever placed) | Disk: `_finish_commit` defers ALL inner commits incl. commit=True via thread-local depth counter (repos.py:64–90, 238–270, mtime 12:44); nested txn refcounted; 8-test atomicity suite incl. KeyboardInterrupt crash integrity. Running process has pre-fix repos.py → original premature-commit hazard technically live but unreachable while intents table stays empty |

**Zero-trade investigation conclusion stands:** funnel shows scanned 200 → eligible 197 → setups 0 all day; prior diagnostic reconstructed trend 87 → pullback 26 → RSI 26 → volume 0 with KOTAKBANK/SBICARD rejected at volume ~09:30. Per-symbol rows for those rejects were never persisted (searched: 0 hits) — consistent with the §8 blind spot. Not an execution bug. Left untouched per instructions.

## 14. Known Bugs (confirmed, excluding strategy-rule design issues)

1. **Stale running process** — server (boot 09:49 IST) executes pre-fix repos.py/service.py/routes_api.py: skewed watchdog-funnel timestamps continue, incidents_24h undercounts, intent→order commit-deferral absent. Fix = restart at a safe moment (§20 Q2). Not a code bug — an ops gap.
2. **Yahoo per-symbol live-fetch failures logged at DEBUG** (live.py:173) — degraded fallback can hide.
3. **Hard-delete leaks scan_funnels rows** — routes_lab.py:236–238 deletes 8 child tables but not scan_funnels; same omission in SessionRepo.delete_hard (repos.py:191–210). Orphaned funnel rows survive deleted sessions.
4. **Sector rules inert in live and backtest** — pseudo-sector-per-symbol makes SECTOR_COUNT/SECTOR_EXPOSURE dead checks (runner.py:556–560, backtest.py:222–224).
5. **Live candidate scoring is nominal** — `score = len(ordered) − i` (arrival order), so selector "ranked" admission is really first-come (runner.py:638).
6. **Backtest/live divergence on the volume rule** — backtester presents full-day OHLCV as the intraday frame (backtest.py:493–499), systematically overstating live signal rates.
7. **update_daily drops provenance column** — `old[COLUMNS]` concat discards `source` added by bhavcopy/index writers on first incremental rewrite (history.py:230).
8. **Duplicated lifecycle paths** — archive/restore implemented twice (raw SQL routes_lab.py:195–222 vs guarded SessionRepo methods repos.py:162–189); flatten-timeout incident inserted via raw SQL bypassing repo isolation (manager.py:276–283). Works today; divergence hazard.

## 15. Suspected Issues (not confirmed as bugs)

- Single shared connection + thread-local txn-depth guard relies on zero-`await` discipline inside `with transaction():` blocks and on manager's bare `conn.commit()` calls never interleaving; safe today (single-threaded loop, no awaits in blocks), fragile by construction.
- Universe-resolution failure silently falls back to the boot NIFTY200 list (manager.py:154–159) — a NIFTY50 session would scan the wrong universe rather than fail closed.
- Queue overflow drops the NEWEST batch (per-subscriber) — missed exit management for that interval; counted/alerted but lossy.
- Watchdog emits "no data" funnels even during open-market hours with flowing bars (observed 14:48 IST between healthy scans) — floor logic may not credit bar-close scans; harmless but noisy.
- `graphs` dict retains stopped sessions' graphs for process lifetime (intentional for API queries; slow growth).
- `stop(FLATTEN)` on a reboot-zombie RUNNING session raises LifecycleError until resumed (edge).
- No shutdown hook / no supervision (launchd absent) — resilience rests wholly on recovery-on-boot.
- Bhavcopy data is raw/unadjusted (acknowledged debt in code comments); corp-actions adjustment tooling exists but isn't fused into the daily store.
- Equities parquet can lag index parquet by a session pre-refresh (due-check keys off `_NSEI` only).
- Dead attribute read `factory.py:203` (`_opened_wallclock` never set) — harmless.
- `.env` present in repo dir (gitignored contents unknown; repo is not git-initialized anyway).

## 16. Missing Capabilities (required by project goals, absent today)

- **Setup-stage rejection persistence** — per-symbol rule outcomes at the stage where everything currently dies; `top_rejections_json` plumbed but never populated. Blocks the core "why?" promise.
- INITIALIZING (vs STALE) feed/boot state.
- Real sector mapping + real correlation matrix inputs (currently pseudo-sector, pairwise corr fn exists but fed by pseudo sectors).
- Informative candidate ranking (ML layer entirely unbuilt; deterministic ranking nominal).
- Broker abstraction beyond PaperBroker (Sandbox/Live adapters absent; Upstox seam is clean at the Poller interface — new sources plug in beside NSEQuotePoller/YahooChartPoller — but nothing implemented).
- Supervision/launchd + shutdown hooks; git initialization (!); automated Playwright e2e wired into CI (current qa_*.mjs are manual scripts with hardcoded paths); network-marked tests excluded from CI by default.
- Session dossier extras: profit factor/win-rate/expectancy surfaces exist in compare_extra metrics but per-session sector-exposure and correlation views are absent (data doesn't exist to feed them honestly).

## 17. Test Coverage

**281 passed, 5 deselected (network-marked), 0 failures/skips** — collected 286. Strong quality signals: injected clocks everywhere, seeded property tests (500-iteration risk-engine and stop-fill loops), golden values (costs to the paisa, Wilder RSI 70.46), full-graph integration through real service/runner/broker, temp-dir SQLite per test, fail-closed philosophy asserted repeatedly.

Highlights: crash/restart recovery proves decision replay + equity continuity to 1e-6 after hard restart (test_persistence_recovery); reboot-zombie resume; flatten-timeout honesty; atomicity incl. simulated mid-chain crashes; storage isolation incl. cross-session hijack refusal; feed failover incl. cross-thread publish and overflow counting; calendar/holiday grid; migrations incl. legacy-DB upgrade preserving rows.

Six-defect regression mapping: (1) covered (11 tests), (2) covered semantically, (3) covered for degradation contracts (not the DEBUG residual), (4) covered (15 tests incl. audit-trail migration), (5) covered (boundary-inclusive window math), (6a) partially named (behavior covered, no INITIALIZING literal), (6b) fully covered.

Untested/weakly tested: N>2 concurrent-runner contention, pytest-level UI e2e, timeline/delete/archive/board endpoints (only manual qa scripts), live-network integration (deselected in CI), walkforward end-to-end training loop (doesn't exist), corporate-action edge cases (reverse splits etc.), ML path beyond fallback incident, disk-full/DB-lock contention.

## 18. Experiment Readiness

| Dimension | Rating | Rationale |
|---|---|---|
| Data | **YELLOW** | Real NSE/Yahoo with honest failover and freshness gates; but bhavcopy unadjusted, equities lag index pre-refresh, refresh wiring brand-new (first due tonight), provenance column dropped on rewrite |
| Strategy | **YELLOW** | Deterministic, well-tested, fully journaled — but the volume rule structurally prevents morning entries and the backtester diverges; as-is, a one-month run likely produces ~zero trades |
| Execution | **GREEN** | Honest fill model (adverse sequencing, spread/slippage, circuit bands, expiry), exact costs, property-tested; minor caveat: never exercised by a real order yet |
| Persistence | **GREEN** (schema/engine) / **RED** (replay completeness) | Atomic intent→order chains, migrations+audits, recovery proven — but the setup-stage "why" is unpersisted, which is the experiment's central question |
| Observability | **YELLOW** | Funnel/incident/activity/alert coverage good; skewed watchdog timestamps + incidents_24h wrong until restart; DEBUG yahoo residual |
| UI | **GREEN** | Functional dossier/board/compare/create on real data; aesthetic implemented; minor QA-selector nit |
| Session isolation | **GREEN** | Structurally enforced + test-proven; matches prior diagnostic |
| Recovery | **GREEN** | Boot recovery, ledger rebuild, zombie respawn, crash-integrity all test-proven; lacks supervision around the process itself |

## 19. Recommended Next Steps (prioritized — NOT implemented)

1. **Restart the server in a safe window** (after 15:30 close or before next open) to load the 12:44–12:51 fixes: converges funnel timestamps, incidents_24h, and commit-deferral; also lets tonight's 16:30 daily-refresh wiring take effect. Verify post-restart: watchdog funnels aware-UTC, health strip counts real incidents.
2. **Persist per-symbol setup-stage rejections** (populate top_rejections_json or a candidates table) — prerequisite for answering "why no trade" and for any future ML dataset.
3. **Human decision on H1 volume reformulation** (§20 Q1) before the one-month experiment; otherwise expect another zero-trade month.
4. Initialize git and commit current state (history currently lives only in dated markdown).
5. Add supervision (launchd KeepAlive plist) + graceful shutdown hook.
6. Small fixes: scan_funnels delete cascade; WARNING-level yahoo per-symbol failures; impersonating session in history bootstrap/update; preserve `source` column; unify archive/restore + raw-SQL incident insert through repos.
7. Decide fate of archived lineup-/qa- sessions (keep for provenance vs purge).
8. Later/optional: real sector map + correlation inputs; informative deterministic ranking; INITIALIZING state; Upstox poller spike behind the existing interface; wire network tests + Playwright into CI.

## 20. Questions Requiring Human Decisions

1. **H1 volume rule reformulation** (the big one): options include time-of-day pacing (expected-volume-by-clock, e.g. cumulative ÷ typical fraction-of-day profile ≥1.5), comparing against prior-day same-time cumulative, switching to relative-volume surge on the breakout bar itself, or moving the entry window later. Note the backtester must be changed in lockstep or results will keep lying to us.
2. **Restart timing** for the stale process: immediately (mid-session, brief gap; recovery-on-boot is proven) vs after close today.
3. **Session lineup policy**: keep `hybrid-main/det-only/random-k` as the official long-running trio? The UI "recommended lineup" button recreates `lineup-*` copies on click — keep, relabel, or remove?
4. **random-k's role** in the experiment set (ablation baseline vs noise).
5. **Universe variation across sessions** (NIFTY50 vs NIFTY200 mixes) and whether different parameter sets should be staged now or after the first trade-bearing month.
6. **INITIALIZING vs STALE** boot semantics — accept the current grace-by-alert-threshold compromise or add the explicit state?
7. **Upstox analytics-token feed** — pursue as a third poller alongside NSE/Yahoo, and do you want me to prepare (not implement) an interface assessment?
8. **CI policy**: include network-marked tests and automated Playwright runs, or keep the lab local-first?
9. **Capital plan** for the month-long run (session sizes, micro/small tier mix, max concurrent sessions ≤10).

---

### Verification notes

Personally re-verified during cross-checking (not merely inherited from subagents): process identity/boot time; port binding; migration application timestamps; scan_funnels/watchdog timestamp skew with paired-event microseconds; correct UTC on bar-close funnels; incidents_24h fixed code on disk; maybe_refresh_daily wiring; feed-status boot-grace branch; WATCHDOG_STALE_BAR_AFTER_S=300 and liveness floors; `utc_iso` IST-assuming conversion; transaction depth-counter deferral; live.py:173 DEBUG residual; funnel payload shape (aggregate-only); zero rows in intents/orders/fills/positions; flat ₹25,000 equity; session roster/configs; 281-test suite result; fix-wave mtimes (12:44–12:51 IST) vs boot (09:49 IST).
