# Architecture v1.2 — Multi-Session Trading Lab Revision

**Date:** 2026-08-24 · **Supersedes:** the single-experiment model in v1.0/v1.1 · **Status:** Design (pre-Phase-1)

All other content of ARCHITECTURE_V1.1 (fill model, stop semantics, risk formulas, hyperparameter methodology, ML gating, no-KYC build path, reliability playbook) carries over unchanged unless explicitly modified below.

---

## 1. Concept change

**From:** one long-running experiment (`exp_001`) with a single capital pool.
**To:** a **Trading Lab** — a host application managing **N concurrent, fully isolated trading sessions** over shared market-data infrastructure.

A **session** = one autonomous trader: its own virtual capital, configuration snapshot, portfolio, orders, positions, strategy binding, ML-model binding, journal, and metrics. Sessions never see each other. The user creates sessions through the dashboard and manages their lifecycle; they **never specify a stock** — every session autonomously scans its configured universe and selects its own first trade (creation form has no instrument field, enforced by schema).

Session lifecycle state machine:

```
CREATED ──start()──► RUNNING ──pause()──► PAUSED ──resume()──► RUNNING
   │                    │                     │
   │                    stop(policy)         stop(policy)
   ▼                    ▼                     ▼
 ABORTED ◄────────── STOPPING ──► STOPPED (terminal: FLATTENED | HELD)
```

Lifecycle semantics (deterministic, journaled in `session_events`):
- **pause():** no new entries; open positions continue to be managed normally (stops/targets/time-stops still fire). Rationale: abandoning risk management while paused would be the only unsafe interpretation.
- **stop(policy):** policy chosen at creation — `FLATTEN` (cancel working orders, exit all positions at next actionable prices, then terminal) or `HOLD` (freeze decisions entirely; positions remain, session restartable). Terminal states are immutable except by cloning into a new session.
- **crash/reboot:** RUNNING sessions auto-resume via journal recovery (unchanged v1.1 §14 playbook, now per-session); PAUSED/STOPPED sessions stay in their states.

## 2. What is shared vs isolated (hard rule)

| Shared (read-only, global singletons) | Isolated (per session) |
|---|---|
| Market-data feeds (WS/polling), bar builder, candle cache | Capital/cash ledger & equity curve (own PaperBroker/SandboxBroker instance) |
| Parquet candle store, reference data (symbols, membership, corp actions, calendar) | Configuration snapshot (content-hashed, immutable at start) |
| Instrument master, market calendar | Portfolio state, orders, fills, positions |
| Cost schedule (`costs.yaml`) | Risk-engine instance (built from session config) |
| Observability bus (logs/metrics infrastructure) | Strategy instance (bound to pinned versions) |
| Dashboard/API process | Journal partition, metrics series, decision-replay records, incidents |

Enforcement mechanisms:
- Storage-layer API requires an explicit session context; every mutating query is `WHERE session_id = :ctx` by construction (parameterized repository objects, not ad-hoc SQL).
- Feed fan-out: one bar-close event published on an internal bus; each RUNNING session's asyncio task consumes independently. A slow session cannot block others (per-session bounded queue, drop+incident on overflow).
- Feed failure is a **broadcast**: all sessions fail-closed simultaneously and identically (v1.1 §14), each logging its own incident.
- Isolation test suite (Phase 1 deliverable): two sessions, identical data, divergent configs → assert zero shared mutable state; capital-conservation property test (each session's equity evolves independently; sum of ledgers constant absent fees).

## 3. Concurrency model (still a modular monolith)

Single Python process. `LabManager` owns one asyncio task per RUNNING session plus the shared market-data task. Sizing: sessions are cheap (one 5-min decision pass ≈ 200 symbols × pure functions ≪ 1s); target ≤ 10 concurrent sessions on the Mac. SQLite WAL serializes writes safely; per-tick journal batching keeps lock contention negligible. If ever exceeded → Postgres migration point (documented trigger: >10 sessions or >250 journal rows/sec sustained).

## 4. Revised data model (v2 schema)

Global tables unchanged (parquet candles/ref data). Runtime journal (SQLite) becomes:

```sql
-- LIFECYCLE
sessions(id TEXT PK, name TEXT, status TEXT CHECK(status IN
           ('CREATED','RUNNING','PAUSED','STOPPING','STOPPED','ABORTED')),
         terminal_state TEXT,                    -- FLATTENED | HELD | NULL
         mode TEXT,                              -- paper | sandbox | live
         capital_initial REAL,
         config_yaml TEXT, config_hash TEXT,     -- immutable after start
         universe_snapshot_id TEXT,
         strategy_id TEXT, strategy_version TEXT, param_version TEXT,
         ml_model_id TEXT,                       -- may be NULL => deterministic-only
         costs_version TEXT, data_manifest_id TEXT,
         on_stop_policy TEXT,                    -- FLATTEN | HOLD
         created_at TS, started_at TS, ended_at TS)

session_events(id INTEGER PK, session_id TEXT, ts TS,
               event TEXT,                        -- CREATED/STARTED/PAUSED/RESUMED/
               actor TEXT, detail_json TEXT)      -- STOP_REQUESTED/STOPPED/FAULTED/RECOVERED

-- TRADING (all carry session_id FK, indexed)
intents(id PK, session_id FK, ts, symbol, market_state_ref, feature_vector_json,
        signals_json, ml_score, ml_prob, risk_checks_json,
        decision, rejection_reason, portfolio_snapshot_json, versions_json)
orders(id PK, session_id FK, intent_id FK, broker_order_id, replaced_by_id,
       side, type, qty, limit_px, trigger_px, status, filled_qty, avg_fill_px,
       submitted_at, updated_at, idempotency_key, UNIQUE(session_id, idempotency_key))
fills(id PK, session_id FK, order_id FK, ts, px, qty, cost_breakdown_json)
positions(id PK, session_id FK, symbol, qty, avg_entry, stop, target2, trail_px,
          opened_at, closed_at, status, exit_reason,
          strategy_version, ml_model_id, param_version)
account_snapshots(id PK, session_id FK, ts, cash, invested, unrealized, realized,
                  equity, hwm, drawdown)
metrics_timeseries(session_id FK, ts, metric, value)
incidents(id PK, session_id FK NULLABLE, ts, severity, kind, detail_json, resolved_at)
```

Notes: `experiments` table from v1.1 is replaced by `sessions` (migration trivial — pre-implementation so no back-compat needed). `orders.idempotency_key` uniqueness is now per-session. `replaced_by_id` added (Alpaca replace-semantics lesson, NO_KYC_BUILD_PATH §4). `ml_model_id` is pinned at session start; ML training remains offline/research-side — **no training occurs inside the lab runtime**.

## 5. Module changes

New package, rest untouched:

```
src/sts/lab/
├── manager.py      # LabManager: owns session tasks, lifecycle transitions, event journaling
├── runner.py       # SessionRunner: per-session loop = [refresh ctx → manage exits → scan → select → risk → execute]
├── factory.py      # builds isolated graph per session: BrokerInstance + RiskEngine + Strategy + Selector from config_hash
└── policies.py     # pause/stop/flatten semantics
```

Dependency rule addition: `lab → {strategy, ml, portfolio, risk, execution, brokers, storage}`; nothing in those modules knows about `lab` or other sessions (import-linter rule).

## 6. Dashboard & API (revised)

Routes (FastAPI + HTMX, read-mostly):

| View | Route | Contents |
|---|---|---|
| **Lab Overview** | `/lab` | Grid of session cards: name, status badge, equity sparkline, total return, max DD, open positions, last-decision age, health dot; global system-health strip (feed freshness, DB, supervisor); "New Session" button |
| Create Session | `/lab/sessions/new` | Form fields ONLY: name, initial capital, mode (paper/sandbox; live hidden until interlock), universe selector (dated snapshots), strategy id + version, risk-profile preset, on-stop policy. **No symbol field exists.** Shows effective config preview + hash before commit |
| **Session Detail** | `/lab/session/{id}` | Everything from v1.0 §17 (portfolio strip, positions, funnel, experiment stats, health) scoped to the session + lifecycle controls (Start/Pause/Resume/Stop with confirm dialog showing flatten implications) + **Decision Replay browser** |
| **Compare** | `/lab/compare?ids=a,b,c` | Normalized equity curves overlaid (+NIFTY50/NIFTY200 benchmarks), aligned by calendar date AND by trade number; metrics table (return, Sharpe-lite, max DD, win rate, PF, expectancy, exposure, turnover, cost drag, coverage %); divergence callouts (same-bar differing decisions between clones) |
| Decision Replay | `/lab/session/{id}/decisions/{intent_id}` | Rendered chain: market state → features → signals → ML score → portfolio state → individual risk checks → decision → order → fill (v1.1 §13 schema powers this) |

Control-plane API: `POST /api/sessions`, `POST /api/sessions/{id}/start|pause|resume|stop`, `GET /api/sessions/{id}/…` (metrics/orders/positions/intents). Mutating endpoints require confirmation token in UI; `MODE=LIVE` sessions additionally require the v1.0 interlock phrase. Stop/pause are safe-by-construction: they act through `LabManager`, never by killing tasks mid-order (order lifecycle completes or fails-closed first).

## 7. Experiment protocol impact

The one-month protocol (v1.0 §18) becomes **one session among several**, which strengthens it: the recommended default lineup at experiment start is

1. `hybrid-main` — full stack (primary result)
2. `det-only` — identical config minus ML (ablation clone)
3. `random-k` — random selection baseline (noise floor)
4. *(optional)* parameter-variant clone (e.g., ATR multiplier 1.25×) — robustness probe under live conditions

Clones share data manifests and differ by exactly one config field, making attribution clean. Pre-registered success criteria attach to the *lineup*, not a single session. Auto-extension rule unchanged (trade-count driven, set at t=0). Comparisons come free from the Compare view instead of bespoke scripts.

## 8. Repository structure deltas

```
src/sts/lab/            # NEW (§5)
src/sts/storage/        # schema v2, session-scoped repositories
dashboard/templates/    # lab_overview.html, session_detail.html, compare.html,
                        # replay.html, session_new.html
tests/test_lab_isolation.py   # §2 isolation suite
tests/test_session_lifecycle.py
```

## 9. Implementation gate addendum

Before Phase 1 begins, additionally confirm:
- ☐ This document reviewed; session lifecycle semantics (§1) accepted as specified
- ☐ Schema v2 (§4) approved — including `experiments→sessions` replacement
- ☐ Default experiment lineup (§7) accepted

**Nothing else changes:** fill model, risk formulas, stop semantics, ML gating, reliability playbook, security model, and deployment plan all stand as written in v1.1.
