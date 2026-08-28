# Autonomous Swing-Trading Research Platform — Architecture & Research Design Package

**Version:** 1.0 · **Date:** 2026-08-23 · **Status:** Design (pre-implementation)

**Guiding principle:** *Build a scientifically defensible live trading experiment first, and a trading bot second.*

**Legend used throughout:** `Known` = verified from primary/near-primary sources cited · `Assumption` = reasonable inference, must hold · `Needs verification` = confirm with broker/exchange/SEBI before relying on it.

> This document is research + design only. It is **not legal advice**, and regulatory items marked below must be confirmed with the broker/exchange before any live order is placed.

---

## 1. Project Interpretation

### 1.1 Restatement

We are building an **autonomous systematic swing-trading research platform** targeting Indian equities (NSE). The user supplies only: initial capital (e.g., ₹25,000), market/universe, mode (`PAPER` first), risk limits, and experiment duration (~1 month). Everything else — universe filtering, scanning, candidate generation, ML ranking, position sizing, entries, stops, exits, monitoring, capital management, decision logging, performance measurement, failure recovery — is performed by the system without manual intervention.

The core research question: **Can an autonomous hybrid deterministic + ML swing-trading system select and manage profitable trades in live market conditions over an extended period?**

### 1.2 What this is NOT

| Not | Why |
|---|---|
| Manual swing trading | No human picks stocks, sizes positions, or places orders. Human role = configuration, monitoring, kill switch. |
| A stock screener | Screeners produce lists; this system executes, manages risk, monitors open positions end-to-end, and is accountable for outcomes. |
| High-frequency trading | Holding period = hours to weeks; order rate ≤ a handful per day; latency budget measured in seconds, not microseconds. |
| Day trading | Positions are held overnight by default (swing horizon); no intraday square-off mandate in our design (cash-equity delivery-style simulation). |
| Purely ML trading | ML only ranks/scores candidates produced by deterministic strategy logic. It cannot override hard risk constraints. |
| Purely rule-based system | Deterministic rules generate candidates; ML provides probabilistic ranking/regime awareness on top. |
| Backtesting-only | The deliverable is a continuously running live-data paper experiment with full audit trails; backtests are tooling inside Phase 3, not the product. |

---

## 2. Research Findings

### 2.1 Regulatory environment (researched Aug 2026)

Primary sources consulted:

- SEBI circular `SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013` (Feb 4, 2025) — "Safer participation of retail investors in Algorithmic Trading" ([sebi.gov.in](https://www.sebi.gov.in/legal/circulars/feb-2025/safer-participation-of-retail-investors-in-algorithmic-trading_91614.html))
- NSE circular `NSE/INVG/67858` (May 5, 2025) — implementation standards
- NSE circular `NSE/INVG/69255` (Jul 22, 2025) — operational modalities
- NSE FAQ "Safer participation of retail investors in Algo Trading" (Nov 3, 2025) — [nsearchives.nseindia.com](https://nsearchives.nseindia.com/web/sites/default/files/inline-files/FAQ_Retail%20Algo_03112025_NSE.pdf)
- BSE equivalent notice 20251829-12 (Aug 29, 2025) / BSE draft FAQ (Nov 3, 2025)
- Broker API documentation: [myapi.fyers.in/docsv3](https://myapi.fyers.in/docsv3), Kite Connect docs, DhanHQ docs

Key findings relevant to this project:

| Item | Finding | Status |
|---|---|---|
| Retail algo framework | SEBI's Feb 2025 framework is implemented by NSE/BSE since ~Aug–Nov 2025. All client orders received **via API are treated as algo orders** and require exchange tagging. | Known |
| Order tagging | Standardised tagging for self-directed ("tech-savvy") retail clients: first 12 digits `444444444444`, 13th digit `0`/`2`/`4`. Brokers handle tag insertion via their API layer. | Known (verify exact mechanics per broker) |
| OPS threshold | Orders-per-second threshold of **10 OPS** per client per exchange. Below/at threshold: standardised tagging applies without individual algo registration. Above: registration through broker required. Our system will be far below 10 OPS (a few orders/day). | Known |
| Static IP | Mandatory for "tech-savvy" clients using direct APIs from their own infrastructure (NSE/INVG/67858). Broker-hosted algos use the Trading Member's static IP instead. | Known (verify whether our broker requires whitelisting at activation) |
| Order types | Per NSE circular NSE/MSD/67753 (Apr 29, 2025): **Market orders not permitted for algo orders** (equity); IOC restrictions in some segments. → Our execution layer must use **LIMIT and SL-LIMIT style orders** primarily. This is a hard constraint on execution design. | Known (verify current scope of restriction with broker) |
| Mock trading sessions | Individual tech-savvy clients running self-hosted algos are exempt from mandatory monthly mock sessions (brokers/vendors are not). | Known |
| Empanelment | Applies to third-party algo vendors selling to others. A personal, non-commercial, single-user system does **not** require vendor empanelment. | Assumption (verify with broker) |
| Paper trading | No SEBI/NSE regulatory burden for simulated execution on locally generated fills using licensed live data. Sandbox environments (e.g., Dhan sandbox) exist at broker discretion. | Known |

**What changes paper → live:**
1. Broker account + API subscription needed; credentials custody becomes security-critical.
2. Static IP / IP whitelisting may need to be arranged.
3. Every order becomes a tagged algo order under broker RMS controls (price/qty/value/margin checks).
4. Real fills, partial fills, rejections, margin shortfalls, circuit-limit halts become real.
5. Kill-switch responsibility shifts entirely to us; no simulator to absorb mistakes.

**Regulatory/Infrastructure Decision**

- **Development:** local Mac, own code, historical data from broker API or free sources. No regulatory interaction.
- **Live-data paper trading:** broker WebSocket/REST data feed + internal PaperBroker. Zero regulatory exposure; still respect broker data-subscription limits. `MODE=PAPER` is the hard-coded default.
- **Broker-connected sandbox (if available):** use where offered (e.g., Dhan sandbox) to validate adapter correctness against real API semantics without real money.
- **Eventual small-scale live:** single-broker direct API as a tech-savvy client; expect static-IP requirement, algo tagging handled by broker, LIMIT-order-only execution, explicit written confirmation from the broker covering all points marked *Needs verification* above.

---

### 2.2 Broker/API landscape (as of 2026)

Synthesised from official portals and current comparison research ([multibagg comparison](https://www.multibagg.ai/market-pulse/articles/dhan-api-alternatives-indian-brokers-cmpgarbdyhotqp40j24hrogv2), [optionx broker compare](https://optionx.trade/brokers/compare-3/zerodha-vs-dhan-vs-fyers), [Kite pricing note](https://jayadevrana.com/zerodha-kite-connect-api-pricing-rate-limits-2026)):

| Broker API | Cost (reported) | Rate limits (reported) | Data | Notes |
|---|---|---|---|---|
| **Fyers API v3** | Free | ~10 orders/s | Free REST+WebSocket; minute-level history ~1–2 yrs | Python SDK `fyers-apiv3`; good historical depth; symbol cap on WS subscriptions |
| Zerodha Kite Connect | ₹500–2,000/mo (sources conflict) | ~3 orders/s | Paid add-ons historically; recent reports say WS+historical bundled | Most mature ecosystem, best docs |
| DhanHQ v2 | Trading API free; data ~₹499/mo | ~25 orders/s | WebSocket + OHLC history | Modern docs; has sandbox |
| Angel One SmartAPI | Free | ~10 orders/s | Free | Some volatility-period reliability complaints |
| Upstox v2 | Free/paid tiers reported inconsistently | ~25 orders/s | WebSocket | Detailed but mixed reliability reports |

**Decision:** Primary integration target = **Fyers API v3** (free API + free real-time data + deep minute history fits a zero-budget research experiment). Abstract behind `Broker` interface so DhanHQ (sandbox-friendly) or Kite Connect can be added later. All cost/rate figures are `Needs verification` against live portal pages at implementation time — pricing has churned repeatedly.

### 2.3 Latency analysis

Expected holding period: hours to weeks. Decision cadence: candle-close driven (5-min bars primary; daily bars for regime). Order flow: <20 orders/day. Slippage sensitivity: for a multi-day trade, a 1–5 second execution delay moves price by roughly one spread tick in liquid large-caps — noise relative to typical stop distances (0.8–2 × ATR ≈ several %).

| Option | Verdict |
|---|---|
| A: REST polling only | Acceptable fallback; wasteful and laggy for 200-symbol universe at 5-min cadence; fine for reconciliation. |
| **B/C: WebSocket streaming (+ periodic REST reconciliation)** | **Recommended (Option C).** WS gives LTP/quote updates for position monitoring; REST every N minutes reconciles truth (orders/fills/account) because WS drops happen and WS is not authoritative. |
| D: Dedicated low-latency infra | Rejected. No edge exists at seconds-scale decision granularity; cost/complexity unjustifiable. |

Quantified: with 5-minute candles, worst-case reaction time to a stop breach ≈ candle interval (300 s). Reducing that to 50 ms improves exit price by ≈ 0–0.05% in liquid names while multiplying operational complexity ~100×. Meanwhile, a *correct* fill model, honest slippage accounting, and reliable state recovery affect results by whole percentage points. **Correctness ≫ latency.**

---

## 3. Recommended Overall Architecture

**Modular monolith**, single Python process (asyncio event loop), cleanly layered modules with hard dependency rules:

```
                ┌────────────────────────────────────────────────┐
                │                 CONFIG (versioned)             │
                └────────────────────────────────────────────────┘
 MarketData ──► Store ◄── HistoricalLoader                          │
 (WS+REST)        │                                                 │
                  ▼                                                 ▼
           Universe ──► Strategy(deterministic) ──► Candidates     │
                                                        │          │
                                                        ▼          │
                                              ML Ranker (scores    │
                                              only, no overrides)  │
                                                        │          │
                                                        ▼          ▼
                                          Portfolio Selector ──► Risk Engine (hard vetoes)
                                                                        │
                                                                        ▼
                                                          ExecutionEngine ──► Broker interface
                                                                        │        ├─ PaperBroker (default)
                                                                        ▼        └─ LiveBroker (gated)
                                                          State/Journal (SQLite)
                                                                        │
                                              ┌────────────────────────┼──────────────┐
                                              ▼                        ▼              ▼
                                         Scheduler/Supervisor   Observability    Dashboard(FastAPI)
```

Why each layer exists:
- **Universe**: cheap pre-filter; keeps compute and data quality tractable and prevents trading illiquid junk.
- **Deterministic strategy**: reproducible hypothesis generator; defines *what counts as a setup*. Auditable, testable, versioned.
- **ML ranker**: converts "many setups" into "best few" using learned probabilities; isolated so it can be ablated (deterministic-only vs ML-enhanced comparisons).
- **Risk engine**: single choke point enforcing non-negotiable constraints; ML output is advisory here, never authoritative.
- **Portfolio selector**: avoids correlated over-concentration (five banks ≠ five independent trades).
- **Execution engine**: owns order lifecycle, retries, idempotency; strategy never touches broker APIs.
- **Paper/Live broker adapters**: identical interface; mode is invisible to strategy.
- **Journal/state**: every decision and transition persisted → post-hoc explainability, crash recovery, reproducibility.

---

## 4. Technology Stack

| Component | Options considered | Recommendation | Reason |
|---|---|---|---|
| Language | Python, TypeScript, Rust, C++ | **Python 3.12** | Dominant quant/ML ecosystem (pandas/polars/sklearn/LightGBM), official broker SDKs (fyers-apiv3 etc.), speed irrelevant at our latency budget; Rust/C++ buy nothing here |
| Dataframes | pandas vs Polars vs NumPy | **Polars** for batch/research pipelines, pandas-compatible shims where SDKs demand | Faster, lazy evaluation, less foot-gun mutation; NumPy underneath everywhere |
| Indicators | pandas-ta / TA-Lib / hand-rolled | **Hand-rolled minimal indicator library** (SMA, RSI, ATR, ROC, vol) with unit tests | Small surface, no binary TA-Lib dependency pain on macOS, fully auditable formulas |
| ML | sklearn, XGBoost/LightGBM, PyTorch | **scikit-learn baseline (logreg) + LightGBM** | Tabular ranking problem; gradient boosting SOTA-ish here; PyTorch unjustified at this sample size |
| Runtime DB | SQLite, Postgres, TimescaleDB | **SQLite (WAL)** for runtime journal/state | Single-process writer, zero ops on Mac, ACID, trivially backed up by file copy; Postgres is a later swap if multi-process ever emerges |
| Research store | Parquet, DuckDB, CSV | **Parquet files + DuckDB** for candles/features/backtests | Columnar analytics, free, fast, reproducible snapshots per data version |
| Cache | Redis vs in-proc dict | **In-process dict** (V1) | One process; Redis adds ops burden for nothing yet |
| App/backend | FastAPI vs Flask | **FastAPI** | Async-native (fits asyncio loop), serves dashboard + health endpoints, pydantic config validation |
| Dashboard | React/Next.js vs server-rendered HTMX vs Streamlit | **FastAPI + Jinja2 + HTMX** (server-rendered, auto-refresh) | Monitoring-only read UI; zero build pipeline; React is overkill |
| Scheduler | cron, APScheduler, celery, asyncio loops | **asyncio tasks + APScheduler for cron-like jobs** | Single-process coherence; celery/kafka are HFT-grade ceremony we don't need |
| Deployment V1 | Mac vs VPS vs cloud | **Local Mac + caffeinate + launchd supervision** for paper phase; **cheap VPS with static IP** for eventual live | See §16 |
| Packaging | pip, poetry, uv, Docker | **uv + venv; Docker optional later** | Simple; Docker adds value only when moving to VPS/live |
| Config | YAML/env vs DB | **YAML + env vars, content-hashed & snapshotted into DB per run** | Versioned, diffable, reproducible |

---

## 5. Market Universe

| Option | Symbols | Liquidity | Opportunities/day | Data load | Risk |
|---|---|---|---|---|---|
| NIFTY 50 | 50 | Excellent | Low | Trivial | Too few candidates; selection already done by index committee |
| NIFTY 100 | 100 | Very high | Low–med | Small | Still narrow |
| **NIFTY 200** | **200** | High | **Medium** | **Manageable** | **Recommended** |
| NIFTY 500 | 500 | Mixed | Higher | Heavy | Mid/small-cap slippage & data-quality issues dominate at ₹25k scale |
| All NSE | ~2000 | Poor tail | Many but noisy | Very heavy | Illiquidity, circuit limits, manipulation-prone small caps |

**Decision: NIFTY 200 constituents** (reconstituted list pulled quarterly from NSE indices), further filtered daily by eligibility rules:

- Median daily traded value (20d) ≥ ₹10 crore
- Close ≥ ₹50 (avoid penny-tick distortion)
- ≥ 60 trading days of continuous history (indicator warm-up)
- Not in ban-on-trading / halt / T-group-suspicion states (`Needs verification`: NSE surveillance group metadata availability)
- Spread proxy `(high-low)/close` 20d median ≤ 4%

Rationale: large enough to demonstrate autonomous selection (~200 → typically 5–30 candidates/day expected), liquid enough that ₹25,000 capital never moves any market and slippage modelling stays credible. NIFTY 500 is a documented V2 extension once the pipeline is proven.

Index membership changes introduce mild survivorship handling: we snapshot the constituent list with dates and evaluate point-in-time membership where historical backtests reach back across reconstitutions (`Assumption`; full point-in-time index histories are a known hard problem — flagged, partially mitigated).

---

## 6. Data Architecture

### 6.1 Requirements

| Class | Contents | Source | Cadence |
|---|---|---|---|
| Historical | Daily OHLCV ~5y; minute OHLCV ~1–2y for research | Fyers history API (and/or yfinance cross-check) | Bulk load + nightly append |
| Live | LTP/quotes via WebSocket; 5-min bars built internally; EOD bars via REST close-out | Fyers WS v3 + REST | Streaming + 5-min bar closes |
| Reference | Symbol master, instrument tokens, lot size, sector classification, NIFTY 200 membership w/ dates, market calendar (holidays/half-days), corporate actions (splits/dividends/bonus) | NSE archives + broker master + curated files | Weekly refresh + event-driven checks |
| Regime inputs | NIFTY 50 index OHLCV, India VIX | Fyers/NSE | Daily + intraday |

Corporate actions matter: unadjusted splits will fabricate false breakouts/crashes and poison labels. We adjust historical OHLCV using corporate-action records and store both raw and adjusted series.

### 6.2 Storage layout

```
data/
  parquet/
    candles_1d/{symbol}.parquet      # adjusted + raw columns
    candles_5m/date=YYYY-MM-DD.parquet  # partitioned by session date
    index/nifty50_1d.parquet, indiavix_1d.parquet
    ref/symbols.parquet, nifty200_membership.parquet,
        corp_actions.parquet, market_calendar.parquet
  sqlite/
    journal.db                        # runtime: decisions, orders, positions, account_state, metrics
```

Workload reasoning: research queries are analytical/columnar → Parquet+DuckDB excels. Runtime workload is tiny (a few thousand rows/day) transactional writes → SQLite WAL is ideal and needs no server. Redis/TimescaleDB/Kafka rejected as premature.

### 6.3 Freshness & integrity rules

- Heartbeat per feed; `last_tick_age > 120s` during market hours → degrade to REST quotes; > 10 min → **fail-closed** (no new entries; exits may still fire off last-known-good data with widened slippage assumption).
- Every stored bar carries source, ingest timestamp, gap-flag. Gap detector reconciles expected bar counts per session against the market calendar.
- Nightly job: fetch official EOD candles, overwrite intraday-built dailies, log discrepancies.

---

## 7. Deterministic Strategy — V1 Specification

**Label: V1 RESEARCH PARAMETERS — chosen from literature/principle, NOT optimized.** Every parameter lives in versioned config and is subject to the robustness methodology in §8.

**Setup archetype (long-only V1): trend continuation on pullback in liquid mid/large caps.**

Market regime gate (all must hold for *new entries*):
- NIFTY 50 close > SMA50(NIFTY 50) AND SMA20(NIFTY 50) > SMA50(NIFTY 50)
- India VIX < 22 and VIX 5-day change < +15% (vol-spike brake)
- If regime fails: no new entries; existing positions managed normally.

Per-symbol entry conditions (evaluated on 5-min bars within 09:30–14:30 IST window; daily-bar confirmation uses previous completed session):

1. **Trend:** Close > SMA50 AND SMA20 > SMA50 AND SMA50 slope over 10 days > 0
2. **Pullback structure:** Within last 5 sessions, price touched or dipped ≤ 1.0×ATR(14) below SMA20 (pullback toward rising mean), then reclaimed SMA20
3. **Momentum sanity:** RSI(14, daily) between 45 and 70 (excludes exhausted and falling knives)
4. **Volume confirmation:** Breakout/pullback-resume day volume ≥ 1.5× SMA20(volume)
5. **Structure trigger:** Intraday break above prior day's high OR consolidation-range top (20-session Donchian upper) with ≥ 30 min left in session

Initial risk parameters per candidate:
- Stop-loss = entry − 1.5 × ATR(14,daily) (hard, exchange-side where possible; always enforced internally)
- Target 1 (half exit) = entry + 1.5 × risk distance (R multiple = 1.0); Target 2 (rest) = entry + 3.0 × risk distance; trailing stop after T1 = highest-high − 1.5×ATR
- Time stop: exit at close of day 10 if neither target nor stop hit
- Minimum reward:risk at signal time ≥ 1.5 (distance to next resistance ≥ 1.5 × risk)

Parameter provenance (not optimization): SMA 20/50 and RSI 14/45–70 are standard trend-following conventions (Wilder; classic trend literature); ATR 14 is Wilder's original; 1.5×ATR stops sit inside the widely-used 1–3×ATR band giving ~breakeven-plus win-rate expectations; 10-day time stop matches the swing horizon definition; volume 1.5× is a common unusual-volume heuristic. These form a **robust region to be tested for plateau behavior** (§8), not claimed optima.

Reproducibility: given the same candles and config hash, candidate generation is a pure function → bit-identical outputs. Unit-tested golden fixtures enforce this.

---

## 8. Hyperparameter Methodology

Principles: parameters define **regions**, not points. A parameter is acceptable only if performance degrades smoothly around it.

Process for each tunable (e.g., ATR-stop multiplier m ∈ {1.0…2.5}, RSI band, vol multiple):

1. **Coarse grid** (deliberately small: ≤ 5 values per param, ≤ 2 params varied jointly per study — bounded multiple-testing).
2. Evaluate each combo on **walk-forward segments** (§11): report mean and std of OOS-segment Sharpe/expectancy.
3. Accept a parameter value only if a **neighborhood** (±1 grid step on each side) also performs acceptably (≥ 60% of peak OOS metric). If `RSI=52` works but 48 and 56 fail → reject; if 48–58 all work → robust plateau, choose center.
4. Final selected config frozen **before** the live paper month; the paper month is itself out-of-sample.
5. Defenses: purged/embargoed walk-forward splits (no train/test temporal adjacency), features computed strictly from data available at decision time, costs+slippage always included, and a **deflated performance view**: with N configs tested, report the expected max-Sharpe-under-null (multiple-testing haircut, à la Bailey/López de Prado) alongside headline numbers.
6. Explicit anti-patterns banned in CI/review: random train/test splits, optimizing on test fold, selecting peak-return combos, survivorship-ignorant universes, look-ahead indicators (any feature using bar t must only use bars ≤ t−1 for decisions executed at t's close... precisely: decisions at time t use data through t inclusive, executed at next actionable price).

---

## 9. ML Architecture

**Role:** score & rank deterministic candidates; estimate P(favorable outcome); optional regime classifier. Never bypasses risk engine.

**Target formulation (chosen): triple-barrier classification.**
Label per candidate: did price hit `+1.5×risk` before `−1.5×risk` within 10 sessions? (Mirrors actual trade geometry — far better aligned than raw next-N-day return regression.) Secondary head: expected R-multiple regression for score blending. Ranking = calibrated probability × expected R / risk.

**Model progression:**
1. Logistic regression (interpretable baseline, calibration natural)
2. LightGBM binary classifier (monotone constraints where domain demands, e.g., ↑score with ↑volume confirmation)
3. Neural/temporal models: **only if** feature-importance analysis shows structured sequential info unused by GBDT *and* sample size justifies it. Default answer: no.

**Features (all strictly causal, computed from ≤ decision-time data):** trend (dist to SMA20/50 normalized by ATR, slope), momentum (RSI, 5/10/20d returns, relative strength vs NIFTY), volume (rel-volume, OBV slope), volatility (ATR%, realized vol percentile), structure (dist to 20d high/low, days since 20d high), sector relative strength, regime (NIFTY above/below SMA50, VIX level & change). ~25–35 features; no raw prices.

**Training discipline:**
- Purged K-fold walk-forward with 5-day embargo; train window 24 months rolling, validate 3 months forward, step quarterly
- Class imbalance: label prevalence ~40–55% expected; handle via `scale_pos_weight`, verify calibration (isotonic) — probabilities drive sizing thresholds so calibration is mandatory (report Brier/ECE)
- Retraining: monthly, versioned (`model_id` = hash(code+config+data range)); live month uses ONE frozen model — no mid-experiment retraining
- Leakage guards: features built only from bars ≤ t; label window excluded from training via purge+embargo; group-by (symbol,date) so same-day correlated samples don't straddle folds; unit test asserting no feature column correlates > 0.98 with future return (sanity tripwire)

**Deployment gating:** model ships only if OOS AUC ≥ 0.53 *and* calibrated top-quintile hit-rate beats bottom quintile by ≥ 5pp across ≥ 3 consecutive walk-forward segments. Otherwise system runs deterministic-only (ablation by design).

---

## 10. Portfolio Selection Logic

Funnel: 200 eligible → ~10–40 deterministic candidates → ML-scored → portfolio selection.

Selection procedure (greedy constrained):
1. Filter candidates with calibrated P(win) < threshold (config, e.g., 0.52) unless deterministic-ablation mode
2. Sort by score = P(win) × avg_R − costs_in_R
3. Greedily admit if ALL pass:
   - `open_positions < MAX_POSITIONS (default 4)`
   - `sum(risk_amount) ≤ 2% × equity` (total open risk)
   - `position_value ≤ 20% × equity`
   - pairwise correlation of candidate's 60d daily returns vs each open position ≤ 0.7
   - sector exposure: ≤ 2 positions per sector, ≤ 40% equity in one sector
4. Else record rejection reason (machine-readable) and continue.

Five banks signaling together → correlation + sector caps admit at most two. Principled basis: with a ₹25k book, idiosyncratic single-name risk dominates; correlation capping is the cheapest variance reduction available. Diversification benefit is empirically checkable in the experiment via portfolio-vol vs average-position-vol.

---

## 11. Risk Management Engine

Hard constraints (enforced in code as pre-order veto; ML cannot touch these):

| Constraint | Default | Formula/Rule |
|---|---|---|
| Risk per trade | 1% equity | `qty = floor(0.01 × equity / (entry − stop))`; skip if qty×price < min notional ₹5,000 |
| Max positions | 4 | count check |
| Max total open risk | 2% equity | Σ per-trade risks |
| Max position notional | 20% equity | qty×price ≤ 0.2×equity |
| Max gross exposure | 80% equity | Σ notional |
| Daily loss limit | −3% equity day | new entries blocked for rest of day |
| Drawdown kill switch | −10% from high-water mark | flatten all, halt, alert, require manual restart flag |
| Data-integrity shutdown | stale/gap/corrupt data | fail closed: no NEW orders; exits allowed with conservative slippage |
| Broker/API anomaly | repeated auth/order failures ≥ 3 | halt new orders, alert |
| Order-size sanity | qty ≤ 0.5% of 20d ADV | prevents accidental illiquid prints |

State machine always materializes: `cash, invested, unrealized_pnl, realized_pnl, buying_power, positions[{sym,qty,avg,stop,target,trail,held_days}], portfolio_risk`. Persisted after every transition (write-ahead journal) → crash-safe.

Exits hierarchy (checked every 5-min bar close + on WS ticks for stop proximity): hard stop (exchange SL-L where supported) → trailing stop → target ladder → time stop → regime emergency exit (NIFTY closes < SMA50 by >2% → tighten all stops to highest-low−1×ATR).

---

## 12. Execution Architecture

```
Strategy → TradeIntent{symbol, side, order_type, qty, limit_px, stop, targets, meta} 
        → RiskEngine.validate(intent, portfolio_state) → APPROVED/REJECTED(reason)
        → OrderManager (idempotency key = intent_hash; dedupes resubmits; tracks lifecycle)
        → BrokerAdapter (FyersAdapter | PaperBroker | SandboxAdapter)
```

Order-type policy given NSE algo rules (market orders disallowed for tagged algo orders — see §2.1): entries = **LIMIT at ask ± small buffer**, expires/refreshed each 5-min bar; stops = **SL-LIMIT** (trigger + limit offset 0.3%); exits = LIMIT. Where the broker supports server-side GTT/SL leg attachment, prefer exchange-side stops so protection survives our process crash (`Needs verification` per broker).

Failure matrix handled in OrderManager: timeout→query-before-retry (never blind resend); partial fill→hold remainder as working order, reconcile on poll; rejection→log+alert+mark intent dead; WS drop→REST reconciliation loop; state mismatch (broker says filled, we say open)→broker wins + incident log; market closure→queue to next session open; circuit limit hit→cancel, blacklist symbol for the day; insufficient funds→reject at risk layer (pre-check buying power).

---

## 13. Paper-Trading Architecture (live data, simulated fills)

PaperBroker implements `Broker` exactly, but fills are **modelled**:

- Fill price for BUY LIMIT @ L: fills only if traded price ≤ L that bar; fill price = L + half-spread penalty (spread estimated from 20d median (H−L)/C, floored at 0.05%)
- Adverse slippage: +0.05% liquid (top-100 ADV), +0.10% otherwise; scaled up ×2 when bar volume < 0.5× average
- Latency sim: order submitted at t executes at next bar's open (never same-tick LTP) — kills magical same-price fills
- Costs applied per side, from published schedule (delivery equity): brokerage ₹0 (discount broker), STT 0.1% sell, exchange txn ~0.00297%, GST 18% on txn charges, SEBI ₹10/crore, stamp 0.02% buy, DP ₹12–15/sell (`Needs verification` — rates change periodically; loaded from a versioned `costs.yaml`)
- Rejections simulated: qty > circuit/limit bands → reject

This makes the paper month a conservative lower bound on live performance, which is the correct bias for a go/no-go experiment.

## 14. Live Architecture Delta

Changes only behind the `Broker` interface: real auth/token refresh (daily token expiry — automate TOTP login flow carefully, `Needs verification` per broker), real order lifecycle, real partial fills, real margin/buying power, static IP provisioning, algo-tagging handled by broker API layer, and a physical/emergency kill procedure. Plus: MODE=LIVE requires explicit env override + typed confirmation string + separate secrets profile; a `LIVE_ARMED=false` interlock defaults everything off. Strategy code is unchanged — that is the point of the abstraction and it will be validated in Phase 7/8 parity tests (same intents replayed through Paper vs Sandbox adapters must produce equivalent state transitions).

---

## 15. Database / Data Model

Runtime journal (SQLite):

```
experiments(id, name, config_yaml, config_hash, started_at, ended_at, status)
positions(id, exp_id, symbol, qty, avg_entry, stop, target2, trail_px,
          opened_at, closed_at, status, strategy_version, ml_model_version, param_version)
intents(id, exp_id, ts, symbol, features_json, signals_json, ml_score, ml_prob,
        risk_assessment_json, decision, rejection_reason, versions_json)
orders(id, exp_id, intent_id, broker_order_id, side, type, qty, limit_px,
       status, filled_qty, avg_fill_px, submitted_at, updated_at, idempotency_key UNIQUE)
fills(id, order_id, ts, px, qty, cost_breakdown_json)
account_snapshots(id, exp_id, ts, cash, invested, unrealized, realized, equity, hwm)
candles_meta(symbol, timeframe, first_ts, last_ts, row_count, source, adjusted_version)
metrics_timeseries(exp_id, ts, metric, value)
incidents(id, ts, severity, kind, detail_json, resolved_at)
```

Research store: Parquet (immutable, content-addressed by ingestion manifest) + DuckDB views. Every experiment row pins: config hash, universe snapshot id, data manifest id, strategy/model/param versions → full reproducibility.

---

## 16. Deployment & Uptime

**Paper phase (V1): local Mac.** Justification: zero cost, data + dashboard local, failure modes are exactly what we want to observe and engineer for, and a missed evening due to sleep is a *recorded incident*, not lost money. Mitigations:
- `caffeinate -dis -w <pid>` wrapper (prevents display/idle/disk sleep) — sufficient for paper phase
- launchd `KeepAlive` plist → auto-restart on crash; process must be idempotent-on-boot (recover-from-journal)
- On boot: reconcile state = (local journal) ⊕ (broker/paper account query); mismatches → incident + fail-closed until human acknowledges
- UPS/power-loss: Mac restarts, launchd relaunches, recovery path exercised deliberately in Phase 7 chaos tests

**Live phase: cheap VPS (static IP)** — chosen because (a) NSE static-IP requirement for direct-API clients aligns naturally with a fixed-IP VM, (b) 9:15–15:30 IST sessions survive laptop lid-closes, (c) ~₹500–800/month is proportionate only once real money is at stake. Cloud-not-automatic: for paper, Mac is strictly better value; for live, VPS is justified by compliance + uptime, not hype.

Security: `.env` (gitignored) + macOS Keychain or `direnv`; keys never in repo (pre-commit secret scan); dashboard bound to localhost + token auth in VPS case; least-privilege broker API scopes; rotation on any suspicion; MODE=PAPER hardcoded default, MODE=LIVE gated by env + interactive confirmation phrase.

---

## 17. Observability

Every intent row records timestamp, symbol, price, full feature vector, signals, ML score/prob, risk verdict, decision, rejection reason; every order/fill/exit carries reason codes and P&L attribution. "Why did you buy RELIANCE?" = one SQL query returning the complete JSON decision record.

- **Logs:** structured JSON, per-module levels, shipped to `logs/` (rotated)
- **Metrics:** heartbeat age, WS reconnect count, bar-gap count, order latency, fill-vs-model slippage, intent→fill conversion, per-layer rejection counters
- **Alerts:** Telegram bot (kill switch, drawdown, data-stale, repeated API errors) — push, not pull, for a solo operator
- **Health:** `/healthz` (process), `/readyz` (state reconciled), freshness gauges on dashboard
- **Daily digest:** auto-generated morning/evening summary of decisions, positions, anomalies

Dashboard sections (read-only): portfolio strip (equity, cash, invested, uPnL, rPnL, return, HWM, DD); positions table (sym, entry, last, stop, target, qty, P&L, days held, trailing state); funnel today (eligible→candidates→ML-pass→selected→rejected-with-reason); experiment stats (#trades, win rate, avg win/loss, profit factor, expectancy, Sharpe-lite, max DD, exposure, turnover, cumulative costs & slippage vs modeled); system health panel (feeds, broker conn, last update ages, DB ok, supervisor uptime); benchmark overlay vs NIFTY 50 buy&hold.

---

## 18. Experiment Protocol (one month, live data, paper)

Fixed in advance and committed to `experiments.config_yaml` before start:

- Capital ₹25,000 · Mode PAPER · Universe NIFTY 200 (snapshot dated) · Strategy `v1.0.0` · Model `<frozen model_id>` · Params `p1.0.0` · Costs `c1.0.0`
- Sessions: all NSE trading days in window; scan every 5-min bar close 09:30–14:45 IST; entries only 09:45–14:30; exits evaluated every bar + tick-adjacent stop checks; force-flat none (swing holds overnight); max 4 positions; risk rules §11
- End of experiment: freeze journal, generate report automatically

Report metrics: total & net return (after all modeled costs), max drawdown, Sharpe (daily, rf=0), Sortino, win rate, expectancy (₹ and R), profit factor, avg holding days, turnover, cost drag, realized-vs-modeled slippage gap, #operational incidents, benchmark delta vs NIFTY 50 & vs NIFTY 200.

Baselines run in parallel on identical data (paper accounts too):
1. NIFTY 50 buy-and-hold
2. NIFTY 200 buy-and-hold
3. Deterministic-only (no ML filter)
4. Full hybrid (primary)
5. Random-selection baseline (pick k random candidates/day, same risk rules; quantifies selection alpha vs noise)

Success criteria (defined BEFORE starting):
- Operational: zero unresolved incidents > 24h; ≥ 90% of sessions fully covered; state recovery demonstrated ≥ once
- Statistical honesty: ≥ 20 closed trades else declare "insufficient evidence" regardless of P&L
- Performance: net positive expectancy in R (mean R > 0); max DD ≤ 10%; profit factor ≥ 1.3
- Value-of-complexity: hybrid beats deterministic-only AND random baseline on expectancy (else ML layer is declared non-value-adding for V2 iteration)
- Benchmark: beating NIFTY buy-hold is *reported* but NOT a success requirement in month one (one month ≠ skill evidence; a rising market flatters everything)

Explicit disclaimer baked into report: one profitable month proves the *system operates correctly*, not that the *edge is real*. Confidence interval on expectancy with n≈30 trades is wide enough to include modestly negative true edge.

---

## 19. Path to Real Money (checklist, all must be ✅)

☐ ≥ 60 paper trades across ≥ 2 distinct market regimes (may extend beyond 1 month)
☐ Expectancy confidence interval excludes materially-negative edge
☐ Modeled-vs-realized slippage gap tracked and stable (sandbox micro-live tests help calibrate)
☐ Broker chosen; static IP provisioned; algo tagging flow confirmed in writing; LIMIT-only execution verified
☐ Written broker confirmation on all §2.1 *Needs verification* items
☐ Live risk limits: capital ≤ ₹10,000; risk/trade 0.75%; daily stop −2%; hard monthly stop −8% → auto-flatten+halt
☐ Kill switch tested live (one deliberate manual trigger drill)
☐ Crash-recovery drill passed on the live adapter (kill -9 mid-session, restart, reconcile)
☐ Secrets handling audited; MODE=LIVE interlock tested

---

## 20. Risks

| Risk | Mitigation |
|---|---|
| Market risk | Hard stops, exposure caps, regime gate, kill switch |
| Model risk / overfitting | Walk-forward, plateaus-not-points, deflated metrics, frozen live model, ablation baselines |
| Data risk | Dual-source cross-check, gap detection, EOD reconciliation, fail-closed on staleness |
| Survivorship/selection bias | Dated universe snapshots, point-in-time membership where possible |
| Execution risk | Limit-only orders, idempotent order manager, broker-wins reconciliation, exchange-side stops |
| API/broker risk | Adapter isolation, retry/query-not-resend, incident halts, second broker adapter kept warm |
| Operational risk | launchd supervision, journal-based recovery, chaos drills, alerts |
| Regulatory risk | Paper-first posture; LIMIT-only; sub-OPS-threshold order rates; written broker confirmations before live; not legal advice |
| Cost drag at ₹25k | Delivery-style (no leverage), low turnover (~≤4 new positions/wk), cost model always on |
| One-month illusion | Pre-registered criteria, "insufficient evidence" branch, baselines, no live-money trigger on profit alone |

---

## 21. Open Questions

1. Fyers/Dhan exact current pricing, WS symbol caps, and historical depth — verify on portals at kickoff.
2. Does the chosen broker whitelist a home dynamic IP for development-phase data access (data ≠ trading)? Verify.
3. Availability and fidelity of broker sandbox (order-state realism) — determines Phase 8 test depth.
4. Point-in-time NIFTY 200 membership history accessibility (NSE index factsheets vs licensed vendors).
5. Surveillance-group (T/Z/GSM/ASM) metadata source for eligibility filtering.
6. Exact current scope of "no market orders for algo clients" (equity cash vs derivatives) with the chosen broker.
7. Whether STT/DP/etc. schedule in `costs.yaml` needs updating at experiment start (rates churn).
8. Corporate-actions feed quality from free sources vs paid (adjustment errors directly corrupt labels).

---

## 22. Repository Structure

```
swing-trading/
├── docs/                    # this package, experiment reports
├── configs/
│   ├── base.yaml            # strategy/risk/universe params (versioned content-hash)
│   ├── experiments/exp_001.yaml
│   └── costs.yaml
├── src/sts/
│   ├── market_data/         # ws_client.py, rest_client.py, bar_builder.py, freshness.py
│   ├── universe/            # membership.py, eligibility.py
│   ├── strategy/            # regime.py, signals.py, candidates.py  (pure functions)
│   ├── features/            # indicators.py, pipeline.py
│   ├── ml/                  ├── labels.py ├── train.py ├── ranker.py ├── calibrate.py
│   ├── portfolio/           # selector.py
│   ├── risk/                # engine.py (hard constraints), limits.py
│   ├── execution/           # intent.py, order_manager.py
│   ├── brokers/             # base.py, paper.py, fyers.py, sandbox.py
│   ├── storage/             ├── journal.py (sqlite) ├── parquet_store.py ├── models.py
│   ├── scheduler/           # loops.py (bar loop, eod jobs), calendar.py
│   ├── observability/       ├── logging.py ├── metrics.py ├── alerts.py
│   ├── dashboard/           # fastapi app + templates (HTMX)
│   ├── config.py            # pydantic settings, version hashing
│   └── main.py              # composition root, MODE gate
├── scripts/                 # bootstrap_history.py, daily_digest.py, report.py
├── tests/                   # unit + golden-fixture reproducibility + chaos/recovery sims
├── notebooks/               # research only, never imported by src/
├── data/                    # parquet/, sqlite/ (gitignored)
├── .env.example
├── launchd/com.sts.paper.plist
└── pyproject.toml
```

Dependency rule (enforced by import-linter): `strategy/ml/portfolio → risk → execution → brokers`; nothing above `storage` except `main`.

---

## 23. Development Phases

| Phase | Deliverable | Exit criterion |
|---|---|---|
| 0 Research (done) | This document | Decisions logged; open questions ticketed |
| 1 Data foundation | History loader, ref data, calendar, parquet store | 5y daily + 1y minute for NIFTY 200 loaded; gap-audit passes; corp-action adjustment validated on 3 known splits |
| 2 Strategy engine | Signals/candidates + golden tests | Bit-reproducible candidates on fixture data |
| 3 Backtest engine | Event-driven walk-forward runner w/ costs | Deterministic-only backtest produces sane trade stats over 3y; leakage test suite green |
| 4 ML layer | Labels, features, LightGBM + calibration, walk-forward eval | Deployment gate §9 met OR documented deterministic-only fallback |
| 5 Parameter robustness | Plateau studies per §8 | Frozen `params p1.0.0` committed |
| 6 Paper engine | PaperBroker + live WS loop + journal | 1 week soak, zero unreconciled states |
| 7 Observability/dashboard/alerts | Full §17 stack | Daily digests flowing; kill-switch drill passes |
| 8 Chaos/stability | kill -9 / network-cut / stale-feed drills | Recovery reconciles correctly in all drills |
| 9 THE EXPERIMENT | 1-month protocol §18 | Report vs baselines vs pre-registered criteria |
| 10 Broker integration | Fyers live adapter + parity tests | Replay parity paper↔sandbox green |
| 11 Micro-live | ₹10k checklist §19 | Only after every box checked |

Phases 1–8 overlap-safe ordering; 9 runs ~20 trading days.

---

## 24. Final Recommended Architecture (summary)

Single Python asyncio modular monolith on the user's Mac (`caffeinate`+launchd) for the paper experiment: Fyers WebSocket + REST-reconciled market data → Parquet/DuckDB research store + SQLite operational journal → NIFTY 200 liquidity-filtered universe → deterministic pullback/trend V1 strategy (research parameters, plateau-validated) → calibrated LightGBM ranker (frozen for the experiment) → greedy correlation-aware portfolio selector → hard-constraint risk engine → idempotent order manager → `Broker` interface with realistic PaperBroker (next-bar-open fills, spread+slippage+statutory costs) — FastAPI/HTMX read-only dashboard + Telegram alerts. No microservices, no Redis, no Kafka, no GPUs, no co-location, no C++. Correctness, honest fills, and full decision auditability beat nanoseconds at every turn of this design.

**Does this project need low latency? No.** Holding periods of days, decision granularity of minutes, and <20 orders/day make seconds-scale latency immaterial versus the P&L impact of fill modeling, cost accounting, risk enforcement, and state integrity — which is where this architecture spends its complexity budget.

---
---

# V1 BUILD SPECIFICATION (hand to coding agent)

Build the system described above. Scope for V1 = Phases 1–8 (paper experiment ready). Do NOT implement live-broker order placement beyond a stubbed adapter.

**Stack:** Python 3.12, uv-managed; polars, numpy, duckdb, lightgbm, scikit-learn, fyers-apiv3 (SDK isolated behind adapter), fastapi, uvicorn, jinja2+htmx dashboard, apscheduler, pydantic-settings, structlog, pytest. SQLite WAL for journal; Parquet for candles.

**Non-negotiable requirements:**
1. `Broker` ABC (`get_positions, get_orders, get_quotes, get_account_state, place_order, cancel_order, modify_order`). `PaperBroker` is default. Strategy modules import nothing broker-specific.
2. `MODE=paper` default; live mode exists only as an inert stub requiring explicit config + confirmation string.
3. Every decision persisted to journal with full feature vector, scores, risk verdict, and machine-readable rejection reasons.
4. Fail-closed: stale data (>10 min in session) or unreconciled state ⇒ no new entries; alert; exits continue conservatively.
5. Risk engine = sole authority on approvals; formulas exactly as §11 table; ML score advisory only.
6. Paper fills: execute at NEXT bar open, plus half-spread + tiered slippage + full statutory costs from `configs/costs.yaml`. Never fill at signal-time LTP.
7. Time-series hygiene everywhere: walk-forward splits with embargo; no random splits anywhere in the codebase; decisions at t consume data ≤ t.
8. Reproducibility: config content-hash + data manifest + strategy/model/param versions stamped on every experiment, position, and intent.
9. Idempotent startup: boot → load journal → reconcile → resume; duplicate order submission impossible (idempotency keys).
10. Tests: indicator math vs hand-computed values; candidate-generation golden fixtures; risk-engine property tests (no approval ever violates any constraint); recovery chaos test (kill mid-write, restart, assert consistency).

**Concrete V1 parameters:** as §7 (SMA20/50, RSI14∈[45,70], ATR14, stop 1.5×ATR, targets 1.0R/3.0R halves, trail hh−1.5×ATR post-T1, time stop 10 sessions, vol ≥1.5×SMA20vol, regime NIFTY>SMA50 & VIX<22, entry window 09:45–14:30 IST, MAX_POSITIONS=4, risk/trade 1%, max open risk 2%, DD kill −10%).

**Universe:** NIFTY 200 members (dated snapshot file) + §5 eligibility filters, recomputed each morning pre-open.

**Deliverable of the run:** a one-command start (`uv run sts --experiment exp_001`) that boots the supervised paper loop, plus `scripts/report.py` producing the §18 end-of-experiment report automatically.
