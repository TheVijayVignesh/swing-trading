# No-KYC Build Path — Making the Platform Possible Without Brokerage Accounts

**Date:** 2026-08-24 · **Constraint:** User cannot complete Indian broker KYC right now.
**Finding in one line:** *Nothing in Phases 1–8 (data foundation → strategy → ML → paper experiment → observability → chaos tests) requires any brokerage account. External validation of the broker-adapter contract is available free and KYC-free via Alpaca's Paper Only Account (email-only signup).*

---

## 1. Why almost nothing was actually blocked

Re-examining the architecture against the dependency graph:

| Phase | Needs broker account? | Needs paid data? | What it actually needs |
|---|---|---|---|
| 1 Data foundation | ❌ | ❌ | NSE public archives |
| 2 Strategy engine | ❌ | ❌ | candles from Phase 1 |
| 3 Backtest engine | ❌ | ❌ | local data |
| 4 ML layer | ❌ | ❌ | local data |
| 5 Parameter robustness | ❌ | ❌ | local data |
| 6 Paper engine | ❌ | ❌ | **live prices** (see §3) |
| 7 Observability/dashboard | ❌ | ❌ | running system |
| 8 Chaos/stability | ❌ | ❌ | running system |
| Sandbox gate (adapter contract test) | ❌ **with §4** | ❌ | Alpaca email-only account |
| 9 One-month NSE paper experiment | ❌ | see §3 | live prices + internal PaperBroker |
| 10–11 Indian broker integration & micro-live | ✅ KYC | optional | deferred until user ready |

The Dhan sandbox failure hurt only one thing — external validation of *Indian* order-API contracts — and even that is now covered differently (§4).

## 2. Historical/research data — free, no accounts

| Source | Coverage | Access | Status |
|---|---|---|---|
| **NSE Bhavcopy EOD archives** (nseindia.com) | ALL listed equities, daily OHLCV, multi-year archive; authoritative primary source; published ~30–60 min after close | Direct CSV/ZIP download, no login; Python libs (`NseIndiaApi`, `pynse`, `getbhavcopy`) handle cookie flows | 🟡 documented; will verify in Phase 1 |
| **NSE historical index/VIX endpoints** | NIFTY family, India VIX daily history | Public JSON endpoints (no key); `NseIndiaApi.fetch_historical_index_data / fetch_historical_vix_data` | 🟡 |
| **yfinance `.NS` symbols** | NSE equities daily (years); intraday 1m ≈ last 7–30d, 5m ≈ last ~60d (provider-side limits) | pip install; no key | 🟡 |
| Kaggle NIFTY minute dataset (2015–2024) | Index-only long-history intraday | manual download once | 🟡 |

Corporate-action adjustment remains OUR job on these sources (Open Question from v1.1 — unchanged, still blocking-critical for label quality).

## 3. Live data for the paper experiment — the honest trade-off

Without a broker subscription, live NSE price options are all degraded:

1. **yfinance/NSE-quote polling every 60s during market hours** (free): adequate for 5-minute bar construction for ~200 symbols IF polled politely (rate-limit-aware, jittered); risk: gaps, delays up to ~15 min on some quote surfaces, breakage when NSE changes site behavior.
   → **Mitigation already built into the architecture:** fail-closed on staleness, EOD reconciliation against Bhavcopy nightly, gap-flagging. The experiment design (v1.0 §18) tolerates degraded feed days by counting them as coverage incidents rather than trading on stale data.
2. **Broker WS feed** (needs KYC'd account + possibly data subscription): strictly better; becomes available the moment the user completes eKYC later.

**Decision:** run the experiment on option 1 with explicit degradation accounting; upgrade the feed when a broker account exists. This does not invalidate the experiment — it adds a measured, disclosed data-quality caveat, and the strategy layer never sees the difference because `MarketData` is an interface too.

## 4. Adapter-contract validation without KYC — Alpaca Paper Only Account

### ✅ VERIFIED BY ACTUAL TEST (2026-08-24, live against paper-api.alpaca.markets)

| Capability (our Broker interface) | Result |
|---|---|
| authenticate / get account state | ✅ `GET /v2/account` → HTTP 200, ACTIVE account, $100k virtual cash, buying power fields present |
| place order (limit) | ✅ `POST /v2/orders` with `client_order_id` (native idempotency key!) → full order object |
| query order | ✅ `GET /v2/orders/{id}` → status transitions observable (`new`) |
| modify order | ✅ `PATCH /v2/orders/{id}` — **replace semantics**: original becomes `replaced`, new `order_id` issued via `replaced_by` |
| cancel order | ✅ `DELETE /v2/orders/{id}` → HTTP 204; final state `canceled` with `canceled_at` timestamp |
| order history | ✅ `GET /v2/orders?status=all` |
| **simulated fills** | ✅ market order → `pending_new` → filled at real-time quote ($312.05 on AAPL); visible in `filled_qty`/`filled_avg_price` |
| trades/fills log | ✅ `GET /v2/account/activities/FILL` returns symbol/price/qty |
| positions | ✅ `GET /v2/positions` reflects the simulated fill (AAPL qty 1) |
| funds/account state | ✅ cash/buying_power/portfolio_value/equity all exposed |

**Adapter-design finding:** modification is *replacement* — the order ID changes. Our OrderManager must follow `replaced_by` chains and never cancel a stale ID (we hit exactly that error: cancelling a replaced order → HTTP 422). This maps cleanly onto our existing write-ahead + broker-wins reconciliation design.

Not exercised yet (documented 🟡): stop/stop-limit/trailing/bracket order classes, WebSocket trade-update stream, rejection paths. All are part of Phase 8 parity tests.

From official docs ([docs.alpaca.markets/us/docs/paper-trading](https://docs.alpaca.markets/us/docs/paper-trading)):

> *"Anyone globally can create an Alpaca Paper Only Account! All you need to do is sign up with your email address."*

What it gives us, free, with zero KYC/funding:

| Capability (our Broker interface) | Alpaca paper API |
|---|---|
| authenticate | API key/secret, env-based 🟡 |
| get_account/profile/state | `GET /v2/account` 🟡 |
| place order (market/limit/stop/stop-limit/trailing/bracket) | `POST /v2/orders` 🟡 |
| query order | `GET /v2/orders/{id}` 🟡 |
| modify order | `PATCH …` 🟡 |
| cancel order | `DELETE …` 🟡 |
| order history | `GET /v2/orders` w/ filters 🟡 |
| trades/fills | `GET /v2/account/activities` + order fill fields 🟡 |
| positions | `GET /v2/positions` 🟡 |
| funds/account state | `GET /v2/account` 🟡 |
| **realistic simulated fills** | fills simulated off real-time quotes, partial fills, rejections, order states incl. `pending_new/new/partially_filled/filled/canceled/rejected/expired` 🟡 |
| WebSocket order/trade updates | v2 streaming events 🟡 |
| production parity | same API surface as live; flip = base URL + keys 🟡 |

**Why this matters even though instruments are US equities:** the thing we need to validate pre-live is *our* adapter machinery — auth handling, idempotent order management, state-machine transitions, reconciliation logic ("broker wins"), retry/query-before-resend, WebSocket drop recovery, partial-fill bookkeeping — against a REAL third-party OMS whose responses we don't control. That validation is instrument-agnostic. It converts our SandboxBroker from "stub" into "proven pattern," leaving only *field-mapping to an Indian broker* unverified until KYC day.

**Backup candidates (same class):** Tradier sandbox (US, free, full lifecycle) — secondary; IBKR paper — rejected (requires funded live account first).

## 5. Revised sandbox recommendation

### Primary Sandbox
**Alpaca Paper Only Account** (`paper-api.alpaca.markets`) — email signup, globally available, complete lifecycle + realistic simulation + WebSocket. Satisfies the original SANDBOX GATE intent: a coding agent can fully implement and prove `SandboxBroker` before any Indian credentials exist. Status: 🟡 officially documented, live verification pending account creation (user action: sign up with any email, generate paper API keys — ~3 minutes, no KYC).

### Backup #1
Tradier sandbox (US) — same class, if Alpaca onboarding hits friction.

### Backup #2
Internal PaperBroker hardening + micro-live deferral (unchanged from ARCHITECTURE_V1.1 amendment) — always available since it depends on nobody.

Upstox's Indian sandbox remains the best *Indian* venue and stays the recommended first adapter target **after** KYC becomes possible; its token generation then takes two minutes.

## 6. Updated gate statements

> **SANDBOX GATE: READY — VERIFIED** (2026-08-24). Alpaca paper account created and the full lifecycle was executed live: auth ✅ place ✅ query ✅ modify ✅ cancel ✅ history ✅ fills ✅ positions ✅ funds ✅. The SandboxBroker adapter can be implemented and proven before any Indian credentials exist.

> **EXPERIMENT GATE: READY NOW** — Phases 1–9 can start immediately using NSE public data + free live polling + internal PaperBroker. Nothing waits on brokerage access or KYC.

## 7. Required architecture changes (minimal)

1. `brokers/alpaca_paper.py` added as first `SandboxBroker` provider; capability-matrix mechanism (from ALTERNATIVE_SANDBOX_COMPARISON §6) applies.
2. `market_data/providers`: add `YFinanceProvider` (polling) + `NseArchiveProvider` (EOD truth) alongside future `DhanFeedProvider`; freshness/staleness rules unchanged.
3. Experiment protocol gains a standing "data-quality appendix": feed source version, observed staleness histogram, coverage % — reported alongside results so conclusions carry their own caveats.
4. Indian LiveBroker work (Phase 10–11) formally gated on future KYC; Upstox chosen then for its sandbox-assisted parity path.

## 8. Immediate next actions (no KYC anywhere)

1. ☐ User: create Alpaca Paper Only account (email only) → paste paper API keys (or run everything below without them until Phase 8)
2. ☐ Begin Phase 1: NSE Bhavcopy loader + yfinance backfill + corporate-action validation (the true blocker identified in ARCHITECTURE_V1.1 §17 item 2)
3. ☐ Phase 2–5 proceed on local data
4. ☐ Phase 8 runs `scripts/alpaca_lifecycle_verify.sh` once keys exist
5. ☐ Phase 9 paper experiment on polled free feed with degradation accounting
